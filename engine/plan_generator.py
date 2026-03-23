"""AI 训练计划生成器 — 基于用户目标、当前体能和历史数据自动生成周训练计划。

安全架构（先建护栏，再装发动机）:
  0. Training Phase State Machine — 周期化阶段判定，约束负荷包络
  1. Trigger Engine — 优先级仲裁矩阵（P1-P5），Cooldown 防抖
  2. Fallback Templates — 本地兜底：AI 故障/红线触发时的安全课表
  3. PostCheck — AI 输出校验：TSS/IF/时长/连续高强度/周跃迁/动作越界
  4. Pipeline — 编排：Phase → Trigger → AI 生成 (or Fallback) → PostCheck → 落库

核心原则: LLM 是受限规划器，不是决策中心。
  - 本地规则决定「该不该动」和「负荷包络」
  - AI 在包络内做「填充和微调」
  - PostCheck 兜底一切
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from engine import llm_client

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Layer 0: Training Phase State Machine — 周期化阶段
# ═══════════════════════════════════════════════════════════════════════════

class TrainingPhase:
    """训练周期阶段。每个阶段有不同的负荷包络和关键课类型。"""
    BASE = "base"          # 有氧基础期：Zone 2 为主，低强度高量
    BUILD = "build"        # 能力构建期：加入间歇，渐进负荷
    PEAK = "peak"          # 巅峰期：高强度低量，赛前准备
    RECOVERY = "recovery"  # 恢复期：主动恢复，降负荷 40-60%
    TRANSITION = "transition"  # 过渡期：赛季间休整

# 每个阶段的硬约束（周级别）
_PHASE_CONSTRAINTS = {
    TrainingPhase.BASE: {
        "weekly_tss_multiplier": 1.0,     # 基准 TSS
        "max_intensity_days": 1,          # 每周最多 1 天高强度（Zone 4+）
        "max_daily_tss": 120,
        "min_rest_days": 1,
        "primary_zones": "Zone 1-3",
        "key_workouts": ["长距离耐力骑", "有氧基础跑", "全身力量"],
        "description": "有氧基础期：以 Zone 2 有氧骑行为主，建立耐力底座",
    },
    TrainingPhase.BUILD: {
        "weekly_tss_multiplier": 1.1,
        "max_intensity_days": 2,
        "max_daily_tss": 150,
        "min_rest_days": 1,
        "primary_zones": "Zone 2-4",
        "key_workouts": ["甜点间歇", "VO2max 短间歇", "阈值巡航", "力量训练"],
        "description": "能力构建期：加入 Zone 3-4 间歇训练，渐进增加 TSS",
    },
    TrainingPhase.PEAK: {
        "weekly_tss_multiplier": 0.85,
        "max_intensity_days": 2,
        "max_daily_tss": 130,
        "min_rest_days": 2,
        "primary_zones": "Zone 4-5",
        "key_workouts": ["VO2max 间歇", "比赛模拟", "短冲刺"],
        "description": "巅峰期：高强度低量，保持锐度，赛前减量",
    },
    TrainingPhase.RECOVERY: {
        "weekly_tss_multiplier": 0.5,
        "max_intensity_days": 0,
        "max_daily_tss": 60,
        "min_rest_days": 3,
        "primary_zones": "Zone 1-2",
        "key_workouts": ["恢复骑行", "轻松跑", "拉伸瑜伽"],
        "description": "恢复期：主动恢复为主，严禁高强度",
    },
    TrainingPhase.TRANSITION: {
        "weekly_tss_multiplier": 0.4,
        "max_intensity_days": 0,
        "max_daily_tss": 50,
        "min_rest_days": 3,
        "primary_zones": "Zone 1-2",
        "key_workouts": ["自由骑行", "交叉训练", "休息"],
        "description": "过渡期：赛季间休整，保持基础活动量即可",
    },
}


def detect_training_phase(conn) -> Tuple[str, str]:
    """根据当前数据自动判定训练阶段。

    判定逻辑:
    1. 用户手动设置 > 自动检测
    2. CTL 趋势 + TSB + 赛事距离 → 阶段推断

    Returns:
        (phase, reason)
    """
    from engine import database

    # 1. 用户手动设置优先
    manual_phase = database.get_setting(conn, "training_phase")
    if manual_phase and manual_phase in (TrainingPhase.BASE, TrainingPhase.BUILD,
                                          TrainingPhase.PEAK, TrainingPhase.RECOVERY,
                                          TrainingPhase.TRANSITION):
        return manual_phase, f"用户手动设置: {manual_phase}"

    # 2. 自动检测
    fitness_rows = conn.execute(
        "SELECT ctl, atl, tsb, date FROM fitness_history ORDER BY date DESC LIMIT 14"
    ).fetchall()

    if not fitness_rows:
        return TrainingPhase.BASE, "无体能数据，默认基础期"

    current = dict(fitness_rows[0])
    tsb = current.get("tsb", 0)
    ctl = current.get("ctl", 0)

    # CTL 趋势（过去 14 天）
    ctl_values = [dict(r).get("ctl", 0) for r in fitness_rows]
    ctl_trend = ctl_values[0] - ctl_values[-1] if len(ctl_values) >= 7 else 0

    # 检查是否有近期赛事
    event_date_str = database.get_setting(conn, "athlete_event_date")
    days_to_event = None
    if event_date_str:
        try:
            event_d = date.fromisoformat(event_date_str)
            days_to_event = (event_d - date.today()).days
        except (ValueError, TypeError):
            pass

    # 判定逻辑
    if tsb < -25:
        return TrainingPhase.RECOVERY, f"TSB={tsb:.1f} 过低，自动进入恢复期"

    if days_to_event is not None:
        if 0 < days_to_event <= 14:
            return TrainingPhase.PEAK, f"距赛事 {days_to_event} 天，进入巅峰期"
        if 14 < days_to_event <= 42:
            return TrainingPhase.BUILD, f"距赛事 {days_to_event} 天，构建期"

    if ctl < 40:
        return TrainingPhase.BASE, f"CTL={ctl:.0f} 较低，基础期"
    if ctl_trend > 3:
        return TrainingPhase.BUILD, f"CTL 上升趋势 (+{ctl_trend:.1f}/14d)，构建期"
    if ctl_trend < -5:
        return TrainingPhase.RECOVERY, f"CTL 下降趋势 ({ctl_trend:.1f}/14d)，恢复期"

    return TrainingPhase.BUILD, f"CTL={ctl:.0f}，默认构建期"


# ═══════════════════════════════════════════════════════════════════════════
# Layer 1: Trigger Engine — 优先级仲裁矩阵
# ═══════════════════════════════════════════════════════════════════════════

class TriggerAction:
    NO_ACTION = "NO_ACTION"              # 正常生成
    LOCAL_OVERRIDE = "LOCAL_OVERRIDE"     # 跳过 AI，使用本地兜底
    REDUCE_LOAD = "REDUCE_LOAD"          # 调用 AI 但强制降负荷


class TriggerPriority:
    """显式优先级（数字越小越高优先级）。多触发时只执行最高优先级。"""
    P1_MANUAL_INTERRUPT = 1   # 疾病/差旅（用户主动标记）
    P2_SAFETY_REDLINE = 2    # TSB 透支 / 连续 HRV 异常
    P3_ENV_STRESS = 3        # 高温高湿热应激（拦截假阳性，不触发调整）
    P4_EXECUTION_DEVIATION = 4  # 课表脱落 / TSS 偏差 >20%
    P5_CAPACITY_UPGRADE = 5   # eFTP/W' 跃迁


# Cooldown 最短间隔（小时）— 防止频繁重写
_COOLDOWN_HOURS = 6


def evaluate_triggers(conn, phase: str) -> Tuple[str, str, Optional[str], List[Dict]]:
    """评估当前身体状态，决定计划生成策略。

    优先级矩阵: P1 > P2 > P3 > P4 > P5
    Cooldown: 距上次生成不足 _COOLDOWN_HOURS 则跳过

    Returns:
        (action, reason, fallback_template_or_None, all_triggers_hit)
    """
    from engine import database

    all_triggers: List[Dict] = []

    # ── Cooldown 检查 ──
    last_gen = database.get_setting(conn, "last_plan_generated_at")
    if last_gen:
        try:
            last_dt = datetime.fromisoformat(last_gen)
            hours_since = (datetime.now() - last_dt).total_seconds() / 3600
            if hours_since < _COOLDOWN_HOURS:
                logger.info("Cooldown: %.1fh since last generation (< %dh), allowing but noting",
                            hours_since, _COOLDOWN_HOURS)
                # 不阻止，但记录。用户主动点击生成时应该允许
        except (ValueError, TypeError):
            pass

    # 获取最新体能状态
    fitness_row = conn.execute(
        "SELECT * FROM fitness_history ORDER BY date DESC LIMIT 1"
    ).fetchone()
    tsb = dict(fitness_row).get("tsb", 0) if fitness_row else 0
    ctl = dict(fitness_row).get("ctl", 0) if fitness_row else 0

    # 获取最近 wellness 数据
    wellness = database.list_wellness(conn, days=7)
    rhr_values = [w.get("resting_heart_rate", 0) for w in wellness
                  if w.get("resting_heart_rate")]
    hrv_values = [w.get("hrv_rmssd", 0) or w.get("hrv", 0) for w in wellness
                  if (w.get("hrv_rmssd") or w.get("hrv", 0))]

    # RHR 异常：最近值比 7 天均值高 8+ bpm
    rhr_abnormal = False
    if len(rhr_values) >= 3:
        avg_rhr = sum(rhr_values[1:]) / len(rhr_values[1:])
        if rhr_values[0] > avg_rhr + 8:
            rhr_abnormal = True

    # HRV 异常：最近值比 7 天均值低 20%+
    hrv_abnormal = False
    if len(hrv_values) >= 3:
        avg_hrv = sum(hrv_values[1:]) / len(hrv_values[1:])
        if avg_hrv > 0 and hrv_values[0] < avg_hrv * 0.8:
            hrv_abnormal = True

    # 用户标记（疾病/差旅）
    user_sick = database.get_setting(conn, "user_status_sick") == "true"
    user_travel = database.get_setting(conn, "user_status_travel") == "true"

    # ── P1: 用户主动标记疾病/差旅 ──
    if user_sick or user_travel:
        reason = "用户标记: " + ("生病" if user_sick else "差旅")
        all_triggers.append({"priority": TriggerPriority.P1_MANUAL_INTERRUPT,
                             "code": "TRG_PAUSE", "reason": reason})

    # ── P2: 安全红线 ──
    if tsb < -30:
        all_triggers.append({"priority": TriggerPriority.P2_SAFETY_REDLINE,
                             "code": "TRG_FATIGUE_CRITICAL",
                             "reason": f"TSB={tsb:.1f} < -30，极度疲劳"})
    if rhr_abnormal and hrv_abnormal:
        all_triggers.append({"priority": TriggerPriority.P2_SAFETY_REDLINE,
                             "code": "TRG_OVERTRAINING",
                             "reason": "RHR 飙升 + HRV 下降，疑似过度训练"})
    elif rhr_abnormal:
        all_triggers.append({"priority": TriggerPriority.P2_SAFETY_REDLINE,
                             "code": "TRG_RHR_SPIKE",
                             "reason": "静息心率异常升高"})

    # ── P3: 环境应激（拦截假阳性，标记但不触发调整）──
    # 需要天气数据接入，暂时跳过
    # if temp > 32 and hr_drift > 6:
    #     all_triggers.append(...)

    # ── P4: 执行偏差 ──
    # 检查最近 3 天实际 vs 计划 TSS
    recent_deviation = _check_execution_deviation(conn)
    if recent_deviation:
        all_triggers.append({"priority": TriggerPriority.P4_EXECUTION_DEVIATION,
                             "code": "TRG_DEVIATION", "reason": recent_deviation})

    # ── P5: 能力跃迁（eFTP 提升 >3%）──
    # 需要 PDC 拟合逻辑，暂时通过手动 FTP 更新触发
    # TODO: 自动 eFTP 检测

    # ── 黄线: TSB 偏低但没到红线 ──
    if tsb < -15 and not any(t["priority"] <= TriggerPriority.P2_SAFETY_REDLINE
                              for t in all_triggers):
        all_triggers.append({"priority": 3,  # 介于 P2 和 P4 之间
                             "code": "TRG_FATIGUE_MODERATE",
                             "reason": f"TSB={tsb:.1f}，疲劳偏高"})

    # ── 仲裁：只取最高优先级 ──
    if not all_triggers:
        return (TriggerAction.NO_ACTION, "体能状态正常", None, [])

    all_triggers.sort(key=lambda t: t["priority"])
    primary = all_triggers[0]

    logger.info("Triggers hit: %s", [(t["code"], t["priority"]) for t in all_triggers])
    logger.info("Primary trigger: %s (P%d) — %s", primary["code"], primary["priority"],
                primary["reason"])

    # 根据最高优先级决定动作
    if primary["priority"] <= TriggerPriority.P2_SAFETY_REDLINE:
        # P1/P2 → 直接本地兜底，不调 AI
        return (TriggerAction.LOCAL_OVERRIDE, primary["reason"],
                "RECOVERY_WEEK", all_triggers)

    if primary["code"] == "TRG_FATIGUE_MODERATE":
        return (TriggerAction.REDUCE_LOAD, primary["reason"], None, all_triggers)

    if primary["priority"] == TriggerPriority.P4_EXECUTION_DEVIATION:
        # 执行偏差 → 调 AI 微调，但给它约束
        return (TriggerAction.REDUCE_LOAD, primary["reason"], None, all_triggers)

    return (TriggerAction.NO_ACTION, "体能状态正常", None, all_triggers)


def _check_execution_deviation(conn) -> Optional[str]:
    """检查最近 3 天计划 vs 实际的执行偏差。"""
    cutoff = (date.today() - timedelta(days=3)).isoformat()

    # 获取计划 TSS
    planned = conn.execute(
        "SELECT COALESCE(SUM(target_tss), 0) as total FROM planned_workouts WHERE date >= ?",
        (cutoff,)
    ).fetchone()
    planned_tss = planned["total"] if planned else 0

    if planned_tss == 0:
        return None  # 没有计划，无法比较

    # 获取实际 TSS
    actual = conn.execute(
        "SELECT COALESCE(SUM(tss), 0) as total FROM activities WHERE date >= ?",
        (cutoff,)
    ).fetchone()
    actual_tss = actual["total"] if actual else 0

    deviation = abs(actual_tss - planned_tss) / planned_tss if planned_tss > 0 else 0

    if deviation > 0.3:
        return (f"3天执行偏差 {deviation*100:.0f}%"
                f"（计划TSS={planned_tss:.0f}, 实际={actual_tss:.0f}）")

    return None


# ═══════════════════════════════════════════════════════════════════════════
# Layer 2: Fallback Templates — 本地兜底课表
# ═══════════════════════════════════════════════════════════════════════════

def get_fallback_plan(template: str, week_start: date) -> List[Dict[str, Any]]:
    """AI 故障或红线触发时的安全课表。"""

    templates = {
        "RECOVERY_WEEK": [
            {"day_offset": 0, "sport": "rest",     "title": "完全休息",
             "description": "全天休息，拉伸放松", "target_tss": 0, "target_duration_min": 0,
             "target_intensity": "Rest", "muscle_groups": []},
            {"day_offset": 1, "sport": "cycling",  "title": "恢复骑行",
             "description": "Zone 1-2 轻松骑行，踏频90+，保持放松", "target_tss": 30, "target_duration_min": 45,
             "target_intensity": "Zone 1-2", "muscle_groups": ["quadriceps", "glutes"]},
            {"day_offset": 2, "sport": "training", "title": "轻量核心训练",
             "description": "平板支撑3x30s, 死虫式3x10, 臀桥3x15, 鸟狗式3x10。全程轻柔，以激活为主",
             "target_tss": 10, "target_duration_min": 30,
             "target_intensity": "Recovery", "muscle_groups": ["core", "glutes"]},
            {"day_offset": 3, "sport": "running",  "title": "轻松恢复跑",
             "description": "Zone 1-2 慢跑，心率<140bpm，可以和朋友聊天的配速", "target_tss": 25, "target_duration_min": 30,
             "target_intensity": "Zone 1-2", "muscle_groups": ["quadriceps", "calves"]},
            {"day_offset": 4, "sport": "rest",     "title": "完全休息",
             "description": "全天休息", "target_tss": 0, "target_duration_min": 0,
             "target_intensity": "Rest", "muscle_groups": []},
            {"day_offset": 5, "sport": "cycling",  "title": "有氧骑行",
             "description": "Zone 2 骑行，稳定心率，不冲刺", "target_tss": 40, "target_duration_min": 60,
             "target_intensity": "Zone 2", "muscle_groups": ["quadriceps", "glutes"]},
            {"day_offset": 6, "sport": "rest",     "title": "完全休息",
             "description": "拉伸放松，准备下周训练", "target_tss": 0, "target_duration_min": 0,
             "target_intensity": "Rest", "muscle_groups": []},
        ],
        "AI_FAILURE": [
            {"day_offset": 0, "sport": "cycling",  "title": "有氧基础骑行",
             "description": "Zone 2 骑行60-90分钟，踏频85-95", "target_tss": 50, "target_duration_min": 75,
             "target_intensity": "Zone 2", "muscle_groups": ["quadriceps", "glutes"]},
            {"day_offset": 1, "sport": "training", "title": "全身力量训练",
             "description": "深蹲4x8, 硬拉4x6, 推举3x10, 引体3xMax, 平板支撑3x45s",
             "target_tss": 20, "target_duration_min": 45,
             "target_intensity": "Mixed", "muscle_groups": ["quadriceps", "back", "chest", "core"]},
            {"day_offset": 2, "sport": "running",  "title": "轻松基础跑",
             "description": "Zone 2 跑步30-40分钟", "target_tss": 35, "target_duration_min": 35,
             "target_intensity": "Zone 2", "muscle_groups": ["quadriceps", "calves"]},
            {"day_offset": 3, "sport": "rest",     "title": "休息日",
             "description": "完全休息", "target_tss": 0, "target_duration_min": 0,
             "target_intensity": "Rest", "muscle_groups": []},
            {"day_offset": 4, "sport": "cycling",  "title": "甜点间歇骑行",
             "description": "热身15min, 3x10min Zone 4 (FTP 95-105%), 间休5min, 放松10min",
             "target_tss": 70, "target_duration_min": 75,
             "target_intensity": "Zone 3-4", "muscle_groups": ["quadriceps", "glutes"]},
            {"day_offset": 5, "sport": "training", "title": "上肢+核心训练",
             "description": "推举3x10, 哑铃划船3x12, 侧平举3x12, 平板支撑3x45s, 俄罗斯转体3x15",
             "target_tss": 15, "target_duration_min": 40,
             "target_intensity": "Mixed", "muscle_groups": ["chest", "back", "shoulders", "core"]},
            {"day_offset": 6, "sport": "cycling",  "title": "长距离耐力骑行",
             "description": "Zone 2 长骑 2-3小时，中途补给", "target_tss": 80, "target_duration_min": 150,
             "target_intensity": "Zone 2", "muscle_groups": ["quadriceps", "glutes"]},
        ],
    }

    plan_template = templates.get(template, templates["AI_FAILURE"])
    workouts = []
    for t in plan_template:
        w = dict(t)
        d = week_start + timedelta(days=w.pop("day_offset"))
        w["date"] = d.isoformat()
        w["compliance_status"] = "pending"
        w["muscle_groups_json"] = json.dumps(w.pop("muscle_groups"))
        workouts.append(w)

    return workouts


# ═══════════════════════════════════════════════════════════════════════════
# Layer 3: PostCheck — AI 输出校验器
# ═══════════════════════════════════════════════════════════════════════════

# 每个训练类型的 TSS 硬上限
_TSS_LIMITS = {
    "cycling": 250,
    "running": 150,
    "training": 80,
    "rest": 0,
}

# 单项训练时长上限（分钟）
_DURATION_LIMITS = {
    "cycling": 300,
    "running": 150,
    "training": 90,
    "rest": 0,
}


def postcheck_workout(w: Dict[str, Any], ftp: float = 229,
                       phase_constraints: Optional[Dict] = None) -> Dict[str, Any]:
    """校验单条训练。钳位不合理数值，不直接拒绝。"""
    sport = w.get("sport", "cycling")
    tss = w.get("target_tss", 0) or 0
    duration = w.get("target_duration_min", 0) or 0
    issues = []
    pc = phase_constraints or {}

    # CK_01: TSS 硬上限（取 sport 上限和 phase 上限的较小值）
    tss_limit = _TSS_LIMITS.get(sport, 150)
    phase_daily_limit = pc.get("max_daily_tss", 999)
    effective_limit = min(tss_limit, phase_daily_limit)
    if tss > effective_limit:
        issues.append(f"TSS {tss} → {effective_limit} (sport={tss_limit}, phase={phase_daily_limit})")
        w["target_tss"] = effective_limit

    # CK_02: 时长上限
    dur_limit = _DURATION_LIMITS.get(sport, 180)
    if duration > dur_limit:
        issues.append(f"时长 {duration}min → {dur_limit}min")
        w["target_duration_min"] = dur_limit

    # CK_03: IF 隐含校验（骑行 >30min，IF >1.15 不合理）
    if duration >= 30 and sport == "cycling" and ftp > 0 and tss > 0:
        implied_if = (w.get("target_tss", tss) / (duration / 60)) ** 0.5
        if implied_if > 1.15:
            safe_tss = int(1.15 ** 2 * (duration / 60))
            issues.append(f"IF={implied_if:.2f}>1.15, TSS→{safe_tss}")
            w["target_tss"] = safe_tss

    # CK_04: 休息日清零
    if sport == "rest" and tss > 0:
        w["target_tss"] = 0
        w["target_duration_min"] = 0
        issues.append("休息日清零")

    # CK_05: 负数拦截
    for key in ("target_tss", "target_duration_min"):
        if (w.get(key) or 0) < 0:
            w[key] = 0

    if issues:
        logger.warning("PostCheck [%s %s]: %s", w.get("date"), sport, "; ".join(issues))

    return w


def postcheck_plan(workouts: List[Dict[str, Any]], ftp: float = 229,
                    weekly_tss_cap: Optional[float] = None,
                    phase_constraints: Optional[Dict] = None) -> List[Dict[str, Any]]:
    """校验整个周计划。"""
    pc = phase_constraints or {}

    # 逐条校验
    checked = [postcheck_workout(w, ftp, pc) for w in workouts]

    # CK_06: 周 TSS 总量
    total_tss = sum(w.get("target_tss", 0) or 0 for w in checked)
    if weekly_tss_cap and total_tss > weekly_tss_cap:
        scale = weekly_tss_cap / total_tss
        logger.warning("PostCheck: 周TSS %.0f → %.0f (cap)", total_tss, weekly_tss_cap)
        for w in checked:
            if w.get("target_tss"):
                w["target_tss"] = round(w["target_tss"] * scale)

    # CK_07: 连续高强度天数（3天 TSS>60 不合理）
    sorted_ws = sorted(checked, key=lambda x: x.get("date", ""))
    streak = 0
    for w in sorted_ws:
        if (w.get("target_tss", 0) or 0) > 60:
            streak += 1
            if streak >= 3:
                logger.warning("PostCheck: 连续 %d 天 TSS>60，降至40", streak)
                w["target_tss"] = min(w["target_tss"], 40)
                w["description"] = w.get("description", "") + "（系统降负荷：避免连续高强度）"
        else:
            streak = 0

    # CK_08: 高强度天数限制（按阶段）
    max_intensity_days = pc.get("max_intensity_days", 3)
    intensity_days = [w for w in checked
                      if w.get("sport") == "cycling"
                      and (w.get("target_tss", 0) or 0) > 70]
    if len(intensity_days) > max_intensity_days:
        # 按 TSS 降序，多余的降级
        intensity_days.sort(key=lambda w: w.get("target_tss", 0), reverse=True)
        for w in intensity_days[max_intensity_days:]:
            logger.warning("PostCheck: 高强度天数超限，%s TSS %d→40", w.get("date"), w.get("target_tss", 0))
            w["target_tss"] = min(w["target_tss"], 40)
            w["target_intensity"] = "Zone 2"
            w["description"] = w.get("description", "") + "（系统降级：本阶段高强度天数已满）"

    # CK_09: 最少休息日
    min_rest = pc.get("min_rest_days", 1)
    rest_count = sum(1 for w in checked if w.get("sport") == "rest")
    if rest_count < min_rest:
        logger.warning("PostCheck: 休息日 %d < %d，需要补充", rest_count, min_rest)
        # 不强制修改（AI 可能把力量日当恢复日），仅警告

    return checked


# ═══════════════════════════════════════════════════════════════════════════
# Athlete Profile & Context
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_PROFILE = {
    "ftp": 229,
    "max_hr": 192,
    "resting_hr": 42,
    "goal": "提升骑行能力，维持跑步基础，增强核心力量",
    "focus": "cycling",
    "weekly_hours_available": 10,
    "event_name": "",
    "event_date": "",
    "constraints": [
        "每周至少1次跑步",
        "每周2-3次力量训练",
        "骑行为主要训练项目",
    ],
}


def gather_context(conn) -> Dict[str, Any]:
    """Gather all context needed for plan generation from database."""
    from engine import database

    fitness_row = conn.execute(
        "SELECT * FROM fitness_history ORDER BY date DESC LIMIT 1"
    ).fetchone()
    fitness = dict(fitness_row) if fitness_row else {"ctl": 0, "atl": 0, "tsb": 0}

    cutoff = (date.today() - timedelta(days=14)).isoformat()
    recent = conn.execute(
        """SELECT date, name, sport, distance_m, total_timer_s, tss,
                  normalized_power, avg_hr, intensity_factor
           FROM activities WHERE date >= ? ORDER BY date DESC""",
        (cutoff,),
    ).fetchall()
    recent_list = [dict(r) for r in recent]

    body = database.get_latest_body_comp(conn)
    last_week_stats = database.weekly_stats(conn)
    fatigue = database.get_muscle_fatigue(conn, date.today().isoformat())

    prev_monday = date.today() - timedelta(days=date.today().weekday() + 7)
    prev_sunday = prev_monday + timedelta(days=6)
    prev_planned = conn.execute(
        "SELECT COALESCE(SUM(target_tss), 0) as total FROM planned_workouts WHERE date >= ? AND date <= ?",
        (prev_monday.isoformat(), prev_sunday.isoformat()),
    ).fetchone()
    prev_week_tss = prev_planned["total"] if prev_planned else 0

    return {
        "fitness": fitness,
        "recent_activities": recent_list,
        "body": dict(body) if body else None,
        "last_week_stats": last_week_stats,
        "muscle_fatigue": fatigue,
        "prev_week_tss": prev_week_tss,
    }


def _load_profile(conn, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Load athlete profile from DB settings, merged with overrides."""
    from engine import database

    profile = dict(DEFAULT_PROFILE)
    for key in DEFAULT_PROFILE:
        val = database.get_setting(conn, f"athlete_{key}")
        if val:
            if key in ("ftp", "max_hr", "resting_hr", "weekly_hours_available"):
                profile[key] = float(val) if "." in val else int(val)
            elif key == "constraints":
                try:
                    profile[key] = json.loads(val)
                except Exception:
                    pass
            else:
                profile[key] = val
    if overrides:
        profile.update(overrides)
    return profile


# ═══════════════════════════════════════════════════════════════════════════
# Plan Generation Prompt — Action Space 约束版
# ═══════════════════════════════════════════════════════════════════════════

_PLAN_PROMPT = """你是一位专业的自行车和铁三教练，精通 TrainingPeaks 和 Intervals.icu 的训练方法论。

请根据以下运动员信息，为下周（{week_start} 至 {week_end}）生成详细的训练计划。

## 运动员档案
{athlete_profile}

## 当前训练阶段
{phase_info}

## 当前体能状态
{fitness_status}

## 系统约束（硬性，不可违反）
{hard_constraints}

## 最近训练记录（过去2周）
{recent_activities}

## 身体数据
{body_data}

## 肌肉疲劳状态
{muscle_fatigue}

## 输出要求
1. 生成7天的训练计划（周一到周日）
2. 每个训练包含：日期、运动类型(cycling/running/training/rest)、训练名称、描述、目标TSS、预计时长(分钟)、强度区间、涉及肌群
3. **严格遵守系统约束中的 TSS 上限、高强度天数限制和休息日要求**
4. 符合当前训练阶段的目标和重点
5. 力量训练要具体到动作和组数
6. 骑行训练包含功率区间目标（基于FTP={ftp}W）
7. 跑步训练包含配速或心率区间目标

请以 JSON 数组格式返回：
```json
[
  {{
    "date": "YYYY-MM-DD",
    "sport": "cycling|running|training|rest",
    "name": "训练名称",
    "description": "详细训练内容",
    "target_tss": 80,
    "duration_min": 90,
    "intensity": "Zone 2|Zone 3|Zone 4|Mixed|Recovery|Rest",
    "muscle_groups": ["quadriceps", "glutes", "core"]
  }}
]
```

只返回 JSON 数组，不要其他文字。"""


# ═══════════════════════════════════════════════════════════════════════════
# Pipeline: Phase → Trigger → AI (or Fallback) → PostCheck → Return
# ═══════════════════════════════════════════════════════════════════════════

def generate_weekly_plan(
    conn,
    profile: Optional[Dict[str, Any]] = None,
    week_offset: int = 1,
) -> List[Dict[str, Any]]:
    """Generate a weekly training plan.

    Full pipeline:
      Phase Detection → Trigger Arbitration → AI Generate (or Fallback) → PostCheck
    """
    from engine import database

    merged_profile = _load_profile(conn, profile)
    ftp = merged_profile.get("ftp", 229)

    # Calculate week dates
    today = date.today()
    ref = today + timedelta(weeks=week_offset)
    week_start = ref - timedelta(days=ref.weekday())
    week_end = week_start + timedelta(days=6)

    # ── Layer 0: 训练阶段判定 ──
    phase, phase_reason = detect_training_phase(conn)
    phase_constraints = _PHASE_CONSTRAINTS.get(phase, _PHASE_CONSTRAINTS[TrainingPhase.BUILD])
    logger.info("Phase: %s — %s", phase, phase_reason)

    # ── Layer 1: Trigger 仲裁 ──
    trigger_action, trigger_reason, fallback_template, all_triggers = evaluate_triggers(conn, phase)
    logger.info("Trigger: %s — %s (hits: %d)", trigger_action, trigger_reason, len(all_triggers))

    # 如果阶段本身就是 Recovery，强制使用恢复课表
    if phase == TrainingPhase.RECOVERY and trigger_action != TriggerAction.LOCAL_OVERRIDE:
        logger.info("Phase=recovery → 降级为恢复课表")
        trigger_action = TriggerAction.LOCAL_OVERRIDE
        trigger_reason = phase_reason
        fallback_template = "RECOVERY_WEEK"

    if trigger_action == TriggerAction.LOCAL_OVERRIDE:
        logger.warning("⚠️ 安全红线/恢复期: %s → 兜底课表 [%s]", trigger_reason, fallback_template)
        workouts = get_fallback_plan(fallback_template, week_start)
        workouts = postcheck_plan(workouts, ftp=ftp, phase_constraints=phase_constraints)
        _record_generation(conn, phase, trigger_action, trigger_reason, len(workouts))
        return workouts

    # 计算周 TSS 上限
    ctx = gather_context(conn)
    prev_tss = ctx.get("prev_week_tss", 0)
    base_weekly_tss = prev_tss if prev_tss > 0 else 350
    weekly_tss_cap = base_weekly_tss * phase_constraints["weekly_tss_multiplier"]

    if trigger_action == TriggerAction.REDUCE_LOAD:
        weekly_tss_cap = min(weekly_tss_cap, base_weekly_tss * 0.8)
        logger.info("REDUCE_LOAD: 周TSS上限 %.0f", weekly_tss_cap)

    # ── 构建 prompt ──
    fitness_str = (
        f"CTL(体能): {ctx['fitness'].get('ctl', 0):.1f}\n"
        f"ATL(疲劳): {ctx['fitness'].get('atl', 0):.1f}\n"
        f"TSB(状态): {ctx['fitness'].get('tsb', 0):.1f}\n"
    )
    if prev_tss > 0:
        fitness_str += f"上周TSS: {prev_tss:.0f}\n"

    phase_info = (
        f"阶段: {phase} — {phase_constraints['description']}\n"
        f"本阶段重点训练类型: {', '.join(phase_constraints['key_workouts'])}\n"
        f"主要强度区间: {phase_constraints['primary_zones']}\n"
    )

    hard_constraints_str = (
        f"- 本周TSS总量上限: {weekly_tss_cap:.0f}\n"
        f"- 单日TSS上限: {phase_constraints['max_daily_tss']}\n"
        f"- 高强度训练(Zone 4+)最多: {phase_constraints['max_intensity_days']} 天\n"
        f"- 至少休息日: {phase_constraints['min_rest_days']} 天\n"
        f"- 周TSS增幅不超过上周的10%\n"
    )
    if trigger_action == TriggerAction.REDUCE_LOAD:
        hard_constraints_str += f"- ⚠️ 系统检测到疲劳偏高，本周强制降负荷\n"

    recent_str = ""
    for a in ctx["recent_activities"][:10]:
        sport_zh = {"cycling": "骑行", "running": "跑步", "training": "力量"}.get(
            a.get("sport", ""), a.get("sport", ""))
        dist = f"{a['distance_m']/1000:.1f}km" if a.get("distance_m") else ""
        dur = f"{a['total_timer_s']/60:.0f}min" if a.get("total_timer_s") else ""
        tss_s = f"TSS:{a['tss']:.0f}" if a.get("tss") else ""
        np_s = f"NP:{a['normalized_power']:.0f}W" if a.get("normalized_power") else ""
        recent_str += f"- {a['date']} | {sport_zh} | {a.get('name','')} | {dist} {dur} {tss_s} {np_s}\n"
    if not recent_str:
        recent_str = "暂无近期训练数据"

    body_str = "暂无"
    if ctx["body"]:
        b = ctx["body"]
        body_str = (
            f"体重: {b.get('weight_kg', '?')}kg, 体脂: {b.get('body_fat_pct', '?')}%, "
            f"骨骼肌: {b.get('skeletal_muscle_kg', '?')}kg"
        )

    fatigue_str = "暂无"
    if ctx["muscle_fatigue"]:
        parts = []
        for group, score in ctx["muscle_fatigue"].items():
            level = "偏高" if score > 60 else ("中等" if score > 30 else "良好")
            parts.append(f"{group}: {score:.0f}%({level})")
        fatigue_str = ", ".join(parts)

    last_week = ctx["last_week_stats"]
    athlete_str = (
        f"FTP: {ftp}W\n"
        f"最大心率: {merged_profile['max_hr']}bpm\n"
        f"静息心率: {merged_profile['resting_hr']}bpm\n"
        f"训练目标: {merged_profile['goal']}\n"
        f"主项: {merged_profile['focus']}\n"
        f"每周可用时间: {merged_profile['weekly_hours_available']}小时\n"
        f"约束条件: {', '.join(merged_profile['constraints'])}\n"
    )
    if merged_profile.get("event_name"):
        athlete_str += f"目标赛事: {merged_profile['event_name']} ({merged_profile.get('event_date', '待定')})\n"
    athlete_str += (
        f"上周数据({last_week.get('week_label', '上周')}): "
        f"骑行{last_week.get('cycling_distance_km', 0):.1f}km, "
        f"跑步{last_week.get('running_distance_km', 0):.1f}km, "
        f"力量{last_week.get('strength_count', 0)}次, "
        f"TSS{last_week.get('total_tss', 0):.0f}"
    )

    prompt = _PLAN_PROMPT.format(
        week_start=week_start.isoformat(),
        week_end=week_end.isoformat(),
        athlete_profile=athlete_str,
        phase_info=phase_info,
        fitness_status=fitness_str,
        hard_constraints=hard_constraints_str,
        recent_activities=recent_str,
        body_data=body_str,
        muscle_fatigue=fatigue_str,
        ftp=ftp,
    )

    # ── AI 生成（带 Fallback）──
    logger.info("Generating plan for %s to %s (phase=%s, tss_cap=%.0f)...",
                week_start, week_end, phase, weekly_tss_cap)
    try:
        text = llm_client.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
            temperature=0.7,
        )
        workouts = llm_client.extract_json(text, expect_array=True)
    except Exception as e:
        logger.error("AI 生成失败: %s → 兜底课表", e)
        workouts = get_fallback_plan("AI_FAILURE", week_start)
        workouts = postcheck_plan(workouts, ftp=ftp, phase_constraints=phase_constraints)
        _record_generation(conn, phase, "AI_FAILURE", str(e), len(workouts))
        return workouts

    # ── 字段映射 + 肌群标准化 ──
    _MG_ALIASES = {"quads": "quadriceps", "quad": "quadriceps", "hams": "hamstrings",
                   "lats": "back", "biceps": "arms", "triceps": "arms", "abs": "core",
                   "lower_back": "back", "upper_back": "back"}
    for w in workouts:
        w.setdefault("compliance_status", "pending")
        if "name" in w and "title" not in w:
            w["title"] = w.pop("name")
        if "duration_min" in w and "target_duration_min" not in w:
            w["target_duration_min"] = w.pop("duration_min")
        if "intensity" in w and "target_intensity" not in w:
            w["target_intensity"] = w.pop("intensity")
        if isinstance(w.get("muscle_groups"), list):
            normalized = list(dict.fromkeys(
                _MG_ALIASES.get(g.lower().strip(), g.lower().strip())
                for g in w["muscle_groups"]
            ))
            w["muscle_groups_json"] = json.dumps(normalized)
            del w["muscle_groups"]

    # ── Layer 3: PostCheck ──
    workouts = postcheck_plan(workouts, ftp=ftp,
                               weekly_tss_cap=weekly_tss_cap,
                               phase_constraints=phase_constraints)

    total_tss = sum(w.get("target_tss", 0) or 0 for w in workouts)
    logger.info("Generated %d workouts (TSS: %.0f, cap: %.0f, phase: %s)",
                len(workouts), total_tss, weekly_tss_cap, phase)

    _record_generation(conn, phase, trigger_action, trigger_reason, len(workouts))
    return workouts


def _record_generation(conn, phase: str, action: str, reason: str, count: int):
    """记录生成事件（用于 Cooldown 和审计）。"""
    from engine import database
    database.set_setting(conn, "last_plan_generated_at", datetime.now().isoformat())
    database.set_setting(conn, "last_plan_phase", phase)
    database.set_setting(conn, "last_plan_trigger", f"{action}: {reason}")
    database.set_setting(conn, "last_plan_count", str(count))


# ═══════════════════════════════════════════════════════════════════════════
# Save Plan to DB
# ═══════════════════════════════════════════════════════════════════════════

def save_plan(conn, workouts: List[Dict[str, Any]]) -> int:
    """Save generated workouts to database.

    IMPORTANT: Clears existing workouts for the target week before inserting.
    """
    from engine import database

    if workouts:
        dates = [w["date"] for w in workouts if w.get("date")]
        if dates:
            week_start = min(dates)
            week_end = max(dates)
            conn.execute(
                "DELETE FROM planned_workouts WHERE date >= ? AND date <= ?",
                (week_start, week_end),
            )
            conn.execute(
                "DELETE FROM muscle_fatigue WHERE date >= ? AND date <= ?",
                (week_start, week_end),
            )

    count = 0
    for w in workouts:
        database.upsert_planned_workout(conn, w)
        count += 1
        mg_json = w.get("muscle_groups_json")
        if mg_json:
            groups = json.loads(mg_json)
            for group in groups:
                _update_muscle_fatigue(conn, w["date"], group, w.get("target_tss", 50))

    return count


def _update_muscle_fatigue(conn, workout_date: str, muscle_group: str, tss: float):
    """Update muscle_fatigue table based on planned workout."""
    fatigue_score = min(100, tss * 0.8)
    conn.execute(
        """INSERT INTO muscle_fatigue (date, muscle_group, fatigue_score, source_activity_ids)
           VALUES (?, ?, ?, 'planned')
           ON CONFLICT(date, muscle_group) DO UPDATE SET fatigue_score=excluded.fatigue_score""",
        (workout_date, muscle_group, fatigue_score),
    )
