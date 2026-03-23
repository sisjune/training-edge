# 输入/输出文档

本文档描述 TrainingEdge 的数据输入来源、输出格式与数据流转过程。

---

## 输入 (Input)

### 1. FIT 文件

- **格式**: Garmin FIT 二进制协议（Flexible and Interoperable Data Transfer）
- **来源**: Garmin 手表通过 Garmin Connect 云端同步
- **内容**: 活动汇总（时长、距离、心率、功率等）、逐秒记录（心率、功率、速度、GPS）、圈速数据、设备信息
- **解析工具**: `fitparse` 库
- **存储路径**: `state/fit_files/<activity_id>.fit`

### 2. Garmin Connect API

- **认证**: OAuth token，存储于 `state/garmin_token.json`
- **数据**: 活动列表、FIT 文件下载、设备 FTP 值
- **库**: `garminconnect`

### 3. Intervals.icu API

- **认证**: API key + Athlete ID
- **用途**: 校验期交叉验证，自动导入初始 CTL/ATL/FTP
- **基础 URL**: `https://intervals.icu/api/v1`
- **获取数据**: 活动的 NP、TSS、IF，以及体能的 CTL、ATL 值

---

## 输出 (Output)

### 1. SQLite 数据库

数据库路径: `state/training_edge.db`（WAL 模式）

#### activities 表 — 活动汇总

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | Garmin 活动 ID |
| sport | TEXT | 运动类型（cycling, running 等） |
| name | TEXT | 活动名称 |
| date | TEXT | 日期 (YYYY-MM-DD) |
| start_time | TEXT | 开始时间 (ISO datetime) |
| total_elapsed_s | REAL | 总用时（秒） |
| total_timer_s | REAL | 运动时间（秒） |
| distance_m | REAL | 距离（米） |
| avg_hr / max_hr | INTEGER | 平均/最大心率 |
| avg_power / max_power | INTEGER | 平均/最大功率 |
| normalized_power | REAL | 标准化功率 (NP) |
| tss | REAL | 训练压力分数 (TSS) |
| intensity_factor | REAL | 强度因子 (IF) |
| xpower | REAL | 指数加权功率 |
| estimated_ftp | REAL | 估算 FTP (eFTP) |
| w_prime | REAL | W'（无氧做功能力） |
| trimp | REAL | 训练冲量 |
| vdot | REAL | 跑步能力指数 |
| carbs_used_g | REAL | 碳水消耗估算 (g) |
| drift_pct | REAL | 心率漂移百分比 |
| power_zones_json | TEXT | 功率区间分布 (JSON) |
| hr_zones_json | TEXT | 心率区间分布 (JSON) |
| pdc_json | TEXT | 功率持续时间曲线 (JSON) |
| laps_json | TEXT | 圈速数据 (JSON) |
| validation_json | TEXT | 校验结果 (JSON) |

#### records 表 — 逐秒数据

| 字段 | 类型 | 说明 |
|------|------|------|
| activity_id | INTEGER | 活动 ID (FK) |
| offset_s | INTEGER | 距活动开始的秒数 |
| heart_rate | INTEGER | 心率 (bpm) |
| power | INTEGER | 功率 (W) |
| speed | REAL | 速度 (m/s) |
| cadence | INTEGER | 踏频 (rpm) |
| altitude | REAL | 海拔 (m) |
| latitude / longitude | REAL | GPS 坐标 |

#### fitness_history 表 — 每日体能快照

| 字段 | 类型 | 说明 |
|------|------|------|
| date | TEXT PK | 日期 |
| ctl | REAL | 慢性训练负荷 |
| atl | REAL | 急性训练负荷 |
| tsb | REAL | 训练压力平衡 |
| ramp_rate | REAL | 负荷增长率 |
| daily_tss | REAL | 当日 TSS |

#### pdc_bests 表 — 最佳功率记录

| 字段 | 类型 | 说明 |
|------|------|------|
| duration_s | INTEGER | 持续时间（秒） |
| power | REAL | 最佳功率 (W) |
| activity_id | INTEGER | 来源活动 |
| date | TEXT | 记录日期 |

#### wellness 表 — 每日健康数据

| 字段 | 类型 | 说明 |
|------|------|------|
| date | TEXT PK | 日期 |
| ctl / atl / tsb | REAL | 体能指标 |
| sleep_hours | REAL | 睡眠时长 |
| resting_hr | INTEGER | 静息心率 |
| hrv | REAL | 心率变异性 |
| weight_kg | REAL | 体重 |

#### activity_ai_reviews 表 — AI 活动复盘 (v0.6.0+)

| 字段 | 类型 | 说明 |
|------|------|------|
| activity_id | INTEGER PK | 活动 ID (FK → activities) |
| overall_rating | TEXT | 总评 (excellent/good/fair/poor) |
| key_judgments | TEXT | 3 条关键判断 (JSON array) |
| subsequent_impact | TEXT | 后续影响描述 |
| full_analysis | TEXT | 六段式完整分析 (JSON object) |
| model | TEXT | 生成使用的 LLM 模型 |
| created_at | TEXT | 生成时间 (ISO datetime) |

#### settings 表 — 键值配置

| 字段 | 类型 | 说明 |
|------|------|------|
| key | TEXT PK | 配置键（如 `ftp`、`max_hr`） |
| value | TEXT | 配置值 |

---

### 2. REST API JSON 响应

#### GET /api/activities — 活动列表

```json
{
  "ok": true,
  "count": 5,
  "activities": [
    {
      "id": 18628473920,
      "sport": "cycling",
      "name": "下午骑行",
      "date": "2026-03-15",
      "distance_m": 42350.0,
      "total_timer_s": 5400.0,
      "normalized_power": 218.5,
      "tss": 85.3,
      "intensity_factor": 0.87
    }
  ]
}
```

#### GET /api/analyze/{id} — 完整分析（AI Skill 接口）

```json
{
  "ok": true,
  "activity": {
    "id": 18628473920,
    "name": "下午骑行",
    "sport": "cycling",
    "date": "2026-03-15",
    "distance_km": 42.35,
    "duration_min": 95.0,
    "avg_hr_bpm": 155,
    "avg_power_w": 195,
    "normalized_power_w": 218
  },
  "training_load": {
    "tss": 85.3,
    "intensity_factor": 0.87,
    "ftp_w": 250,
    "w_prime_j": 15200
  },
  "fitness": {
    "ctl": 62.5,
    "atl": 78.3,
    "tsb": -15.8
  },
  "zones": {
    "power_zones": [
      {"zone": 1, "name": "Active Recovery", "min_w": 0, "max_w": 137, "seconds": 600, "pct": 10.5}
    ],
    "hr_zones": [
      {"zone": 1, "name": "Zone 1", "min_bpm": 100, "max_bpm": 130, "seconds": 480, "pct": 8.4}
    ]
  },
  "drift": {
    "method": "power_hr",
    "drift_pct": 3.2,
    "classification": "良好"
  },
  "pdc": {"5": 420, "30": 350, "60": 310, "300": 265, "1200": 240},
  "wellness": null
}
```

#### GET /api/fitness — 体能趋势

```json
{
  "ok": true,
  "count": 90,
  "history": [
    {
      "date": "2026-03-15",
      "ctl": 62.5,
      "atl": 78.3,
      "tsb": -15.8,
      "ramp_rate": 1.2,
      "daily_tss": 85.3
    }
  ]
}
```

#### GET /api/pdc — 功率持续时间曲线

```json
{
  "ok": true,
  "bests": [
    {"duration_s": 5, "power": 850.0, "activity_id": 18628473920, "date": "2026-03-10"},
    {"duration_s": 60, "power": 380.0, "activity_id": 18628473920, "date": "2026-03-10"},
    {"duration_s": 300, "power": 275.0, "activity_id": 18625110523, "date": "2026-03-08"},
    {"duration_s": 1200, "power": 248.0, "activity_id": 18625110523, "date": "2026-03-08"}
  ]
}
```

#### GET /api/decision-summary — 统一决策摘要 (v0.5.0+)

面板页和计划页共用的决策对象，避免文案漂移。

```json
{
  "ok": true,
  "readiness": {
    "level": "green",
    "label": "可执行关键课",
    "confidence": 0.82,
    "confidence_reasons": [
      "HRV 高于 7 日均值",
      "TSB 在 -10 ~ +5 区间",
      "昨日睡眠 7.5h"
    ]
  },
  "weekly_deviation": {
    "status": "on_track",
    "label": "正常",
    "primary_completion_pct": 75,
    "strength_completion_pct": 66,
    "tss_actual": 320,
    "tss_planned": 420
  },
  "anomaly_alerts": [
    {
      "condition": "rhr_elevated",
      "message": "静息心率连续 3 天偏高 (+6 bpm vs 7日均值)",
      "severity": "warning"
    }
  ],
  "phase": "Build",
  "today_suggestion": "今日建议执行计划中的 Z3 节奏骑行"
}
```

#### GET /api/constraint-status — 本周约束满足情况 (v0.5.0+)

```json
{
  "ok": true,
  "constraints": [
    {"rule": "周一休息", "status": "met", "detail": "周一无训练记录"},
    {"rule": "骑行 3-4 次/周", "status": "in_progress", "detail": "已完成 2/3"},
    {"rule": "跑步 ≥1 次/周", "status": "met", "detail": "已完成 1 次"},
    {"rule": "力量 3-4 次/周", "status": "in_progress", "detail": "已完成 2/3"},
    {"rule": "避免连续3天高负荷", "status": "met", "detail": "最长连续 2 天"},
    {"rule": "强度骑后次日不排腿部大重量", "status": "met", "detail": "无冲突"},
    {"rule": "总时长 10-12h/周", "status": "in_progress", "detail": "已 7.5h / 目标 10h"}
  ]
}
```

#### GET /api/activities/{id}/ai-review — AI 活动复盘 (v0.6.0+)

```json
{
  "ok": true,
  "review": {
    "activity_id": 18628473920,
    "overall_rating": "good",
    "key_judgments": [
      "节奏骑行执行质量良好，NP 218W 符合 Z3 目标",
      "心率漂移 3.2% 表明有氧基础扎实",
      "后半段功率略有下降，可能与补给时机有关"
    ],
    "subsequent_impact": "预计产生 85 TSS，明日建议安排恢复骑或休息",
    "full_analysis": {
      "session_type": "Z3 节奏骑行（有氧阈值训练）",
      "execution_quality": "NP 218W / IF 0.87，符合 Build 阶段 Z3 目标...",
      "physiological_cost": "TSS 85.3，心率漂移 3.2%...",
      "capability_signals": "20min best power 持平上周...",
      "anomaly_factors": "无明显异常",
      "recommendations": "明日安排恢复骑 (TSS < 30)..."
    },
    "model": "openai/gpt-5.4",
    "created_at": "2026-03-18T14:30:00"
  }
}
```

#### POST /api/activities/{id}/ai-review/regenerate — 重新生成 AI 复盘 (v0.6.0+)

强制重新调用 LLM 生成复盘，覆盖已有缓存。

```json
{
  "ok": true,
  "review": { "...同 GET /api/activities/{id}/ai-review 的 review 结构..." }
}
```

#### GET /api/activities/{id}/ai-review/summary — AI 复盘摘要 (v0.6.0+)

返回精简版复盘（仅总评 + 关键判断 + 后续影响），用于列表页快速展示。

```json
{
  "ok": true,
  "summary": {
    "activity_id": 18628473920,
    "overall_rating": "good",
    "key_judgments": ["..."],
    "subsequent_impact": "..."
  }
}
```

---

### 3. Web HTML 页面

| 路由 | 说明 | 渲染内容 |
|------|------|----------|
| `/` | 主仪表盘 | 今日决策 → 本周偏差 → 恢复证据(4卡) → 7日训练总览 → 负荷趋势 → 最近活动 |
| `/activity/{id}` | 活动详情 | AI 复盘摘要 + 功率/心率时间序列 + 区间分布 + 圈速 |
| `/plan` | AI 训练计划 | 本周结论条 + 周课表 + 约束清单 + AI 决策依据 |
| `/body-data` | 身体数据 | 身体组成状态卡 + 趋势图表 + 数据来源表 |

页面使用 Jinja2 模板渲染，Chart.js 绑定图表数据，全部为中文界面。

---

## 数据流图

```
┌─────────────┐     ┌──────────────────┐     ┌────────────────┐
│ Garmin Watch │────►│  Garmin Connect   │────►│  FIT 文件下载   │
│  (设备端)    │     │  (云端同步)       │     │  (garminconnect) │
└─────────────┘     └──────────────────┘     └───────┬────────┘
                                                      │
                                                      ▼
                                              ┌────────────────┐
                                              │  FIT 解析       │
                                              │  (fitparse)     │
                                              └───────┬────────┘
                                                      │
                                                      ▼
                                              ┌────────────────┐
                    ┌─────────────────┐       │  指标计算       │
                    │ Intervals.icu   │◄─────►│  (metrics.py)   │
                    │ (交叉校验)      │       │  NP/TSS/IF/...  │
                    └─────────────────┘       └───────┬────────┘
                                                      │
                                                      ▼
                                              ┌────────────────┐
                                              │  SQLite 存储    │
                                              │  (database.py)  │
                                              └───────┬────────┘
                                                      │
                                       ┌──────────────┼──────────────┐
                                       ▼              ▼              ▼
                               ┌──────────┐   ┌──────────┐   ┌──────────┐
                               │ REST API │   │ Web 仪表盘│   │ CLI 工具 │
                               │ (JSON)   │   │ (HTML)    │   │ (终端)   │
                               └──────────┘   └──────────┘   └──────────┘
                                    │              │
                                    ▼              ▼
                              AI Skill 调用    浏览器展示
```
