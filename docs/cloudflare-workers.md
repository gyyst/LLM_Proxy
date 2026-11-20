# Cloudflare Workers 部署

本仓库提供 Cloudflare Workers 运行入口。Workers 版是独立部署形态，与现有 Flask + gevent 单机版并存；两者复用协议转换器、请求构建、用量解析和管理界面资源。

## 能力范围

Workers 版提供网关数据面与主要管理面：

- `/v1/chat/completions`
- `/v1/responses`
- `/v1/responses/compact`（OAuth 模型）
- `/v1/messages`
- `/v1/images/generations`、`/v1/images/edits`（OAuth 图片模型）
- `/v1/models`、CORS 预检
- Provider 与 Auth Group
- 用户、IP 白名单和模型权限
- API Key 模型权限、Token 总量、并发、分钟和每日请求限制
- 模型映射与多目标故障切换
- OAuth 账号、PKCE 登录、Token 刷新、导入导出和模型目录
- 请求日志、日聚合统计、统计概览和 Excel 导出
- LLM 数据记录（R2）
- 动态 Hook（部署时随 Worker 打包）

Agent 本机 / SSH 配置功能不包含在 Workers 版；管理界面不会显示对应入口。

## 架构

```text
浏览器 / API 客户端
        |
        v
Cloudflare Worker（Python + Pyodide）
  ├─ Flask：同源管理 API、页面和 bootstrap
  ├─ 原生 Worker：数据面、SSE/NDJSON 流和上游 fetch
  ├─ 现有 translators / proxy_core：协议转换和编码
  ├─ D1：Provider、用户、Key、映射、OAuth 元数据、日志和统计
  ├─ Durable Objects：按 Key、Auth Group、模型映射和 OAuth 账号分片的一致性状态
  ├─ R2：LLM 数据记录与导出对象
  └─ Static Assets：现有 Vue 管理界面构建产物
```

- D1 不提供当前 SQLite 文件的直接 DB-API 连接；Workers 版使用异步 D1 访问层。
- API Key 并发和窗口限额、认证组 inflight/游标、映射粘滞状态、OAuth Token 刷新和配额状态保存在业务实体对应的 Durable Object 中，不依赖单个 isolate 的内存。
- 流式请求在客户端连接结束、取消或异常时释放并发名额并写入最终 usage。
- D1 与 Durable Object 不是分布式事务。结算使用请求 ID 幂等，异常租约按时间回收；统计数据以 D1 事务批量写入。

## 前置条件

- Cloudflare 账号。
- `cf` CLI 1.0 release candidate 或兼容版本。
- 根目录 Node.js 依赖（`cf` 和 `wrangler` 构建工具）。
- `uv`。
- 目标 Worker 使用兼容日期 `2026-11-18`、`python_workers` 和 `enable_ctx_exports`。

推荐使用 Workers Paid 计划。免费计划的 CPU 限制较低，适合验证而不适合完整网关流量。

## 首次部署

### 1. 安装工具与 Python 依赖

```bash
npm install
uv run python scripts/lock_workers.py
npm run sync
```

`scripts/lock_workers.py` 生成 Workers 专用 `pylock.toml`，只包含 Flask、PyYAML、ruamel.yaml、tomlkit 和 Workers Runtime SDK。不要把它替换为 Linux 后端的完整依赖集。

### 2. 创建独立资源

```bash
cf d1 create --name llm-proxy-gateway --primary-location-hint apac --read-replication-mode auto
cf r2 buckets create --name llm-proxy-gateway --location-hint apac
```

把 D1 名称和 UUID、R2 bucket 名称写入 `cloudflare.config.ts` 的 `worker.env`。`cloudflare.config.ts` 是唯一 Worker 配置来源；`wrangler.config.ts` 只保存构建目录和静态资源设置。

### 3. 部署

```bash
npm run deploy
```

该命令先同步 Python 包并构建 Vue 资源，再调用 `cf deploy`。不要直接运行 `cf deploy`：它不会刷新 Workers 使用的共享 `src/` 副本。

部署成功后得到 Worker URL，例如 `https://llm-proxy-gateway.<subdomain>.workers.dev`。D1 表结构在首次请求时自动创建。

### 4. 初始化管理员

首次部署尚无管理员，管理 API 可用于一次初始化。立即执行：

```bash
curl -X PUT "https://llm-proxy-gateway.<subdomain>.workers.dev/api/settings/system" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<strong-password>"}'
```

初始化后，除 `/api/health`、`/api/ui/bootstrap` 和登录相关入口外，管理 API 都需要管理员会话。

### 5. 验证部署

```bash
WORKERS_BASE_URL="https://llm-proxy-gateway.<subdomain>.workers.dev" \
ADMIN_USER=admin ADMIN_PASSWORD="<strong-password>" \
uv run python scripts/verify_workers.py
```

如已有公开上游模型，可额外传入 `WORKERS_MODEL` 和 `WORKERS_API_KEY` 验证数据面。

## 管理面

登录后同一套 Vue 管理界面提供 Provider、OAuth、模型映射、API Key、用户、统计和设置页面：

- 页面和 `/api/ui/bootstrap` 使用 `no-store`。
- 静态哈希资源位于 `/static/app/assets/`，可长期缓存。
- 管理员会话保存在 D1，默认有效期 24 小时。
- Provider、Key、用户和映射的写入在 D1 中持久化，请求使用同一配置快照。
- OAuth Token 以账号 Durable Object 为运行时权威，D1 保存管理元数据。
- OAuth 账号卡片显示最近保存的额度快照；Workers 版不执行 Codex 配额后台轮询、额度重置卡消费和 Agent 配置管理。
- 统计与请求日志来自 D1；LLM 数据记录写入 R2，键为 `llm/<request_id>.json`。

## Hook

Hook 仍从 `hooks/` 相对路径加载，导出 `Hook` 类并支持 `header_hook`、`request_guard`、`response_guard` 和 `fetch_models`。Workers 包是只读部署产物：

- Hook 版本随 Worker 部署发布。
- 不支持运行时修改本地 Hook 文件后热更新。
- `None` 仍表示保留当前 payload。

## Cloudflare 与工具边界

- `workers.dev` 上的 Worker 访问另一个 Cloudflare 托管域名的 Worker 可能返回 `1042`，不能用公网 URL 做 Worker 间回环验证；使用 Service Binding 或非 Cloudflare 上游。
- `cf` 的项目配置与 `pywrangler` 的 Python 依赖管理是两层。首次部署在 `npm run sync` 中原子地调用 Workers Runtime SDK 的锁文件和安装实现。
- Worker 内存、CPU、子请求、D1 和 Durable Object 调用受 Cloudflare 计划限制；长流本身可继续，但计算 CPU 和存储调用仍受限。
- 不支持 `server.host` / `server.port` 的进程监听语义；设置页中的监听字段在 Workers 部署中没有运行时效果。
- 不支持把现有 `data/requests.db`、`data/oauth/`、`config.yaml` 或 Agent 文件直接挂载到 Worker。Workers 版通过管理 API 重新建立配置，数据迁移需要独立实施。

## 验证记录

实现验收使用独立测试资源，包含：

- 线上 `/api/health`、管理员登录和 bootstrap。
- Provider、Auth Group、用户、API Key、模型映射和 OAuth 账号 CRUD。
- Chat、Responses、compact、Claude Messages 的非流式与 SSE 响应。
- 图片生成、图片编辑和图片流式部分帧。
- API Key 鉴权、分钟配额和未认证拒绝。
- 映射首选目标不可达时切换备用目标。
- D1 请求日志与统计、R2 LLM 数据记录落盘。
- 管理界面逐页浏览器加载，无失败 API 请求。
