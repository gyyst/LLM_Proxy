# Technical Design: Cloudflare Workers 网关迁移候选架构

## 1. 设计目标与边界

采用 Python Workers 原生化作为推荐路线，保留协议转换核心，替换 Flask/gevent 运行模型、本地状态与文件依赖。Workers 版覆盖除 Agent 本机/SSH 配置之外的网关能力，并保证 OAuth、配额和故障切换状态跨实例一致。

本文件是实施前候选设计，不是兼容性承诺。详细路线比较与组件表见 `research/deployment-options.md`。

## 2. 架构与依赖

```text
Vue Static Assets / API client
              |
       Python Worker entry
       (WSGI or native)
              |
    authentication / admission
              |
    route + snapshot resolution
              |
   provider / OAuth orchestration
              |
 fetch Streams <-> protocol translators
              |
   encoded downstream nonstream/stream

D1: users, keys, provider config, mappings, request logs, statistics
DO per key: concurrency and rate/token budget lease
DO per auth group: inflight, cursor, sticky target, cooldown
DO per mapping: target selection and runtime state
DO per OAuth account: token refresh, quota, account state
R2: large import/export snapshots, optional LLM records
Cron/Queues: OAuth quota refresh and deferred idempotent work
```

- 无状态 Worker 负责协议与请求编排，不以全局单例 DO 代理所有流量。
- DO 以业务协调实体分片；持久化先于内存缓存，重启后从存储恢复。
- D1 负责关系查询和统计，不承担跨 isolate 的 Python DB-API 连接伪装。
- 现有 Flask/gevent 部署保留；共享协议核心通过共同 golden cases 防止语义分叉。

## 3. 请求与数据契约

### 3.1 协议核

复用 `src/translators/registry.py` 的 translator 接口、`src/proxy_core/decoders.py` 的 `Iterable[bytes]` 输入和 `src/proxy_core/encoder.py` 的 bytes 输出。新异步传输适配器把这些迭代接口连接到 JS Streams，而不是把 `requests.Response` 暴露给业务层。

统一上游响应适配契约需覆盖：

- 状态、headers、content type、流格式；
- 增量字节读取、完整 JSON 聚合、响应关闭；
- transport/timeout/cancel 错误映射；
- 首个非空已编码下游字节作为提交边界；
- 提交后只编码流内错误，协议终止后保持逻辑完成。

### 3.2 配置快照

- Provider、Auth Group、用户、Key 和映射配置以 D1 为正式数据；大 YAML/导入导出快照可放 R2。
- 配置版本随写入递增，管理操作采用版本校验，避免丢失更新。
- 请求开始时解析一次快照；后续重试使用同一逻辑路由配置，状态更新通过对应 DO。
- Secrets 只存部署级少量静态密钥，不代替可管理业务配置。

### 3.3 一致性

- D1 内日志、日统计和 Key 用量使用单个事务 batch。
- API Key 并发与周期限额在 Key DO 内原子预占/结算，使用请求 ID 防重复结算。
- Auth Entry/OAuth 的占用与刷新采用可恢复租约；进程崩溃不留下永久 inflight。
- 模型映射按 ID 管理 sticky target 和 cooldown，不把实例内游标视为共享状态。
- 跨 D1/DO 无分布式事务：必须设计过期恢复、幂等写入和对账，不允许宣称天然原子。
- D1 读复制只通过 Sessions 保证会话顺序一致，写路径仍到主库；需要实时一致性的读取不直接走 KV/普通副本。

## 4. OAuth、Hook 与后台任务

- 按 OAuth 账号 DO 串行 token 刷新、启停、配额更新和最近成功账号状态。
- 管理与 PKCE Session 使用有 TTL 的持久存储；cookie/CSRF 保持现有安全约束。
- Codex 周期刷新使用 Cron 触发、账号 DO 防重入，必要时 Queues 分发；不启动无限 greenlet。
- Codex 推理缓存若迁移到 DO，按业务 scope 分片并设置过期/容量；若只作为可重建优化，应证明缺失缓存不会改变协议正确性。
- Hook 保持现有接口与 `None` 语义，首选随部署版本化 Hook 包与配置启用映射。缓存按版本/摘要失效。
- 任意管理员 Python 源码热写不是默认设计；其安全性、加载/重载和跨实例一致性另行评估。
- LLM 记录使用 R2/D1/Queue 的幂等异步路径；采集故障不能改变客户端响应。

## 5. 前端与安全

- Vue 构建产物由 Static Assets 承载，Worker 优先处理 `/api/*`、`/v1/*` 与受保护页面。
- 动态 bootstrap 继续由 Worker 生成；Workers 版移除 `/agents` 导航、API 和 Agent capability。
- 静态 hash 资源通过 `_headers` 设置长期缓存；HTML、bootstrap 与敏感 API 由 Worker 设置 `no-store`。
- OAuth/Provider 凭据仅在受控绑定和业务后端内使用；不写入静态包、普通日志或公开导出。
- 需要单独评估出站 HTTP/SOCKS 代理、私网目标和 TLS 自定义策略的支持矩阵，未知项不默认承诺。

## 6. 兼容、上线与回退

- 以独立开发资源、合成上游和测试凭据验证，禁止第一阶段操作真实数据库/OAuth 账号。
- 阶段顺序：构建链 → 协议核 → 流与 Hook → 共享状态 → 全功能管理面 → 上线验证。
- 任一阶段发现关键不兼容，停止扩大迁移，保留现有 Flask/gevent 正式部署；根据实证选择继续 Python 适配或 Containers，而不是提前双写两套生产状态。
- 数据迁移另立实施任务：冻结/增量同步、校验、回滚与备份必须在真实迁移前确定。
- Workers 版本回滚不回滚 D1/DO/R2 数据；schema 与状态格式需向后兼容或独立迁移方案。

## 7. 关键未决技术验证

- `cf` 项目配置与 Python 包 vendoring 的可重复构建方式。
- WSGI 与异步绑定/流组合的可用范围，是否应仅保留 Controller 逻辑而使用原生入口。
- Hook 打包、导入、exec 与缓存失效的目标运行时行为。
- Streams 背压、取消、流关闭与 WSGI 首字节提交语义。
- 资源限制下的协议翻译、OAuth、统计和导出实际调用量与费用。
