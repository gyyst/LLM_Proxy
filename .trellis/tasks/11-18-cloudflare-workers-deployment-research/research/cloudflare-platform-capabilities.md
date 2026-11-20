# Research: Cloudflare Workers 平台能力与 Flask + gevent 代理适配依据

- **Query**: 为 Python Flask + gevent 协议翻译代理适配 Cloudflare Workers 的可行性规划收集当前官方依据。
- **Scope**: external（Cloudflare 官方文档为主；Cloudflare 文档直接链接的 Pyodide 官方文档与 Cloudflare 官方源码为辅）
- **统一检索日期**: 2026-11-19。本文所有链接均在该日读取，除非单独说明。
- **主要参考资料**: [Cloudflare Python Workers 官方主页](https://developers.cloudflare.com/workers/languages/python/)（用户指定，优先核对并引用）。
- **边界**: 仅做只读资料调研。本项目没有 Wrangler 配置；未安装或登录 CLI，未部署，未创建资源。本文不给出 Wrangler 配置、产品代码改造或费用/性能结论。

## 阅读约定与证据分级

- **支持 / 不支持 / 受限**: 官方文档明确陈述的能力或限制。
- **GA**: 官方明确写为 generally available；**beta**: 官方明确标为 beta；**官方未标阶段**: 当前官方参考文档陈述支持，但该页未标注 GA/beta。后者不自行推断为 GA。
- **包级未知**: 没有 Cloudflare 针对该包及其使用方式的明确兼容结论。纯 Python 包可被打包、Pyodide 包列表含某个名字、或某包含 Wasm，均不能单独证明其网络、线程、进程、原生 ABI 或持久化功能可用。
- **推断 / 待验证**: 将已核实平台事实映射到本项目时的分析，不冒充官方对本项目的兼容保证。
- 状态具有时效性；具体 Python/Pyodide 版本由兼容性日期及标志选择，不应只读当前默认值后固定长期结论。[版本选择机制](https://developers.cloudflare.com/workers/languages/python/how-python-workers-work/#pyodide-and-python-versions)、[兼容性日期](https://developers.cloudflare.com/workers/configuration/compatibility-dates/)。

## 结论速览

| 范围 | 官方事实对应的能力状态 | 对本项目的边界（推断，非官方包级结论） |
|---|---|---|
| Python + Flask 路由外壳 | 支持；Python Workers 已有 GA 表述，Flask/WSGI 有官方入口 | 可保留部分 Flask 路由组织，但入口须由 `workers.wsgi` 或 `WorkerEntrypoint` 承接，不能沿用 gevent `WSGIServer` 进程模型 |
| HTTP(S) 出站、双向流、协议翻译 | 支持 `fetch`、Web Streams；有 Python 流式示例 | 有实现核心代理的平台基础，但 SSE/NDJSON 边界、背压、取消、首字节前重试须专项验证，不能由“支持流”直接推定现有管线兼容 |
| 本地 YAML、动态 Hook、SQLite 文件、内存 Session | 仅临时/隔离内文件与内存；持久状态须使用外部服务或绑定 | 现有原子文件写回、按文件签名热更新、原生 SQLite 连接和进程内全局状态不能直接保留语义 |
| gevent / 原生线程 / 子进程 | `threading`、`multiprocessing` 可导入但不可运行；Pyodide 不支持 fork/pthreads/subprocess | 现有 gevent 协作 socket 和 Hub 原生线程池没有可直接套用的运行基础；包级结论仍为未知 |
| Paramiko SSH、PySocks、任意代理 | Workers 有受限的 JS `connect()` TCP API | 底层 TCP API 存在不等于 Paramiko/PySocks 可运行；SSH、SOCKS、私网目标等客户端能力未获官方确认 |
| Vue 静态构建产物 | 支持 Static Assets，含 Vue 指南、SPA 回退与 Worker 路由 | 构建产物可作为静态资源承载；bootstrap、manifest、缓存头与 API 优先路由需重新适配和验证 |
| D1 / Durable Objects / KV / R2 | 支持，且各有不同一致性及访问模型 | 是重新设计持久化与协调的候选，不是 `sqlite3`/本地文件的透明替换 |
| Containers | 支持 Workers Paid；Linux 容器、完整文件系统类负载 | 是保留原生运行时的可能独立路线，仍有生命周期和默认临时磁盘约束，不等于长期单机 |
| AI Gateway | 支持通用 AI Provider 网关及自定义 HTTPS Provider | 与上游 AI 路由/观测有关；没有证据表明它承载本项目任意协议翻译、OAuth、Hooks、SSH 或管理平面 |

## 1. Python Workers、Pyodide 与 Python 版本

### 1.1 入口和运行时

**状态：支持；Python Workers 有官方 GA 表述。** 2026-10-15 官方更新写明 “Now that Python Workers are generally available”；该文是旁证，不是 Python Workers 的完整 GA 发布公告。[官方更新](https://developers.cloudflare.com/changelog/post/2026-10-15-phonellm-mini-workers-ai/)

主要主页给出的正式能力：

> “Cloudflare Workers provides a first-class Python experience”
>
> “A robust foreign function interface (FFI) … including all Runtime APIs”
>
> “You must add the `python_workers` compatibility flag to your Worker.”

入口是继承 `WorkerEntrypoint` 的 `Default` 类并实现异步 `fetch(self, request)`，返回 `Response`，不是启动一个监听 socket 的 Python 服务器。[Python Workers 主页](https://developers.cloudflare.com/workers/languages/python/)、[Fetch Handler](https://developers.cloudflare.com/workers/runtime-apis/handlers/fetch/)

官方运行机制原文：

> “Workers written in Python are executed by Pyodide. Pyodide is a CPython … compiled to WebAssembly.”
>
> “your code is interpreted directly by Pyodide, within a V8 isolate.”

这是 CPython 的 Wasm 运行时，不是常规 Linux CPython 进程。[How Python Workers Work](https://developers.cloudflare.com/workers/languages/python/how-python-workers-work/)

### 1.2 当前可选/默认版本

| 项目 | 状态与官方事实 | 原文依据与 URL |
|---|---|---|
| 当前默认 Python | **支持**；兼容性日期为 **2026-09-08 或之后**的新 Python Workers 默认 Python 3.14 | “Python workers now use Python 3.14 by default.” [Python 3.14 更新](https://developers.cloudflare.com/changelog/post/2026-09-08-python-workers-314/) |
| 对应 Pyodide | 上述更新发布时为 **314.0.6**；这是发布时事实，不宣称所有后续运行时永远固定该补丁版本 | “Internally, this change updates the Pyodide runtime to 314.0.6.” 同上 |
| 显式版本标志 | `python_workers_314`；2026-09-08 起默认 | “When this flag is set, Python 3.14 is used for Python Workers.” [Compatibility Flags](https://developers.cloudflare.com/workers/configuration/compatibility-flags/#python-314-for-python-workers) |
| 历史兼容版本 | 官方说明 Pyodide 新版本通过兼容性标志/日期引入；旧线上 Worker 继续运行，但停止安全支持后的版本不建议新项目使用 | “new version of Pyodide … gating it behind a Compatibility Flag” [版本机制](https://developers.cloudflare.com/workers/languages/python/how-python-workers-work/#pyodide-and-python-versions) |
| 完整历史版本矩阵 | **本次未在指定主页或兼容标志页找到完整的 3.12/3.13 当前可选矩阵** | 需按实际目标兼容日期验证；不从当前 3.14 默认反推旧版本不可用 |

**推断 / 待验证**: 本项目声明 Python `>=3.12`，并不自动意味着所有已有语法和依赖在 3.14/Pyodide 下兼容；需固定目标兼容日期，再运行实际导入及行为测试。官方主页的包管理示例要求 `requires-python = ">=3.13"`，而 Flask 示例为 `>=3.12`；这是示例约束，不是平台完整版本矩阵。[主页](https://developers.cloudflare.com/workers/languages/python/)、[包管理](https://developers.cloudflare.com/workers/languages/python/packages/)、[Flask](https://developers.cloudflare.com/workers/languages/python/packages/flask/)

### 1.3 初始化与部署模型

官方说明部署时执行入口模块及其顶层 imports，并对 Wasm 线性内存做快照；请求到来后加载该快照。[How Python Workers Work](https://developers.cloudflare.com/workers/languages/python/how-python-workers-work/#deployment-lifecycle-and-cold-start-optimizations)

> “Cloudflare executes the Worker entrypoint module and everything it imports at top level and then take a snapshot of the Worker’s WebAssembly linear memory.”

**推断 / 待验证**: 顶层初始化可能被纳入部署快照，因此现有启动时读取配置、注册 Provider、创建运行时缓存的时间和可见状态需专项核对。官方未在此页承诺任意动态外部配置都能在每次请求时重建。

## 2. Flask / WSGI 支持

**状态：支持，官方专用文档未标 beta。** 2026-09-02 官方发布 WSGI/ASGI 支持；Flask 页面明确：

> “Flask is supported in Python Workers.”
>
> “Python Workers include a WSGI server … which you can use with Flask applications.”

适配入口为 `Default = wsgi.entrypoint(app)`；需要更多控制时，可在 `WorkerEntrypoint.fetch()` 内调用 `wsgi.fetch(app, request, self.env)`。[Flask 官方指南](https://developers.cloudflare.com/workers/languages/python/packages/flask/)、[WSGI/ASGI 发布公告](https://developers.cloudflare.com/changelog/post/2026-09-02-python-workers-web-framework-support/)

Flask 页面还提供 Static Assets 集成：WSGI `request.environ["workers.env"]` 可取得绑定，异步 asset 操作通过 Pyodide `run_sync` 桥接到同步 Flask handler。[Flask + Static Assets](https://developers.cloudflare.com/workers/languages/python/packages/flask/#serve-a-frontend)

官方链接的 `workers.wsgi` 当前实现显示：请求体优先以流式 `wsgi.input` 读取；WSGI 响应 iterable 被包装为 `ReadableStream`，在构造 Response 前先取首个非空 chunk；没有流式读取能力时回退为预缓冲请求体。[Cloudflare `workers-py` 官方源码 `wsgi.py`](https://github.com/cloudflare/workers-py/blob/main/packages/runtime-sdk/src/workers/wsgi.py)

**边界 / 待验证（不是官方对现有应用的承诺）**:

- Flask 支持不等于 `gevent.monkey.patch_all()` 或 gevent `WSGIServer` 支持。运行模型是 Pyodide + V8 isolate + Cloudflare WSGI adapter。
- 上述源码证明适配器存在流式实现，不证明本项目 translator、重试提交边界、流内错误编码、客户端取消处理可直接工作。SSE/NDJSON 应逐协议做端到端验证。
- WSGI 示例的 Static Assets 路径调用 `asset_response.bytes()`，会读取 body；不应据此推断所有前端资源都自动流式服务。平台通用 Streams 与直接静态命中路由见第 4、8 节。

## 3. Python 包兼容：逐项核实

### 3.1 官方通用规则

主要主页明确列出 FastAPI、LangChain、Pydantic 等包支持；包管理页的完整规则是：

> “Python Workers support pure and PyEmscripten Python packages on PyPI. Additionally, Python Workers support packages that are included in Pyodide.”
>
> “WebAssembly support for Python packages is still in early stages, and some packages may not yet be available as PyEmscripten wheels on PyPI.”

来源：[Python Workers 主页](https://developers.cloudflare.com/workers/languages/python/)、[Supported Libraries](https://developers.cloudflare.com/workers/languages/python/packages/#supported-libraries)。

**重要限制**: 这是包分发/打包支持规则，不是“所有包的所有功能均可运行”。Cloudflare 并未在这里给出纯 Python socket、threads、subprocess 或本地持久化会自动可用的承诺。

### 3.2 指定依赖证据矩阵

| 包/依赖 | 官方证据下的状态 | 能下的结论与不能下的结论 |
|---|---|---|
| `pydantic` / 纯 Python 包 | **包层面有支持表述**；纯 Python/PyEmscripten/Pyodide 包受支持 | Cloudflare 主页明确点名 Pydantic。不能由此证明任意 C 扩展版本、所有插件或本项目指定版本均兼容。[主页](https://developers.cloudflare.com/workers/languages/python/) |
| `requests` | **Cloudflare 包级未知** | Pyodide 官方包列表本次出现 `requests 2.33.1`，但列表存在不等于其网络传输、流式、代理在 Workers 可用。Workers 文档明确给出的是 `fetch`。[Pyodide 包列表](https://pyodide.org/en/stable/usage/packages-in-pyodide.html)、[Workers Fetch](https://developers.cloudflare.com/workers/runtime-apis/fetch/) |
| `httpx` | **Cloudflare 包级未知** | Pyodide 列表本次出现 `httpx 0.28.1` 与 `httpcore 1.0.9`；未找到 Cloudflare 对本项目 sync/async、流式或自定义 transport 的兼容结论。[Pyodide 包列表](https://pyodide.org/en/stable/usage/packages-in-pyodide.html) |
| `PyYAML` | **Pyodide 已列包；Workers 使用方式待验证** | Pyodide 列表本次出现 `pyyaml 6.0.3`，符合 Workers 文档“Pyodide included packages”规则的包来源；YAML 读写之外的本地文件持久性另受第 5 节约束。[Pyodide 包列表](https://pyodide.org/en/stable/usage/packages-in-pyodide.html)、[Workers 包规则](https://developers.cloudflare.com/workers/languages/python/packages/) |
| `ruamel.yaml` | **Pyodide 已列包，但版本约束冲突需处理** | 列表本次为 `ruamel.yaml 0.19.1`；本项目要求 `>=0.18.6,<0.19`，不能声称现约束与该包列表版本匹配。纯 Python 安装路径及全部运行时行为仍待验证。[Pyodide 包列表](https://pyodide.org/en/stable/usage/packages-in-pyodide.html)、本项目 `pyproject.toml` |
| `tomlkit` | **包级未知** | 本次 Pyodide 包列表未检索到；不能据此判定不可用，因为 Workers 还支持纯 Python/PyEmscripten PyPI 包。未找到 Cloudflare 对 tomlkit 的明确测试/支持结论。[Workers 包规则](https://developers.cloudflare.com/workers/languages/python/packages/) |
| `gevent` | **包级未知；其运行前提受明确平台限制** | 本次 Pyodide 列表未检索到；Cloudflare 明确 `threading`/`multiprocessing` 可导入但不具功能。不能从“gevent 是 Python 包”推断 greenlet、协作 socket 或 Hub 线程池可用。[Python 标准库限制](https://developers.cloudflare.com/workers/languages/python/stdlib/) |
| `pysqlite3-binary` | **包级未知；不能作为持久数据库文件方案** | 本次 Pyodide 列表未检索到 `pysqlite`；即便能导入，Python Workers 文件仍是隔离内临时文件。D1/DO 的 SQL 通过绑定 API，不是该二进制包的替代名称。[标准库与文件系统](https://developers.cloudflare.com/workers/languages/python/stdlib/)、[D1 Binding](https://developers.cloudflare.com/d1/worker-api/d1-database/) |
| `paramiko` | **包级未知** | 本次 Pyodide 列表未检索到；未找到 Cloudflare 对 Paramiko、其 socket transport、SSH client 或密钥文件用法的兼容承诺。Workers 的 JS `connect()` 存在不等于 Paramiko 的 Python socket API 可用。[TCP Sockets](https://developers.cloudflare.com/workers/runtime-apis/tcp-sockets/) |
| `pysocks` | **包级未知** | 本次 Pyodide 列表未检索到；未找到 SOCKS4/5 client、代理链或 Python socket 集成的官方支持结论。不能由 TCP `connect()` 推断 PySocks 可用。同上 |
| `flask` | **支持** | 有专用官方指南及 WSGI adapter。[Flask 指南](https://developers.cloudflare.com/workers/languages/python/packages/flask/) |

**Pyodide 列表核对说明**: 以上包名和版本来自 2026-11-19 读取的 [Packages built in Pyodide](https://pyodide.org/en/stable/usage/packages-in-pyodide.html)。该列表由 Cloudflare 包规则直接引用，故用作补充官方上游依据；不是 Cloudflare 对每个包的 Workers 端到端认证。未检索到只记为“未找到”，不写成“不支持”。

## 4. 出站 HTTP、双向流与平台限制

### 4.1 HTTP(S) 出站与 API 边界

**状态：支持。** Workers 通过 Fetch API 发起异步 HTTP 请求；Python 可 `from workers import fetch` 并 `await fetch(...)`。`fetch` 等异步任务必须在 handler 内执行，不能在 global scope 发起。[Fetch](https://developers.cloudflare.com/workers/runtime-apis/fetch/)、[Python FFI](https://developers.cloudflare.com/workers/languages/python/ffi/)

官方 Python FFI 说明：“if an API is accessible in JavaScript, you can also access it in a Python Worker”，并列出 `Request`、`Response`、`fetch()` 与所有绑定。[FFI](https://developers.cloudflare.com/workers/languages/python/ffi/)

**流式状态：支持，含 Python 示例。**

> “Workers do not need to prepare an entire response body before returning a `Response`.”
>
> “Any data provided through the `ReadableStream` will be streamed to the client as it becomes available.”

官方 Python 示例直接转发 `response.body`，另一个示例通过 `ReadableStream` 和 `asyncio.sleep()` 分块生成响应。`TransformStream` 可在传输中修改 body。[Streams](https://developers.cloudflare.com/workers/runtime-apis/streams/)

`Response` 构造器接受 `ReadableStream`；普通 `ReadableStream` 响应使用 chunked encoding，手工 `Content-Length` 可能被运行时覆盖。[Response](https://developers.cloudflare.com/workers/runtime-apis/response/)

**推断 / 待验证**: 平台提供出站 HTTP 与增量读写的基本件，可支持协议代理核心探索；但官方没有针对本项目 3×3 文本翻译、Responses compact、Images、OAuth 重试、强制上游流式聚合、SSE/NDJSON split UTF-8 与 `[DONE]` 的认证。请求体流式在 WSGI 适配器源码有实现依据，但仍需验证 backpressure、取消、异常后响应状态与首字节前重试语义。

### 4.2 Workers 资源限制（以检索日页面为准）

全部数字来自 [Workers Limits](https://developers.cloudflare.com/workers/platform/limits/)，不是性能预测或费用判断。

| 限制 | Free | Paid / 通用 | 原文要点 |
|---|---:|---:|---|
| 每 HTTP 请求 CPU | 10 ms | 默认 30 s，可配置至 5 min | “CPU time … Waiting on network requests … does not count” |
| 内存 | 128 MB / isolate | 128 MB / isolate | 包含 JS heap 与 Wasm allocations；不是每请求独立 128 MB |
| 子请求 / invocation | 50 | 默认 10,000，可申请更高 | Fetch、R2、KV、D1 等计入；redirect 链也计数 |
| 同时等待响应头的出站连接 | 6 | 6 | 收到 response headers 后不再占这 6 个名额 |
| HTTP wall-clock duration | 无固定硬限制 | 客户端保持连接时可继续处理/子请求/流式响应 | 运行时更新有最长 20 min 的进程 draining grace period；崩溃、资源压力和 shutdown 可更早中断 |
| `waitUntil()` 响应后工作 | — | 最长 30 s | 同一请求所有 `waitUntil` 共享该窗口 |
| 入站请求体 | 依 zone 计划 100 MB / 100 MB / 200 MB / 最高 5 GB | 非 Workers 计划本身决定 | Free/Pro/Business/Enterprise 依页面表格 |
| 响应体 | 无强制大小限制 | 同左 | 仍受内存、断连与流式实现约束 |
| URL / 请求头 / 响应头 | 16 KB / 128 KB / 128 KB | 同左 | 请求/响应头均为 total |
| Worker uncompressed size | 64 MiB | 64 MiB | 启动全局 scope 另有限制 |
| 启动时间 | 1 s | 1 s | 顶层 parse/execute 限制 |
| 环境变量（secrets + text） | 64 / Worker | 128 / Worker | 每个值 5 KB |

官方 Duration 原文：

> “There is no hard limit on duration for HTTP-triggered Workers. As long as the client remains connected, the Worker can continue processing, making subrequests, and streaming a response body.”

同时必须保留其限制条件：客户端断开或响应完成后，未交由 `waitUntil` 的任务可取消；运行时更新 draining 不是每请求固定 20 min 保证。[Duration](https://developers.cloudflare.com/workers/platform/limits/#duration)、[Context `waitUntil`](https://developers.cloudflare.com/workers/runtime-apis/context/#waituntil)

**推断 / 待验证**: CPU 不计网络等待有利于转发型负载，但 JSON/协议翻译、脱敏与流编码的 CPU 是否适合 Free 10 ms，不能凭文档预测。多上游重试、模型发现、D1 写入与日志记录会消耗子请求额度；Images 大请求还需核对入站 body 与 DO/Container 出站限制。

## 5. 文件系统、动态 import/exec、环境变量与 Secrets

### 5.1 文件与打包资源

**状态：临时文件支持；持久本地文件不支持。**

> “Python Workers have access to an ephemeral, in-memory filesystem.”
>
> “all data is lost when the Worker isolate is destroyed. The filesystem is not shared between different isolate instances.”

可用 `open()` / `pathlib.Path` 做临时文件操作；持久数据文档指向 KV、R2 或 Durable Objects。[Python Standard Library / In-memory filesystem](https://developers.cloudflare.com/workers/languages/python/stdlib/#in-memory-filesystem)

官方 Python 示例能读取与 Worker 一同 bundle 的 `Path(__file__).parent / "file.html"`；这证明随包资源可读，不证明项目目录可写或跨 isolate 共享。[Python Examples: Read bundled asset files](https://developers.cloudflare.com/workers/languages/python/examples/#read-bundled-asset-files-in-your-worker)

作为 Workers VFS 的补充说明：bundle 文件只读，临时文件不持久且受内存限制；不支持 `fs.watch`/`fs.watchFile`。该页是 Node `fs` API 文档，不能直接当作 Python `open()` 每请求隔离语义的证明；Python 的明确语义以 stdlib 页为准。[Workers VFS](https://developers.cloudflare.com/workers/runtime-apis/nodejs/fs/)

**推断 / 待验证**: 当前 YAML 原子写回、`logs/*.log`、SQLite DB 文件、OAuth 本地认证文件、本机 Agent 配置及按文件 mtime/签名热更新均缺少原运行环境。打包只读配置可成为一种输入，但不能等同于管理界面可写配置。

### 5.2 多文件 import、动态 import 与 exec

**官方明确支持多文件 Python module**：入口可 `from hello import hello`，构建/部署时依赖被收集。[Python Basics: Modules](https://developers.cloudflare.com/workers/languages/python/basics/#modules)、[包管理](https://developers.cloudflare.com/workers/languages/python/packages/)

**动态 import / exec 的完整边界：官方未给出本项目所需的明确结论，标为未知。** 已核实的相邻事实：

- Python 运行时未被官方声明禁用一般 Python 动态编译；Pyodide 官方 API 提供 `eval_code()` 和 `eval_code_async()`，用于执行字符串形式的 Python 源码。[Pyodide `pyodide.code`](https://pyodide.org/en/stable/usage/api/python-api/code.html)
- Workers Python 支持多文件 import 不等于支持从任意运行时 URL/外部目录加载模块。
- 兼容性标志页说明 Python Workers 当前继续使用 legacy module registry；该段是 registry 行为说明，不是 Python 任意文件动态加载许可。[Compatibility Flags: New module registry](https://developers.cloudflare.com/workers/configuration/compatibility-flags/#new-module-registry)
- `.pth` 自 2026-05-26 起可处理，可声明式扩展 `sys.path` 或注册 import hooks；该能力也不是外部不可信代码加载许可。[Compatibility Flags: Process `.pth` files](https://developers.cloudflare.com/workers/configuration/compatibility-flags/#process-pth-files-for-python-workers)

**推断 / 待验证**: 当前 `hooks/` 相对路径约束、文件导出 `Hook`、运行时按文件签名重载和弱引用缓存必须作为独立适配项验证。不能仅因 Pyodide 有 `eval_code` 就宣布现有 Hook 热加载可行；也不能仅因 bundle 静态模块可导入就宣布动态 Hook 不可行。需在固定打包模型下验证模块查找、重新导入、缓存失效、代码来源及安全边界。

### 5.3 环境变量与 Secrets

**状态：支持。** Python 通过 `self.env.NAME` 访问环境变量、Secrets 和绑定。[Python Basics: env](https://developers.cloudflare.com/workers/languages/python/basics/#the-env-attribute)

普通环境变量是 text/JSON binding，未加密；Secrets 是加密 text binding，定义后其值不在 CLI 或 dashboard 显示。对 Worker 代码而言两者访问方式相同。[Environment Variables](https://developers.cloudflare.com/workers/configuration/environment-variables/)、[Secrets](https://developers.cloudflare.com/workers/configuration/secrets/)

**推断 / 待验证**: 它们能承载简单配置和凭据，但不是现有可写 YAML、OAuth 多账号 JSON 文件或 SQLite 的等价物。每变量 5 KB、每 Worker 数量限制见第 4.2 节；Secrets Store 官方页明确标 **beta**，不应与每 Worker Secrets 混为同一状态。[Secrets](https://developers.cloudflare.com/workers/configuration/secrets/)

## 6. TCP、SSH、原生线程/进程与请求外后台工作

### 6.1 TCP / SSH / 私网目标

**状态：JS TCP socket API 支持但有明显限制；Paramiko/SSH 客户端可用性未知。**

Workers `cloudflare:sockets` 的 `connect()` 返回 readable/writable streams，支持 TLS/StartTLS。官方承认 SSH 等应用层协议需要 TCP socket，但示例与 API 是 JavaScript Runtime API，不是 Paramiko 支持声明。[TCP Sockets](https://developers.cloudflare.com/workers/runtime-apis/tcp-sockets/)

> “Many application-layer protocols … including SSH … require an underlying TCP socket API in order to work.”

明确限制：

- 不得在 global scope 创建并跨请求共享 socket；应在 handler 中创建。
- 禁止连接 Cloudflare IP ranges；`localhost` 与 private network IPs 也属于文档列出的 disallowed address 示例。
- 默认禁止 outbound port 25；TCP loopback 回自身 Worker 不支持。
- socket 计入同时等待连接的限制；来自其他来源的 inbound TCP 文档仍写 coming soon。

**推断 / 待验证**: 不能因为 SSH 出现在举例中，就声称现有 Paramiko SSH Agent 配置可迁移。需要验证 Python socket 语义、Paramiko transport、算法依赖和私网目标约束；现有 PySocks 代理链也无官方兼容结论。

### 6.2 原生线程、进程与 gevent

**状态：不支持原生线程/多进程实际执行。**

Cloudflare stdlib 页原文：

> “The following modules can be imported, but are not functional due to the limitations of the WebAssembly VM. `multiprocessing`, `threading`”

[Python Standard Library](https://developers.cloudflare.com/workers/languages/python/stdlib/)

Pyodide 官方 FAQ 更明确：

> “No, fork and pthreads do not work in Pyodide … Attempts to use `threading`, `multiprocessing`, or `subprocess` will raise a `RuntimeError`.”

[Pyodide FAQ](https://pyodide.org/en/stable/usage/faq.html#can-i-use-threading-multiprocessing-subprocess)

Workers isolate 是 single-threaded event loop，可并发处理多个请求，但请求不保证落在同一实例；官方建议不要依赖/修改 global state。[How Workers Works](https://developers.cloudflare.com/workers/reference/how-workers-works/#distributed-execution)

**推断 / 待验证**: 现有 gevent monkey patch、greenlet 调度、Hub native threadpool 和 `run_blocking()` 不能直接作为 Workers 的并发实现。可用 asyncio/JS Promise/Streams 的方向需实证，不等于能透明替换 gevent。

### 6.3 请求外后台工作、Queues 与 Cron

**状态：受控支持，不支持任意脱离生命周期的后台线程。**

- `ctx.waitUntil(promise)` 可延长 Worker 生命周期；HTTP 响应结束或客户端断开后最多 30 s。未 `await`、未返回且未传给 `waitUntil` 的异步工作可被取消。[Context](https://developers.cloudflare.com/workers/runtime-apis/context/#waituntil)
- 对超过该窗口的工作，官方文档指向 Queues；Queues 支持 offload、batch、retry、delay 和 dead-letter queues，Free/Paid 均可用。[Queues](https://developers.cloudflare.com/queues/)、[Queues 在 Context 中的说明](https://developers.cloudflare.com/workers/runtime-apis/context/#waituntil)
- Cron Triggers 通过 `scheduled()` handler 定期执行，UTC 时间；Free 每账号 5 个、Paid 250 个；单次 Cron wall time 15 min。[Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/)、[Limits](https://developers.cloudflare.com/workers/platform/limits/)

**推断 / 待验证**: 现有有界 native queue + 单写入线程、请求结束后的日志落盘以及临时 OAuth/缓存维护，需要改成请求内等待、`waitUntil`、Queues 或定时任务之一；这些服务的原子性、重复投递和跨表一致性需另行设计，不能假设与现有线程模型相同。

## 7. SQLite、状态协调与存储替代关系

### 7.1 存储产品语义对照

| 服务 | 状态与官方事实 | 与现有 SQLite / 状态的关系（推断） |
|---|---|---|
| **D1** | 支持 Free/Paid；managed serverless database，使用 SQLite SQL semantics，通过 Worker binding/HTTP API 访问。[D1](https://developers.cloudflare.com/d1/) | 可承载关系型用户、Key、映射、统计的数据访问重写；不是打开现有 DB 文件，也不复用 Python DB-API connection |
| **Durable Objects (DO)** | 支持 Free/Paid；每 Object 全球唯一、单实例协调、私有强一致存储。SQLite storage `sql.exec` 已从 beta 转为 **GA**。[DO](https://developers.cloudflare.com/durable-objects/) | 可协调 Provider/Auth Group/OAuth 状态、Session 或 sticky 状态；需要按一致性边界划分 Object，非全局共享进程内存 |
| **KV** | 支持；最终一致，写入在其他地点可能需 60 s 或更久才可见；不适合原子操作或同事务读写。[How KV Works](https://developers.cloudflare.com/kv/concepts/how-kv-works/#consistency) | 可用于允许最终一致的高读配置/缓存；不能直接替代需要立即可见和原子更新的权限、配额、Session 或凭据状态 |
| **R2** | 支持对象存储；put/delete 强一致，body 可按 `ReadableStream` 获取。[R2 Workers API](https://developers.cloudflare.com/r2/api/workers/workers-api-reference/) | 可承载配置快照、导出文件、认证材料或大对象；不是本地文件系统/事务数据库。敏感材料还需单独设计加密与访问控制 |
| **Python 临时文件** | 支持但 isolate 销毁即丢失、跨 isolate 不共享。[Stdlib](https://developers.cloudflare.com/workers/languages/python/stdlib/) | 只能临时计算，不可作为现有 `data/requests.db` 或配置写回的持久层 |

Cloudflare 官方存储选择页将 D1 定位为关系数据，DO 定位为 global coordination/strongly consistent transactional storage，KV 为配置/Session 类高读但不要求立即可见的数据，R2 为对象存储。[Storage Options](https://developers.cloudflare.com/workers/platform/storage-options/)

### 7.2 事务和一致性细节

- **D1 `batch()`** 是 SQL transaction： statements 顺序、非并发执行，任一失败会中止并回滚整个 sequence。普通 D1 operate in auto-commit。[D1 `batch()`](https://developers.cloudflare.com/d1/worker-api/d1-database/#batch)
- **D1 Sessions** 维持同一 session 查询间的 sequential consistency；`first-primary` 可使首查询读取最新 primary。只有使用 Sessions API 才启用 read replication，否则查询走 primary。[D1 `withSession()`](https://developers.cloudflare.com/d1/worker-api/d1-database/#withsession)
- **DO storage** 官方明确为 transactional、strongly consistent，且存储私有于该 Object；SQLite 后端每个对象有自己的 DB。[DO Storage](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/)、[Access DO Storage](https://developers.cloudflare.com/durable-objects/best-practices/access-durable-objects-storage/)
- **DO 并发并非简单互斥锁**：官方存储选择页提醒 execution single-threaded，但多个 request events 仍可能不按到达顺序处理；需要按具体事务/input gate 语义设计。[Storage Options: DO](https://developers.cloudflare.com/workers/platform/storage-options/#durable-objects)
- **D1 限制**：检索日文档列最大数据库 10 GB Paid / 500 MB Free、单查询最长 30 s、结果最大 50 MB、每 Worker invocation 查询 Paid 1,000 / Free 50；`batch()` 按一个 query 计入该 invocation 限额。[D1 Limits](https://developers.cloudflare.com/d1/platform/limits/)
- **DO SQLite 限制**：每 Object 10 GB，单 row/string/BLOB 最大 8 MB，SQL statement 最大 100 KB，bound parameters 最大 100。[DO Limits](https://developers.cloudflare.com/durable-objects/platform/limits/)

**推断 / 待验证**: 当前“请求日志 + 日聚合 + API Key 用量同事务”可映射到一次 D1 `batch()` 的候选，但这只是能力对应，不是完整迁移结论。需验证 SQL 方言、迁移、异步仓储接口、统计正确性、查询量与失败重试。跨 D1、R2、KV、Queues 的组合写入没有被上述文档承诺为统一事务。

## 8. Workers Static Assets 与现有 Vue 构建产物

**状态：支持；官方有 Vue 指南。**

Workers 可将 HTML、CSS、images 和其他文件作为 Worker version 的 static assets 上传，并由 Cloudflare 缓存和提供给浏览器。Asset manifest 将请求路径映射到上传文件。[Static Assets](https://developers.cloudflare.com/workers/static-assets/)

官方 Vue 指南以 Vue + Vite + Workers Assets 为架构，支持 SPA fallback，并说明 client build output 可作为资产目录来源。[Vue Guide](https://developers.cloudflare.com/workers/framework-guides/web-apps/vue/)

路由能力：

- 默认精确匹配静态文件时不调用 Worker；未命中时调用 Worker，若无 Worker 则 404。
- `single-page-application` 可对未命中路径返回 `index.html`。
- `run_worker_first` 可让所有或指定路径先进入 Worker；assets binding 可从 Worker 内获取资源。
- Flask 官方页面也明确“any static frontend”可与 Flask backend 配合，通过 Static Assets 承载。[Flask: Serve a frontend](https://developers.cloudflare.com/workers/languages/python/packages/flask/#serve-a-frontend)

限制与版本注意：

- 每 Worker version 文件数 Free 20,000 / Paid 100,000，单文件 25 MiB。[Limits: Static Assets](https://developers.cloudflare.com/workers/platform/limits/#static-assets)
- 带 `Range` 的静态资源请求当前返回全文件 `200 OK`，不是默认 `206 Partial Content`。[Assets Limitations](https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/)
- 部署传播期间 HTML 与 assets 可命中不同 version，官方有 version-skew 说明。[Static Assets: How it works](https://developers.cloudflare.com/workers/static-assets/#how-it-works)

**推断 / 待验证**: 现有 Vue 的 HTML/JS/CSS/manifest 静态产物具备托管基础，但 Flask 当前 bootstrap 注入、HTML/manifest 重校验、hash 资源长期缓存及 API 优先顺序不会因上传目录自动保留。需验证动态 bootstrap 是否改由 Worker 生成、静态 HTML 是否仍合适、缓存头和登录/管理 API 的路由优先级。

## 9. AI Gateway 与 Containers：仅在相关边界内考虑

### 9.1 AI Gateway

**状态：支持所有计划；与上游 AI 路由/观测相关，不是通用 Python 运行时。**

AI Gateway 提供 analytics/logging、caching、rate limiting、retries、model fallback、routing，支持 OpenAI、Anthropic 等 Provider。[AI Gateway](https://developers.cloudflare.com/ai-gateway/)

Custom Providers 可连接任何有 HTTPS API endpoint 的 AI Provider，支持 provider-specific endpoint 的任意路径/原生 body，及 `/compat/chat/completions` 的 OpenAI-compatible 统一入口。[Custom Providers](https://developers.cloudflare.com/ai-gateway/configuration/custom-providers/)

**边界**: 官方文档没有说明 AI Gateway 能执行本项目的 Flask 控制平面、任意 Chat/Responses/Claude Messages 3×3 转换、Hooks、OAuth 账号文件、SQLite 或 SSH Agent 管理。因此它只能作为上游 AI 网关能力的可能选项，不能替代整体应用托管；具体协议映射、流式行为和路由语义需另查对应功能文档。

### 9.2 Containers

**状态：支持，Workers Paid 可用；当前官方 Containers 页面未标 beta。**

Containers 可运行任何语言/运行时的镜像，适用于需要 parallel CPU cores、更多 memory/disk、完整 filesystem、Linux-like environment 或既有 container image 的应用。[Containers](https://developers.cloudflare.com/containers/)

这使其成为保留 CPython、gevent、Paramiko 等原生依赖的**可能路线**，而非 Python Workers 兼容性的证明。明确边界：

- Container 只能经其 Durable Object 访问，Worker 再路由到该 DO；外部终端用户不能直接对 Container 发起 non-HTTP TCP/UDP。[Container Lifecycle](https://developers.cloudflare.com/containers/concepts/architecture/)
- 实例在 Firecracker microVM 内运行 Linux container；可由 inactivity timeout、rollout、process exit 等停止。[Container Runtime / Shutdown](https://developers.cloudflare.com/containers/concepts/architecture/#container-runtime)
- 默认所有 disk 都是 ephemeral；睡眠后重启使用镜像的新 disk。特定 scheduling policy 支持 snapshot，也可用 R2/FUSE 等方式另做持久化。[Disk Persistence](https://developers.cloudflare.com/containers/faq/#is-disk-persistent-what-happens-to-my-disk-when-my-container-sleeps)
- 允许 Internet 时可配置出站规则；其他端口可直接联网的细节见官方 outbound 文档。[Containers Outbound Traffic](https://developers.cloudflare.com/containers/configuration/outbound-traffic/)

**推断 / 待验证**: Containers 可减少 Python 原生运行时替换范围，但本地 SQLite/YAML 仍需处理重启、实例位置变化、持久性和一致性；它不是一个永久单进程 VM。是否支持现有完整数据平面/SSH/后台线程，应按具体镜像与网络行为验证，不在本调研中声称已兼容。

## 10. 关键阻碍与可探索方向（分析，不是官方承诺）

### 主要阻碍

1. **运行模型不兼容假设**: gevent、原生线程、子进程、原生 socket 与单进程内存状态是现有请求/仓储/日志设计基础；Pyodide + V8 isolate 不提供同样环境。[Stdlib](https://developers.cloudflare.com/workers/languages/python/stdlib/)、[Pyodide FAQ](https://pyodide.org/en/stable/usage/faq.html)
2. **本地持久化与动态配置**: YAML 原子写回、SQLite 文件、OAuth 文件、日志文件及按文件热更新 Hooks 都不能依赖本地文件长期保存或跨 isolate 共享。[Filesystem](https://developers.cloudflare.com/workers/languages/python/stdlib/#in-memory-filesystem)
3. **SSH / 代理链路未证实**: Workers 有 TCP API，但 Paramiko、PySocks、私网 SSH 和任意代理配置没有包级官方结论。[TCP Sockets](https://developers.cloudflare.com/workers/runtime-apis/tcp-sockets/)
4. **状态一致性重构**: Session、Provider/Auth Group 运行态、配额与统计需要按 D1/DO/KV/R2 的不同一致性边界重新实现，不能只更换连接字符串。[Storage Options](https://developers.cloudflare.com/workers/platform/storage-options/)
5. **流式协议的正确性待实证**: 平台支持流，但现有提交边界、首字节前重试、取消、流内错误和多协议终止条件需要专门验证。[Streams](https://developers.cloudflare.com/workers/runtime-apis/streams/)、[Limits: Duration](https://developers.cloudflare.com/workers/platform/limits/#duration)
6. **部署规模与额度**: 128 MB isolate 内存、Worker 64 MiB、启动 1 s、CPU/子请求/变量额度对依赖与翻译路径形成硬约束。[Limits](https://developers.cloudflare.com/workers/platform/limits/)

### 有官方能力基础、可进入后续验证的方向

- **Python Workers 原生 HTTP 代理核心**: `WorkerEntrypoint`/Flask WSGI + Python FFI `fetch` + Streams；优先验证无 SSH、无本地持久化的协议翻译子集。[Python 主页](https://developers.cloudflare.com/workers/languages/python/)、[Streams](https://developers.cloudflare.com/workers/runtime-apis/streams/)
- **Vue 前端与同源 Worker 后端**: Static Assets 承载构建产物，Worker 处理 `/api/*` 与动态 bootstrap；按现有缓存及路由规则验证。[Vue](https://developers.cloudflare.com/workers/framework-guides/web-apps/vue/)、[Flask](https://developers.cloudflare.com/workers/languages/python/packages/flask/)
- **D1 + DO 的持久化/协调候选**: D1 承载关系数据和事务 batch，DO 承载需要唯一协调的状态；KV/R2 仅用于符合其一致性和对象语义的部分。[D1](https://developers.cloudflare.com/d1/worker-api/d1-database/)、[DO](https://developers.cloudflare.com/durable-objects/)
- **保留 Linux 运行时的 Containers 路线**: 若原生库、SSH 或既有 Flask/gevent 不可替换，可作为独立适配方向评估，但必须同时解决临时磁盘、生命周期与状态外置。[Containers](https://developers.cloudflare.com/containers/)
- **AI Gateway 作为上游可选集成**: 仅在需要其 Provider 路由、观测、缓存/限流等能力时评估，不将其视为应用运行平台。[AI Gateway](https://developers.cloudflare.com/ai-gateway/)

## 11. 明确未证实事项与后续证据需求

| 待验证项 | 当前缺口 | 所需证据（后续，不在本次执行） |
|---|---|---|
| Flask + 本项目完整流管线 | 官方只有通用 WSGI/Streams 能力和 adapter 源码 | 固定 runtime/兼容日期，实测 SSE/NDJSON 分块、首字节前重试、断连、异常、背压与流关闭 |
| `requests` / `httpx` | 无 Cloudflare 对本项目传输方式的支持结论 | 在目标 runtime 测 sync/async、stream、timeout、TLS、redirect、自定义 transport；不要以能 import 代替 |
| `gevent` | 包级未知且平台线程模型不同 | 实测 import/monkey patch/greenlet/socket；若失败记录具体边界，不假设存在替代补丁 |
| `paramiko` / `pysocks` | 无包级 SSH/SOCKS 官方结论 | 分别验证 TCP transport、认证、SOCKS 握手、目标地址限制；与 JS `connect()` 区分 |
| `tomlkit` / `ruamel.yaml` 指定版本 | 无 tomlkit 包级结论；Pyodide 的 ruamel 版本超出本项目上限 | 以实际 lockfile 验证可安装、序列化 round-trip 与配置文件行为 |
| Hook 动态加载/热更新 | 多文件 import 已支持，但运行时外部代码来源、重载与缓存语义未明 | 验证打包模块集合、import cache 清除、签名变更、跨 isolate 一致性与安全约束 |
| D1/DO 替代仓储 | SQL/事务 API 已证实，现有 SQL/调用模型未认证 | 验证 schema、事务、读写一致性、错误/重试、统计原子性与数据迁移 |
| Containers 完整兼容 | Linux 运行时成立，但项目镜像/网络/磁盘未测试 | 验证 Flask/gevent/SSH/原生依赖、持久化、重启及多实例行为 |
| 性能、容量与费用 | 本调研未做基准或计费方案，官方限制不是性能/价格结论 | 后续按明确部署范围、真实负载、目标和当时官方计费资料单独评估 |

## Caveats / Not Found

- 当前工具环境未提供 `mcp__plugin_cloudflare_cloudflare__docs` 或 Exa 工具，因此使用 `curl` 直接读取 `developers.cloudflare.com` 官方 Markdown 页面；主要 Python 主页已优先读取并引用。
- 2024 年 Cloudflare Python Workers 介绍博客仍描述 open beta，属于历史状态；本报告采用 2026 年官方“generally available”表述，未把旧博客当作当前状态。[历史介绍](https://blog.cloudflare.com/python-workers/)
- 除明确标为 beta 的 Secrets Store 外，本报告选用的核心服务页面未自行补写 beta/GA 标签。Containers、Static Assets、D1 等的“支持”表示官方当前文档列明能力。
- 未找到 Cloudflare 对 `requests`、`httpx`、`tomlkit`、`gevent`、`pysqlite3-binary`、`paramiko`、`pysocks` 在本项目用法下的包级保证；均未写成确定支持或确定不支持。
- 未检索第三方兼容性博客、未根据 Wasm 名称猜测 C/Rust 扩展可运行、未执行包安装或云端验证。
- 本报告只覆盖平台能力证据，不替代任务内对现有代码架构的逐项清单，也不决定最终部署范围。
