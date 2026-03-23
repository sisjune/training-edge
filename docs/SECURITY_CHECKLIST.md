# TrainingEdge — Security Checklist

上次审查: 2026-03-18
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
| Web UI (8420) | Cloudflare Tunnel → training-edge.<your-domain> | ✅ 密码门 (TRAININGEDGE_PASSWORD) | 已加固 |
| SSH (2222) | Tailscale <TAILSCALE_IP> + IP 白名单 | ✅ SSH key | 已加固 |
| /api/health | 公开 | 无需认证 (设计如此) | ✅ OK — 不暴露敏感信息 |
| /api/* | Cloudflare Tunnel | API Key (X-API-Key) + 密码门 | ✅ 双层保护 |
| Docker 端口 8420 | 仅 NAS 内网 <NAS_IP>:8420 | 无认证 (内网) | ⚠️ 可接受 — 内网信任 |

## 身份认证

- [x] Web 访问需要密码 (TRAININGEDGE_PASSWORD 环境变量)
- [x] Session cookie: httponly + secure(动态) + samesite=lax + 30天过期
- [x] Cookie secure 属性根据请求协议动态设置 — HTTPS 时 secure=True，HTTP 时 secure=False (v0.4.0 修复)
- [x] Session token: HMAC-SHA256 签名
- [x] API 调用需要 X-API-Key header
- [ ] 未实现: 登录失败次数限制 (brute force protection) — **TODO**
- [ ] 未实现: 密码复杂度要求 — **低优先级**

## 权限控制

- [x] 单用户系统，无需 RBAC
- [x] 写操作 (POST/DELETE) 需要 API key 或登录态
- [x] /api/health 是唯一无需认证的端点

## 密钥与配置

- [x] OpenRouter API Key 存储在 DB settings 表，设置页显示掩码 (****)
- [x] TRAININGEDGE_PASSWORD 通过环境变量传入，不在代码中
- [x] TRAININGEDGE_SESSION_SECRET 通过环境变量传入
- [x] Garmin tokens 存储在 /data/tokens/ 目录
- [x] .gitignore 包含 state/, *.log, .env
- [ ] 缺少: .env.example 文件 — **TODO**

## 输入校验

- [x] LLM 输出经过 PostCheck 9 条规则校验 (CK01-CK09)
- [x] TSS/时长/IF 硬上限钳位
- [x] 肌群名称标准化 (别名映射)
- [x] JSON 提取有容错 (code block / raw / 深度搜索)
- [ ] Web 表单输入未做严格校验 — **低风险** (单用户)
- [ ] API 输入缺少 schema validation — **TODO: 加 Pydantic models**

## AI 复盘安全 (v0.6.0+)

- [x] AI 复盘 LLM prompt 不含 API key、密码等机密信息 — 仅传递活动指标和训练上下文
- [x] `activity_ai_reviews` 表不存储 PII — 仅存储分析结论、评分、判断
- [x] 新增 API (`/api/activities/{id}/ai-review`, `/regenerate`, `/summary`) 均在 AccessGateMiddleware 之后，需要登录态
- [x] `/api/decision-summary` 和 `/api/constraint-status` 为内部页面调用，受密码门保护
- [x] 异常复核提示不做医疗诊断声明 — 仅提示"建议人工复核"

## 页面免责声明 (v0.5.0+)

- [x] 面板页 (`/`) 底部显示免责声明
- [x] 训练计划页 (`/plan`) 底部显示免责声明
- [x] 身体数据页 (`/body-data`) 底部显示免责声明
- [x] 声明内容明确"不替代医疗诊断"，不做医疗建议

## 日志与审计

- [x] 结构化日志 (logging module)
- [x] 训练计划生成记录: phase, trigger, count, timestamp
- [x] LLM 请求日志: model, base, proxy, message count
- [x] PostCheck 警告日志
- [x] API key 不会出现在日志中 (Authorization header 不记录)

## 依赖与镜像

- [x] 基于 python:3.13-slim 官方镜像
- [x] 依赖锁定在 pyproject.toml (最低版本)
- [ ] 缺少: 依赖漏洞扫描 — **TODO: 加 pip-audit 或 safety**
- [ ] 缺少: Docker 镜像扫描 — **低优先级**

## 部署加固

- [x] Docker --restart unless-stopped
- [x] HEALTHCHECK 配置
- [x] Cloudflare Tunnel (不暴露公网端口)
- [x] Cloudflare 自动 HTTPS
- [x] cloudflared 使用 HTTP/2 协议 (QUIC 在 NAS 上不稳定)
- [x] cloudflared 开机自启 (/usr/local/etc/rc.d/cloudflared.sh)
- [ ] 缺少: Docker 容器以非 root 用户运行 — **TODO**
- [ ] 缺少: 日志轮转配置 — **TODO**

## 回滚与备份

- [x] SQLite 数据在 /volume1/docker/training-edge/data/ (独立于容器)
- [x] Docker 镜像可回滚到之前版本
- [ ] 缺少: 定期数据库备份脚本 — **TODO**
- [ ] 缺少: FIT 文件备份策略 — **低优先级**

## 已知风险

| 风险 | 严重度 | 缓解措施 | 状态 |
|------|--------|---------|------|
| 登录无 brute force 保护 | 中 | Cloudflare 有基础 DDoS 防护; 可加 rate limit | TODO |
| Docker 以 root 运行 | 低 | NAS 内网环境; 可加 USER 指令 | TODO |
| 无数据库备份 | 中 | 数据可从 Garmin 重新同步; 计划数据不可恢复 | TODO |
| 日志无轮转 | 低 | 磁盘空间充足; 可加 logrotate | TODO |
