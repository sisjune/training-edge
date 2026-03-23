<p align="center">
  <h1 align="center">TrainingEdge</h1>
  <p align="center">
    自托管运动数据分析引擎 — 让训练决策有据可依
    <br />
    <a href="#快速开始">快速开始</a> · <a href="#功能特性">功能特性</a> · <a href="#api-参考">API 参考</a>
  </p>
</p>

> 🤖 本项目由 [Claude Code](https://claude.ai/claude-code) 辅助开发，包括核心引擎、Web 仪表盘、部署脚本和本文档。

---

## 这是什么？

TrainingEdge 是一个**完全自托管**的运动训练分析平台。它从 Garmin 手表同步数据，计算专业训练指标，并通过 AI 生成训练计划和骑行复盘。

**所有数据留在你自己的机器上。** 没有云服务，没有订阅，没有第三方拿走你的训练数据。

### 核心能力

- 🔄 **Garmin 自动同步** — 活动、睡眠、HRV、静息心率、Body Battery
- 📊 **专业指标计算** — NP / TSS / IF / CTL / ATL / TSB / PDC / eFTP / W'
- 🤖 **AI 训练计划** — 基于你的体能状态和约束条件自动生成周计划
- 📋 **计划执行追踪** — 自动匹配实际训练与计划（支持换天做）
- 🏥 **每日准备度评估** — 综合 HRV、睡眠、TSB 判断今天能不能练
- 📈 **中文 Web 仪表盘** — 深色主题，结论优先，移动端适配

### 设计理念

**"结论 → 证据 → 动作"** — 每个页面先告诉你该做什么，再展示为什么，最后给操作入口。

---

## 功能特性

### 仪表盘

| 页面 | 内容 |
|------|------|
| 主面板 | 今日准备度、本周训练总览、负荷趋势图、异常警报 |
| 活动详情 | AI 骑行复盘、功率/心率时间序列、区间分布、圈速分析 |
| 训练计划 | AI 周计划、约束满足清单、计划 vs 实际对比 |
| 身体数据 | 健康趋势（HRV/睡眠/心率）、体成分记录（InBody） |

### 计算指标

| 指标 | 说明 |
|------|------|
| NP / TSS / IF | 标准化功率、训练压力、强度因子 |
| CTL / ATL / TSB | 体能 / 疲劳 / 状态平衡 |
| PDC / eFTP / W' | 功率曲线、估算 FTP、无氧做功能力 |
| xPower / TRIMP | 指数加权功率、心率训练冲量 |
| HR Drift / VDOT | 心率漂移、跑步能力指数 |

---

## 快速开始

### 环境要求

- Python 3.10+ 或 Docker
- Garmin 手表 + Garmin Connect 账号

### Docker 部署

```bash
git clone https://github.com/sisjune/training-edge.git
cd training-edge
cp .env.example .env   # 编辑填入你的参数
docker compose up -d
```

访问 `http://localhost:8420`

### 本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
python scripts/cli.py init
python scripts/cli.py sync --days 7
python scripts/cli.py serve --reload --port 8420
```

### 配置

复制 `.env.example` 为 `.env`，核心参数：

| 变量 | 说明 |
|------|------|
| `TRAININGEDGE_FTP` | 你的 FTP (W) |
| `TRAININGEDGE_MAX_HR` | 最大心率 (bpm) |
| `TRAININGEDGE_RESTING_HR` | 静息心率 (bpm) |
| `TRAININGEDGE_PASSWORD` | Web 访问密码（可选） |
| `GARMINTOKENS` | Garmin OAuth token 目录 |
| `OPENROUTER_API_KEY` | AI 功能所需（可在 Web 设置页配置） |

完整变量列表见 [.env.example](.env.example)。

---

## 架构

```
Garmin Watch → Garmin Connect → garminconnect API
                                       │
                                       ▼
                              FIT 解析 (fitparse)
                                       │
                                       ▼
                             指标计算 (engine/metrics.py)
                                       │
                                       ▼
                             SQLite (/data/training_edge.db)
                                       │
                          ┌────────────┼────────────┐
                          ▼            ▼            ▼
                     REST API    AI 计划生成    Web 仪表盘
                     (FastAPI)   (OpenRouter)   (Jinja2)
```

### 技术栈

Python 3.13 · FastAPI · SQLite (WAL) · Jinja2 · Chart.js · fitparse · garminconnect · Docker

---

## API 参考

### 活动

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/activities` | GET | 活动列表 |
| `/api/activity/{id}` | GET | 活动详情（含计算指标） |
| `/api/activities/{id}/ai-review` | GET | AI 活动复盘 |

### 体能与健康

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/fitness` | GET | CTL/ATL/TSB 历史 |
| `/api/pdc` | GET | 功率持续时间曲线 |
| `/api/wellness` | GET | HRV/睡眠/静息心率 |
| `/api/decision-summary` | GET | 今日准备度 |

### 训练计划

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/plan/generate` | POST | AI 生成周训练计划 |
| `/api/plan/workouts` | GET | 当前计划训练列表 |
| `/api/constraint-status` | GET | 约束满足情况 |

### 同步与设置

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/sync` | POST | 触发 Garmin 数据同步 |
| `/api/settings` | GET/POST | 读取/更新设置 |
| `/api/health` | GET | 健康检查 |

---

## 项目结构

```
training-edge/
├── engine/              # 核心计算引擎
│   ├── metrics.py       # NP/TSS/IF/CTL/ATL/TSB/PDC 计算
│   ├── database.py      # SQLite 数据层
│   ├── sync.py          # Garmin 数据同步
│   ├── readiness.py     # 每日准备度评估
│   ├── plan_generator.py # AI 训练计划生成
│   └── fit_parser.py    # FIT 文件解析
├── api/app.py           # FastAPI 应用
├── web/templates/       # Jinja2 中文页面模板
├── scripts/cli.py       # CLI 工具
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

---

## License

[MIT](LICENSE)
