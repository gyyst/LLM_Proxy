# Cloudflare Workers 网关部署调研结论与候选设计

- **日期**: 2026-11-19
- **范围**: Cloudflare 实现现有网关数据面、管理面、OAuth、权限配额、模型映射、统计、日志与 Hook；不迁移 Agent 本机/SSH 配置功能。
- **性质**: 基于代码与官方资料的方案建议，不是已经通过 Workers 运行验证的实现。

## 1. 结论

可以采用 **Python Workers + 平台状态服务**路线，但应为“保留协议核心、替换运行时与状态架构”的迁移，不是给 `main.py` 增加一个启动参数即可部署。

官方资料已经支持以下基础能力：

- Python Worker 的 `WorkerEntrypoint` 与 `python_workers` 兼容标志：[Python Workers](https://developers.cloudflare.com/workers/languages/python/)。
- Flask/WSGI 适配入口：[Flask](https://developers.cloudflare.com/workers/languages/python/packages/flask/)。
- Python FFI 调用 `fetch`、Streams、D1、Durable Objects 等绑定：[FFI](https://developers.cloudflare.com/workers/languages/python/ffi/)、[Python Examples](https://developers.cloudflare.com/workers/languages/python/examples/)。
- Python Durable Object 与 SQLite storage：[Python DO 支持](https://developers.cloudflare.com/changelog/post/2025-05-14-python-worker-durable-object/)。
- Static Assets 托管 Vue 构建产物：[Vue](https://developers.cloudflare.com/workers/framework-guides/web-apps/vue/)。

现有协议转换器、字节 decoder 和响应 encoder 在源码层面不依赖 Flask、gevent、requests 或 SQLite，是最优先复用的部分。完整代理链路、OAuth、配额、状态和 Hook 并不自动复用；证据见 `current-runtime-inventory.md:30-46` 与 `cloudflare-platform-capabilities.md:75-145`。

## 2. 路径比较

| 路径 | 保留现有能力的方式 | 代价与限制 | 结论 |
|---|---|---|---|
| **A. Python Workers 原生化（推荐）** | 保留 Python translators / proxy_core，重写传输、配置、存储、调度和 Worker 入口 | 多协议流、动态 Hook、跨服务一致性均需适配和实证；有额外存储调用 | 与目标最匹配；按阶段验证，验证通过才继续迁移 |
| **B. TypeScript 网关重写** | 新实现全部网关逻辑，平台 API 更直接 | 丢失现有 Python 转换器、测试和业务语义复用，双维护或重复实现成本最高 | 不作为首选；只有 Python 关键路径验证失败时评估 |
| **C. Workers + Containers** | 以 Linux 容器保留 Flask/gevent 与原生依赖，外部持久化状态 | 容器有 DO 访问、生命周期和默认临时磁盘约束；并非永久单机 VM | 作为保底对照，不在第一期扩大运行拓扑 |
| **D. Flask 只做 WSGI 包装** | 尽量保留 `Application` | gevent、SQLite 文件、原生线程、动态本地文件和进程内状态没有同等运行基础 | 不满足完整网关目标，排除 |

AI Gateway 可以提供部分上游 AI 路由、日志或缓存能力，但不能替代本项目任意协议转换、OAuth、Hook 与管理面；不纳入必要架构。[AI Gateway](https://developers.cloudflare.com/ai-gateway/)、[Custom Providers](https://developers.cloudflare.com/ai-gateway/configuration/custom-providers/)。

## 3. 候选组件边界

| 现有职责 | Workers 候选实现 | 备注 |
|---|---|---|
| Flask 路由、Controller | 先验证 `workers.wsgi` 适配现有路由；不能承载同步阻塞 I/O 时拆分原生 `WorkerEntrypoint` | 不执行 gevent monkey patch、不保留 ListenerRuntime |
| 协议转换、流事件 | 复用 `src/translators` 与 `src/proxy_core` 数据契约 | 使用共享 golden cases 对现有实现和新实现做等价测试 |
| requests HTTP executor | Python FFI `fetch` + Request/Response Streams | 统一映射状态、headers、lazy bytes、错误、关闭和取消；禁止假设 requests 流接口直接可用 |
| YAML 与 Provider 管理 | D1 存正式配置与元数据；版本号/etag 管理读改写 | 可选择 R2 保存大 YAML/导入导出快照；不把 Secrets 当管理数据库 |
| 用户、API Key、映射、日志与统计 | D1 异步仓储，保留原业务字段和查询语义 | 权限、配额和限额写路径使用事务；会话读取使用 D1 Sessions 或主库 |
| API Key 并发/周期额度 | 按 Key ID 分片 Durable Object，原子预占/释放；D1 记录和查询 | 跨实例不能依赖实例字典；崩溃回收与幂等需原型验证 |
| Auth Group | 按组名 Durable Object 保存 inflight、轮转/粘滞游标、冷却和禁用 | 上游长请求中租约必须可恢复；不可用内存锁代替 |
| 模型映射 | 按映射 ID Durable Object 管理当前目标与冷却 | 不采用一个全局 DO 处理所有请求 |
| OAuth 文件与账号状态 | 按 OAuth 账号 Durable Object 协调刷新、启停、配额和最近成功状态 | 认证材料仅通过受控 RPC/绑定访问，不能进入静态资源或普通日志 |
| 管理与 PKCE Session | 有 TTL、不可伪造标识的持久 Session；管理会话和 OAuth state 分开存储 | 不使用进程字典； cookie、CSRF 与现有鉴权语义不变 |
| Codex 推理缓存 | 按 scope 选择 Durable Object 或带版本过期策略的缓存 | 先证明允许降级；不能把进程 LRU 当跨实例共享缓存 |
| 动态 Provider Hook | 受控的部署期 Hook 包 + D1 中的启用映射；代码版本经部署发布 | 保持 Hook 接口，取消任意运行时源码热写作为首选；exec 兼容性仍是阻断性验证项 |
| LLM JSONL 与导出 | R2 对象 + D1 元数据/队列，按请求 ID 幂等写入 | 不承诺本地滚动文件语义；记录失败不能改变客户端响应 |
| 日志 | Workers Logs + D1 请求明细/统计 | 日志采集与业务事务解耦 |
| Codex 配额周期刷新 | Cron 分发任务，账号 DO 防重入并串行刷新；必要时 Queues | 不再运行无限 greenlet；失败和重复投递须可重试 |
| Vue 与 bootstrap | Static Assets + 同源 Worker API，Worker 处理动态 bootstrap 与 HTML 策略 | `/agents` 导航、API 与 Agent capability 在 Workers 版移除 |
| server.host/port | 无对应功能 | 由 Workers 域名、路由、环境变量/部署配置替代 |

官方一致性依据：[D1 batch](https://developers.cloudflare.com/d1/worker-api/d1-database/#batch)、[D1 Sessions](https://developers.cloudflare.com/d1/worker-api/d1-database/#withsession)、[DO SQLite Storage](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/)、[KV 一致性](https://developers.cloudflare.com/kv/concepts/how-kv-works/#consistency)、[R2 Workers API](https://developers.cloudflare.com/r2/api/workers/workers-api-reference/)。

## 4. 一致性与数据流

### 请求准入

1. 解析下游 API Key，查询 Key 与用户权限目录。
2. 在 Key 对应 DO 中原子预占并发名额与周期配额。
3. 读取 Provider/Auth Group/OAuth/映射路由快照。
4. 失败释放预占；完成后结算实际 usage。
5. 日志、日统计和 Key 用量在 D1 的一个事务 batch 内写入，跨 DO 的预占/结算通过请求 ID 幂等关联。

需要实测的边界：D1 与 DO 不是一个分布式事务。不能宣称“预占成功、D1 写入失败、Worker 崩溃”天然一致；需要租约/过期恢复、幂等结算与对账策略。相关现有事务证据：`current-runtime-inventory.md:48-74`。

### 流式代理

1. 读取请求、鉴权与模型路由。
2. 应用 header hook、请求翻译、request guard。
3. 以 `fetch` 发起上游，保留流 body。
4. 增量 decode → 原始 usage 采集 → translate → response guard → encode。
5. 预取首个非空目标协议字节后才提交下游响应；之前允许重试/切换目标。
6. 提交后错误仅编码为当前下游协议流内错误，不做透明重试。
7. 客户端取消传播至上游 body cancel，并结算已采集 usage。

现有实现以 Flask `Response` 和 requests `iter_content()` 为中心；新边界必须复现上述语义，不能只做 `return fetch(...)`。代码锚点：`current-runtime-inventory.md:40-46`；平台流能力：[Streams](https://developers.cloudflare.com/workers/runtime-apis/streams/)。

### 配置与 Hook 发布

- 管理面修改业务配置，递增版本；新请求读取新版本，旧请求继续使用已开始的快照。
- Hook 包默认随部署发布，启用配置与 Hook 版本关联；隔离内缓存以 `(package_version, digest)` 失效。
- 目标接口保持 `header_hook`、`request_guard`、`response_guard`、`fetch_models` 和 `None` 保留原 payload 的约定。
- **待决策触发条件**: 若产品要求管理员粘贴任意 Python 并立即全实例生效，部署期包模型不满足，必须另做安全执行模型评估。当前首选是不支持任意代码热写，而不是未经验证复用 `exec(compile(...))`。现有动态加载证据：`current-runtime-inventory.md:59-64`。

## 5. 凭据与前端

- OAuth token 与 Provider 密钥不能进入前端 bundle、Static Assets、日志或公开导出接口。
- Durable Object 数据官方提供静态和传输加密：[DO Data Security](https://developers.cloudflare.com/durable-objects/reference/data-security/)。是否再加应用层密钥封装属于后续安全设计，不用 Workers Secrets 的显示限制推导所有 DO 数据的管理可见性。
- Workers Secrets 可用于部署级密钥、管理初始化凭据等少量静态值：[Secrets](https://developers.cloudflare.com/workers/configuration/secrets/)。
- Static Assets 默认缓存头不是 hash 资源长期缓存；`_headers` 可覆盖静态响应，但 Worker 生成的 HTML/API 需自己设置 `no-store`：[Asset Headers](https://developers.cloudflare.com/workers/static-assets/headers/)。
- 保留 `/api/ui/bootstrap` 的动态认证状态与功能开关；Workers 版的 Agent capability 和相关导航移除。

## 6. 验证关卡

### Gate 0：部署工具与最小运行时

- 验证 `cf` 管理项目与 Python 依赖 vendoring 的组合，不直接照搬 `pywrangler` 命令、不新增 Wrangler 配置。
- 当前官方 `cf` 项目配置文档未明确 Python 包构建流程；Python 官方文档明确 `pywrangler` 是 Wrangler 包装器。需要以实际 CLI 干跑验证，不得凭工具名猜测。
- 验证固定兼容日期、Python 入口、第三方包、Worker 包体与冷启动。

### Gate 1：纯协议核与传输

- 对九个 translator 执行共享请求/非流式/流式 golden cases。
- 验证 split UTF-8、`[DONE]`、工具调用、多模态、usage 和协议终止。
- 验证上游 lazy bytes、下游背压、取消和关闭。

### Gate 2：真实流语义与 Hook

- 首字节前 transport 失败重试、首字节后流内错误、终止后 framing error、客户端取消。
- 非流式下游 + 强制上游流式聚合与不完整 EOF。
- Hook 打包/import/exec、版本失效、异常隔离与跨实例一致性。

### Gate 3：共享状态

- 多请求并发下 Key 并发/额度、Auth Entry 租约、映射粘滞切换、OAuth 单账号刷新。
- D1 事务回滚、跨 DO/D1 崩溃恢复、重复完成回调、管理 Session 跨实例。
- Cron 重复执行、限频与 OAuth 配额状态恢复。

### Gate 4：全功能回归与上线

- Chat、Responses、compact、Messages、Images 与 `/v1/models`。
- Provider、Auth Group、用户、Key、映射、OAuth、设置、日志、统计、导入导出和 LLM 记录管理 API。
- Vue 构建、页面鉴权、bootstrap、缓存与 E2E；`/agents` 在 Workers 版不可见。
- 使用合成上游与独立测试资源；不直接迁移真实生产数据库或账号。

## 7. 风险、回退与验收口径

- **高**: WSGI 同步路由与异步绑定/流的组合；Hook 动态加载；跨存储事务与崩溃恢复。
- **高**: CPU、内存、包体、子请求和 D1/DO 调用量，数值见 `cloudflare-platform-capabilities.md:147-172`；必须实测，不能从平台限制推导实际吞吐。
- **中**: 自定义 HTTP/SOCKS 代理、证书策略、私网目标与平台 fetch 能力不同；应按实际使用情况形成支持矩阵，未知项不默认承诺。
- **中**: 状态服务有固定位置，全球部署不等于状态访问零延迟：[DO Data Location](https://developers.cloudflare.com/durable-objects/reference/data-location/)、[D1 Replication](https://developers.cloudflare.com/d1/best-practices/read-replication/)。
- 任一 Gate 失败时停止扩大迁移范围，保留现有 Flask/gevent 正式运行路径，记录具体不兼容点后再选择 Python 异步适配、缩小平台特性或 Containers 路线。
- “Flask Hello World 可运行”“转发一个 SSE 成功”“页面能打开”均不能作为完整网关验收；须通过上述分层验证和全功能清单。
