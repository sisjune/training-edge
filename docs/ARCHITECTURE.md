# TrainingEdge — 系统架构

## 架构概览

```
┌─────────────────────────────────────────────────┐
│                  用户浏览器                        │
│         training-edge.<your-domain> (HTTPS)             │
└──────────────────────┬──────────────────────────┘
                       │
              ┌────────▼────────┐
              │  Cloudflare CDN  │  ← 自动 HTTPS, DDoS 防护
              │  (HKG 节点)      │
              └────────┬────────┘
                       │ HTTP/2 Tunnel
              ┌────────▼────────┐
              │   cloudflared    │  ← NAS 上的 tunnel daemon
              │  (主动外连)       │
              └────────┬────────┘
                       │ localhost:8420
    ┌──────────────────▼──────────────────────┐
    │            FastAPI (uvicorn)             │
    │  ┌──────────┐  ┌──────────┐  ┌────────┐ │
    │  │ 密码门    │  │ Web 页面  │  │ REST   │ │
    │  │ Middleware│  │ (Jinja2) │  │ API    │ │
    │  └──────────┘  └──────────┘  └────────┘ │
    └──────────────────┬──────────────────────┘
                       │
    ┌──────────────────▼──────────────────────┐
    │              Engine 核心                   │
    │  ┌────────────────────────────────────┐  │
    │  │   plan_generator.py (4层安全架构)    │  │
    │  │   L0: Phase State Machine          │  │
    │  │   L1: Trigger Engine (P1-P5)       │  │
    │  │   L2: Fallback Templates           │  │
    │  │   L3: PostCheck (CK01-CK09)        │  │
    │  └────────────────────────────────────┘  │
    │  ┌──────────┐  ┌──────────┐  ┌────────┐ │
    │  ┌──────────┐  ┌──────────┐  ┌────────┐ │
    │  │readiness │  │ ai_review│  │decision│ │
    │  │就绪度判定 │  │AI活动复盘 │  │决策摘要 │ │
    │  └──────────┘  └──────────┘  └────────┘ │
    │  ┌──────────┐  ┌──────────┐  ┌────────┐ │
    │  │ metrics  │  │ database │  │ config │ │
    │  │ 指标计算  │  │ SQLite   │  │ 配置   │ │
    │  └──────────┘  └──────────┘  └────────┘ │
    └──────────────────┬──────────────────────┘
                       │
    ┌──────────────────▼──────────────────────┐
    │          外部服务                          │
    │  ┌──────────┐  ┌──────────────────────┐ │
    │  │  Garmin   │  │  OpenRouter API      │ │
    │  │  Connect  │  │  (GPT-5.4 默认)      │ │
    │  └──────────┘  └──────────────────────┘ │
    └─────────────────────────────────────────┘
```

## AI 训练计划 — 4 层安全架构

核心原则: **LLM 是受限规划器，不是决策中心。**

```
Phase Detection → Trigger Arbitration → AI Generate (or Fallback) → PostCheck → DB
```

### Layer 0: Training Phase State Machine

自动判定训练周期阶段，决定负荷包络:

| 阶段 | 周TSS倍率 | 高强度天数 | 单日TSS上限 | 最少休息日 |
|------|-----------|-----------|------------|-----------|
| Base | 1.0x | 1 | 120 | 1 |
| Build | 1.1x | 2 | 150 | 1 |
| Peak | 0.85x | 2 | 130 | 2 |
| Recovery | 0.5x | 0 | 60 | 3 |
| Transition | 0.4x | 0 | 50 | 3 |

判定优先级: 用户手动设置 > 赛事距离 > CTL趋势 + TSB

### Layer 1: Trigger Engine

优先级矩阵 (P1 > P2 > P3 > P4 > P5):

| 优先级 | 触发器 | 动作 |
|--------|--------|------|
| P1 | 用户标记疾病/差旅 | LOCAL_OVERRIDE → 恢复课表 |
| P2 | TSB < -30 / HRV+RHR 双异常 | LOCAL_OVERRIDE → 恢复课表 |
| P3 | 环境应激 (待接入天气数据) | 标记但不触发 |
| P4 | 执行偏差 > 30% | REDUCE_LOAD |
| P5 | eFTP 跃迁 (待实现) | NO_ACTION |

Cooldown: 6 小时最短间隔

### Layer 2: Fallback Templates

AI 故障或红线触发时的本地安全课表:
- `RECOVERY_WEEK`: 3 天休息 + 轻量恢复
- `AI_FAILURE`: 均衡基础课表

### Layer 3: PostCheck (9 条规则)

| 规则 | 检查内容 |
|------|---------|
| CK01 | TSS 硬上限 (sport + phase 取较小值) |
| CK02 | 时长上限 |
| CK03 | IF 隐含校验 (> 1.15 不合理) |
| CK04 | 休息日清零 |
| CK05 | 负数拦截 |
| CK06 | 周 TSS 总量 cap |
| CK07 | 连续高强度天数 (≥ 3天 TSS>60 降负荷) |
| CK08 | 高强度天数限制 (按阶段) |
| CK09 | 最少休息日检查 |

## 数据流

### Garmin 同步
```
Garmin Connect → garminconnect SDK → FIT 文件 → fitparse → metrics 计算 → SQLite
```

### 训练计划生成
```
用户点击生成 → gather_context() → detect_phase() → evaluate_triggers()
  → [P1/P2] → fallback_plan → postcheck → save_plan
  → [正常] → build_prompt → OpenRouter API → extract_json → postcheck → save_plan
  → [AI失败] → fallback_plan(AI_FAILURE) → postcheck → save_plan
```

### 决策摘要与约束状态
```
页面加载 → /api/decision-summary → 统一决策对象 (面板+计划页共用)
        → /api/constraint-status → 7 条约束满足情况
```

### AI 活动复盘
```
活动页加载 → GET /api/activities/{id}/ai-review → 返回缓存结果或空
用户点击生成 → POST /api/activities/{id}/ai-review/regenerate → LLM 生成 → 存储 → 返回
摘要视图 → GET /api/activities/{id}/ai-review/summary → 精简版结果
```

## 页面架构: "结论→证据→动作" 三层模型 (v0.5.0+)

所有页面统一按三层组织:

```
┌─────────────────────────────────────────┐
│  结论层 (Conclusion)                      │
│  · 今日训练建议 / 本周状态 / 身体状态     │
│  · 一句话可执行结论，优先级最高           │
├─────────────────────────────────────────┤
│  证据层 (Evidence)                        │
│  · 指标卡片 / 趋势图表 / 约束清单        │
│  · 支撑结论的数据，默认折叠非核心部分     │
├─────────────────────────────────────────┤
│  动作层 (Action)                          │
│  · 生成计划 / 重新分析 / 跳转详情        │
│  · 用户可执行的操作                       │
└─────────────────────────────────────────┘
```

页面实例:
- **面板页** (`/`): 今日决策 → 本周偏差 + 恢复证据(4卡) + 7日训练总览 + 负荷趋势 → 最近活动
- **训练计划页** (`/plan`): 本周结论条 → 周课表 + 约束清单 + AI 依据 → 生成/调整操作
- **身体数据页** (`/body-data`): 身体组成状态卡 → 趋势图表 + 数据来源表 → 时间范围切换
- **活动详情页** (`/activity/{id}`): AI 复盘摘要 → 功率/心率/区间数据 → 重新分析

## 异常复核系统 (v0.5.0+)

5 个触发条件，触发时显示人工复核提示而非强结论:

| 条件 | 触发规则 | 显示行为 |
|------|---------|---------|
| RHR 连续偏高 | 静息心率连续 3 天高于 7 日均值 5+ bpm | ⚠️ 警告卡 + 原因 |
| HRV 连续下降 | HRV 连续 3 天下降 | ⚠️ 警告卡 + 原因 |
| 睡眠连续不足 | 连续 3 天睡眠 < 6 小时 | ⚠️ 警告卡 + 原因 |
| 用户标记伤病 | 用户手动标记 | ⚠️ 警告卡 + 原因 |
| 数据缺失 | 关键指标连续 3+ 天无数据 | ⚠️ 降低置信度 |

置信度使用 `confidence_reasons` 数组（多因子），不再是单一原因字符串。

## AI 活动复盘 (v0.6.0+)

```
用户查看活动详情 → 检查 activity_ai_reviews 表
  → [有缓存] → 直接展示摘要卡 + 可展开完整分析
  → [无缓存] → 显示空状态 + "生成 AI 复盘" 按钮
  → [点击生成] → gather_activity_context() → build_review_prompt()
    → OpenRouter API → parse_review_json() → 存入 activity_ai_reviews
    → 返回六段式分析结果
```

六段式分析: 类型识别 / 执行质量 / 生理成本 / 能力信号 / 异常因素 / 后续建议

## 数据库 Schema (SQLite)

主要表:
- `activities` — Garmin 同步的活动数据
- `fitness_history` — CTL/ATL/TSB 每日历史
- `planned_workouts` — AI 生成的训练计划
- `activity_ai_reviews` — AI 活动复盘结果 (v0.6.0+)
- `body_composition` — InBody 体成分数据
- `wellness` — Garmin 健康数据 (HRV, 睡眠, RHR)
- `settings` — KV 配置存储 (API key, 运动员档案, 训练阶段等)

### activity_ai_reviews 表 (v0.6.0+)

| 字段 | 类型 | 说明 |
|------|------|------|
| activity_id | INTEGER PK | 活动 ID (FK → activities) |
| overall_rating | TEXT | 总评 (excellent/good/fair/poor) |
| key_judgments | TEXT (JSON) | 3 条关键判断 |
| subsequent_impact | TEXT | 后续影响描述 |
| full_analysis | TEXT (JSON) | 六段式完整分析 |
| model | TEXT | 生成使用的 LLM 模型 |
| created_at | TEXT | 生成时间 |

## 部署架构

```
Synology DS923+ (AMD Ryzen R1600)
├── Docker (ContainerManager)
│   └── training-edge:latest
│       ├── Port: 8420
│       ├── Volume: /volume1/docker/training-edge/data → /data
│       └── Env: TRAININGEDGE_PASSWORD, TZ=Asia/Shanghai, ...
├── cloudflared (用户态进程)
│   ├── Tunnel: training-edge → training-edge.<your-domain>
│   ├── Protocol: HTTP/2
│   └── Auto-start: /usr/local/etc/rc.d/cloudflared.sh
└── tailscaled (用户态, 备用)
    └── IP: <TAILSCALE_IP> (性能差, 仅 SSH 用)
```
