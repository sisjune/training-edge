# Bug 修复记录

## BF-008 | 2026-03-18 | 内网 HTTP 访问无法登录

- **现象**: 内网 HTTP 访问 (http://<NAS_IP>:8420) 无法登录，输入正确密码后仍跳转回登录页
- **根因**: Cookie `secure=True` 硬编码，HTTP 协议下浏览器拒绝保存带 `secure` 标记的 cookie
- **影响范围**: 所有 HTTP 访问场景（内网直连、Tailscale HTTP）
- **修复**: 根据请求协议动态设置 `secure` 属性 — HTTPS 请求时 `secure=True`，HTTP 请求时 `secure=False`
- **回归风险**: 低。HTTPS (Cloudflare Tunnel) 场景不受影响，仍保持 secure=True
- **验证**: 内网 HTTP 和外网 HTTPS 均可正常登录

## BF-007 | 2026-03-18 | Cloudflare Tunnel 无认证暴露

- **现象**: training-edge.<your-domain> 公开可访问，任何人输入域名即可看到全部训练数据
- **根因**: 创建 Cloudflare Tunnel 时未同步添加访问控制，直接给了用户公网 URL
- **影响范围**: 全站所有页面和 API（约 10 分钟窗口）
- **修复**: 添加 AccessGateMiddleware — 基于 TRAININGEDGE_PASSWORD 环境变量的密码门，session cookie 认证
- **回归风险**: 低。/api/health 仍公开（设计如此），其余路径均需登录
- **验证**: 5 项 curl 测试通过（未认证 302 跳转、健康检查公开、登录流程、cookie 验证）
- **教训**: **公网服务 = 先加认证，后给链接。这不是可选项，是前置条件。**

## BF-006 | 2026-03-17 | Cloudflare Tunnel QUIC 协议连接失败

- **现象**: cloudflared 默认 QUIC 协议在 NAS 上持续 timeout
- **根因**: Synology DSM 的 UDP buffer size 过小（208KB vs 需要 7168KB），QUIC 无法建立连接
- **影响范围**: Cloudflare Tunnel 无法启动
- **修复**: config.yml 添加 `protocol: http2` 强制使用 HTTP/2
- **回归风险**: 无。HTTP/2 是完全支持的备选协议
- **验证**: 4 条 tunnel connection 成功注册到 hkg 节点

## BF-005 | 2026-03-17 | upsert_setting 函数不存在

- **现象**: AI 计划生成时 _record_generation() 报 AttributeError
- **根因**: 代码调用 database.upsert_setting() 但实际函数名是 database.set_setting()
- **影响范围**: 计划生成成功但生成记录未保存
- **修复**: 改为 database.set_setting()
- **回归风险**: 无
- **验证**: 语法检查通过

## BF-004 | 2026-03-17 | python-multipart 缺失导致容器启动崩溃

- **现象**: Docker 容器启动后，表单提交报 RuntimeError: Form data requires "python-multipart"
- **根因**: pyproject.toml 未包含 python-multipart 依赖
- **影响范围**: 所有表单提交功能（设置页、目标设定）
- **修复**: pyproject.toml 添加 `python-multipart>=0.0.6`
- **回归风险**: 无
- **验证**: 容器重建后表单正常工作

## BF-003 | 2026-03-17 | 肌肉疲劳全部显示 0%

- **现象**: 肌肉疲劳热力图所有肌群都显示 0%
- **根因**: 查询只看当天数据，但疲劳需要 7 天回溯
- **影响范围**: 肌肉疲劳可视化
- **修复**: 改为 7 天回溯查询
- **回归风险**: 无
- **验证**: 页面显示正常疲劳值

## BF-002 | 2026-03-17 | 训练计划重复堆积

- **现象**: 多次生成计划后，同一天出现多个训练
- **根因**: 生成新计划前未清除目标周的旧计划
- **影响范围**: 训练计划页面
- **修复**: save_plan() 先 DELETE 目标周范围内的旧计划
- **回归风险**: 低。用户手动添加的训练也会被清除
- **验证**: 重新生成后只有新计划

## BF-001 | 2026-03-17 | 设置页 API Key 被掩码覆盖

- **现象**: 保存设置时，已配置的 API Key 被 "****" 覆盖
- **根因**: 前端回传掩码字符串，后端未检测
- **影响范围**: LLM API Key 配置
- **修复**: 保存前检查值是否包含 `*` 字符，包含则跳过
- **回归风险**: 无
- **验证**: 保存后 API Key 保持不变
