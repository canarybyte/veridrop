# AGENT.md · Veridrop 项目结构说明

面向 AI 编码代理的项目导览。用于快速定位代码、理解分层与约定。详细产品介绍见 [README.md](README.md)，探针设计见 [DESIGN.md](DESIGN.md)。

> 注意：README 中的“项目结构”树有部分过时（例如未列出 `web-ui/`、`web/` 下的新模块、`protocols/openai/` 的 Azure 探针）。以本文件和实际目录为准。

## 1. 项目是什么

AI API 中转站（relay）真伪与质量检测工具。输入 `base_url + api_key + model`，并发执行一组探针请求，把结果与官方基线、协议规范、评分规则做比对，输出评分、verdict 与风险项。

支持三个协议：

| 协议包 | 说明 |
|---|---|
| `anthropic` | Anthropic Messages API（Claude） |
| `openai` | OpenAI Chat Completions（GPT，含 Azure 行为探针） |
| `gemini` | Gemini 的 OpenAI 兼容接口，请求方式与 `openai` 相同 |

## 2. 顶层目录

```
veridrop/
├── src/relay_detector/     # 核心 Python 包：探针、调度、评分、CLI
├── web/                    # FastAPI 服务端：页面、任务、数据库、API
├── web-ui/                 # React + Vite 控制台（/app），构建产物输出到 web/static/app/
├── tests/                  # pytest 测试
├── scripts/                # 工具脚本（探针脚手架、PDF 生成等）
├── data/baselines/         # 官方真品基线（JSON）
├── docs/                   # 设计/规划文档（含 dev/ 下的 GPT-Azure 判断标准）
├── .trae/skills/           # 项目内 skill，veridrop-probe-author 为探针开发规范
├── .trae/specs/            # 功能 spec（spec.md / tasks.md / checklist.md）
├── bench.sh                # 用官方 key 收集基线
├── deploy.sh               # rsync 部署脚本
├── veridrop.service        # systemd unit
├── pyproject.toml          # 依赖与包配置
├── AzureGPT鉴别测试.py      # 独立的 Azure GPT 鉴别脚本（不属于 veridrop 包）
└── AzureGPT鉴别-离线自检.py  # 上述脚本的离线桩测试
```

注意：`web-ui/node_modules/` 与 `web/static/app/` 是生成内容，不要手改。

## 3. 核心包 `src/relay_detector/`

### 3.1 `core/`：协议无关的框架层

| 文件 | 职责 |
|---|---|
| `detectors_base.py` | `BaseDetector` / `ActiveDetector` / `PassiveDetector` 基类，`_result()` / `skip()` |
| `models.py` | `Mode`（quick/standard/full）、`DetectorResult`、`DetectorStatus`、`ExecutionConfig` |
| `probes.py` | **探针分类体系** `PROBE_CATEGORIES` 与展示顺序 `CATEGORY_ORDER` |
| `runner.py` | 按 mode 与 `applies_to` 选取探针，并发执行，异常兜底为 `error` |
| `scorer.py` | 加权评分 `compute_total`，critical 硬否决，verdict 阈值 |
| `trace.py` | 请求/响应采集 `record_exchange`、`probe_scope`，用于原始报文留存 |
| `long_context.py` | needle-in-haystack 长上下文的共享逻辑 |
| `comparator_framework.py` | 与官方基线对比的通用框架 |

分类当前有六个：`basic`、`identity`、`capability`、`consistency`、`billing`、`azure`。分类只用于展示分组，**不参与评分**。

### 3.2 `protocols/<协议>/`：协议实现

每个协议包结构基本一致：

```
protocols/<协议>/
├── __init__.py         # 协议入口：build_detectors / make_client / build_runner / verdict_caption 等
├── config.py           # DETECTOR_WEIGHTS（权重）与 MODE_DETECTORS（模式成员）
├── client.py           # HTTP 客户端（httpx）+ ThrottledClient（并发限流、重试、采集、广播给 passive）
├── runner.py           # 组装 core Runner
└── detectors/
    ├── __init__.py     # build_all()：探针唯一注册入口
    ├── base.py         # 兼容性 re-export
    └── <name>.py       # 一个探针一个文件
```

`anthropic/` 额外有 `comparator.py`，用于基线比对。

**探针清单（以 `build_all()` 为准）**：

- `openai`：`basic_request`、`model_consistency`、`function_calling`、`structured_output`、`protocol`、`integrity`、`token_billing`、`long_context`、`azure_api_version`、`azure_content_filter`、`azure_model_echo`。另有 `token_parity` 未注册，仅被测试显式引用。
- `anthropic`：`identity`、`behavioral_signature`、`thinking_signature`、`consistency`、`knowledge`、`pdf`、`structured_output`、`long_context`、`protocol`、`message_id`、`token_usage`、`integrity` 等。
- `gemini`：`basic_request`、`model_info`、`function_calling`、`structured_output`、`protocol`、`integrity`、`token_usage`。

Azure 三个探针（`azure_*`）只在 GPT 协议下，分类为 `azure`，只属于 full 模式。它们依赖 `client.chat_completions_create(query_params=...)` 传递 `api-version` 等 URL 查询参数。

### 3.3 `cli.py`、`client.py`、`runner.py`、`scorer.py`、`report.py` 等顶层文件

顶层模块是较早的通用入口，`cli.py` 提供 `detect` / `compare` / `ping` 三个命令（typer）。新代码优先放在 `core/` 与 `protocols/` 下。

## 4. Web 服务 `web/`

| 文件 | 职责 |
|---|---|
| `server.py` | FastAPI 路由：旧站页面（`/`、`/claude`、`/openai`、`/gemini`、`/r/{id}`、`/leaderboard`、`/faq`），以及 `/app` 控制台 |
| `console.py` | React 控制台（`/app`）的后端接口与 SSE 进度 |
| `jobs.py` | 检测任务队列（asyncio，限制并发），API Key 只存在于内存 `Job` 中，结束后清空 |
| `progress.py` | 探针粒度的进度事件（SSE，支持 `Last-Event-ID` 断线重放） |
| `probe.py` | 提交前 `GET /v1/models` 探活 |
| `probe_catalog.py` | 把协议探测器元数据整理成前端探针目录（分类、适用性、权重） |
| `deep_probe.py` | 深度诊断（13–25 项，`deep=true` 才运行，短时采样） |
| `credentials.py` | 凭据库：API Key 用 Fernet 加密保存，用户需为凭据命名 |
| `db.py`、`repository.py` | 数据库与持久化；默认 SQLite，`DATABASE_URL` 可切换为 Postgres |
| `leaderboard.py` | 中转站红黑榜聚合 |
| `image_report.py` | 报告转 JPG 卡片 |
| `ratelimit.py` | IP 限速 |
| `faq_data.py` | FAQ 内容（含 JSON-LD） |

### 4.1 环境变量

| 变量 | 说明 |
|---|---|
| `DATABASE_URL` | 默认 `sqlite+aiosqlite:///./web_data/veridrop.db` |
| `VERIDROP_AGENT_API_KEY` | 外部 agent 只读 API 的密钥，未设置时 `/api/v1/*` 返回 503 |
| `VERIDROP_SECRET_KEY` | 凭据加密用 Fernet key；未设置则读写 `VERIDROP_SECRET_FILE` |
| `VERIDROP_SECRET_FILE` | 默认 `web_data/secret.key` |

### 4.2 外部 agent API

`GET /api/v1/reports`、`GET /api/v1/reports/{id}`、`GET /api/v1/reports/{id}/analysis`，通过 `X-API-Key` 或 `Authorization: Bearer` 鉴权。

## 5. 前端控制台 `web-ui/`

React + TypeScript + Vite。

| 路径 | 职责 |
|---|---|
| `src/App.tsx`、`main.tsx` | 应用入口与路由 |
| `src/pages/console.tsx` | 检测控制台：左侧配置，中间探针进度与日志，右侧评分 |
| `src/pages/reports.tsx`、`report-detail.tsx` | 报告列表与详情 |
| `src/pages/credentials.tsx` | 凭据管理 |
| `src/components/probe-picker.tsx` | 探针选择器：厂商页签（Claude / GPT / Gemini）、分类分组、全选与清空 |
| `src/components/probe-grid.tsx`、`event-log.tsx`、`exchange-viewer.tsx` | 进度网格、事件日志、原始报文查看 |
| `src/components/config-form.tsx`、`model-picker.tsx`、`credential-form.tsx` | 表单组件 |
| `src/components/score-panel.tsx`、`deep-panel.tsx` | 评分与深度诊断面板 |
| `src/api.ts` | 调用后端接口的封装（`getProbeCatalog` 等） |
| `src/hooks/use-detection-stream.ts` | 订阅 SSE 进度 |

构建：`cd web-ui && npm install && npm run build`，产物写入 `web/static/app/`。

## 6. 测试

```bash
# 完整测试（需要 venv 与全部依赖，含 rapidfuzz、pytest 等）
./venv/bin/pytest tests/ -v

# 只跑探针注册与分类相关
./venv/bin/pytest tests/test_probe_registry.py -v
```

关键测试：

- `tests/test_probe_registry.py`：探针元数据完整性、分类快照（`EXPECTED_CATEGORIES`）、分类顺序。**新增或改分类时必须同步更新此快照**。
- `tests/test_passive_detectors.py`、`test_active_detectors.py`：探针行为。
- `tests/test_openai_*.py`：openai 协议相关。
- `tests/test_leaderboard_*.py`、`test_jobs_long_context.py`：web 层。

## 7. 开发约定

### 7.1 新增探针

1. 在 `protocols/<协议>/detectors/` 新建一个文件，继承 `ActiveDetector` 或 `PassiveDetector`。
2. 设置 `name`（snake_case，与配置键一致）、`display_name`、`weight`（正数）、`category`（必须是 `PROBE_CATEGORIES` 中的 id）、`description`（一句中文）。
3. 实现 `run()`（active）或 `observe()` + `finalize()`（passive）。
4. 在 `detectors/__init__.py` 的 `build_all()` 注册。
5. 在 `protocols/<协议>/config.py` 的 `DETECTOR_WEIGHTS` 与 `MODE_DETECTORS` 中加入。
6. 更新 `tests/test_probe_registry.py` 的分类快照，并补充测试。

也可以用脚手架：

```powershell
.\venv\Scripts\python.exe scripts\scaffold_probe.py --protocol openai --name cache_probe --display-name "缓存命中验证" --category billing --weight 10 --kind active --passive false
```

完整规范见 [.trae/skills/veridrop-probe-author/SKILL.md](.trae/skills/veridrop-probe-author/SKILL.md)。

### 7.2 结果语义

- `pass` / `fail` / `error` / `skip`。`skip` 不进入评分分母。
- 证据不足时返回 `skip` 并写明 `skip_reason`，不要把不确定结果判成 `fail`。
- 报告中要留下 `evaluation_zh`（中文判定说明）与原始关键字段，便于人工复核。

### 7.3 其他约定

- 代码注释使用中文。
- API Key 不落盘：任务结束即清空内存中的 Key；报告里的 Key 脱敏显示。
- 评分、分类、verdict 的逻辑不受商业合作影响，修改时保持透明可审计。
- 不要提交 `.env`、`web_data/`、`out/`、`venv/`、`node_modules/`。

## 8. 常用命令

```bash
# 安装
python3 -m venv venv
./venv/bin/pip install -e ".[dev,web]"

# CLI
./venv/bin/relay-detector ping --model claude-haiku-4-5
./venv/bin/relay-detector detect --model claude-haiku-4-5 --mode full -o out/test.json
./venv/bin/relay-detector compare out/test.json

# Web 服务
./venv/bin/uvicorn web.server:app --host 0.0.0.0 --port 8000
# 控制台：http://localhost:8000/app

# 前端
cd web-ui && npm install && npm run build
```

Windows 下使用 `venv\Scripts\python.exe` 与 `venv\Scripts\pytest.exe`。

## 9. 已知问题与注意事项

- README 的目录树与实际不符，参考本文件。
- `web/static/app/` 是前端构建产物，改前端后必须重新 build。
- `AzureGPT鉴别测试.py` 与 `AzureGPT鉴别-离线自检.py` 是独立脚本，不属于 `relay_detector` 包，修改时不要影响 veridrop 包的探针。
- `anthropic` 协议依赖 `rapidfuzz`，缺失时 `protocols/anthropic` 无法导入，会导致 `test_probe_registry.py` 整体失败。
- 部分 Anthropic 新模型（如 Opus 4.7）对 `temperature`、`budget_tokens` 等参数有限制，相关处理在客户端层按 model 派发。
