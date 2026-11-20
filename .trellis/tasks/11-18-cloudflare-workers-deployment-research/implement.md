# Implementation Plan: 调研 Cloudflare Workers 部署方案

## 任务交付性质

本任务交付调研报告、候选架构与后续验证计划，不修改产品代码、不创建 Cloudflare 资源、不迁移真实配置或数据。后续实施需依据本计划拆分为独立验证/迁移任务，并经用户确认。

## 已完成实施与验证

- [x] 建立 Python Workers 入口、Flask 管理适配和原生 fetch/Streams 数据面。
- [x] 复用现有 translators、proxy_core、Hook 契约和 Vue 管理资源。
- [x] 接入 D1、Durable Objects、R2、Static Assets 和 `cf` 部署配置。
- [x] 实现 Provider/Auth Group/用户/API Key/模型映射/OAuth/设置/统计/LLM 记录管理。
- [x] 实现 API Key 准入与配额、流式结算、OAuth 多账号候选、映射故障切换。
- [x] 在 Cloudflare 实际部署并验证 Chat、Responses、compact、Claude、Images、SSE、R2 和浏览器页面。
- [x] 清理测试 Provider、OAuth 账号、API Key、用户、映射、日志和 R2 对象。
- [x] 添加部署文档、架构说明和 `scripts/verify_workers.py`。
- 详细结果见 `research/verification-results.md`。

## 后续实施顺序（不随本任务自动执行）

### Phase A: 构建链与运行时原型

- [ ] 用 `cf` 干跑确认 Python 项目构建入口与依赖打包方式；不引入 Wrangler 配置。
- [ ] 固定兼容日期，验证 Python 入口、Flask、目标依赖与打包大小。
- [ ] 验证 translators/proxy_core 共享 golden cases 在目标运行时通过。
- **回退点**: Python 包或协议核不满足时，保留现有部署并重新评估 Containers。

### Phase B: 流式传输与 Hook

- [ ] 实现并验证 JS fetch/Streams 与 translator/decoder/encoder 的适配契约。
- [ ] 覆盖首字节前重试、提交后错误、终止后 framing error、取消和强制上游流式聚合。
- [ ] 验证 Hook 版本化打包、加载、缓存失效、异常和跨实例一致性。
- **回退点**: 流提交/Hook 不满足时，不进入状态迁移或全量路由改造。

### Phase C: 状态与管理面

- [ ] 建立 D1 schema/异步仓储、D1 Sessions 与事务 batch 验证。
- [ ] 按 Key、Auth Group、映射和 OAuth 账号分片 DO，验证并发、租约、幂等和崩溃恢复。
- [ ] 迁移管理 Session、OAuth、Cron/Queue、LLM 记录、统计和导入导出。
- [ ] 接入 Static Assets、动态 bootstrap、鉴权与缓存，移除 Workers 版 Agent 入口。
- **回退点**: 跨服务一致性无法验证时停止，不能用单机测试掩盖多实例问题。

### Phase D: 全功能验收与独立部署评审

- [ ] 验证全部下游协议、模型映射、OAuth 文本/图片/compact 及管理 API。
- [ ] 验证多实例状态、重复任务、日志失败隔离、资源使用与恢复。
- [ ] 形成真实数据迁移、备份、回滚、观测与费用评审后，才决定是否部署。

## 验证命令与证据

```bash
# 后端全量测试（包含 Workers 状态测试）
uv run python -m unittest discover -s tests

# Workers 构建与部署
npm run deploy
WORKERS_BASE_URL="https://<worker>.workers.dev" ADMIN_USER=... ADMIN_PASSWORD=... \
  uv run python scripts/verify_workers.py
```

- 研究交付物是 Markdown；未修改 Python 文件，不运行 Ruff。
- 未执行产品测试，因为产品代码未改动；不得把文档核查描述为 Workers 运行测试。
- 未执行 `cf dev`、`cf build`、部署或线上验证；这些属于后续经批准的独立原型任务。

## 风险文件与变更边界

- 当前仅写 `.trellis/tasks/11-18-cloudflare-workers-deployment-research/`。
- 现有 `.gitattributes`、`AGENTS.md`、`CLAUDE.md`、`mise.toml` 等非本任务改动不得纳入本任务提交。
- 后续高风险实现区域：`src/services/proxy_response_builder.py`、`src/services/codex_*`、`src/repositories/*`、`src/config/provider_runtime_factory.py`、`src/application/*` 和前端装配。
- 后续部署拓扑、运行时状态或模块职责实际变化时，按仓库约定同步 `docs/architecture-4plus1.md`；本调研未改变现有架构，不修改该文档。
