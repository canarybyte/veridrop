# React 探针控制台 Spec

## Why

现有 Web 是 FastAPI + Jinja 模板，用户提交检测后只能整页轮询 `/api/status`，看不到“哪个探针跑到哪一步”，报告也只落盘 JSON，无法沉淀查询。用户需要一个新的 React 控制台：左中右三栏（配置 / 探针并发进度 / 评分结论），后端并发跑探针并把进度实时推给前端，结果进数据库，并提供一把服务端密钥给外部 agent 查询分析结果。

本次采用**并存策略**：旧站（hub / leaderboard / faq / result 等页面）完全不动，新站挂在 `/app`，复用同一套 `relay_detector` 探测内核与 `web/jobs.py` 作业队列。

## What Changes

- 新增 React + Vite + TypeScript 前端工程 `web-ui/`，构建产物输出到 `web/static/app/`，由 FastAPI 在 `/app` 下托管。
- 新增 `web/db.py` + `web/repository.py`：异步 SQLAlchemy，默认 SQLite（`sqlite+aiosqlite`），配置 `DATABASE_URL` 时切 Postgres（`asyncpg`）；启动时 `create_all` 建表。
- 新增 `web/progress.py`：进程内事件总线，按 job_id 维护事件序列 + 订阅队列，支持 `Last-Event-ID` 断线重放。
- 修改 `src/relay_detector/core/runner.py`：新增**可选**进度回调（默认 `None`，CLI 行为不变），在运行开始、每个主动探针开始/结束、被动探针终结时发事件。
- 修改 `web/jobs.py`：把回调事件转发给事件总线；作业结束（成功/失败）时把检测记录与逐探针结果写入数据库（写库失败不影响作业状态与落盘 JSON）。
- 新增 `web/console.py`：挂载 `/app/api/*`（前端同源使用，无需密钥）与 `/api/v1/*`（外部 agent 只读，需 `X-API-Key`）。
- `/app` 提供 React 单页应用：
  - `/app` 主控制台：配置表单 → 并发探针网格实时状态 → 运行日志 → 评分结论（动画计数 + 环形评分 + 风险等级 + 诊断摘要）。
  - `/app/reports` 历史检测列表（来自数据库）。
  - `/app/reports/:id` 报告详情（逐探针得分/状态/耗时/关键 details 摘要）。
- 依赖新增：`sqlalchemy>=2.0`、`aiosqlite`、`asyncpg`（均加入 `pyproject.toml` 的 `web` extras）。前端依赖只在本目录 `web-ui/package.json`，不影响 Python 包。

非目标（本次不做）：
- 不迁移旧站任何 Jinja 页面，不做服务端渲染与 SEO 改造。
- 不做多租户 / 多密钥管理 / 权限后台，外部 agent 只用一把服务端密钥。
- 不做 Alembic 迁移体系（`create_all` 足够；后续再引入）。
- 不改变探测算法、权重、报告 JSON 结构。

## Impact

- 受影响能力：检测作业编排（进度事件）、检测结果存储、Web 前端、对外只读 API。
- 受影响代码：
  - `src/relay_detector/core/runner.py`（新增可选回调，向后兼容）
  - `web/jobs.py`（事件发布 + 持久化）
  - 新增 `web/db.py`、`web/repository.py`、`web/progress.py`、`web/console.py`
  - `web/server.py`（仅追加 import/注册，不改旧路由）
  - 新增 `web-ui/`（React SPA），构建产物 `web/static/app/`
  - `pyproject.toml`（web extras 依赖）
- 不改动：`relay_detector` 的探测器实现、评分函数、报告 Pydantic 模型、旧站模板与路由。

## ADDED Requirements

### Requirement: 并发探针实时进度

系统 SHALL 在检测运行期间，按探针粒度向前端推送进度事件，前端无需轮询即可实时渲染每个探针的状态。

#### Scenario: 正常检测进度
- **WHEN** 前端 `POST /app/api/detect` 提交检测并拿到 `job_id`，然后连接 `GET /app/api/detect/{job_id}/events`
- **THEN** 服务端按顺序推送 `queued` → `run_started`（含探针总数与探针清单）→ 每个探针的 `probe_started` / `probe_finished`（含 status、score、duration_ms）→ `job_finished`（含总分、结论、风险等级）
- **AND** 每个事件为一条 SSE `data:` 记录，格式 `{"seq":int,"ts":float,"type":str,"payload":object}`，`id:` 为 `seq`

#### Scenario: 断线重连
- **WHEN** 客户端携带 `Last-Event-ID` 重新连接 SSE
- **THEN** 服务端先重放该 id 之后的所有缓存事件，再继续推送新事件
- **AND** 事件缓存为每 job 最近 500 条

#### Scenario: 完成后的补看
- **WHEN** 作业已结束、客户端才连接 SSE
- **THEN** 服务端立即重放全部缓存事件并发送一次 `job_finished`，随后关闭连接（不再空等）

#### Scenario: 连接保活
- **WHEN** 15 秒内没有新事件
- **THEN** 服务端发送 SSE 注释心跳 `: ping`，保持代理不超时

### Requirement: 检测结果入库

系统 SHALL 把每次检测（含失败）与逐探针结果写入数据库，数据库默认 SQLite、可切 Postgres。

#### Scenario: 成功写入
- **WHEN** 作业运行到达终点（`done` 或 `error`）
- **THEN** `detections` 表存在一条以 job_id 为主键的记录（状态、协议、中转站 host、模型、模式、总分、结论、风险等级、摘要、身份自述、性能指标、完整报告 JSON、时间戳）
- **AND** `probe_results` 表存在该检测的逐探针记录（名称、显示名、状态、得分、权重、耗时、错误、details JSON）

#### Scenario: 数据库不可用
- **WHEN** 数据库连接或写入失败
- **THEN** 作业本身仍正常完成、报告 JSON 仍落盘、SSE 仍推完
- **AND** 服务日志记录一次 `db persist failed` 警告，接口不返回 500

#### Scenario: 密钥安全
- **WHEN** 任何检测被持久化
- **THEN** 数据库只存 `mask_api_key()` 后的掩码，原始 API Key 不落库、不落盘、不出现在 SSE 事件中

### Requirement: 评分与结论展示

系统 SHALL 在检测完成后展示“综合评分 + 风险等级 + 结论 + 诊断摘要”，并用动画呈现评分结果。

#### Scenario: 评分动画
- **WHEN** 前端收到 `job_finished`
- **THEN** 右侧结论面板显示：完成进度 `N/N`、综合评分从 0 递增到最终分、环形进度动画填充、总分级（优秀 ≥85 / 通过 ≥70 / 基本合格 ≥50 / 未达标 <50）、风险等级（低 / 中 / 高）
- **AND** 评分颜色随结论变化（通过=绿、基本合格=橙、未达标=红、含 critical 问题=红）

#### Scenario: 风险等级判定
- **WHEN** 报告包含 critical 级别问题，或总分 <50
- **THEN** 风险等级为“高”
- **AND** 总分 50–69 且无 critical 时为“中”，其余为“低”

#### Scenario: 诊断摘要
- **WHEN** 检测完成
- **THEN** 面板展示报告 `summary`、`self_reported_identity`、`detected_non_anthropic_brands`、`performance`（耗时、请求数、TTFT）与失败探针清单摘要

### Requirement: 外部 agent 只读分析 API

系统 SHALL 提供一把服务端密钥保护、只读的 HTTP API，供外部 agent 查询检测结果并做二次分析。

#### Scenario: 正确密钥
- **WHEN** 请求携带 `X-API-Key: <VERIDROP_AGENT_API_KEY>`
- **THEN** 可访问 `GET /api/v1/reports`（分页列表，可按 protocol / model / verdict / 最低分过滤）、`GET /api/v1/reports/{id}`（完整报告）、`GET /api/v1/reports/{id}/analysis`（面向 agent 的紧凑分析载荷）

#### Scenario: 缺失或错误密钥
- **WHEN** 未携带或携带错误密钥
- **THEN** 返回 401，响应体 `{"detail": "invalid or missing api key"}`
- **AND** 服务端未配置 `VERIDROP_AGENT_API_KEY` 时返回 503，`{"detail": "agent api disabled"}`

#### Scenario: 密钥不出现在前端
- **WHEN** 浏览器访问 `/app`
- **THEN** 前端不持有、不请求该密钥；`/app/api/*` 为同源免密钥接口

### Requirement: React 控制台

系统 SHALL 在 `/app` 提供可用的 React 单页控制台，与旧站并存。

#### Scenario: 深链刷新
- **WHEN** 用户直接访问或刷新 `/app/reports/abc123`
- **THEN** FastAPI 返回 React 的 `index.html`，前端路由渲染详情页（不 404）

#### Scenario: 表单提交
- **WHEN** 用户在控制台填写协议、Base URL、API Key、模型、模式后提交
- **THEN** 前端调用 `POST /app/api/detect`，跳到运行态并自动订阅 SSE
- **AND** 提交中/运行中禁用重复提交；失败时展示 `job_error` 的消息

#### Scenario: 历史列表
- **WHEN** 用户打开 `/app/reports`
- **THEN** 展示数据库中的检测记录（时间、中转站 host、模型、总分、结论），点击进入详情

### Requirement: 探针清单动态渲染

系统 SHALL 以 `run_started` 事件中的探针清单为准渲染探针卡片，不硬编码探针数量或名称。

#### Scenario: 不同协议/模式
- **WHEN** 用户选择 anthropic quick 或 openai full
- **THEN** 探针网格数量与名称跟随实际选中探针（含 `applies_to` 过滤结果），未运行的探针显示 `skip` 状态

## MODIFIED Requirements

### Requirement: 检测作业队列

`web/jobs.py` 的作业在原有语义（单 worker、进程内状态、`_MAX_INFLIGHT=6`、完成后落盘 JSON）之上：

- 作业持有事件发布器，`Runner` 运行时把进度事件发给订阅者；
- 作业终态（`done` / `error`）时尽力写入数据库；
- 作业对象内存中仍然只保留原始 API Key 到运行结束，随后丢弃，行为不变。

### Requirement: 探测内核 Runner

`Runner.run()` 新增可选参数 `on_event: Callable[[str, dict], None] | None = None`（或构造参数），默认不产生任何行为变化：

- 选中探针后发 `run_started`，payload 含 `total`、`detectors: [{name, display_name, weight, passive}]`；
- 每个主动探针包一层 `probe_started` / `probe_finished`；
- 被动探针 `finalize()` 时发 `probe_finished`；
- 回调异常必须被吞掉，绝不能影响检测结果。

