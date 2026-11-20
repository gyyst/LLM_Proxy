# 调研 Cloudflare Workers 部署方案

## Goal

为现有 Flask + gevent 协议翻译网关制定 Cloudflare Workers 部署方案：覆盖网关数据面与管理面，复用可迁移的协议转换核心，替换不适用的运行时和状态机制，并明确验证与分阶段交付路径。

## Background

- 当前服务是单进程 Flask + gevent 应用，具备多协议数据面、管理控制面、OAuth、SQLite 和本地 YAML / Hook 文件（`main.py:8-12`, `src/application/application.py:67-84`）。
- 用户指定 [Python Workers 官方文档](https://developers.cloudflare.com/workers/languages/python/)作为主要参考资料。Flask/WSGI 有官方支持，但不代表现有 gevent、文件持久化和进程内状态可直接迁移。
- 仓库现状与代码锚点见 `research/current-runtime-inventory.md`；官方平台能力与包级未知项见 `research/cloudflare-platform-capabilities.md`；路线对比与候选组件边界见 `research/deployment-options.md`。

## Requirements

### R1 网关功能范围

- 覆盖 Chat Completions、Responses、Responses compact、Claude Messages、Images 和 `/v1/models` 数据面。
- 覆盖 Provider / Auth Group、用户、API Key、模型映射、OAuth、统计、系统设置与 LLM 记录等管理面。
- 保持协议转换、模型路由、权限、配额、重试、流提交、取消和观测语义，不以简单转发代替现有管线。
- 保留现有 Flask/gevent 部署路径，Workers 适配不得要求一次性替换现有正式运行方式。

### R2 共享状态与一致性

- OAuth、配额和故障切换状态必须跨 Worker 请求实例共享并保持一致。
- 权限、凭据、并发名额、周期限额和映射/认证组运行状态不能依赖 isolate 本地内存或临时文件。
- 明确 D1 事务与 Durable Object 协调的边界，以及跨服务写入的幂等与崩溃恢复方案。

### R3 技术调研与方案

- 记录 Python/Flask、HTTP/Streams、存储、静态资源和后台任务的官方依据与限制。
- 对现有依赖逐项区分可直接保留、需要替换、尚无兼容证据的能力。
- 比较至少两种路径，给出推荐架构、迁移边界、验证关卡和回退条件。
- 优先使用 `cf` 作为 Cloudflare 项目管理工具；Python 官方 `pywrangler` 与 `cf` 构建链的衔接必须验证，不擅自引入 Wrangler 配置。
- 优先复用现有 Python translators / proxy_core；Flask 外壳是否保留取决于异步 I/O 与流式验证。

### R4 安全与兼容

- OAuth、Provider 与管理凭据不得进入静态资源、普通日志或未经认证的接口。
- 现有前端可复用静态构建产物，但 bootstrap、鉴权、API 路由与缓存策略需保持正确。
- 所有兼容性结论必须区分代码/官方事实、候选设计和目标环境待验证项。

## Acceptance Criteria

- [x] **AC1 / R1**: 现有运行时清单覆盖协议核、流管线、OAuth、SQLite、配置、Hook、后台任务与前端装配，保留关键 `file:line` 锚点。
- [x] **AC2 / R3、R4**: 官方资料清单覆盖 Python/Flask、依赖包、Streams、资源限制、文件系统、状态存储、静态资源和 Containers，标明未证实事项。
- [x] **AC3 / R1、R2**: 方案比较 Python Workers 原生化、TypeScript 重写、Containers 和仅 WSGI 包装，说明成本、功能差异与推荐理由。
- [x] **AC4 / R1、R2、R4**: 候选设计给出协议、HTTP、状态、OAuth、Hook、日志、前端与后台任务的迁移边界，以及共享状态一致性风险。
- [x] **AC5 / R3**: 后续验证计划包含部署工具、协议核、流边界、Hook、共享状态、全功能回归与回退关卡。
- [x] **AC6 / R1**: 明确 Agent 本机/SSH 配置不属于 Workers 版，不引入本机协同组件。

## Out of Scope

- Agent 本机与 SSH 配置管理功能不纳入 Workers 版；不需要本机协同组件。
- `server.host` / `port` 等仅适用于自有进程监听器的设置不属于 Workers 部署能力。
- 仅验证请求转发或 SSE 的最小演示不能视为满足部署目标。
- 未经后续计划确认的生产代码改造、真实凭据迁移、Cloudflare 资源创建与实际部署。

## Risks and Deferred Validation

- 动态 Hook 的加载/热更新机制尚无目标环境兼容结论；首选部署期版本化 Hook 包，任意源码热写需另行安全评估。
- D1 与 Durable Object 不是一个分布式事务；配额预占、统计写入与 OAuth 刷新需要专门验证幂等和崩溃恢复。
- requests、gevent、Paramiko、PySocks 等在本项目用法下没有官方包级兼容保证；不以 import 成功替代行为测试。
- Workers 有 CPU、内存、包体、子请求和存储调用限制；需通过目标环境原型评估，不预先承诺吞吐或成本。
- `cf` 与 Python 依赖打包链尚无本项目实测结论，是实施前第一道验证关卡。
