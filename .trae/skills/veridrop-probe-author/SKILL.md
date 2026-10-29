---
name: veridrop-probe-author
description: 在 Veridrop 项目中新增、改造或审查探测探针（detector）时使用；覆盖分类/权重/模式、Active/Passive 结构、事件与采集、评分与测试规范，并提供脚手架命令。
---

# Veridrop 探针开发规范

## 何时使用

- 新增一个探针（detector），或把某个检测点拆成/合并为探针。
- 改造现有探针的判定逻辑、分类、权重或运行模式。
- 审查探针代码是否符合项目规范（成本、异常、采集、测试）。
- 排查「探针不出现 / 不运行 / 不参与评分」的问题。

## 探针体系速览

### 位置

```
src/relay_detector/
├── core/
│   ├── detectors_base.py    # BaseDetector / ActiveDetector / PassiveDetector
│   ├── models.py            # DetectorResult、Mode、DetectorStatus
│   ├── probes.py            # PROBE_CATEGORIES（5 个分类 id）
│   ├── runner.py            # 按 mode + applies_to 选探针，并发执行
│   ├── scorer.py            # compute_total / critical 硬否决
│   └── trace.py             # record_exchange / probe_scope
└── protocols/<protocol>/
    ├── config.py            # DETECTOR_WEIGHTS、MODE_DETECTORS
    ├── client.py            # ThrottledClient：自动采集、广播给 passive
    ├── runner.py            # 组装 core Runner
    └── detectors/
        ├── __init__.py      # build_all()：唯一注册入口
        ├── base.py          # 兼容性 re-export
        └── <name>.py        # 一个探针一个文件
```

协议包只有三个：`anthropic`、`openai`、`gemini`。`gemini` 走 OpenAI Chat
Completions 兼容协议，所以请求方法和 openai 相同。

### Active vs Passive

| | ActiveDetector | PassiveDetector |
|---|---|---|
| 是否发请求 | 是，自己控制 prompt/参数 | 否，零 token 成本 |
| 入口 | `async def run(self, client, model) -> DetectorResult` | `def observe(request, response, headers, latency_ms)` |
| 收尾 | `run()` 直接返回结果 | `def finalize(self) -> DetectorResult` |
| 并发 | Runner 用 `asyncio.gather` 并发执行 | 每个 active 请求完成后由客户端回调 |
| 异常 | Runner 的 `_timed_run` 会兜底成 `error` | 客户端只广播成功响应；`finalize` 自己兜底 |

被动探针适合「所有请求都能顺带检查」的规则（ID 前缀、usage 字段形状、
流式事件序列等），不要为了采集去额外发请求。

### DetectorResult 字段

定义在 `src/relay_detector/core/models.py`：

| 字段 | 说明 |
|---|---|
| `name` | 内部名，snake_case，必须与 config 中的键一致 |
| `display_name` | 前端展示名，中文 |
| `status` | 只能是 `"pass"` / `"fail"` / `"skip"` / `"error"` |
| `score` | 0–100，pydantic 强校验 |
| `weight` | 非负；通常等于类属性 `weight` |
| `details` | 证据字典，前端会展示；`issues` / `critical_issue_count` 有特殊含义 |
| `duration_ms` | active 由 `_timed_run` 自动补；passive 为 null |
| `error` | 异常文本，禁止放明文 API key |

状态语义：

- `pass` / `fail`：探针正常完成，参与加权评分。
- `skip`：模型不适用、缺少观测、用户未开启可选能力（如长上下文）。**skip 不计入分母**，不要用它掩盖失败。
- `error`：请求异常或代码异常。Runner 对 active 异常会兜底，但 `run()` 内部仍应显式 `try/except`，把失败阶段写进 `details`。

### 权重与总分

`core/scorer.py` 的 `compute_total()`：

```
effective_weight = Σ weight   (status != "skip")
total = Σ (score × weight) / effective_weight
```

- 权重只在「该探针实际运行」时影响总分；skip 完全不参与。
- 权重是相对值，不要求总和为 100。新增探针默认 10；高置信度证据（加密签名、
  上下文造假）可给 15–25，低成本辅助检查给 5。
- `weight = 0` 的探针不会影响总分，但仍会出现在报告里。

### critical 硬否决

`effective_verdict()` 会把 verdict 上限锁在 `marginal`，即使总分 ≥ 70：

- `details.critical_issue_count` 为大于 0 的整数；或
- `details.issues` 里任意一项 `severity == "critical"`。

如果探针发现「几乎确定不是真品」的证据（换芯、后端指纹、签名伪造），
应写入 `issues` 并标 `critical`，而不是只扣分了事。普通扣分用 `score`。

## 分类规范

分类 id 固定为 5 个，定义在 `core/probes.py`（只用于前端分组，不参与评分）：

| category | 语义 | 适合放什么 |
|---|---|---|
| `basic` | 基础与协议 | 连通性、请求/响应字段形状、ID 前缀、SSE 序列 |
| `identity` | 身份与真伪 | 模型自述、加密签名、行为指纹、知识题 |
| `capability` | 能力完整性 | PDF、结构化输出、函数调用、长上下文、思考能力 |
| `consistency` | 一致性与完整性 | 模型字段一致性、stream/non-stream 一致性、多次采样稳定性 |
| `billing` | 计费与用量 | usage 字段真实性、token 计数、缓存计费 |

选择建议：

- 先问「这条证据证明什么」：证明它是谁 → `identity`；证明它能做什么 →
  `capability`；证明两次回答自洽 → `consistency`；证明账目可信 → `billing`；
  「协议本身长得对不对」→ `basic`。
- 一个探针只选一个分类。跨类逻辑拆成两个探针，避免分类语义漂移。
- 不要新增分类 id；前端按固定 5 类分组，未知分类会排到最后。

## 新增步骤

### 1. 生成骨架

```powershell
.\venv\Scripts\python.exe scripts\scaffold_probe.py `
  --protocol anthropic `
  --name cache_probe `
  --display-name "缓存命中验证" `
  --category billing `
  --weight 10 `
  --kind active `
  --passive false
```

脚手架会：

- 校验 `--name`（`^[a-z][a-z0-9_]*$`）、分类、权重；
- 生成 `protocols/<protocol>/detectors/<name>.py`；
- 自动在 `detectors/__init__.py` 的 import、`build_all()`、`__all__` 中注册；
- 打印后续步骤。已存在文件时默认报错，`--force` 才覆盖。

`--kind passive`（或 `--passive true`）生成被动探针骨架。

### 2. 登记权重与模式

打开 `src/relay_detector/protocols/<protocol>/config.py`：

```python
DETECTOR_WEIGHTS = {
    ...
    "cache_probe": 10.0,
}

MODE_DETECTORS = {
    Mode.QUICK: {...},
    Mode.STANDARD: {...},
    Mode.FULL: set(DETECTOR_WEIGHTS.keys()),   # 多数协议：full 自动包含
}
```

`MODE_DETECTORS` 是 Runner 的唯一选探针依据（不是类属性 `modes`）。至少加入
`Mode.FULL`；成本低且快速的探针才考虑 quick/standard。三个协议当前都把
`Mode.FULL` 写成 `set(DETECTOR_WEIGHTS.keys())`。

> 探针目录接口 `GET /app/api/probes` 展示的 `modes` 读取的是类属性
> `BaseDetector.modes`（默认三档全含）。如果新探针只应出现在 full，建议同时写
> `modes = {Mode.FULL}`，避免前端展示与 Runner 实际行为不一致。

### 3. 实现判定

Active：

```python
async def run(self, client, model: str) -> DetectorResult:
    try:
        _req, resp, _headers, latency_ms = await client.messages_create(...)
    except Exception as e:  # noqa: BLE001
        return self._result("error", 0.0, {"stage": "request"}, error=str(e))
    # 逐项子检查，累加 score；details 里放证据
    return self._result("pass" if score >= 70 else "fail", score, details)
```

Passive：

```python
def observe(self, request, response, headers, latency_ms) -> None:
    self._observations += 1
    # 记 issue，不发请求

def finalize(self) -> DetectorResult:
    if self._observations == 0:
        return self.skip("no observations")
    return self._result(status, score, {...})
```

要求：

- 子检查要能解释「为什么扣分」，`details` 里保留原始字段与期望范围。
- 面向用户的结论字段建议加 `evaluation_zh`，与现有探针保持一致。
- 不适用时用 `skip(reason)`，不要返回 `pass`。

### 4. 成本与超时

- 每个 active 探针的请求数要写进模块 docstring。full 模式总成本应可控
  （Haiku 基准约 $0.02；长上下文/极限档是显式 opt-in）。
- 可选高成本探针（长上下文等）必须读 `self.config.include_long_context` 等
  开关，未开启时 `return self.skip(...)`。
- 单请求超时、429/503 退避、重试次数由 `ThrottledClient` 统一处理，
  探针里不要再写 `sleep` 或自建重试。
- 整体超时由 Runner 的 `overall_timeout_s` 控制；不要用无限循环或超长 prompt
  拖垮整次检测。

### 5. 事件与采集

- `trace.record_exchange(...)` 已经由 `ThrottledClient` 在每次请求后自动调用，
  **探针里不要自己调用**，否则同一次交换会被记录两次。
- `Runner` 用 `trace.probe_scope(d.name)` 包住每个 active 探针，采集记录会自动
  带上探针名；前端按探针名展示请求/响应。
- 进度事件（`probe_started` / `probe_finished`）由 Runner 发送，探针不要
  自己 `on_event`。
- 需要额外上下文的采集，放到 `details` 里随结果返回即可。

### 6. 测试

新建 `tests/test_<name>.py`，只做离线测试：

```python
import httpx
import pytest

from relay_detector.protocols.openai.client import OpenAIChatClient
from relay_detector.protocols.openai.detectors.cache_probe import CacheProbeDetector


@pytest.mark.asyncio
async def test_cache_probe_passes_on_valid_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": "gpt-5.4-mini",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        })

    client = OpenAIChatClient("https://api.example.com", "sk-test")
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        headers={"authorization": "Bearer sk-test"},
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await CacheProbeDetector().run(client, "gpt-5.4-mini")
    finally:
        await client.aclose()

    assert result.status == "pass"
```

测试清单：

- 成功分支（符合预期 → pass）与失败分支（字段异常 → fail）各至少一条。
- 请求异常分支（mock 500 / 超时）→ `status == "error"`，且 `error` 有内容。
- `applies_to()` 的模型过滤，两个方向都要断言。
- 被动探针：先 `observe()` 若干样本，再 `finalize()`；零观测必须是 `skip`。
- 用 `httpx.MockTransport` 或 `respx`，**禁止在测试里发真实网络请求**。
- 断言 `details` 中的关键证据字段，而不只是 status。

## 代码规范清单

- 文件命名与 `name` 一致，snake_case；一文件一探针。
- 类名 PascalCase + `Detector`（脚手架自动生成）。
- 类属性齐全：`name` / `display_name`（中文）/ `category` / `description`（单句）/
  `weight`。
- 模块 docstring 中文，写清目的、请求数与大致成本、适用模型。
- 注释中文；TODO 必须可搜索（`TODO:`）。
- 所有外部请求都包在 `try/except Exception` 里（`# noqa: BLE001`），并返回
  `"error"` 结果，不能让异常冒泡。
- 不要吞掉关键错误：异常要进 `error` / `details`，不能静默 `pass`。
- 不要记录明文 key、`Authorization` header 或完整请求头。
- 端口/模型参数差异放在 `applies_to()` 或 config，不要散落硬编码。
- 分数计算要有依据：每项子检查的权重之和 = 100，阈值写进常量。
- 权重是相对值；改动现有探针权重前先看 `DETECTOR_WEIGHTS` 的分布。
- 新增文件不要 import 未安装的依赖；脚手架只用标准库 + 项目已有包。

## 常见反例

1. **在被动探针里发请求**：被动探针的 `observe()` 是同步回调，不能 `await`，
   也不该产生 token 成本。
2. **探针内部再调 `trace.record_exchange`**：同一次请求被记录两次，前端
   exchanges 数量翻倍。
3. **用 `skip` 掩盖失败**：skip 不计分母，大量 skip 会把总分抬高；只有真的
   不适用才 skip。
4. **异常直接 `return self._result("pass", 100.0)`**：上游 404/500 被当成
   通过，报告失真。
5. **测试打真实 API**：CI 无 key 会失败，且会消耗额度；一律 mock。
6. **忘记登记 config.py**：`build_all()` 里有实例，但 `MODE_DETECTORS` 没有
   name，Runner 直接把它当 `mode-excluded` 跳过，前端只能看到 skip。
7. **name 与文件名/注册不一致**：`name` 必须等于 config 里的键，否则永远选不中。
8. **把 `details` 当日志**：不要塞超长原文或含密钥的 header；大文本要截断。

## 验证命令

```powershell
# 1. 静态编译（脚手架生成后立刻验证）
.\venv\Scripts\python.exe -m py_compile src\relay_detector\protocols\<protocol>\detectors\<name>.py

# 2. 全量测试
.\venv\Scripts\python.exe -m pytest -q

# 3. 探针目录自查（需要本地服务已启动）
#    返回 categories / probes(name, display_name, description, category,
#    passive, weight, modes, applies) / defaults
curl "http://localhost:8000/app/api/probes?protocol=anthropic&model=claude-haiku-4-5"

# 4. 不走服务时，直接确认注册生效
.\venv\Scripts\python.exe -c "from relay_detector.protocols.anthropic.detectors import build_all; print([(d.name, d.category, d.weight) for d in build_all()])"
```

前端无需改动：控制台按 `category` 自动分组，探针通过目录接口自动出现。
