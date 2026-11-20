# Research: 当前 LLM_Proxy 运行时清单与迁移切分证据

- **Query**: 为现有 LLM_Proxy 迁移或适配 Cloudflare Workers 的规划建立仓库证据清单，重点识别运行时边界。
- **Scope**: internal（仅仓库源代码、静态资源清单与仓库文档；未检索外部平台资料）
- **Date**: 2026-11-19
- **基准**: 当前工作树，`pyproject.toml` 声明版本 3.0.1、Python `>=3.12`（`pyproject.toml:1-18`）。

## 阅读范围与证据口径

- 已阅读 `docs/architecture-4plus1.md` 的 Context、Logical View、Blocking I/O、Agent 配置、Development View、Physical View、主要请求场景与 Runtime Boundaries；物理部署汇总见 `docs/architecture-4plus1.md:1452-1490`。
- 已沿 `main.py`、`src/application/application.py` 追踪普通 Provider、OAuth、仓储、配置、Hook、日志、Agent 配置、监听器及前端装配；行号均指向当前工作树。
- 下文“隔离候选”“迁移切分点”仅表示现有调用接口和依赖边界，**不表示这些模块已经获得 Cloudflare Workers 兼容性，也不表示可以直接部署**。平台运行时、打包、请求时限、流式与存储能力均须另行查证和实验。

## 1. 当前功能与组合根摘要

### 1.1 进程入口与装配

- `main.py` 在导入应用前执行 `gevent.monkey.patch_all()`，默认读取项目根 `config.yaml`，也接受命令行绝对或相对路径；配置不存在即失败（`main.py:4-15`, `main.py:21-44`）。`app()` 只构造 `Application`，`main()` 调用其 `run()`（`main.py:27-48`）。
- `Application` 是唯一组合根：先建 Flask，再依次初始化配置、滚动日志与原生日志消费者、监听器、上下文、五个 SQLite 仓储、Auth Group / Provider 运行时，最后创建服务和控制器（`src/application/application.py:67-84`, `src/application/application.py:274-291`, `src/application/application.py:293-409`）。共享 `AppContext` 明确携带 logger、ConfigManager、root Path 和 Flask app（`src/application/app_context.py:25-32`）。
- `reload_providers()` 从 YAML 深拷贝构造 Auth Group / Provider schema，重建两个管理器，并同步用户和 API Key 模型权限（`src/application/application.py:411-430`）；Provider 路由注册表与可见模型集合保存在 `ProviderManager` 实例字典中（`src/config/provider_manager.py:18-39`, `src/config/provider_manager.py:61-99`）。
- 正常运行时启动 Agent 清理和 Codex 配额刷新后台任务，然后启动 gevent WSGI 监听并等待；关闭时释放监听、Agent、LLM 记录和日志资源（`src/application/application.py:439-467`）。

### 1.2 数据平面与控制平面

- 数据面 Flask 路由包括 Chat Completions、Responses、Responses compact、Claude Messages、Images generations / edits（`src/presentation/proxy_controller.py:284-289`）；架构文档同时列出 `GET /v1/models` 与 `/v1/*` CORS（`docs/architecture-4plus1.md:9-21`）。
- 普通 Provider 由 `ProxyService` 编排，OAuth 裸模型分别由 `CodexProxyService`、`ClaudeProxyService` 处理，三者在组合根分别实例化并注入 `ProxyController`（`src/application/application.py:325-345`, `src/application/application.py:390-401`）。模型映射、用户 / Key 权限和统计也进入该控制器（`src/application/application.py:300-324`, `src/application/application.py:384-401`）。
- 控制面包括管理认证、Provider / Auth Group、用户、API Key、模型映射、OAuth 认证文件、模型发现 / 测试、统计、系统设置和本机 / SSH Agent 配置；各服务的装配及 Controller 注册集中在 `src/application/application.py:293-409`。
- 普通 Provider 的运行时字段包括 API URL、源协议、认证、模型表、代理、超时、最大尝试次数、证书校验、强制上游流式、脱敏和 Hook（`src/external/llm_provider.py:17-36`）。其执行链按 header hook → 请求翻译 → request guard → 签名 / usage / 流式设置构建实际上游请求（`src/services/upstream_request_builder.py:47-93`）。

## 2. 协议翻译器及与传输 / I/O 的隔离证据

### 2.1 已证实的数据层边界

- `Translator` 的三个同步接口只接收模型名、请求字典、`StreamEvent`、每次调用的 `state` 字典和响应 payload，返回字典或 `DownstreamChunk` 列表（`src/translators/registry.py:63-87`）。`StreamEvent` / `DownstreamChunk` 是轻量数据契约（`src/proxy_core/contracts.py:7-8`）。
- 默认 Registry 注册 `openai_chat`、`openai_responses`、`claude_chat` 的 3×3 共九个转换器，并按源 / 目标字符串查找（`src/translators/registry.py:1243-1288`）。
- 对 `src/translators/*.py` 与 `src/proxy_core/*.py` 的实际 import 检查未发现 Flask、gevent、requests、SQLite、paramiko、YAML 或 `open()` 调用。翻译模块使用标准库 `copy`、`json`、`time`、`base64`、`hashlib`、`re`、`secrets`、`Decimal` 等，以及内部 contracts / usage / bridge 模块；代表性依赖见 `src/translators/registry.py:5-60`, `src/translators/responses_claude_bridge.py:5-23`, `src/translators/codex_compat.py:3-13`。
- 字节流 decode 输入只是 `Iterable[bytes]`，增量 UTF-8 解码后产出统一 SSE / NDJSON / raw-text 事件（`src/proxy_core/decoders.py:48-75`, `src/proxy_core/decoders.py:128-173`）。下游 encode 接收 `DownstreamChunk` 并返回 bytes，非流式响应亦编码为 bytes（`src/proxy_core/encoder.py:13-40`, `src/proxy_core/encoder.py:64-134`）。
- 因而，**9 个 translator、SSE/NDJSON decoder 和下游 encoder 在源代码结构上可作为独立数据转换候选进行复用评估**。这只是一项 import / 接口结论，不是 Workers 运行验证结论。

### 2.2 当前包围它的运行时耦合

- `ProxyService` 在同一对象中创建 HTTP executor、translator registry、transport gateway、脱敏服务和 Flask 响应构建器（`src/services/proxy_service.py:57-76`）；`ProxyResponseBuilder` 直接调用 `response.iter_content()`，把 decode → translate → response guard → encode 串接起来（`src/services/proxy_response_builder.py:415-446`），并创建 Flask `Response(stream_with_context(...))`（`src/services/proxy_response_builder.py:213-243`）。
- HTTP executor 的接口虽已用 `Executor` / `OpenedUpstreamResponse` 抽象（`src/executors/contracts.py:13-39`），但 `response` 字段仍为 `Any`，默认实现返回 requests 对象，并负责 Session、代理、TLS、连接池和流格式探测（`src/executors/registry.py:31-108`）。抽象接口存在不等于已有另一种传输实现。
- 流语义还依赖首个预取字节、requests 异常、`GeneratorExit`、上游 `.close()`、Flask response `call_on_close` 和完成回调（`src/services/proxy_response_builder.py:213-243`, `src/services/proxy_response_builder.py:477-550`, `src/services/proxy_response_builder.py:584-667`）。`RequestObservation` 继续包装 Flask 响应，读取 `content_type`、非流式 `get_data()` / `get_json()` 和流式 iterator（`src/services/request_observation.py:201-239`）。
- Codex / Claude OAuth 虽复用 translator / decoder / encoder，但其代理服务直接 import requests 与 Flask Response，并调用 `requests.post(...)`；例如 `src/services/codex_proxy_service.py:16-30`, `src/services/codex_proxy_service.py:541-560`, `src/services/claude_proxy_service.py:13-26`, `src/services/claude_proxy_service.py:252-271`。它们还包含 OAuth 候选、token 刷新、配额、图片、compact、推理缓存和账号状态逻辑，不能视为普通 translator 的简单调用器。
- Hook 位于翻译前后与响应编码前；`LLMProvider` 同步调用 Hook，并把 `None` 解释为保留原 payload（`src/external/llm_provider.py:51-106`）。因此“复用 translator”并不会自动携带现有 Hook、重试、流提交、统计、映射故障切换等完整语义。

## 3. 运行时依赖与实际用途清单

| 依赖 / 状态 | 代码已证实的用途与边界 |
|---|---|
| **文件系统与只读包资源** | 根路径和配置文件由 `main.py` 解析（`main.py:27-44`）；模板、静态目录、manifest 在每次页面渲染时从磁盘读取（`src/presentation/app_factory.py:12-22`, `src/presentation/ui.py:26-67`）。内置 OpenAI 模型目录在模块 import 时读 JSON（`src/config/openai_model_catalog.py:12-13`）；bootstrap 的版本还会读 `pyproject.toml` 并缓存（`src/utils/app_version.py:16-24`, `src/utils/app_version.py:29-51`）。 |
| **YAML 配置** | `ConfigManager` 启动加载并规范化 YAML，旧字段发生变化时会回写；写入采用同目录临时文件、flush、`fsync`、`os.replace`，读写在 `blocking_io` 边界内（`src/config/config_manager.py:138-188`, `src/config/config_manager.py:191-246`）。`get_raw_config()` 深拷贝，配置写锁协调读改写和重载（`src/config/config_manager.py:121-140`）。 |
| **SQLite 与仓储** | Linux import `pysqlite3`，其他平台 import 标准库 `sqlite3`（`src/utils/database.py:16-23`）。连接工厂建父目录、强制 WAL，每次操作创建并关闭连接；完整仓储函数在 Hub 原生线程执行，写操作由 gevent RLock 串行化（`src/utils/database.py:28-64`, `src/utils/database.py:72-85`）。组合根装配 User、ApiKey、Log、AuthGroup、ModelMapping 五个 SQLite 仓储（`src/application/application.py:274-282`）；表和 migration 示例见 `src/repositories/model_mapping_repository.py:22-75`。 |
| **事务与准入计数** | 请求日志、日聚合、API Key 累计用量在同一 `LogRepository.insert` 事务上下文中写入（`src/repositories/log_repository.py:332-361`, `src/repositories/log_repository.py:383-495`）；分钟 / 日额度用 `BEGIN IMMEDIATE` 读改（`src/repositories/api_key_repository.py:418-443`）。这些不仅是一次性配置 CRUD，也位于数据面准入和完成路径。 |
| **gevent 与监听 socket** | monkey patch 是启动前置条件（`main.py:8-12`）；`ListenerRuntime` 直接使用 gevent socket、Pool、Event、RLock、WSGIServer，支持运行时重绑和旧连接排空（`src/application/listener_runtime.py:6-10`, `src/application/listener_runtime.py:44-70`, `src/application/listener_runtime.py:72-108`）。 |
| **原生线程与协作等待** | `run_blocking()` 通过 `get_hub().threadpool.spawn()` 执行整个函数，调用 greenlet 等待；取消时仍等待原生任务完成（`src/utils/blocking_io.py:12-43`）。SQLite 与 YAML 依赖此边界（`src/utils/database.py:59-64`, `src/config/config_manager.py:142-170`）。OAuth 认证 JSON 和 Codex 状态文件则直接 `Path.open()`，未见统一 `run_blocking` 包装（`src/services/codex_oauth_service.py:1182-1202`, `src/services/codex_oauth_service.py:1278-1311`, `src/services/codex_oauth_service.py:2223-2234`）。 |
| **requests / urllib3** | 普通上游用线程本地 requests Session、HTTPAdapter、显式禁用 urllib3 自动重试，并支持代理、证书设置和流式 POST（`src/executors/registry.py:35-75`, `src/executors/registry.py:110-131`）。OAuth 管理、模型发现 / 测试、OAuth 代理等也直接使用 requests；`main.py` 仅全局关闭 urllib3 的证书警告（`main.py:12-15`）。 |
| **paramiko / SSH / 原始 socket** | Agent 配置可读本机用户文件，或通过 Paramiko SFTP 读远程文件，支持 2 MiB 上限、原子替换、备份事务（`src/external/agent_config_transport.py:36-92`, `src/external/agent_config_transport.py:95-180`）。SSH 连接使用系统 known_hosts / 指纹确认，支持直连、HTTP CONNECT、HTTPS、SOCKS5(h) 和 TLS 包装（`src/external/agent_config_transport.py:228-325`）。Agent 服务还做本机地址 / Host 判断与 hostname DNS 查询（`src/services/agent_config_service.py:367`, `src/services/agent_config_service.py:423-424`, `src/services/agent_config_service.py:520`）。 |
| **动态 Provider Hook** | 仅允许项目根 `hooks/` 内相对路径，按 `mtime_ns` + size 检查签名，弱引用缓存实例；加载时修改 `sys.path` / `sys.modules`，读源文件并 `exec(compile(...))`，要求导出 `Hook` 类（`src/config/provider_runtime_factory.py:132-174`, `src/config/provider_runtime_factory.py:176-248`）。支持 `header_hook`、`request_guard`、`response_guard`、`fetch_models`（`src/config/provider_runtime_factory.py:40-79`）。 |
| **动态 Agent 扩展** | `agents/*/agent.yaml` 由 PyYAML 读取，JSONC/TOML/YAML 编辑使用 `ruamel.yaml`；同目录可选 `adapter.py` 通过 `importlib` 执行（`src/services/agent_extensions.py:16-39`, `src/services/agent_extensions.py:62-97`）。这是 Provider Hook 之外的另一条动态 Python 加载路径。 |
| **进程内会话与缓存** | 管理 Session 为实例字典（`src/services/authentication_service.py:12-19`, `src/services/authentication_service.py:43-68`）；Codex / Claude OAuth PKCE state 也为实例字典（`src/services/codex_oauth_service.py:111-149`, `src/services/claude_oauth_service.py:77-98`）。用户 IP 查询有锁保护缓存（`src/services/user_service.py:25-54`, `src/services/user_service.py:518-538`）；Provider Registry（`src/config/provider_manager.py:18-39`）和 Codex 有界推理 LRU（`src/services/codex_reasoning_cache.py:43-75`）也在进程内。 |
| **锁、游标与并发名额** | Auth Group 的组定义、inflight 计数和轮转 / 粘滞游标使用实例状态及 gevent RLock（`src/config/auth_group_manager.py:57-72`, `src/config/auth_group_manager.py:84-124`）；API Key 并发名额为实例字典加 gevent RLock，分钟 / 日额度另落 SQLite（`src/services/api_key_service.py:47-108`）；模型映射有独立 RLock（`src/services/model_mapping_service.py:64`）。Codex token / 配额刷新用 `threading.Lock` / `RLock`（`src/services/codex_oauth_service.py:120-131`, `src/services/codex_oauth_service.py:1097-1104`, `src/services/codex_oauth_service.py:2153-2175`）。 |
| **后台 greenlet** | Agent 每 60 秒清理闲置 SSH Session、凭据和五分钟预览票据（`src/services/agent_config_service.py:37-39`, `src/services/agent_config_service.py:81-119`）。Codex 启动无限配额刷新循环，按周期与 reset_at 唤醒（`src/services/codex_oauth_service.py:254-282`, `src/services/codex_oauth_service.py:1022-1060`），成功请求还可 `spawn_later` 60 秒刷新（`src/services/codex_oauth_service.py:711-755`）。 |
| **专用原生队列 / 写入线程** | 应用、访问、trace 三类滚动日志经有界原生队列和一个 `ThreadPool(1)` 消费者写入（`src/application/application.py:90-180`, `src/utils/async_logging.py:27-48`, `src/utils/async_logging.py:79-124`）。LLM 记录有独立有界队列和另一个 `ThreadPool(1)`，后台创建 prompts / records 目录并写 JSONL / 分片（`src/services/llm_recording.py:125-157`, `src/services/llm_recording.py:329-405`）。 |
| **依赖集合** | Python 3.12+；直接依赖包括 Flask、gevent、paramiko、PySocks、Linux `pysqlite3-binary`、PyYAML、requests、ruamel-yaml、tomlkit、urllib3（`pyproject.toml:6-18`）。 |

## 4. OAuth、代理与本地状态的具体边界

- Codex OAuth 目录固定为项目根 `data/oauth/codex`，认证 JSON、模型列表、图片设置及 `.state/auth_files.json` 保存凭据、启停、配额快照、最近成功账号等（`src/services/codex_oauth_service.py:111-131`, `src/services/codex_oauth_service.py:1278-1320`）。token 交换 / 刷新和配额查询直接发 HTTPS，并在锁内重读和写回文件（`src/services/codex_oauth_service.py:2162-2228`, `src/services/codex_oauth_service.py:2236-2279`）。
- Claude OAuth 同样固定本地 `data/oauth/claude`，包含模型目录与状态文件（`src/services/claude_oauth_service.py:77-84`），PKCE state 仅存在当前服务实例（`src/services/claude_oauth_service.py:86-98`, `src/services/claude_oauth_service.py:136-145`）。
- Agent 状态不是 SQLite，而是数据库同级的 `agent-config/state.json`；该 JSON 由本机文件 transport 原子写入（`src/services/agent_config_service.py:73-85`, `src/repositories/agent_config_repository.py:12-50`）。SSH Session、临时代理 / SSH 凭据和预览票据按浏览器 owner 在内存保存，后台定期清理（`src/services/agent_config_service.py:81-119`）。
- Codex 推理缓存是可选流程状态但影响推理历史补齐，按 scope + 历史指纹保留最多 512 项、8 MiB、一小时（`src/services/codex_reasoning_cache.py:43-75`, `src/services/codex_reasoning_cache.py:77-123`）。
- Auth Group / 模型映射运行状态是“SQLite 持久化 + 进程内选择 / 锁”的混合，而非单纯内存缓存；例如 Auth Group acquire 在锁内查 SQLite 后更新 inflight 与游标（`src/config/auth_group_manager.py:84-124`）。

## 5. 静态前端与 bootstrap

- 前端是 Vue 3 + Vue Router + Pinia，构建由 Node 24 和 Vite 执行；`npm run build` 先类型检查再执行构建脚本（`frontend/package.json:1-17`, `frontend/package.json:19-45`）。仓库已有 `src/presentation/static/app/manifest.json` 和内容哈希 JS / CSS 产物，Node 不是 Flask 处理请求时 import 的依赖。
- Flask 将 `/static` 映射到 `src/presentation/static`，哈希 assets 设置一年 immutable，manifest 为 `no-cache`（`src/presentation/app_factory.py:12-33`）。
- `render_ui()` 读取 manifest，校验引用文件存在且不能越出资产目录，再渲染 `app.html`；HTML 为 `no-store`，资产缺失返回 503（`src/presentation/ui.py:26-68`）。页面入口由后端路由与认证装饰器控制（`src/presentation/web_controller.py:44-54`, `src/presentation/agent_config_controller.py:25-43`），并非任意路径都交给 SPA。
- `/api/ui/bootstrap` 返回版本、认证状态、用户名、OAuth / API Key / 模型映射 / 白名单开关和 Agent capability，不返回 Provider 配置或凭据；响应 `no-store` 且 `Vary: Cookie, Host`（`src/presentation/ui.py:71-99`）。未启用管理认证时，Agent capability 还要求 Host 和连接对端均为回环地址（`src/presentation/ui.py:14-23`, `src/presentation/ui.py:95`）。
- 因此可将“静态构建产物”与“动态 HTML manifest 装配、bootstrap、登录授权、API”分别列入部署验证；现有前端并非完全脱离后端的纯静态站点。

## 6. 关键风险与可验证的迁移切分点

以下是**待验证的切分单元与验收问题**，不是推荐的重构设计或兼容性承诺。

| 切分单元 | 已有代码支点 | 必须验证的风险 / 验收问题 |
|---|---|---|
| **A. 纯协议转换核** | Translator 接口与九种组合（`src/translators/registry.py:63-87`, `src/translators/registry.py:1243-1288`）；字节 iterable decoder 与 bytes encoder（`src/proxy_core/decoders.py:48-75`, `src/proxy_core/encoder.py:13-40`）。 | 在不 import Flask / gevent / requests 的宿主中，对九种组合跑请求、非流式、增量 SSE、工具多模态、usage 与终止事件等价测试。当前仅证实依赖边界，未在 Workers 执行，也未验证性能 / 包体 / CPU 限制。 |
| **B. 普通 Provider HTTP 适配层** | `Executor` Protocol 和标准化响应元数据（`src/executors/contracts.py:13-39`）提供命名边界；translator 调用点在 `src/services/proxy_response_builder.py:415-446`。 | 替换的不只是 POST：还包含 lazy bytes、错误类型、超时、TLS / 自定义代理、连接关闭、自动风险确认重试、流提交与客户端取消（`src/executors/registry.py:38-108`, `src/services/proxy_response_builder.py:477-597`）。需端到端验证首字节前重试、首字节后错误、终止后 framing error 和取消。 |
| **C. 最小只读 Provider 路由** | `ProviderManager` 的 model → Provider 字典查找（`src/config/provider_manager.py:18-59`），Provider schema 到运行时的工厂（`src/config/provider_runtime_factory.py:94-121`）。 | 可先明确验证“部署期固定 Provider / 静态模型表”的独立范围；但 YAML 运行时可写、Auth Group、Hook、映射 / 用户 / Key 权限并非这一层的自动组成。需验证配置 secret 来源、模型 ID 去除前缀规则和禁用 / 隐藏模型行为。 |
| **D. 动态 Hook 与 Agent 扩展** | 两条动态加载路径分别见 `src/config/provider_runtime_factory.py:203-248` 与 `src/services/agent_extensions.py:62-97`。 | 现有语义要求任意本地 Python 源、`sys.path`、模块 import、文件签名热更新，以及 Hook 自己可能发起的文件 / 第三方 I/O。应单独判定保留、静态声明替代或不纳入目标；不能由 translator 可复用推断这些能力可复用。 |
| **E. 持久化与一致性** | SQLite WAL / 原生线程（`src/utils/database.py:28-64`）；配额原子预占（`src/repositories/api_key_repository.py:418-443`）；日志 + 日统计 + Key 用量事务（`src/repositories/log_repository.py:383-495`）。 | 需实际验证目标存储的事务、原子计数、唯一键、迁移、查询 / 导出和并发语义。 inflight、Auth 游标和管理 Session 的进程内性质也需独立决定，不可只迁移表结构。 |
| **F. OAuth / Codex 全链路** | 本地凭据与状态文件、锁内刷新、后台刷新、推理缓存分别见 `src/services/codex_oauth_service.py:111-131`, `src/services/codex_oauth_service.py:2162-2228`, `src/services/codex_oauth_service.py:1022-1060`, `src/services/codex_reasoning_cache.py:43-75`。 | 需验证凭据保密与持久化、PKCE state 跨请求一致性、单账号刷新互斥、配额调度、图片 / compact、推理缓存失效，以及 requests 代理风险确认流程。它比“裸 OAuth 模型转发”包含更多状态与后台职责。 |
| **G. 静态管理前端与动态 bootstrap** | 已有静态产物及 manifest 装配（`src/presentation/ui.py:26-68`）；bootstrap / 回环判定（`src/presentation/ui.py:71-99`）。 | 静态 JS / CSS 可独立做资源存在性和缓存测试；HTML、manifest、bootstrap、版本读取、登录 cookie、同源 / 回环 Host 判定、API URL 必须另行验证。保留静态文件不等于保留完整管理面。 |
| **H. 日志、LLM 记录与后台任务** | 两个独立原生队列 / 消费者（`src/utils/async_logging.py:79-124`, `src/services/llm_recording.py:392-430`）；两个长期 greenlet（`src/services/agent_config_service.py:87-119`, `src/services/codex_oauth_service.py:1022-1060`）。 | 需明确请求后工作的生命周期、失败 / 丢弃语义、退出排空、滚动文件与 JSONL 分片替代，以及周期任务如何触发。不能依赖模块加载时永久运行的后台循环假设。 |
| **I. Agent 本机 / SSH 文件管理** | 原始 socket / CONNECT / SOCKS / TLS / Paramiko（`src/external/agent_config_transport.py:228-325`）；本机用户目录和状态文件（`src/external/agent_config_transport.py:36-92`, `src/repositories/agent_config_repository.py:12-50`）。 | 原始 TCP、系统 known_hosts、SSH agent / 私钥、本机任意受控路径、远程 SFTP 和长期连接都与 HTTP 请求转发是独立能力域，应单独验证目标环境是否提供等价网络与文件语义。 |
| **J. WSGI 入口与监听管理** | `main.py` patch 顺序与 `ListenerRuntime`（`main.py:8-15`, `src/application/listener_runtime.py:44-108`）。 | Flask/Wsgi 宿主适配与平台 fetch 入口、流式响应取消传播是不同问题；`server.host` / `port` 的运行时重绑及连接排空属于当前进程监听器职责，不应混入 translator 验证范围。 |

## 7. 关键文件索引

| 文件 | 本调研中的作用 |
|---|---|
| `main.py` | gevent patch、YAML 路径、进程入口 |
| `src/application/application.py` | 全应用组合根、重载、后台任务、关闭 |
| `src/application/listener_runtime.py` | gevent WSGI socket、监听切换与排空 |
| `src/services/proxy_service.py` | 普通 Provider 编排、重试、registry 组装 |
| `src/services/upstream_request_builder.py` | 请求翻译与 Hook 的准确顺序 |
| `src/services/proxy_response_builder.py` | requests 流、Flask 响应、提交 / 取消边界 |
| `src/translators/registry.py` | 九个转换器与数据接口 |
| `src/proxy_core/decoders.py`, `encoder.py` | 与具体 socket 无关的字节转换层 |
| `src/executors/registry.py` | requests / urllib3 HTTP 实现 |
| `src/config/config_manager.py` | YAML 读改写、原子写、协作锁 |
| `src/config/provider_runtime_factory.py` | Provider 构建与动态 Hook |
| `src/utils/blocking_io.py`, `database.py` | Hub 原生线程和 SQLite WAL 边界 |
| `src/repositories/` | 用户、Key、日志统计、Auth、映射持久化与事务 |
| `src/services/codex_oauth_service.py`, `claude_oauth_service.py` | OAuth 文件、PKCE、token / 配额与后台任务 |
| `src/services/codex_proxy_service.py`, `claude_proxy_service.py` | OAuth 数据面与 requests / Flask 耦合 |
| `src/services/agent_config_service.py`, `src/external/agent_config_transport.py` | 本机 / SSH 文件、原始 socket、会话与清理 |
| `src/services/agent_extensions.py` | YAML 声明与第二条动态 Python 加载路径 |
| `src/services/authentication_service.py`, `user_service.py`, `api_key_service.py` | 管理会话、IP 缓存、准入并发与锁 |
| `src/utils/async_logging.py`, `src/services/llm_recording.py` | 独立原生队列、线程和文件输出 |
| `src/presentation/app_factory.py`, `ui.py` | 静态资源、manifest、bootstrap 和缓存 |
| `docs/architecture-4plus1.md` | 当前协议 / 进程 / 部署契约的总览 |

## 8. Caveats / 尚待验证

- **未开展 Cloudflare 外部调研**：本文件没有查证 Workers Python 支持范围、原生扩展、文件系统、socket、线程、执行 / 流式限制、捆绑限制、Cron、Queue、D1 / Durable Objects / KV / R2 或静态 Assets 能力；需要独立的官方资料证据后再做兼容性判断。
- **未执行目标环境实验**：未在 Workers 本地或线上导入 translator，未打包，未发 SSE / NDJSON，未验证客户端取消、长请求、CPU / 内存、subrequest 或并发行为；本报告不声称兼容。
- **依赖隔离是静态源代码结论**：对 translator / proxy_core 的检查说明当前 import 与接口没有显式 Web / 网络 / 文件依赖，不等于任意第三方 Python 环境都支持其标准库子集，也不等于 `ProxyService` 可整体复用。
- **仓储检查以关键路径为主**：已核对五个 SQLite 仓储的装配、装饰器及关键 SQL / migration，不声称逐项审阅每个查询和每个字段；完整存储迁移仍需以所有 `@database_operation` 与 SQL 为工作清单。
- **OAuth / Agent 文件 I/O 不完全经过通用 blocking_io**：SQLite、YAML、本机 Agent 文件有明确原生线程边界；OAuth JSON、manifest、静态文件、动态 Hook 和 Paramiko 的网络 / 文件行为需按各自实现验证，不能统一套用 `run_blocking` 描述。
- **前端仅核对装配与现有产物**：未在本次调研运行 Vite、前端测试或 Playwright；Node 仅属于开发 / 构建是代码与文档层面的结论。
- **文档与代码冲突时以代码行号为准**：本文未把 `AGENTS.md` 中仅围绕 Chat 的旧摘要当作多协议完整契约；协议与部署事实以当前代码及 `docs/architecture-4plus1.md` 为证据。
