# Tasks

- [x] Task 1: 引入数据库依赖并建立数据层
  - [x] SubTask 1.1: `pyproject.toml` 的 `[project.optional-dependencies].web` 增加 `sqlalchemy>=2.0`、`aiosqlite>=0.20`、`asyncpg>=0.29`；用 `.\venv\Scripts\python.exe -m pip install -e ".[web]"` 安装并验证 `import sqlalchemy, aiosqlite` 成功（Python 3.14 下若 SQLAlchemy async 依赖 greenlet 报错，先装 `greenlet>=3.2`，仍失败则停下来说明）
  - [x] SubTask 1.2: 新建 `web/db.py`：读取 `DATABASE_URL`（默认 `sqlite+aiosqlite:///./web_data/veridrop.db`），定义 `Base`、`Detection`、`ProbeResult` 两张表（字段见 spec.md「检测结果入库」），提供 `init_db()`（建目录 + `create_all`）与 `get_session()`
  - [x] SubTask 1.3: 新建 `web/repository.py`：`save_detection(...)`（幂等 upsert，先删旧 probe_results 再插入）、`list_detections(filters, limit, offset)`、`get_detection(id)`、`count_detections(filters)`
  - [x] SubTask 1.4: 冒烟验证：临时脚本或 `python -c` 走一遍 SQLite 建表 + 写一条 detection + 2 条 probe + 读回

- [x] Task 2: 探测内核进度回调与事件总线
  - [x] SubTask 2.1: `src/relay_detector/core/runner.py` 增加可选 `on_event` 回调（默认 None）：`run_started` / 主动探针 `probe_started`、`probe_finished` / 被动探针 `probe_finished`；回调异常吞掉
  - [x] SubTask 2.2: 新建 `web/progress.py`：按 job_id 的 `EventBus`，`publish(job_id, type, payload)` 生成递增 `seq` 并缓存（deque maxlen=500），`subscribe(job_id, last_event_id)` 返回可迭代的 `asyncio.Queue` 与取消方法；作业结束后标记 `closed`
  - [x] SubTask 2.3: 验证 CLI 路径不受影响（`Runner` 默认参数下行为一致）

- [x] Task 3: 作业队列接入事件与持久化
  - [x] SubTask 3.1: `web/jobs.py` 在 `_run` 中构造 `on_event` 回调，映射为 `probe_started`/`probe_finished` 等事件并用 `progress.publish` 发出；作业开始发 `run_started`（若 runner 未发则由 runner 发，不重复发）
  - [x] SubTask 3.2: 报告生成后发 `job_finished`（payload：total_score、verdict、risk_level、summary、probe 统计、duration_ms、report_id）；异常时发 `job_error`
  - [x] SubTask 3.3: 作业终态调用 `repository.save_detection(...)`，失败只记 warning 日志；原始 API Key 不写入数据库
  - [x] SubTask 3.4: 验证：本地起服务跑一次真实/桩检测，SSE 能看到事件序列，SQLite 里能查到记录

- [x] Task 4: 新 API 路由（前端同源 + 外部 agent）
  - [x] SubTask 4.1: 新建 `web/console.py`，实现 `POST /app/api/detect`（复用 `jobs.submit`，JSON body：protocol/base_url/api_key/model/mode/include_long_context/include_long_context_extreme）、`GET /app/api/detect/{job_id}`（状态 + 已跑/总数 + 结果摘要）、`GET /app/api/detect/{job_id}/events`（SSE，含 `Last-Event-ID` 重放、15s `: ping` 心跳、`X-Accel-Buffering: no`）
  - [x] SubTask 4.2: 实现 `GET /app/api/reports`、`GET /app/api/reports/{id}`（DB 优先，回落磁盘 JSON，避免旧 job 丢数据）
  - [x] SubTask 4.3: 实现 `/api/v1/reports`、`/api/v1/reports/{id}`、`/api/v1/reports/{id}/analysis`，统一 `X-API-Key` 校验（401 / 503 语义按 spec）
  - [x] SubTask 4.4: `web/server.py` 仅追加注册 `console.router` 与 `/app` SPA 兜底路由（`/app`、`/app/assets/*`、`/app/{path:path}` 返回 index.html）；旧路由零改动
  - [x] SubTask 4.5: 用 curl/httpx 验证：SSE 事件流、无 key 401、有 key 200、深链返回 index.html

- [x] Task 5: 搭建 React 工程并接入构建产物
  - [x] SubTask 5.1: 创建 `web-ui/`（Vite + React + TS），`vite.config.ts` 设置 `base: "/app/"`、`build.outDir: "../web/static/app"`、`emptyOutDir: true`、dev server proxy 到 `http://127.0.0.1:8000`
  - [x] SubTask 5.2: `npm install` 并 `npm run build` 产出到 `web/static/app/`，确认 `/app` 能加载打包后的 JS/CSS
  - [x] SubTask 5.3: 基础骨架：`src/main.tsx`、`src/App.tsx`（react-router：`/`、`/reports`、`/reports/:id`）、`src/styles.css`（浅色三栏布局，参考现有 `style.css` 的配色变量）

- [x] Task 6: 主控制台（配置 + 探针进度 + 评分）
  - [x] SubTask 6.1: `ConfigForm` 左栏：协议切换（anthropic/openai/gemini）、Base URL、API Key（password + 掩码显示）、模型、模式（quick/standard/full）、长上下文开关、一键提交；提交中禁用
  - [x] SubTask 6.2: `useDetectionStream` hook：`EventSource` 订阅 `/app/api/detect/{id}/events`，按 `seq` 去重、断线重连带上 `Last-Event-ID`，暴露 `probes`、`progress`、`status`、`result`、`error`
  - [x] SubTask 6.3: 探针网格：根据 `run_started` 的探针清单渲染卡片，状态色（待运行/运行中/通过/失败/跳过/错误）+ 得分 + 耗时；运行中显示脉冲动画
  - [x] SubTask 6.4: 运行日志区：滚动追加事件（时间、类型、探针名、结果），可展开看失败探针的 `error`
  - [x] SubTask 6.5: `ScorePanel` 右栏：完成进度 N/N、评分 0→最终分递增动画、SVG 环形进度、总分级与风险等级、身份/一致性/接入状态、诊断摘要与失败清单；颜色按结论
  - [x] SubTask 6.6: 完成后提供“查看完整报告”入口跳到 `/app/reports/:id`

- [x] Task 7: 报告列表与详情页
  - [x] SubTask 7.1: `/app/reports` 列表页：表格式展示时间、host、模型、模式、总分、结论，支持协议/模型/结论过滤与分页
  - [x] SubTask 7.2: `/app/reports/:id` 详情页：头部评分卡 + 探针表格（状态/得分/权重/耗时/错误摘要）+ details 折叠 JSON + 性能指标
  - [x] SubTask 7.3: 空态与错误态（无报告、id 不存在返回 404 提示）

- [x] Task 8: 联调与收尾
  - [x] SubTask 8.1: 全链路自测：`npm run build` → 起 `uvicorn web.server:app` → 提交一次检测（桩/真实）→ 前端进度与评分正常 → 刷新 `/app/reports/:id` 正常
  - [x] SubTask 8.2: 回归验证旧站：`/`、`/claude`、`/leaderboard`、`/faq`、`/api/status/{id}` 行为不变
  - [x] SubTask 8.3: 运行现有测试 `.\venv\Scripts\python.exe -m pytest -q`（若环境齐全），确认 `runner.py` 改动未破坏 CLI 测试

# Task Dependencies

- Task 2 与 Task 1 无依赖，可并行（Task 2 不依赖数据库）。
- Task 3 依赖 Task 1、Task 2。
- Task 4 依赖 Task 1、Task 2、Task 3。
- Task 5 与 Task 1–4 无代码依赖，可并行启动，但 Task 6 依赖 Task 4 的接口契约。
- Task 6 依赖 Task 4、Task 5。
- Task 7 依赖 Task 4、Task 5。
- Task 8 依赖全部完成。
