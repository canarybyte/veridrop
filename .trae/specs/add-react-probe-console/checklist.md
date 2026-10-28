# Checklist

## 数据库与持久化
- [x] `pyproject.toml` 的 web extras 含 `sqlalchemy`、`aiosqlite`、`asyncpg`，安装成功且可导入
- [x] 默认 `DATABASE_URL` 指向 `sqlite+aiosqlite:///./web_data/veridrop.db`，无需任何额外配置即可建表
- [x] `detections` 表字段覆盖：id、protocol、base_url、host、model、mode、status、total_score、verdict、risk_level、summary、run_error、self_reported_identity、detected_brands、performance、report_json、created_at/started_at/finished_at
- [x] `probe_results` 表与 `detections` 一对多，含 name/display_name/status/score/weight/duration_ms/error/details
- [x] 作业成功与失败两种终态都会落库
- [x] 数据库只保存掩码后的 API Key，原始 Key 不落库、不出现在 SSE 与报告中
- [x] 数据库不可用时作业仍能完成、JSON 落盘与 SSE 推送不受影响

## 进度事件与 SSE
- [x] `Runner` 新增回调为可选参数，默认行为与改动前完全一致（CLI 不受影响）
- [x] 事件格式为 `{"seq","ts","type","payload"}`，SSE 带 `id:` 与 `event:` 字段
- [x] 事件序列包含 `queued`、`run_started`、`probe_started`、`probe_finished`、`job_finished` / `job_error`
- [x] `run_started` 携带实际选中探针清单，前端不硬编码探针数量
- [x] 被动探针在 finalize 时也会推 `probe_finished`
- [x] `Last-Event-ID` 重连能重放缺失事件，缓存上限 500 条
- [x] 作业已结束时连接 SSE 会重放并立即结束，不无限挂起
- [x] 空闲 15s 发送 `: ping` 心跳，响应头含 `X-Accel-Buffering: no`
- [x] 回调/事件推送异常不会影响检测结果

## 新 API
- [x] `POST /app/api/detect` 复用 `jobs.submit`，参数校验失败返回 4xx 且信息可读
- [x] `GET /app/api/detect/{job_id}` 返回状态与进度计数，未知 id 返回 404
- [x] `GET /app/api/reports` 支持分页与 protocol/model/verdict 过滤
- [x] `GET /app/api/reports/{id}` DB 缺失时回落磁盘 JSON
- [x] `/api/v1/*` 全部要求 `X-API-Key`，与 `VERIDROP_AGENT_API_KEY` 比对为常量时间比较
- [x] 未配置 `VERIDROP_AGENT_API_KEY` 时 `/api/v1/*` 返回 503
- [x] 错误/缺失 key 返回 401 且不泄露 key 是否“接近正确”
- [x] `/api/v1/reports/{id}/analysis` 返回面向 agent 的紧凑载荷（评分、结论、风险、逐探针、issues、身份、性能）
- [x] 前端 `/app/api/*` 无需密钥即可使用

## React 前端
- [x] `/app` 返回 React 应用，`/app/reports`、`/app/reports/:id` 深链刷新不 404
- [x] Vite `base` 为 `/app/`，构建产物输出到 `web/static/app/`，FastAPI 能正确托管 assets
- [x] 左栏表单包含协议、Base URL、API Key、模型、模式与长上下文开关；提交中禁用重复提交
- [x] 中间探针网格按事件流实时更新状态色/得分/耗时，运行中有动画
- [x] 运行日志按序追加事件，失败探针可查看错误
- [x] 右栏评分从 0 递增到最终分并伴环形动画，展示 N/N 进度、总分级、风险等级
- [x] 风险等级判定符合 spec（critical 或 <50 = 高；50–69 = 中；其余 = 低）
- [x] 评分颜色随结论变化（通过/基本合格/未达标）
- [x] 完成后可跳转 `/app/reports/:id` 查看完整报告
- [x] 报告列表与详情页数据来自数据库，空态与 404 有友好提示
- [x] 前端代码不包含任何服务端 agent 密钥

## 兼容与验证
- [x] 旧站 `/`、`/claude`、`/openai`、`/gemini`、`/leaderboard`、`/faq`、`/r/{id}`、`/api/status/{id}` 行为未变
- [x] `pytest` 既有用例通过（环境允许时）
- [x] 本地全链路实测：构建前端 → 起服务 → 跑检测 → SSE 进度 → 评分 → 入库 → 列表/详情可查
- [x] `README.md` 或 `docs/` 中补充了 `/app` 启动方式与 `DATABASE_URL`、`VERIDROP_AGENT_API_KEY` 说明（仅说明，不改旧文档主体）
