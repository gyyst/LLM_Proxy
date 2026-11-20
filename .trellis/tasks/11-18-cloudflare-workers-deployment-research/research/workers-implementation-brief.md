# Workers 实施约束速查

- **检索日期**: 2026-11-19
- **用途**: 为后续原型/实现提供不截断的关键事实。完整官方依据和包级矩阵见 `cloudflare-platform-capabilities.md`，路线与状态设计见 `deployment-options.md`。

## 已确认的平台基础

1. **Python 与 Flask**: Python Worker 使用 Pyodide + V8 isolate，必须启用 `python_workers`；入口为 `WorkerEntrypoint`。Flask/WSGI 有官方适配，但这不包含 gevent `WSGIServer`。[Python](https://developers.cloudflare.com/workers/languages/python/)、[Flask](https://developers.cloudflare.com/workers/languages/python/packages/flask/)。
2. **版本**: 兼容日期 2026-09-08 起新 Python Worker 默认 Python 3.14；版本随兼容日期/标志选择，实施时固定并验证，不能只依赖本机 `>=3.12`。[Python 3.14](https://developers.cloudflare.com/changelog/post/2026-09-08-python-workers-314/)、[How Python Workers Work](https://developers.cloudflare.com/workers/languages/python/how-python-workers-work/)。
3. **包**: 支持纯 Python、PyEmscripten 与 Pyodide 包。能安装/导入不证明网络、线程、文件等功能可用。[Packages](https://developers.cloudflare.com/workers/languages/python/packages/)。
4. **HTTP 与流**: Python FFI 可使用 `fetch`、Request/Response 和 Streams；响应可增量发送。需自行验证 SSE/NDJSON 分块、背压、关闭与取消。[FFI](https://developers.cloudflare.com/workers/languages/python/ffi/)、[Streams](https://developers.cloudflare.com/workers/runtime-apis/streams/)。
5. **线程与进程**: `threading` / `multiprocessing` 可导入但不可用；Pyodide 不支持 fork/pthreads/subprocess。不能以 gevent 协作 socket 或 Hub 原生线程作为 Workers 运行假设。[Stdlib](https://developers.cloudflare.com/workers/languages/python/stdlib/)、[Pyodide FAQ](https://pyodide.org/en/stable/usage/faq.html#can-i-use-threading-multiprocessing-subprocess)。
6. **文件**: Python 文件系统仅隔离内临时存储，isolate 销毁即丢失且不跨实例共享；随包资源可读不代表 YAML/OAuth/DB/Hook 可持久写回。[Stdlib Filesystem](https://developers.cloudflare.com/workers/languages/python/stdlib/#in-memory-filesystem)。
7. **状态**: 多请求不保证同一实例，不能依赖全局内存。[Distributed Execution](https://developers.cloudflare.com/workers/reference/how-workers-works/#distributed-execution)。

## 状态与前端候选

| 能力 | 候选与必须遵守的边界 | 官方来源 |
|---|---|---|
| D1 | 关系数据与统计；`batch()` 为事务；Sessions 提供会话顺序一致；不是现有 SQLite DB 文件连接 | [D1](https://developers.cloudflare.com/d1/worker-api/d1-database/) |
| Durable Objects | Python 可定义；每对象唯一协调、私有事务存储。按 Key/Auth Group/映射/OAuth 账号分片，避免全局单例 | [Python DO](https://developers.cloudflare.com/changelog/post/2025-05-14-python-worker-durable-object/)、[Storage](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/) |
| KV | 最终一致，不能替代即时一致的权限、配额或凭据状态 | [KV Consistency](https://developers.cloudflare.com/kv/concepts/how-kv-works/#consistency) |
| R2 | 对象存取强一致，可承载大快照/记录；不是事务数据库或本地文件系统 | [R2 API](https://developers.cloudflare.com/r2/api/workers/workers-api-reference/) |
| Cron / Queue / waitUntil | 替代周期 greenlet 与受控后台工作；考虑重复执行、幂等与生命周期 | [Context](https://developers.cloudflare.com/workers/runtime-apis/context/#waituntil)、[Cron](https://developers.cloudflare.com/workers/configuration/cron-triggers/) |
| Static Assets | 承载 Vue 构建产物；bootstrap、鉴权、API 与缓存需 Worker 适配 | [Vue](https://developers.cloudflare.com/workers/framework-guides/web-apps/vue/)、[Headers](https://developers.cloudflare.com/workers/static-assets/headers/) |
| Secrets | 部署级少量静态密钥；不是可写 Provider/OAuth 管理数据库 | [Secrets](https://developers.cloudflare.com/workers/configuration/secrets/) |

**跨服务事务**: D1、DO、R2、KV、Queue 的组合写入不是一个统一事务。D1 内日志/统计/Key 用量可事务 batch；DO 预占与 D1 完成记录之间需要请求 ID、幂等结算、租约过期与对账。

## 关键限制与验证项

- 检索日 Paid Worker 默认每 HTTP 请求 CPU 30 秒（可配置上限以官方页为准）、内存 128 MiB、未压缩包体 64 MiB、环境变量/Secrets 有数量和大小限制；Free CPU 默认 10 ms。Free 不宜作为完整网关默认目标。[Limits](https://developers.cloudflare.com/workers/platform/limits/)。
- 出站等待响应头的连接同时最多 6 个；HTTP 持续时间没有简单固定上限，但断连、资源压力、运行时更新等仍可中断。[Limits](https://developers.cloudflare.com/workers/platform/limits/#duration)。
- Python Worker 顶层模块在部署时执行并生成内存快照；配置与缓存初始化不能直接沿用单进程启动假设。[How Python Workers Work](https://developers.cloudflare.com/workers/languages/python/how-python-workers-work/)。
- JS `connect()` TCP 存在不等于 Paramiko/PySocks 可用；私网、Cloudflare IP、25 端口等有约束。[TCP Sockets](https://developers.cloudflare.com/workers/runtime-apis/tcp-sockets/)。Agent 功能已明确不在 Workers 范围。
- `requests`、`gevent`、`paramiko`、`pysocks`、`pysqlite3-binary`、`tomlkit` 在本项目用法下无 Cloudflare 包级保证；`ruamel.yaml` 的 Pyodide 版本可能不同于仓库约束。禁止凭 Pyodide 列表推断完整功能兼容。
- 动态 Hook 的外部源码加载、exec、热更新与跨实例一致性未获官方针对本项目确认；优先验证部署期版本化 Hook 包。
- D1/DO 有固定数据位置，全球 Worker 不代表共享状态访问零延迟。[DO Location](https://developers.cloudflare.com/durable-objects/reference/data-location/)、[D1 Replication](https://developers.cloudflare.com/d1/best-practices/read-replication/)。

## 部署工具

- Python 官方文档使用 `pywrangler` 管理包并包装 Wrangler；本项目约定使用 `cf`。
- 当前 `cf` 项目文档未明确 Python 包 vendoring 流程；不能凭 `entrypoint` 字段推断完整 Python 支持。第一道原型关卡须验证 `cf` 与 Python 构建链衔接，不直接引入 Wrangler 配置。
- 参考资料：[Python Packages](https://developers.cloudflare.com/workers/languages/python/packages/)、[cf Projects](https://developers.cloudflare.com/cf/projects/)、[cf Configuration](https://developers.cloudflare.com/cf/projects/cloudflare-config/)。

## 范围与完成定义

- 覆盖完整网关数据面与管理面，不含 Agent 本机/SSH 配置；现有 Flask/gevent 路径保留。
- OAuth、配额、故障切换必须跨实例一致；不能通过单机演示声称完成。
- Flask Hello World、单次 SSE 成功、页面可打开均不等于完整网关可部署；必须按 `deployment-options.md` 的 Gate 0–4 验证。
