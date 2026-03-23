"""训练就绪度评估 + 身体趋势结论 + 周计划偏差分析。

为 v1 Decision Cockpit 提供核心「结论层」逻辑。
"""

from __future__ import annotations

import os
import sqlite3
import statistics
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple


# ───────────────────────────────────────────────────────────────
# 1. Readiness（今日训练就绪度）
# ───────────────────────────────────────────────────────────────

# 状态常量
STATUS_KEY_WORKOUT = "可执行关键课"
STATUS_NORMAL = "可正常训练"
STATUS_RECOVERY = "建议恢复训练"
STATUS_REST = "建议休息"
STATUS_UNKNOWN = "数据不足"

CONFIDENCE_HIGH = "高"
CONFIDENCE_MED = "中"
CONFIDENCE_LOW = "低"


@dataclass
class ReadinessResult:
    """今日训练就绪度评估结果。"""
    status: str = STATUS_UNKNOWN
    confidence: str = CONFIDENCE_LOW
    reasons: List[str] = field(default_factory=list)
    suggestion: str = "请结合主观体感判断"
    raw_data: Dict[str, Any] = field(default_factory=dict)
    scoring: Dict[str, Any] = field(default_factory=dict)
    today_plan_type: Optional[str] = None  # 今日计划类型
    data_missing: bool = False
    # 置信度原因列表，说明置信度评级的依据
    confidence_reasons: List[str] = field(default_factory=list)
    # 异常警报，检测连续性异常模式
    anomaly_alert: Dict[str, Any] = field(default_factory=lambda: {
        "triggered": False, "conditions": [], "message": None
    })

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _safe_float(v: Any) -> Optional[float]:
    """安全转 float，None / 非数字返回 None。"""
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def compute_readiness(conn: sqlite3.Connection, on_date: Optional[str] = None) -> ReadinessResult:
    """计算指定日期的训练就绪度。

    判断逻辑（基于 7 日均值与标准差）：
    - HRV: 在 7日均值 ± 1SD → 正常; < 均值 - 1.5SD → 异常低
    - RHR: 在 7日均值 ± 3bpm → 正常; > 均值 + 5bpm → 异常高
    - 睡眠: >= 6h → 正常; < 5h → 差
    - TSB: > 0 → 恢复充分; -5~0 → 可训练; -15~-5 → 疲劳; < -15 → 高风险
    """
    today_str = on_date or date.today().isoformat()
    result = ReadinessResult()

    # ── 获取今日 wellness ──
    row = conn.execute(
        "SELECT hrv, resting_hr, sleep_hours, sleep_score FROM wellness WHERE date = ?",
        (today_str,),
    ).fetchone()

    today_hrv = _safe_float(row["hrv"]) if row else None
    today_rhr = _safe_float(row["resting_hr"]) if row else None
    today_sleep = _safe_float(row["sleep_hours"]) if row else None
    today_sleep_score = _safe_float(row["sleep_score"]) if row else None

    # ── 获取今日 TSB ──
    fit_row = conn.execute(
        "SELECT ctl, atl, tsb FROM fitness_history ORDER BY date DESC LIMIT 1"
    ).fetchone()
    today_ctl = _safe_float(fit_row["ctl"]) if fit_row else None
    today_atl = _safe_float(fit_row["atl"]) if fit_row else None
    today_tsb = _safe_float(fit_row["tsb"]) if fit_row else None

    # ── 获取 7 日 wellness 历史（不含今天）──
    week_ago = (date.fromisoformat(today_str) - timedelta(days=7)).isoformat()
    hist_rows = conn.execute(
        "SELECT hrv, resting_hr, sleep_hours FROM wellness WHERE date >= ? AND date < ? ORDER BY date",
        (week_ago, today_str),
    ).fetchall()

    hrv_hist = [_safe_float(r["hrv"]) for r in hist_rows if _safe_float(r["hrv"]) is not None]
    rhr_hist = [_safe_float(r["resting_hr"]) for r in hist_rows if _safe_float(r["resting_hr"]) is not None]
    sleep_hist = [_safe_float(r["sleep_hours"]) for r in hist_rows if _safe_float(r["sleep_hours"]) is not None]

    # 7 日均值和标准差
    hrv_mean = statistics.mean(hrv_hist) if len(hrv_hist) >= 3 else None
    hrv_sd = statistics.stdev(hrv_hist) if len(hrv_hist) >= 3 else None
    rhr_mean = statistics.mean(rhr_hist) if len(rhr_hist) >= 3 else None
    sleep_mean = statistics.mean(sleep_hist) if len(sleep_hist) >= 3 else None

    # ── 存储原始数据 ──
    result.raw_data = {
        "hrv": today_hrv,
        "resting_hr": today_rhr,
        "sleep_hours": today_sleep,
        "sleep_score": today_sleep_score,
        "ctl": today_ctl,
        "atl": today_atl,
        "tsb": today_tsb,
        "hrv_7d_mean": round(hrv_mean, 1) if hrv_mean else None,
        "hrv_7d_sd": round(hrv_sd, 1) if hrv_sd else None,
        "rhr_7d_mean": round(rhr_mean, 1) if rhr_mean else None,
        "sleep_7d_mean": round(sleep_mean, 2) if sleep_mean else None,
        "date": today_str,
    }

    # ── 今日计划类型 ──
    plan_row = conn.execute(
        "SELECT sport, title, target_intensity FROM planned_workouts WHERE date = ? ORDER BY id LIMIT 1",
        (today_str,),
    ).fetchone()
    if plan_row:
        intensity = (plan_row["target_intensity"] or "").lower()
        title = (plan_row["title"] or "").lower()
        if any(k in intensity for k in ("z4", "z5", "vo2", "threshold")) or "关键" in title or "间歇" in title:
            result.today_plan_type = "关键课"
        elif any(k in intensity for k in ("z1", "z2", "recovery", "恢复")):
            result.today_plan_type = "恢复"
        else:
            result.today_plan_type = "正常"

    # ── 逐项评分 ──
    anomaly_count = 0
    reasons: List[str] = []
    scoring: Dict[str, str] = {}

    # HRV 评估
    if today_hrv is not None and hrv_mean is not None and hrv_sd is not None and hrv_sd > 0:
        delta = today_hrv - hrv_mean
        if delta < -1.5 * hrv_sd:
            scoring["hrv"] = "异常低"
            anomaly_count += 1
            reasons.append(f"HRV {today_hrv:.0f}ms，低于 7日均值 {hrv_mean:.0f}ms 超过 1.5SD")
        elif delta < -1 * hrv_sd:
            scoring["hrv"] = "偏低"
            reasons.append(f"HRV {today_hrv:.0f}ms，略低于 7日均值")
        else:
            scoring["hrv"] = "正常"
            reasons.append(f"HRV {today_hrv:.0f}ms，正常范围")
    elif today_hrv is not None:
        scoring["hrv"] = "数据不足"
        reasons.append(f"HRV {today_hrv:.0f}ms（历史数据不足，无法评估趋势）")
    else:
        scoring["hrv"] = "缺失"

    # RHR 评估
    if today_rhr is not None and rhr_mean is not None:
        delta = today_rhr - rhr_mean
        if delta > 5:
            scoring["rhr"] = "异常高"
            anomaly_count += 1
            reasons.append(f"静息心率 {today_rhr:.0f}bpm，高于 7日均值 {rhr_mean:.0f} 超过 5bpm")
        elif delta > 3:
            scoring["rhr"] = "偏高"
            reasons.append(f"静息心率 {today_rhr:.0f}bpm，略偏高")
        else:
            scoring["rhr"] = "正常"
            reasons.append(f"静息心率 {today_rhr:.0f}bpm，正常")
    elif today_rhr is not None:
        scoring["rhr"] = "数据不足"
        reasons.append(f"静息心率 {today_rhr:.0f}bpm")
    else:
        scoring["rhr"] = "缺失"

    # 睡眠评估
    if today_sleep is not None:
        if today_sleep < 5:
            scoring["sleep"] = "差"
            anomaly_count += 1
            reasons.append(f"睡眠 {today_sleep:.1f}h，严重不足")
        elif today_sleep < 6:
            scoring["sleep"] = "偏低"
            reasons.append(f"睡眠 {today_sleep:.1f}h，不足")
        else:
            scoring["sleep"] = "正常"
            h = int(today_sleep)
            m = int((today_sleep - h) * 60)
            reasons.append(f"睡眠 {h}h{m:02d}m，充足")
    else:
        scoring["sleep"] = "缺失"

    # TSB 评估
    if today_tsb is not None:
        if today_tsb < -15:
            scoring["tsb"] = "高风险"
            anomaly_count += 1
            reasons.append(f"TSB {today_tsb:.1f}，疲劳累积较高")
        elif today_tsb < -5:
            scoring["tsb"] = "疲劳"
            reasons.append(f"TSB {today_tsb:.1f}，有一定疲劳")
        elif today_tsb <= 0:
            scoring["tsb"] = "可训练"
            reasons.append(f"TSB {today_tsb:.1f}，恢复中")
        else:
            scoring["tsb"] = "恢复充分"
            reasons.append(f"TSB {today_tsb:.1f}，状态良好")
    else:
        scoring["tsb"] = "缺失"

    result.scoring = scoring
    result.reasons = reasons

    # ── 数据完整性检查 ──
    data_items = [today_hrv, today_rhr, today_sleep, today_tsb]
    available = sum(1 for x in data_items if x is not None)

    if available == 0:
        result.status = STATUS_UNKNOWN
        result.confidence = CONFIDENCE_LOW
        result.confidence_reasons = ["今日无任何健康数据"]
        result.suggestion = "请先同步今日 Garmin 数据"
        result.data_missing = True
        return result

    if available <= 1:
        result.data_missing = True

    # ── 综合判断 ──
    if anomaly_count >= 3 or (today_tsb is not None and today_tsb < -15):
        result.status = STATUS_REST
        result.confidence = CONFIDENCE_HIGH if available >= 3 else CONFIDENCE_MED
        result.suggestion = "身体疲劳信号明显，建议今日完全休息或轻微活动恢复"
    elif anomaly_count >= 2:
        result.status = STATUS_RECOVERY
        result.confidence = CONFIDENCE_MED
        result.suggestion = "多项指标偏离正常，建议降级为 Z1-Z2 恢复训练"
    elif anomaly_count == 1:
        result.status = STATUS_NORMAL
        result.confidence = CONFIDENCE_MED
        anomaly_item = [k for k, v in scoring.items() if v in ("异常低", "异常高", "差", "高风险")]
        note = f"，关注{anomaly_item[0].upper()}" if anomaly_item else ""
        result.suggestion = f"可正常训练{note}；若主观疲劳高，降级 1 档"
    else:
        # 全部正常
        if result.today_plan_type == "关键课":
            result.status = STATUS_KEY_WORKOUT
            result.confidence = CONFIDENCE_HIGH if available >= 3 else CONFIDENCE_MED
            result.suggestion = "状态良好，适合执行关键课（阈值/间歇）"
        else:
            result.status = STATUS_NORMAL
            result.confidence = CONFIDENCE_HIGH if available >= 3 else CONFIDENCE_MED
            result.suggestion = "按计划执行；若主观疲劳高，降级 1 档"

    # ── 置信度原因推导 ──
    conf_reasons: List[str] = []
    normal_count = sum(1 for v in scoring.values() if v in ("正常", "恢复充分", "可训练"))
    abnormal_count = sum(1 for v in scoring.values() if v in ("异常低", "异常高", "差", "高风险"))
    missing_count = sum(1 for v in scoring.values() if v in ("缺失", "数据不足"))

    if abnormal_count == 0 and normal_count >= 3:
        conf_reasons.append("恢复指标总体正常")
    elif abnormal_count == 0 and normal_count >= 1:
        conf_reasons.append("已有指标未见异常")
    elif abnormal_count >= 2:
        conf_reasons.append("多项指标异常，信号明确")
    elif abnormal_count == 1:
        conf_reasons.append("存在单项异常指标")

    if available >= 4:
        conf_reasons.append("数据维度完整")
    elif available >= 3:
        conf_reasons.append("主要数据维度可用")
    elif available >= 1:
        conf_reasons.append("数据维度不足，判断可靠性有限")

    has_trend = len(hrv_hist) >= 3 or len(rhr_hist) >= 3
    if has_trend:
        conf_reasons.append("7日基线数据充足")
    else:
        conf_reasons.append("连续趋势证据不完整")

    result.confidence_reasons = conf_reasons

    # ── 异常警报检测（连续性异常模式）──
    anomaly_conditions: List[str] = []
    _detect_anomaly_patterns(conn, today_str, rhr_mean, anomaly_conditions)

    # data_insufficient：3+ 个关键指标缺失
    if missing_count >= 3:
        anomaly_conditions.append("data_insufficient")

    if anomaly_conditions:
        result.anomaly_alert = {
            "triggered": True,
            "conditions": anomaly_conditions,
            "message": "当前存在异常恢复或数据不足情况，建议减少训练强度并由人工复核，不建议仅依据系统自动建议继续推进训练。",
        }

    return result


def _detect_anomaly_patterns(
    conn: sqlite3.Connection,
    today_str: str,
    rhr_7d_mean: Optional[float],
    conditions: List[str],
) -> None:
    """检测连续性异常模式：RHR连续升高、HRV连续下降、睡眠连续不足。

    结果直接追加到 conditions 列表。
    """
    today_d = date.fromisoformat(today_str)

    # 获取近 5 天（含今天）的 wellness 数据，按日期升序
    d5_ago = (today_d - timedelta(days=4)).isoformat()
    recent_rows = conn.execute(
        "SELECT date, hrv, resting_hr, sleep_hours FROM wellness "
        "WHERE date >= ? AND date <= ? ORDER BY date",
        (d5_ago, today_str),
    ).fetchall()

    recent_rhr = [(r["date"], _safe_float(r["resting_hr"])) for r in recent_rows if _safe_float(r["resting_hr"]) is not None]
    recent_hrv = [(r["date"], _safe_float(r["hrv"])) for r in recent_rows if _safe_float(r["hrv"]) is not None]
    recent_sleep = [(r["date"], _safe_float(r["sleep_hours"])) for r in recent_rows if _safe_float(r["sleep_hours"]) is not None]

    # rhr_elevated_3d: RHR 连续 3+ 天高于 7日均值 + 1SD
    if rhr_7d_mean is not None and len(recent_rhr) >= 3:
        d7_ago = (today_d - timedelta(days=7)).isoformat()
        rhr_7d_rows = conn.execute(
            "SELECT resting_hr FROM wellness WHERE date >= ? AND date < ?",
            (d7_ago, today_str),
        ).fetchall()
        rhr_vals_7d = [_safe_float(r["resting_hr"]) for r in rhr_7d_rows if _safe_float(r["resting_hr"]) is not None]
        if len(rhr_vals_7d) >= 3:
            rhr_sd = statistics.stdev(rhr_vals_7d)
            threshold = rhr_7d_mean + rhr_sd
            consecutive = 0
            for _, val in reversed(recent_rhr):
                if val > threshold:
                    consecutive += 1
                else:
                    break
            if consecutive >= 3:
                conditions.append("rhr_elevated_3d")

    # hrv_declining_5d: HRV 连续 5 天下降
    if len(recent_hrv) >= 5:
        last_5 = [v for _, v in recent_hrv[-5:]]
        if all(last_5[i] > last_5[i + 1] for i in range(4)):
            conditions.append("hrv_declining_5d")

    # sleep_deficit_3d: 连续 3+ 天睡眠 < 6h
    if len(recent_sleep) >= 3:
        consecutive = 0
        for _, val in reversed(recent_sleep):
            if val < 6:
                consecutive += 1
            else:
                break
        if consecutive >= 3:
            conditions.append("sleep_deficit_3d")


# ───────────────────────────────────────────────────────────────
# 2. Weekly Deviation（本周执行偏差）
# ───────────────────────────────────────────────────────────────

@dataclass
class WeeklyDeviation:
    """本周训练执行偏差。"""
    week_start: str = ""
    week_end: str = ""
    planned_count: int = 0
    actual_count: int = 0
    planned_tss: float = 0
    actual_tss: float = 0
    deviation_pct: float = 0  # actual/planned %
    judgment: str = "无数据"  # 正常 / 略落后 / 明显落后 / 过载
    suggestion: str = ""
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    remaining_days: int = 0
    remaining_tss: float = 0
    # 主项（骑行/跑步）vs 力量训练分类统计
    primary_planned: int = 0   # 骑行/跑步计划场次
    primary_actual: int = 0    # 骑行/跑步实际场次
    strength_planned: int = 0  # 力量训练计划场次
    strength_actual: int = 0   # 力量训练实际场次

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def compute_weekly_deviation(
    conn: sqlite3.Connection,
    ref_date: Optional[str] = None,
) -> WeeklyDeviation:
    """计算本周训练执行偏差。"""
    d = date.fromisoformat(ref_date) if ref_date else date.today()
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    today = date.today()

    result = WeeklyDeviation(
        week_start=monday.isoformat(),
        week_end=sunday.isoformat(),
    )

    # 计划训练
    workouts = conn.execute(
        "SELECT * FROM planned_workouts WHERE date >= ? AND date <= ? ORDER BY date",
        (monday.isoformat(), sunday.isoformat()),
    ).fetchall()

    # 实际活动
    activities = conn.execute(
        "SELECT * FROM activities WHERE date >= ? AND date <= ? ORDER BY date",
        (monday.isoformat(), sunday.isoformat()),
    ).fetchall()

    # 排除休息日
    planned_workouts = [w for w in workouts if w["sport"] not in ("rest", "stretch")]
    result.planned_count = len(planned_workouts)
    result.planned_tss = sum(_safe_float(w["target_tss"]) or 0 for w in planned_workouts)

    result.actual_count = len(activities)
    result.actual_tss = sum(_safe_float(a["tss"]) or 0 for a in activities)

    # ── 主项 vs 力量分类统计 ──
    # 主项运动类型（骑行/跑步/铁三相关）
    PRIMARY_SPORTS = {"cycling", "running", "swimming", "triathlon", "bike", "run", "swim",
                      "indoor_cycling", "virtual_ride", "trail_running", "open_water_swimming"}
    STRENGTH_SPORTS = {"strength", "strength_training", "weight_training", "gym",
                       "力量", "力量训练", "crossfit"}

    for w in planned_workouts:
        sport = (w["sport"] or "").lower().strip()
        if sport in PRIMARY_SPORTS:
            result.primary_planned += 1
        elif sport in STRENGTH_SPORTS:
            result.strength_planned += 1

    for a in activities:
        sport = (a["sport"] or "").lower().strip()
        if sport in PRIMARY_SPORTS:
            result.primary_actual += 1
        elif sport in STRENGTH_SPORTS:
            result.strength_actual += 1

    # 偏差率
    if result.planned_tss > 0:
        result.deviation_pct = round(result.actual_tss / result.planned_tss * 100, 1)
    elif result.actual_tss > 0:
        result.deviation_pct = 999  # 有实际但没计划

    # 已跳过的训练
    for w in planned_workouts:
        status = w["compliance_status"] or "pending"
        if status in ("missed", "skipped"):
            result.skipped.append({
                "date": w["date"],
                "sport": w["sport"],
                "title": w["title"],
                "target_tss": w["target_tss"],
                "reason": "",  # 用户可后续标记
            })

    # 剩余天数和 TSS
    remaining_dates = [(monday + timedelta(days=i)) for i in range(7) if (monday + timedelta(days=i)) > today]
    result.remaining_days = len(remaining_dates)
    result.remaining_tss = max(0, result.planned_tss - result.actual_tss)

    # 综合判断
    if result.planned_count == 0:
        result.judgment = "无计划"
        result.suggestion = "本周暂无训练计划"
    elif result.deviation_pct >= 110:
        result.judgment = "过载"
        result.suggestion = "实际训练量超出计划，注意恢复"
    elif result.deviation_pct >= 85:
        result.judgment = "正常"
        result.suggestion = "执行情况良好，按计划继续"
    elif result.deviation_pct >= 60:
        result.judgment = "略落后"
        if result.remaining_days > 0:
            daily_needed = result.remaining_tss / result.remaining_days if result.remaining_days > 0 else 0
            result.suggestion = f"略有落后，剩余 {result.remaining_days} 天需完成 {result.remaining_tss:.0f} TSS（约 {daily_needed:.0f}/天）"
        else:
            result.suggestion = "本周完成度偏低"
    else:
        result.judgment = "明显落后"
        if result.remaining_days >= 2:
            result.suggestion = "明显落后于计划，建议评估是否需要调整本周剩余安排"
        elif result.remaining_days > 0:
            result.suggestion = "明显落后，剩余时间有限，考虑顺延到下周"
        else:
            result.suggestion = "本周完成度较低，建议回顾原因并调整下周计划"

    return result


# ───────────────────────────────────────────────────────────────
# 3. Body Trend Summary（身体趋势结论）
# ───────────────────────────────────────────────────────────────

@dataclass
class BodyTrendSummary:
    """身体数据趋势结论。"""
    composition_summary: str = ""
    recovery_summary: str = ""
    training_implication: str = ""
    data_sufficient: bool = True
    # 身体趋势状态标签
    status_label: str = ""  # "偏向增肌回补" / "稳定维持" / "轻度减脂" / "疑似能量不足" / "需关注异常波动"
    # 关键变化摘要，格式如 "体重 +1.7kg ｜ 骨骼肌 +1.1kg ｜ 体脂 -0.5%"
    key_changes: str = ""
    # 短建议
    action: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def compute_body_trend_summary(conn: sqlite3.Connection) -> BodyTrendSummary:
    """基于规则模板生成身体趋势结论（v1 不使用 LLM）。"""
    result = BodyTrendSummary()
    today = date.today()
    d30_ago = (today - timedelta(days=30)).isoformat()

    # ── 获取体测数据 ──
    recent_comp = conn.execute(
        "SELECT * FROM body_composition WHERE date >= ? ORDER BY date DESC",
        (d30_ago,),
    ).fetchall()

    if len(recent_comp) < 2:
        result.data_sufficient = False
        result.composition_summary = "体测数据不足（近 30 天不足 2 条记录），暂无法给出趋势结论。"
        result.recovery_summary = ""
        result.training_implication = "建议定期进行体测以跟踪身体变化。"
        # 仍然尝试恢复评估
        _fill_recovery_summary(conn, result)
        return result

    latest = dict(recent_comp[0])
    oldest = dict(recent_comp[-1])

    # 体重变化
    w_latest = _safe_float(latest.get("weight_kg"))
    w_oldest = _safe_float(oldest.get("weight_kg"))
    w_delta = round(w_latest - w_oldest, 1) if w_latest and w_oldest else None

    # 骨骼肌变化
    sm_latest = _safe_float(latest.get("skeletal_muscle_kg"))
    sm_oldest = _safe_float(oldest.get("skeletal_muscle_kg"))
    sm_delta = round(sm_latest - sm_oldest, 1) if sm_latest and sm_oldest else None

    # 体脂率变化
    bf_latest = _safe_float(latest.get("body_fat_pct"))
    bf_oldest = _safe_float(oldest.get("body_fat_pct"))
    bf_delta = round(bf_latest - bf_oldest, 1) if bf_latest and bf_oldest else None

    # ── 生成组成结论 ──
    parts = []
    days_span = (date.fromisoformat(latest["date"]) - date.fromisoformat(oldest["date"])).days
    period = f"过去 {days_span} 天" if days_span > 0 else "近期"

    if w_delta is not None:
        direction = "上升" if w_delta > 0 else "下降" if w_delta < 0 else "持平"
        parts.append(f"体重{direction} {abs(w_delta):.1f}kg")

    if sm_delta is not None:
        direction = "上升" if sm_delta > 0 else "下降" if sm_delta < 0 else "持平"
        parts.append(f"骨骼肌{direction} {abs(sm_delta):.1f}kg")

    if bf_delta is not None:
        direction = "上升" if bf_delta > 0 else "下降" if bf_delta < 0 else "波动"
        parts.append(f"体脂率{direction} {abs(bf_delta):.1f}%")

    if parts:
        summary = f"{period}，" + "，".join(parts) + "。"

        # 解读
        if w_delta and sm_delta:
            if w_delta > 0 and sm_delta > 0:
                summary += "当前变化更像增肌/糖原回补，不像单纯增脂。"
            elif w_delta > 0 and sm_delta <= 0:
                summary += "体重上升但骨骼肌未增，需关注饮食和训练结构。"
            elif w_delta < 0 and sm_delta < 0:
                summary += "体重和骨骼肌同步下降，注意蛋白质摄入和力量训练。"
            elif w_delta < 0 and sm_delta >= 0:
                summary += "减脂保肌效果良好。"

        result.composition_summary = summary
    else:
        result.composition_summary = "体测数据字段不完整，暂无法给出组成趋势。"

    # ── 恢复状态 ──
    _fill_recovery_summary(conn, result)

    # ── 训练含义 ──
    implications = []
    if w_delta is not None and sm_delta is not None:
        if w_delta > 0 and sm_delta > 0:
            implications.append("可维持正常训练；若继续增肌方向，建议力量训练后补足碳水")
        elif w_delta > 0 and sm_delta <= 0:
            implications.append("建议增加力量训练比例，控制热量盈余")
        elif w_delta < -1:
            implications.append("体重下降中，注意训练强度不宜过高，避免过度消耗")
        else:
            implications.append("身体组成稳定，可按正常计划训练")

    result.training_implication = "。".join(implications) + "。" if implications else "暂无明确训练调整建议。"

    # ── key_changes: 关键变化摘要 ──
    kc_parts = []
    if w_delta is not None:
        sign = "+" if w_delta > 0 else ""
        kc_parts.append(f"体重 {sign}{w_delta:.1f}kg")
    if sm_delta is not None:
        sign = "+" if sm_delta > 0 else ""
        kc_parts.append(f"骨骼肌 {sign}{sm_delta:.1f}kg")
    if bf_delta is not None:
        sign = "+" if bf_delta > 0 else ""
        kc_parts.append(f"体脂 {sign}{bf_delta:.1f}%")
    result.key_changes = " ｜ ".join(kc_parts) if kc_parts else "数据不足"

    # ── status_label: 趋势状态标签 ──
    # ── action: 短建议 ──
    if w_delta is not None and sm_delta is not None and bf_delta is not None:
        if w_delta > 0.5 and sm_delta > 0.3 and bf_delta <= 0:
            result.status_label = "偏向增肌回补"
            result.action = "当前趋势积极，维持训练和营养策略"
        elif abs(w_delta) <= 0.5 and abs(sm_delta) <= 0.3 and abs(bf_delta) <= 0.5:
            result.status_label = "稳定维持"
            result.action = "身体组成稳定，可按计划正常训练"
        elif w_delta < -0.5 and bf_delta < 0 and sm_delta >= -0.2:
            result.status_label = "轻度减脂"
            result.action = "减脂效果良好，注意保持蛋白质摄入"
        elif w_delta < -1 and sm_delta < -0.3:
            result.status_label = "疑似能量不足"
            result.action = "体重和骨骼肌同步下降，建议增加热量摄入并检查训练负荷"
        else:
            result.status_label = "需关注异常波动"
            result.action = "身体组成变化方向不一致，建议观察并调整饮食结构"
    elif w_delta is not None or sm_delta is not None:
        # 部分数据可用
        result.status_label = "需关注异常波动"
        result.action = "体测数据不完整，建议补全后重新评估"
    else:
        result.status_label = ""
        result.action = "数据不足，暂无建议"

    return result


def _fill_recovery_summary(conn: sqlite3.Connection, result: BodyTrendSummary):
    """填充恢复状态结论。"""
    # 最近 7 天 wellness
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    wellness_rows = conn.execute(
        "SELECT hrv, resting_hr, sleep_hours FROM wellness WHERE date >= ? ORDER BY date DESC",
        (week_ago,),
    ).fetchall()

    if not wellness_rows:
        result.recovery_summary = "近 7 天无健康数据，无法评估恢复状态。"
        return

    hrvs = [_safe_float(r["hrv"]) for r in wellness_rows if _safe_float(r["hrv"]) is not None]
    rhrs = [_safe_float(r["resting_hr"]) for r in wellness_rows if _safe_float(r["resting_hr"]) is not None]
    sleeps = [_safe_float(r["sleep_hours"]) for r in wellness_rows if _safe_float(r["sleep_hours"]) is not None]

    parts = []

    if hrvs:
        hrv_mean = statistics.mean(hrvs)
        if len(hrvs) >= 3:
            hrv_sd = statistics.stdev(hrvs)
            if hrv_sd > hrv_mean * 0.2:
                parts.append(f"HRV 波动较大（均值 {hrv_mean:.0f}ms，标准差 {hrv_sd:.0f}），需关注")
            else:
                parts.append(f"HRV 稳定（均值 {hrv_mean:.0f}ms）")
        else:
            parts.append(f"HRV 均值 {hrv_mean:.0f}ms")

    if rhrs:
        rhr_mean = statistics.mean(rhrs)
        latest_rhr = rhrs[0]
        if latest_rhr > rhr_mean + 3:
            parts.append(f"静息心率偏高（{latest_rhr:.0f}bpm，均值 {rhr_mean:.0f}）")
        else:
            parts.append(f"静息心率正常（{latest_rhr:.0f}bpm）")

    if sleeps:
        sleep_mean = statistics.mean(sleeps)
        if sleep_mean < 6:
            parts.append(f"平均睡眠不足（{sleep_mean:.1f}h）")
        else:
            parts.append(f"睡眠充足（均值 {sleep_mean:.1f}h）")

    if parts:
        result.recovery_summary = "，".join(parts) + "，恢复状态" + (
            "正常。" if all("正常" in p or "稳定" in p or "充足" in p for p in parts) else "需关注。"
        )
    else:
        result.recovery_summary = "健康数据不完整，无法全面评估恢复状态。"


# ───────────────────────────────────────────────────────────────
# 4. Metric Comparison（指标对比数据）
# ───────────────────────────────────────────────────────────────

def get_metric_comparisons(conn: sqlite3.Connection) -> Dict[str, Any]:
    """获取关键指标的对比数据（当前值 vs 7日均值 / 上次）。

    返回适合 MetricCard 渲染的数据结构。
    """
    today_str = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=7)).isoformat()

    # 今日 wellness
    today_row = conn.execute(
        "SELECT * FROM wellness WHERE date = ?", (today_str,)
    ).fetchone()

    # 最近一条 wellness（兜底）
    latest_row = conn.execute(
        "SELECT * FROM wellness ORDER BY date DESC LIMIT 1"
    ).fetchone()

    w = dict(today_row) if today_row else (dict(latest_row) if latest_row else {})
    w_date = w.get("date", "")

    # 7 日历史
    hist = conn.execute(
        "SELECT hrv, resting_hr, sleep_hours, sleep_score FROM wellness WHERE date >= ? AND date < ? ORDER BY date",
        (week_ago, today_str),
    ).fetchall()

    def _avg(field):
        vals = [_safe_float(r[field]) for r in hist if _safe_float(r[field]) is not None]
        return round(statistics.mean(vals), 1) if vals else None

    hrv_7d = _avg("hrv")
    rhr_7d = _avg("resting_hr")
    sleep_7d = _avg("sleep_hours")

    # Fitness
    fit_row = conn.execute(
        "SELECT ctl, atl, tsb FROM fitness_history ORDER BY date DESC LIMIT 1"
    ).fetchone()
    fitness = dict(fit_row) if fit_row else {}

    # 本周 TSS
    monday = date.today() - timedelta(days=date.today().weekday())
    sunday = monday + timedelta(days=6)
    week_tss_row = conn.execute(
        "SELECT COALESCE(SUM(tss), 0) as total FROM activities WHERE date >= ? AND date <= ?",
        (monday.isoformat(), sunday.isoformat()),
    ).fetchone()
    week_actual_tss = week_tss_row["total"] if week_tss_row else 0

    week_planned_tss_row = conn.execute(
        "SELECT COALESCE(SUM(target_tss), 0) as total FROM planned_workouts WHERE date >= ? AND date <= ? AND sport NOT IN ('rest', 'stretch')",
        (monday.isoformat(), sunday.isoformat()),
    ).fetchone()
    week_planned_tss = week_planned_tss_row["total"] if week_planned_tss_row else 0

    # 7 日训练次数 + 总时长 + 总 TSS + 高强度天数
    week_activity_count = conn.execute(
        "SELECT COUNT(*) as c FROM activities WHERE date >= ?",
        (week_ago,),
    ).fetchone()["c"]

    week_totals = conn.execute(
        "SELECT COALESCE(SUM(total_timer_s), 0) as total_s, COALESCE(SUM(tss), 0) as total_tss FROM activities WHERE date >= ?",
        (week_ago,),
    ).fetchone()
    week_total_hours = round(week_totals["total_s"] / 3600, 1) if week_totals else 0
    week_total_tss = round(week_totals["total_tss"], 0) if week_totals else 0

    # 高强度天：当天有 IF >= 0.85 或 TSS >= 100 的活动
    high_intensity_rows = conn.execute(
        """SELECT COUNT(DISTINCT date) as c FROM activities
           WHERE date >= ? AND (tss >= 100 OR (normalized_power IS NOT NULL AND normalized_power > 0
                 AND CAST(normalized_power AS REAL) / ? >= 0.85))""",
        (week_ago, int(os.environ.get("TRAININGEDGE_FTP", "229"))),
    ).fetchone()
    high_intensity_days = high_intensity_rows["c"] if high_intensity_rows else 0

    # 各运动类型次数
    sport_counts_rows = conn.execute(
        "SELECT sport, COUNT(*) as c FROM activities WHERE date >= ? GROUP BY sport",
        (week_ago,),
    ).fetchall()
    sport_counts = {r["sport"]: r["c"] for r in sport_counts_rows}

    return {
        "hrv": {
            "value": _safe_float(w.get("hrv")),
            "unit": "ms",
            "avg_7d": hrv_7d,
            "delta_7d": round(_safe_float(w.get("hrv")) - hrv_7d, 1) if _safe_float(w.get("hrv")) and hrv_7d else None,
            "date": w_date,
            "source": "Garmin",
        },
        "sleep": {
            "value": _safe_float(w.get("sleep_hours")),
            "score": _safe_float(w.get("sleep_score")),
            "unit": "h",
            "avg_7d": sleep_7d,
            "delta_7d": round(_safe_float(w.get("sleep_hours")) - sleep_7d, 2) if _safe_float(w.get("sleep_hours")) and sleep_7d else None,
            "date": w_date,
            "source": "Garmin",
        },
        "resting_hr": {
            "value": _safe_float(w.get("resting_hr")),
            "unit": "bpm",
            "avg_7d": rhr_7d,
            "delta_7d": round(_safe_float(w.get("resting_hr")) - rhr_7d, 1) if _safe_float(w.get("resting_hr")) and rhr_7d else None,
            "date": w_date,
            "source": "Garmin",
        },
        "fitness": {
            "ctl": _safe_float(fitness.get("ctl")),
            "atl": _safe_float(fitness.get("atl")),
            "tsb": _safe_float(fitness.get("tsb")),
        },
        "week_tss": {
            "actual": round(week_actual_tss, 1),
            "planned": round(week_planned_tss, 1),
            "pct": round(week_actual_tss / week_planned_tss * 100, 1) if week_planned_tss > 0 else 0,
        },
        "week_activity": {
            "count": week_activity_count,
            "total_hours": week_total_hours,
            "total_tss": week_total_tss,
            "high_intensity_days": high_intensity_days,
            "sport_counts": sport_counts,
        },
    }


# ───────────────────────────────────────────────────────────────
# 5. Body Composition Comparisons（体测对比数据）
# ───────────────────────────────────────────────────────────────

def get_body_comp_comparisons(conn: sqlite3.Connection) -> Dict[str, Any]:
    """获取体测指标的对比数据（当前 vs 上次 / vs 30天均值）。"""
    records = conn.execute(
        "SELECT * FROM body_composition ORDER BY date DESC LIMIT 10"
    ).fetchall()

    if not records:
        return {"has_data": False}

    latest = dict(records[0])
    previous = dict(records[1]) if len(records) >= 2 else None

    # 30 天均值
    d30_ago = (date.today() - timedelta(days=30)).isoformat()
    avg_rows = conn.execute(
        "SELECT AVG(weight_kg) as w, AVG(body_fat_pct) as bf, AVG(skeletal_muscle_kg) as sm, AVG(lean_body_mass_kg) as lbm FROM body_composition WHERE date >= ?",
        (d30_ago,),
    ).fetchone()

    def _comp(field, latest_val, prev_val, avg_30d):
        v = _safe_float(latest_val)
        p = _safe_float(prev_val)
        a = _safe_float(avg_30d)
        return {
            "value": v,
            "vs_prev": round(v - p, 2) if v is not None and p is not None else None,
            "vs_30d": round(v - a, 2) if v is not None and a is not None else None,
        }

    return {
        "has_data": True,
        "latest_date": latest.get("date"),
        "latest_source": latest.get("source", "unknown"),
        "weight": _comp("weight_kg", latest.get("weight_kg"), previous.get("weight_kg") if previous else None, avg_rows["w"] if avg_rows else None),
        "body_fat": _comp("body_fat_pct", latest.get("body_fat_pct"), previous.get("body_fat_pct") if previous else None, avg_rows["bf"] if avg_rows else None),
        "skeletal_muscle": _comp("skeletal_muscle_kg", latest.get("skeletal_muscle_kg"), previous.get("skeletal_muscle_kg") if previous else None, avg_rows["sm"] if avg_rows else None),
        "lean_body_mass": _comp("lean_body_mass_kg", latest.get("lean_body_mass_kg"), previous.get("lean_body_mass_kg") if previous else None, avg_rows["lbm"] if avg_rows else None),
        "latest_full": latest,
        "previous_full": previous,
    }


# ───────────────────────────────────────────────────────────────
# 6. Decision Summary（综合决策摘要）
# ───────────────────────────────────────────────────────────────

def compute_decision_summary(
    conn: sqlite3.Connection,
    ref_date: Optional[str] = None,
) -> Dict[str, Any]:
    """聚合就绪度 + 周偏差为一份统一决策摘要。

    返回扁平化结构，前端可直接渲染：
    - today_*: 今日就绪度相关字段
    - week_*: 本周执行偏差相关字段
    - confidence / confidence_reasons / anomaly_alert: 置信度与异常警报
    """
    today_str = ref_date or date.today().isoformat()

    # 聚合两个结论层
    readiness = compute_readiness(conn, on_date=today_str)
    deviation = compute_weekly_deviation(conn, ref_date=today_str)

    # ── today_status 映射 ──
    STATUS_MAP = {
        STATUS_KEY_WORKOUT: ("key_workout", "可执行关键课"),
        STATUS_NORMAL: ("train_normal", "可正常训练"),
        STATUS_RECOVERY: ("recovery", "建议恢复训练"),
        STATUS_REST: ("rest", "建议休息"),
        STATUS_UNKNOWN: ("unknown", "数据不足"),
    }
    today_code, today_title = STATUS_MAP.get(readiness.status, ("unknown", readiness.status))

    # ── today_action 生成 ──
    today_action = readiness.suggestion

    # ── today_evidence: 从 reasons 中提取关键证据 ──
    today_evidence: List[str] = []
    raw = readiness.raw_data
    if raw.get("tsb") is not None:
        today_evidence.append(f"TSB {raw['tsb']:.1f}")
    if raw.get("sleep_hours") is not None:
        sh = raw["sleep_hours"]
        h, m = int(sh), int((sh - int(sh)) * 60)
        today_evidence.append(f"睡眠 {h}h{m:02d}m")
    if raw.get("resting_hr") is not None:
        rhr_label = readiness.scoring.get("rhr", "")
        stability = "稳定" if rhr_label == "正常" else rhr_label
        today_evidence.append(f"静息心率 {raw['resting_hr']:.0f} {stability}")
    if raw.get("hrv") is not None:
        today_evidence.append(f"HRV {raw['hrv']:.0f}ms")

    # ── week_status 映射 ──
    WEEK_MAP = {
        "正常": ("on_track", "本周进度正常"),
        "略落后": ("slightly_behind", "本周略落后"),
        "明显落后": ("behind", "本周明显落后"),
        "过载": ("overloaded", "本周过载"),
        "无计划": ("no_plan", "本周无计划"),
        "无数据": ("no_data", "无数据"),
    }
    week_code, week_text = WEEK_MAP.get(deviation.judgment, ("unknown", deviation.judgment))

    # ── week_action ──
    week_action = deviation.suggestion
    # 若略落后或明显落后，附加优先策略
    if deviation.judgment == "略落后":
        week_action = "优先保留关键课，降级其余训练"
    elif deviation.judgment == "明显落后":
        week_action = "评估是否调整本周计划，或顺延至下周"

    # ── 主项进度 ──
    primary_progress = {
        "actual": deviation.primary_actual,
        "planned": deviation.primary_planned,
        "pct": round(deviation.primary_actual / deviation.primary_planned * 100) if deviation.primary_planned > 0 else 0,
    }

    # ── 总 TSS 进度 ──
    total_tss = {
        "actual": round(deviation.actual_tss),
        "planned": round(deviation.planned_tss),
        "pct": round(deviation.deviation_pct),
    }

    return {
        "today_status": today_code,
        "today_title": today_title,
        "today_action": today_action,
        "today_evidence": today_evidence,
        "confidence": readiness.confidence,
        "confidence_reasons": readiness.confidence_reasons,
        "anomaly_alert": readiness.anomaly_alert,
        "week_status": week_code,
        "week_status_text": week_text,
        "week_primary_progress": primary_progress,
        "week_total_tss": total_tss,
        "week_strength_count": deviation.strength_actual,
        "week_action": week_action,
    }
