# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@AGENTS.md

## 阅读顺序与当前架构

- 遵循 `AGENTS.md` 中的编码、临时文件和文档同步约定；前端改动还需遵循 `frontend/AGENTS.md`。
- `AGENTS.md` 中仅围绕 Chat Completions 的请求链路、组件归属和模型路由说明不是完整的多协议契约。涉及 Responses、Claude Messages、模型映射、Hook、流式边界和运行时状态时，以当前代码及 `docs/architecture-4plus1.md` 为准，不要从单一协议的摘要推断其他链路。
- 这是一个 Flask + gevent 的单进程协议翻译网关，不只是 OpenAI Chat 转发器。下游支持 Chat Completions、Responses、Responses compact、Claude Messages 和 Images；常规上游协议为 `openai_chat`、`openai_responses`、`claude_chat`。
- 修改 Agent 配置文件管理能力时，同时阅读 `agents/` 下的声明式扩展及 `docs/agent-configuration.md`。仓库没有 Cursor 或 Copilot 规则文件。

## 常用命令

### Python 后端

项目要求 Python 3.12+，统一使用 `uv` 管理环境和运行命令。

```bash
# 安装依赖
uv sync

# 使用独立配置启动；默认配置路径为项目根目录 config.yaml
uv run python main.py --config path/to/config.yaml

# 全部后端单元测试（使用标准库 unittest，不依赖 pytest）
uv run python -m unittest discover -s tests

# 单个测试文件、测试类、测试方法
uv run python -m unittest tests.test_proxy_pipeline
uv run python -m unittest tests.test_proxy_pipeline.StreamDecoderTests
uv run python -m unittest tests.test_proxy_pipeline.StreamDecoderTests.test_sse_json_decoder_handles_split_utf8_and_done

# 校验 Agent 模型参数表
uv run python scripts/check_agent_model_limits.py

# 修改 Python 文件后，对变更文件运行 import 整理、完整 lint 和格式化
uv run --with ruff ruff check --select I --fix <paths>
uv run --with ruff ruff check <paths>
uv run --with ruff ruff format <paths>
```

后端没有构建步骤。运行服务需要已存在的 YAML 配置；日常开发使用独立配置及数据路径，避免读写真实账号和数据库。Ruff 配置位于 `pyproject.toml`，行宽为 120。

### Cloudflare Workers

Workers 是独立部署形态；管理面使用 Flask WSGI 适配，数据面使用原生 fetch/Streams。先阅读 `docs/cloudflare-workers.md`，不要引入 Linux 专有依赖或把 Agent 配置功能带入 Workers。

```bash
npm install
uv run python scripts/lock_workers.py
npm run build
npm run deploy
uv run python scripts/verify_workers.py
```

`npm run deploy` 会同步 Python 包、构建前端并刷新 Workers 副本后调用 `cf deploy`。直接运行 `cf deploy` 可能部署未同步的共享源码。

### Vue 管理界面

只有修改前端时才需要 Node.js 24 和 npm。生产资源已提交到 `src/presentation/static/app/`，仅运行后端不需要 Node.js。

```bash
npm --prefix frontend ci
npm --prefix frontend run dev
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend run test:unit
npm --prefix frontend run build
npm --prefix frontend run test:e2e

# 单个前端单元测试文件
npm --prefix frontend run test:unit -- tests/unit/model-mapping.test.ts

# 单个 Playwright 文件、按用例标题筛选、只发现用例
npm --prefix frontend run test:e2e -- tests/e2e/workflows.spec.ts
npm --prefix frontend run test:e2e -- -g "用例标题"
npm --prefix frontend run test:e2e -- --list

# 使用 Playwright 管理的 Chromium 前安装浏览器
npx --prefix frontend playwright install chromium
```

Vite 默认在 `127.0.0.1:5173` 运行，API 代理到 `127.0.0.1:8080`。`npm run build` 会执行类型检查并更新生产 manifest 和哈希资源；相关产物应与前端源码一起提交。E2E 自动启动 test 模式的 Vite 并拦截管理 API，不需要真实后端账号；其截图、trace 和 HTML 报告保留在 `frontend/` 对应输出目录。

## 跨文件架构

### 组合根与控制平面 / 数据平面

`main.py` 先执行 `gevent.monkey.patch_all()`，再延迟导入 `Application`；新增入口需要保持 patch 顺序。`src/application/application.py` 是唯一组合根：创建 `AppContext`、SQLite 仓储、`ProviderManager` / `AuthGroupManager`、OAuth 与代理服务，然后实例化 Controller 完成 Flask 路由注册。`Application.reload_providers()` 同时重建认证组和 Provider 运行时并同步模型权限。

控制平面包括管理认证、Provider / Auth Group、用户与 API Key、OAuth 认证文件、模型映射、统计、系统设置和本机 / SSH Agent 配置。数据平面由 `src/presentation/proxy_controller.py` 的 `/v1/*` 路由处理；控制平面的模型发现和模型测试直连上游，不复用客户端代理入口。修改功能时保持这两个平面的职责边界。

### 路由选择与准入

Chat、Responses 和 Messages 共用 `_proxy_completion_request()`，Images 走独立分支。入口协议由路由决定，而不是由 Provider 配置决定。

模型 key 的解析顺序为：**自定义模型映射 → `{provider}/{upstream_model}` → Codex OAuth 裸模型名 → Claude OAuth 裸模型名**。映射 ID 可覆盖同名 OAuth 模型；Provider 转发时仅移除开头的 `{provider}/` 前缀。映射层在目标自身的 Provider 重试、Auth Entry 或 OAuth 账号候选耗尽后，再按 `highest_priority` 或 `sticky_failover` 策略切换目标。

准入组合客户端 IP 对应的登记用户与可选下游 API Key。Chat 白名单、用户模型权限、Key 模型权限及配额分别生效；用户和 Key 同时有限制时取权限交集。下游 `Authorization` 不直接转发给上游，Provider API Key、Auth Group 条目或 OAuth token 负责上游认证。

### 普通 Provider 代理管线

`ProxyService` 编排普通 Provider；`CodexProxyService` 和 `ClaudeProxyService` 处理 OAuth 候选账号、认证刷新及各自上游协议。不要把 OAuth 账号选择塞入 `ProviderManager` 或 Auth Group。

普通 Provider 的关键顺序为：

1. 选择 Auth Entry，移除下游认证 / hop-by-hop 头，并按需执行规则型脱敏。
2. `header_hook` 调整请求头。
3. `TranslatorRegistry.get(source_format, target_format)` 将下游请求转换为真实上游协议；三个文本协议共有 3×3 转换组合。
4. `request_guard` 接收**已翻译、model 已替换为真实上游 ID** 的请求体。随后补齐 usage 请求、签名和强制上游流式设置。
5. HTTP executor 发出请求，`proxy_core` 将 SSE / NDJSON 解码为统一事件；原始上游 usage 在响应翻译前采集。
6. translator 转换非流式响应或流式事件，`response_guard` 处理后恢复脱敏占位符，`proxy_core` 编码为当前下游协议。
7. `ProxyResponseBuilder` 预取首个非空的已编码下游字节，建立流提交边界；`RequestObservation` 包装最终响应，统一统计、日志及可选 LLM 数据记录。

`max_retries` 表示包括首次请求在内的最大尝试次数。提交前的 transport 失败可重试 Provider / Auth Entry 或切换映射目标；提交后不能透明重试，需按下游协议编码流内错误。`force_upstream_stream` 在下游非流式时先按上游原生协议聚合并校验完整性，再执行完整响应转换。新增流式逻辑必须区分提交前、提交后、协议终止和客户端取消，不能只检查 HTTP 状态或首个上游字节。

### Hook 与配置边界

Hook 文件放在根目录 `hooks/`，配置路径相对该目录且不能越界；文件导出 `Hook` 类。`ProviderRuntimeFactory` 负责懒加载、文件签名热更新和弱引用缓存，而不是 `ProviderManager` 直接加载模块。可选扩展点为 `header_hook`、`request_guard`、`response_guard`、`fetch_models`；`None` 表示保留当前 body 或回退到内置模型发现。`HookAbortError` 可中止请求。Hook 中的阻塞文件或第三方 I/O 需要显式使用合适的执行边界。

`ConfigManager` 负责 YAML 配置快照、规范化、原子写回和共享写锁；`get_raw_config()` 返回深拷贝。Provider schema 与运行时构建位于 `src/config/`，SQLite 中另存 API Key、模型映射、认证组运行态和请求统计。管理界面的大部分修改需要调用组合根提供的重载或权限同步回调，不能只改配置文件或单个 Service。

### 并发与观测

上游 HTTP、客户端连接和 SSE 等待依赖 gevent 协作 socket。SQLite 和配置 I/O 通过 `run_blocking()` 在 Hub 原生线程池执行；`@database_operation(write=...)` 保证一个仓储操作中的连接、SQL、结果读取及事务收尾位于同一原生任务，写操作由协程锁串行化。不要绕过这些边界在请求 greenlet 中直接执行阻塞 SQLite / 配置操作。

`RequestObservation` 以一次下游请求为单位，跨重试汇总最终 usage、性能与结果，并包装流式响应直到关闭；运行日志和可选 LLM 数据记录使用各自独立的有界原生队列及单写入线程。日志 / 记录故障不能改变客户端响应。请求日志、日聚合统计和 API Key 用量在同一 SQLite 事务提交。

认证 Session、部分 OAuth 状态、用户 IP 缓存、运行时注册表及推理缓存含进程内状态；部署拓扑和多实例约束见 `docs/architecture-4plus1.md`。

### 前端装配

`frontend/` 使用 Vue 3 Composition API、TypeScript strict、Vite、Pinia。页面组合 `features/`，网络请求集中在 `api/`，无业务依赖的控件位于 `components/ui/`；第三方 UI 库的直接导入遵守 `frontend/AGENTS.md` 的边界。

Flask 通过 `UiController` 提供 bootstrap，并从 `static/app/manifest.json` 提供同源生产页面。HTML / bootstrap 不缓存，manifest 每次重校验，哈希资源长期缓存。Node.js 只参与开发、测试和构建。
