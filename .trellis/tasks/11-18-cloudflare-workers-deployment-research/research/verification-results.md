# Workers 实现验证记录

- **日期**: 2026-11-19
- **部署**: `https://llm-proxy-gateway.lantianhaian.workers.dev`
- **Cloudflare 资源**: D1 `llm-proxy-gateway`（APAC/read replication auto）、R2 `llm-proxy-gateway`
- **账号与环境**: 使用独立测试资源；未读取或上传现有本地 SQLite、OAuth 文件、Agent 配置和生产凭据。

## 已通过

| 项目 | 结果 |
|---|---|
| Python Worker 构建与部署 | `cf`/Workers Runtime SDK 构建成功；在线启动 |
| Flask 管理页面与 WSGI | `/login` 返回 Vue 页面，浏览器无控制台错误 |
| 管理员初始化/登录/会话 | WebCrypto PBKDF2 + D1 Session 成功 |
| Provider CRUD/复制/启停/导入导出接口 | 通过 |
| Auth Group CRUD 和运行态列表 | 通过 |
| 用户/API Key/模型映射 CRUD | 通过 |
| 设置读取/分组保存/功能开关 | 通过 |
| Chat 非流式 | 通过 |
| Chat SSE | 通过，终止事件与 usage 完整 |
| Responses 非流式/SSE | 通过 |
| Claude Messages 非流式/SSE | 通过 |
| responses compact | 通过（OAuth 路径） |
| Images generation | 通过 |
| Images edits multipart | 通过 |
| Images 流式 partial/completed | 通过 |
| API Key 未认证拒绝 | 401 |
| API Key 分钟配额 | 第 1 次 200，后续 429 |
| 模型映射故障切换 | 首选不可达目标失败后，备用目标 200 |
| 模型目录 `/v1/models` | 200，含 provider/source/target 字段 |
| D1 请求日志与日统计 | 写入并可查询 |
| R2 LLM 记录 | 写入对象并可读取，包含 request/response/usage |
| 统计 Excel 导出 | 生成合法 XLSX |
| 管理页逐页浏览器检查 | Provider、OAuth、模型映射、API Key、用户、统计、设置，无失败 API/JS 错误 |
| 静态资源缓存 | hash JS 返回 `max-age=31536000, immutable` |
| 后端回归 | 932 个 unittest 通过 |
| 前端检查 | lint、typecheck、178 个单元测试通过 |

## 临时验证环境约束

- `workers.dev` Worker 回环访问另一个 Cloudflare 托管 Worker 会返回 `1042`；协议数据面验证使用同 Worker Service Binding 的合成上游完成。
- 最终部署已移除合成上游、测试 Provider、OAuth 账号、API Key、用户和映射；D1 仅保留管理员设置。
- 最终管理员密码未写入仓库。
- 本机 DNS 曾返回错误地址；验证使用 Cloudflare 公共 DNS 返回的 worker 地址解析。
