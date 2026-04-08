# TrainingEdge — Security Checklist

上次审查: 2026-04-04 (v0.7.0)
审查人: Claude Opus 4.6

## 资产面

| 资产 | 位置 | 敏感度 |
|------|------|--------|
| SQLite 数据库 | /data/training_edge.db | 高 — 含训练数据、体能数据、设置 |
| Garmin tokens | /data/tokens/ | 高 — OAuth 凭证 |
| OpenRouter API Key | DB settings 表 | 高 — 付费 API 凭证 |
| FIT 文件 | /data/fit_files/ | 中 — 含 GPS 轨迹 |
| 应用日志 | /data/training_edge.log | 低 |

## 暴露面

| 接口 | 暴露方式 | 认证 | 状态 |
|------|---------|------|------|
| Web UI (8420) | Cloudflare Tunnel | ✅ 密码门 (TRAININGEDGE_PASSWORD) | 已加固 |
| /api/health | 公开 | 无需认证 (设计如此) | ✅ OK — 不暴露敏感信息 |
| /api/* | Cloudflare Tunnel | API Key (X-API-Key) + 密码门 | ✅ 双层保护 |
| Docker 端口 8420 | 仅 Tailscale 内网 | 无认证 (内网) | ⚠️ 可接受 — 内网信任 |

## 身份认证

- [x] Web 访问需要密码 (TRAININGEDGE_PASSWORD 环境变量)
- [x] Session cookie: httponly + secure(动态) + samesite=lax + 30天过期
- [x] Cookie secure 属性根据请求协议动态设置
- [x] Session token: HMAC-SHA256 签名
- [x] API 调用需要 X-API-Key header
- [ ] 未实现: 登录失败次数限制 — **低优先级** (Cloudflare 缓解)

## 权限控制

- [x] 单用户系统，无需 RBAC
- [x] 写操作 (POST/DELETE) 需要 API key 或登录态
- [x] /api/health 是唯一无需认证的端点

## 密钥与配置

- [x] OpenRouter API Key 存储在 DB settings 表，设置页显示掩码
- [x] TRAININGEDGE_PASSWORD 通过环境变量传入，不在代码中
- [x] TRAININGEDGE_SESSION_SECRET 通过环境变量传入
- [x] Garmin tokens 存储在 /data/tokens/ 目录
- [x] .gitignore 包含 state/, *.log, .env
- [x] .env.example 文件已提供

## 输入校验

- [x] LLM 输出经过 PostCheck 9 条规则校验 (CK01-CK09)
- [x] TSS/时长/IF 硬上限钳位
- [x] JSON 提取有容错 (code block / raw / 深度搜索)
- [ ] API 输入缺少 schema validation — **低风险** (单用户)

## AI 复盘安全 (v0.6.0+)

- [x] AI 复盘 LLM prompt 不含 API key、密码等机密信息
- [x] `activity_ai_reviews` 表不存储 PII
- [x] 新增 API 均在 AccessGateMiddleware 之后
- [x] v0.7.0: AI 复盘运动类型感知 — 跑步/骑行使用独立 prompt，不会混用分析框架

## 数据源安全 (v0.7.0)

- [x] Garmin Training Load 从 Garmin Connect API 获取，走 HTTPS
- [x] Garmin OAuth token 不在代码库中
- [x] Intervals.icu API Key 仅存储在 DB settings 表
- [x] FIT 文件仅存储在 /data/ 卷中，不进 git

## 页面免责声明

- [x] 面板页 (`/`) 底部显示免责声明
- [x] 训练计划页 (`/plan`) 底部显示免责声明
- [x] 身体数据页 (`/body-data`) 底部显示免责声明

## 日志与审计

- [x] 结构化日志 (logging module)
- [x] LLM 请求日志: model, base, message count
- [x] API key 不会出现在日志中

## 测试与 CI (v0.7.0)

- [x] 42 个单元测试 (metrics + database)
- [x] GitHub Actions CI: lint (ruff) + test (pytest) on push/PR
- [x] ruff linting 配置 (pycodestyle, pyflakes, isort, bugbear)
- [ ] 缺少: API 集成测试 — **TODO**
- [ ] 缺少: 依赖漏洞扫描 (pip-audit) — **TODO**

## 依赖与镜像

- [x] 基于 python:3.13-slim 官方镜像
- [x] 依赖声明在 pyproject.toml (版本范围)
- [ ] 缺少: 依赖锁定文件 (requirements.lock) — **TODO**

## 部署加固

- [x] Docker --restart unless-stopped
- [x] HEALTHCHECK 配置
- [x] Cloudflare Tunnel (不暴露公网端口)
- [x] Cloudflare 自动 HTTPS
- [ ] Docker 容器以 root 运行 — **低风险** (内网信任)
- [ ] 日志无轮转 — **低优先级**

## 已知风险

| 风险 | 严重度 | 缓解措施 | 状态 |
|------|--------|---------|------|
| 登录无 brute force 保护 | 低 | Cloudflare DDoS + 内网 | 可接受 |
| Docker 以 root 运行 | 低 | Tailscale 内网 | 可接受 |
| 无数据库定期备份 | 中 | Garmin 可重新同步 | TODO |
| 无依赖锁定/漏洞扫描 | 中 | 版本范围约束 | TODO |
