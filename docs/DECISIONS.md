# 技术决策记录

## D-007 | 2026-03-18 | 应用层密码门 vs Cloudflare Access

**背景**: Cloudflare Tunnel 暴露到公网后需要访问控制

**选项**:
1. Cloudflare Access (Zero Trust) — 邮箱 OTP 验证
2. 应用层密码门 (TRAININGEDGE_PASSWORD 环境变量)
3. HTTP Basic Auth

**决策**: 选项 2 — 应用层密码门

**理由**:
- 不依赖 Cloudflare API token（用户未提供）
- 实现简单，session cookie + HMAC 签名
- 可通过环境变量配置，不修改代码
- 后续可叠加 Cloudflare Access 作为双层防护

**风险**: 无 brute force 保护（Cloudflare 自带基础 DDoS 防护）

---

## D-006 | 2026-03-18 | Cloudflare Tunnel vs Tailscale 远程访问

**背景**: 公司访问家里 NAS 延迟 5 秒+

**选项**:
1. Tailscale 优化（自建 DERP 中继）
2. Cloudflare Tunnel
3. FRP / NPS 内网穿透

**决策**: 选项 2 — Cloudflare Tunnel

**理由**:
- 免费，零端口暴露
- 用户已有 <your-domain> 域名在 Cloudflare
- 香港节点 (hkg) 延迟 ~360ms，远好于 Tailscale relay 5000ms
- 自动 HTTPS
- Tailscale 保留用于 SSH 管理

---

## D-005 | 2026-03-17 | 三 AI 协作架构设计

**背景**: 训练计划安全架构需要跨模型验证

**决策**: Gemini Pro 设计框架 → GPT-5.4 Thinking 交叉验证 → Claude Opus 4.6 实现

**理由**: 三个模型各有所长，交叉验证减少单模型盲点

---

## D-004 | 2026-03-17 | OpenRouter vs 直连各家 API

**背景**: 需要调用多种 LLM 模型

**决策**: 统一走 OpenRouter API

**理由**:
- 一个 API key 访问所有模型
- OpenAI SDK 兼容，切换模型只改 model 参数
- 支持代理配置（国内访问）
- 按需付费，无最低消费

---

## D-003 | 2026-03-17 | AI 输出安全: PostCheck 规则 vs AI 自约束

**背景**: LLM 生成的训练计划可能不合理（TSS 过高、无休息日等）

**决策**: 本地 PostCheck 规则校验，不依赖 AI 自约束

**理由**: "先建护栏，再装发动机" — LLM 是受限规划器，不是决策中心

---

## D-002 | 2026-03-17 | SQLite vs PostgreSQL

**决策**: SQLite (WAL 模式)

**理由**: 单用户、NAS 部署、无并发写入需求、零运维

---

## D-001 | 2026-03-16 | 自建 vs 使用 TrainingPeaks/Intervals.icu

**决策**: 自建

**理由**: 完全控制数据、可集成 AI、学习系统设计、数据不离家
