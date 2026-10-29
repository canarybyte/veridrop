# 判断是否为 Azure GPT 协议 · 中转平台版

> 适用：通过第三方 API 中转平台接入 GPT 资源，需判断上游是 **Azure OpenAI**（`{resource}.openai.azure.com`，部署名路由、内容过滤字段、api-version）还是**标准 OpenAI 兼容**（OpenAI 官方或中转改写）。
>
> **核心结论：纯成功响应（`id`/`choices`/`usage`）无法区分 Azure 与 OpenAI 兼容——这些字段太通用。中转平台还可改写响应字段，因此「过滤字段」只能作参考。判别应按伪造代价排序，主证据是行为层：① `api-version` 敏感性（需实现路由逻辑，最难伪造）；② 内容审核真拦截；③ `model` 回显带日期戳；其次才是 ④ Azure 错误措辞；⑤ 过滤字段（可完全伪造）。** 详见 **三·补、行为层探针**。

---

## 一、官方 API 要点（判别背景）

| 对比项 | Azure OpenAI | 标准 OpenAI / 兼容中转 |
|---|---|---|
| 端点 | `https://{resource}.openai.azure.com/openai/deployments/{deployment}/chat/completions?api-version=2024-06-01` | `https://api.openai.com/v1/chat/completions` 或中转域名 |
| 路由 | **部署名（deployment）在 URL 路径**；请求体通常**不带 `model`**（Azure 兼容模式可带，但部署名优先） | **模型名在请求体 `model` 字段** |
| 必填参数 | **`api-version` query 参数必填**（缺了报 400） | 无 api-version 概念 |
| 鉴权 | `api-key: <key>` 头（也有 Entra/托管身份） | `Authorization: Bearer <key>` |
| 内容过滤 | 响应带 `prompt_filter_results` / `choices[].content_filter_results`（可配置关闭） | 无此字段 |

> 中转平台（new-api 等）通常走 **OpenAI 兼容口**（`/v1/chat/completions` + body 带 `model`），把 Azure 部署名映射为模型名。此时**路由方式已不可观察**，判别只能靠过滤字段和错误措辞。

---

## 二、成功响应判别

### 2.1 Azure 特征字段（参考级 —— 中转可伪造，不可单独作证）

```json
{
  "prompt_filter_results": [
    {
      "prompt_index": 0,
      "content_filter_results": {
        "hate": { "filtered": false, "severity": "safe" },
        "self_harm": { "filtered": false, "severity": "safe" },
        "sexual": { "filtered": false, "severity": "safe" },
        "violence": { "filtered": false, "severity": "safe" },
        "jailbreak": { "filtered": false, "detected": false },
        "indirect_attack": { "filtered": false, "detected": false }
      }
    }
  ],
  "choices": [
    { "content_filter_results": { "hate": { "filtered": false, "severity": "safe" } } }
  ]
}
```

| 字段 | 位置 | 含义 |
|---|---|---|
| `prompt_filter_results` | 顶层 | 输入内容的过滤结果（每段 prompt 一个对象） |
| `choices[].content_filter_results` | choices 内 | 输出内容的过滤结果 |
| 子字段 | `hate` / `self_harm` / `sexual` / `violence` / `jailbreak` / `indirect_attack` | 分类；各含 `filtered`（是否拦截）与 `severity`（严重度 `safe`/`low`/`medium`/`high`） |

**判读**：
- 出现 `prompt_filter_results` 或 `choices[].content_filter_results` → **Azure 底层特征迹象**，但**中转可手工拼这些字段**，只能作参考，不能单独作证（见 **三·补**）
- **缺失不判非法**——内容过滤可被 Azure 端配置关闭，也可被中转剥离；缺失只能说明"无法确认"

### 2.2 通用字段（不能证明 Azure）

`id` / `object` / `created` / `model` / `choices` / `usage` / `choices[].message.content` / `choices[].finish_reason`——OpenAI 兼容格式通用字段，**只有这些不能证明是 Azure**。

---

## 三、错误响应判别（主证据之二，最可靠）

### 3.1 Azure 原生错误（更像 Azure）

| 线索 | 示例 |
|---|---|
| `error.code = DeploymentNotFound` | 部署名不存在 |
| message 含 `deployment` | `The API deployment for this resource does not exist` |
| message 含 `api-version` | `The api-version query parameter must be set` 或版本无效 |
| message 含 `Azure OpenAI` | 官方措辞点名 |
| 429 + `Retry-After` 头 | Azure 限流（OpenAI 兼容也有，但 Azure 措辞不同） |
| 401 message 含 `api-key` / `Ocp-Apim-Subscription-Key` | Azure 鉴权措辞 |

### 3.2 OpenAI 兼容/中转错误（更像兼容层）

| 线索 | 示例 |
|---|---|
| `error.code = model_not_found` | 标准 OpenAI 措辞 |
| `error.type = invalid_request_error` / `error.param = model` | OpenAI 错误结构 |
| message 含 `model does not exist` | OpenAI 措辞 |
| `error.type = new_api_error` / `insufficient_quota` / request id 纯数字 | **中转平台层**（new-api 等），不含上游信息 |
| 503 + message 含 `No available channel ... under group (distributor)` | **new-api distributor 路由错误**：未知模型名无渠道组可路由——说明中转按模型名路由（Azure 原生会报 DeploymentNotFound） |

### 3.3 判别口诀

```
错误里有 DeploymentNotFound / deployment / api-version / Azure OpenAI
  → 很像 Azure 原生协议
错误里有 model_not_found / param: model / invalid_request_error
  → 更像 OpenAI 兼容（标准 OpenAI 或中转改写）
错误里有 new_api_error / insufficient_quota / 纯数字 request id
  → 中转平台层错误，不含上游信息，不能下 Azure 结论
```

> **推荐做法**：用不存在的模型名/部署名发一个请求，观察错误结构——这是成本最低、区分度最高的单探针。

---

## 三·补、行为层探针（中转可改写字段时的正解）

> **前提**：中转平台**可以改写响应字段**——它可以原样透传真实 Azure 响应，也可以在自己拼的响应壳里手工塞上 `prompt_filter_results`。**单看响应字段，这两种情况输出完全相同。**

因此字段证据只能算「参考级」。要判断上游真身，必须测**行为差异**——即「只有 Azure 会做、且中转顺手伪造代价高」的事情。

### 3.4.1 证据强度分级（按伪造代价排序）

| 级别 | 探针 | 伪造代价 | 说明 |
|---|---|---|---|
| **最强** | `api-version` 敏感性 | **需实现路由逻辑** | 参数是否真的参与下游路由；⚠ **前提分端点**——新版 `/openai/v1/` 不再要求 api-version，见 3.4.2 |
| **强** | 内容审核**真拦截** | 需真的把请求拦下来 | 不是"返回 200 带个字段"，是请求被拒 |
| 中 | `model` 回显**带日期戳** | 无动机伪造 | 区分的是「真上游 vs 自造响应壳」，**不是 Azure vs OpenAI**（官方 OpenAI 直连同样回快照别名） |
| 中 | `error` 措辞（DeploymentNotFound 等） | 一句 `replace()` | 文案可被整体替换，强度低于行为；**且中转通常在前置层拦掉未知模型名，根本打不到 Azure** |
| 参考 | `prompt_filter_results` 字段 | 拼一个 JSON 对象 | **可完全伪造，不可单独作证** |
| 参考 | 响应头（`apim-request-id` / `x-ms-region`） | 需改整个网关 | 中转常把上游头剥光，仅剩自己的 `Server`/`X-Oneapi-*`；剥掉 ≠ 有问题，观测到才加分 |

### 3.4.2 api-version 敏感性（**判据已收紧，且前提分端点**）

**原理**：经典 Azure 端点是 `.../deployments/<部署名>/chat/completions?api-version=<版本>`，**`api-version` 是路由的必要组成**；标准 OpenAI 的 `/v1/chat/completions` **没有 api-version 概念**。

> ⚠️ **前提已分化（2026-10-15 核对官方文档）**：微软 v1 GA 文档明确，`/openai/v1/` 路径下 **api-version 不再必需，且带上会被拒**
> （400 `api-version query parameter is not allowed when using /v1 path`）。即：**新版 Azure 端点下，非法 api-version 同样不引发分化**，
> 与「纯 OpenAI 上游」表现一致。故本探针的「无分化 → 不能判假」必须保留全量效力——
> **无分化既可能是非 Azure，也可能是新版 Azure 端点**，单此项永远不能判假。

**测法**：同一请求发三次，只改 `api-version`。

| 输入 | Azure 经典端点 | 新版 `/openai/v1/` 端点 | 标准 OpenAI 上游 |
|---|---|---|---|
| 不带 | 400 必填 | 200 | 200（忽略） |
| 合法值 | 200 | 200 | 200（忽略） |
| **非法值** | **400/404 路由失败** | **200 或 400（拒 api-version 本身）** | **200（被忽略）** |

**判据（收紧后）**：

| 观测 | 判定 | 理由 |
|---|---|---|
| 合法值 ≠ 200 | **NA**，作废本轮 | 渠道掉线/限流/配额会伪装成「非法值被分化」，本轮无证据价值 |
| 合法值 200 且非法值报错、**错误文本含 api-version 措辞** | PASS | 参数参与下游路由且措辞为 Azure 特征 |
| 合法值 200，非法值与合法值状态不同但**无 Azure 措辞** | INFO，不作正面证据 | 状态差异可能来自满负载/超时/审核，与版本无关 |
| 三者一致 | INFO，不能判假 | 新版 Azure 端点或网关吃参数，均为此表现 |

> **旧判据的问题（2026-10-15 修复）**：原实现「非法值状态不同于其余两种即 PASS」，
> 在渠道 503 掉线时会产出**假阳性**——实测 lyyueqian 站 `gpt-azure` 分组掉线期间，
> `gpt-5.6-sol`/`gpt-5.5` 被判 PASS（非法=503、不带=200、合法=503），
> 而真实原因是**渠道整组不可用**，与 api-version 无关。判据已改为先校验合法值 200、再要求错误含 Azure 措辞。

**实测样例**（api1.netwx.cn，5/5 模型一致）：

```
不带 api-version: HTTP 200
合法 api-version: HTTP 200
非法 api-version: HTTP 404 {"error":{"message":"Resource not found","type":"upstream_error"}}
```

> ⚠️ 上例是 2026-09-24 的观测，当时新版 v1 端点尚未普及；如今遇到「三者一致」不要直接判非 Azure，先按上表记 INFO。

### 3.4.3 内容审核真拦截（行为层）

**原理**：中转可以手工拼 `prompt_filter_results`，但要**真的把请求拦下来并返回审核结构**，代价高得多。

**测法**：发一段明确违规的内容，看是否被真实拦截。

| 结果 | 含义 |
|---|---|
| 被拒 + `code=content_filter` + `filtered/severity` | ✅ **真拦截**，下游确实执行了内容审核 |
| HTTP 200 + `finish_reason=content_filter` | ✅ 拦截（另一种形态） |
| HTTP 200 正常回答 | ⚠ 不直接判假：渠道审核宽松或上游已过滤，**需与其它探针合看** |

**实测样例**：

```
HTTP 400 code=content_filter
The response was filtered due to the prompt triggering Azure OpenAI's
content management policy.
```

> **注意**：错误文案里的 "Azure OpenAI" 字样**本身仍可被替换**。此探针的强度在"**真拦截**"这个行为，不在那句文案。

### 3.4.4 model 字段回显（看服务端是否自行决定）

**原理**：Azure 走 deployment 路由，响应 `model` 常回**部署名/带日期版本号**；中转自造响应壳时通常**只会原样回显请求值**。

| 回显 | 含义 |
|---|---|
| **与请求名不一致**（如带日期戳 `-2026-04-24`） | ✅ 服务端自行决定回显值，**必须来自下游** |
| 原样回显请求名 | ⚠ **无鉴别力**：官方直连和中转改写都会这样 |

**实测样例**（同站 5 个模型，全部带日期戳且代际递增）：

| 请求名 | 回显 |
|---|---|
| `gpt-5.5` | `gpt-5.5-2026-04-24` |
| `gpt-5.6-sol` | `gpt-5.6-sol-2026-07-09` |
| `gpt-5.6-terra` | `gpt-5.6-terra-2026-07-09` |
| `gpt-6-astra` | `gpt-6-astra-2026-09-03` |
| `gpt-6-sol` | `gpt-6-sol-2026-09-22` |

**判读要点**：同代共享日期（5.6 两个都是 07-09）、跨代递增、日期最新者贴近当下 —— **伪造者没有动机编出这样一套自洽的日期**（图省事只会原样回显或填同一个假日期）。

### 3.4.5 多模型一致性对照

把上述探针**在全部模型上跑一遍**，重点看**有没有哪个模型与其他不一致**：

- **全部一致** → 同一套上游路由，无渠道被单独替换
- **个别模型表现不同**（如非法 api-version 仍返 200） → **该模型被单独接上了别的渠道**，值得追查

> **实测副产品**：同一站不同模型的部署配置可能有差异。例如 `gpt-5.5` 的 `content_filter_results` 是**空对象** `{}`，其余 4 个是完整分类（`hate`/`violence`/`self_harm`）——与它版本号最老（04-24）一致。**这不代表有问题**，但说明同站多模型的底层部署配置确实不同。

### 3.4.6 探针的固有局限（务必保留）

1. **没有任何单一探针能给出 100% 结论**。中转可任意改写的前提下，能做的是**堆叠多个独立维度**，让伪造代价高于真实透传代价。
2. **「是 Azure」与「全透传」是两回事**。可能存在「上游确实是 Azure，但中转做了部分改写（剥字段、改回显）」——前者成立不代表后者成立。
3. **一致 ≠ 证明**：多维度自洽提高了可信度，但不构成数学证明。

---

## 四、判定流程

```
1. 先发成功请求：
   ├─ 有 prompt_filter_results / content_filter_results → 参考级（可被中转伪造，不单独作证）
   └─ 只有 choices/usage → 不能下结论（兼容格式）
2. 行为层三探针（伪造代价最高，优先做）：
   ├─ api-version：非法值行为分化 → 参数真的参与下游路由（最强）
   ├─ 内容审核：真拦截（请求被拒）→ 下游确实执行审核（强）
   └─ model 回显：与请求名不一致（带日期戳）→ 服务端自行决定（强）
3. 再发不存在的模型名（错误探针）：
   ├─ DeploymentNotFound / api-version / deployment → Azure 原生（强）
   ├─ model_not_found / invalid_request_error → OpenAI 兼容层
   └─ new_api_error / 纯数字 id → 中转层改写，需其他旁证
4. 多模型一致性：全部模型跑同一套探针，看有无个别模型行为不同 → 渠道被单独替换的信号
5. 若走 OpenAI 兼容口（body 带 model、无 api-version）：
   → 路由方式不可观察，Azure 出身只能靠上述组合证据
```

**结论分级**：

| 证据组合 | 结论 |
|---|---|
| 行为层 ≥2 项命中（api-version + 拦截/回显）+ 多模型一致 | **高可信 Azure 上游** |
| 仅过滤字段 | **不可作证**（中转可拼字符串伪造） |
| 仅 Azure 错误措辞 | 低可信（文案可整体替换） |
| 仅 OpenAI 兼容字段与措辞 | 不能判 Azure；更可能是标准 OpenAI 或中转 |
| new_api_error 等平台层错误 | 无法判断上游，需渠道审计 |

> **注意**：「是 Azure 上游」与「全透传无改写」是两条独立结论。中转可以做「上游真 Azure + 中间剥字段/改回显」——前者成立不代表后者成立。

---

## 五、边界与注意

1. **过滤字段可被关闭/剥离**：Azure 端可配置关闭内容过滤，中转也可能剥离 → 缺失**不判非法**，只降证据强度。
2. **`usage` 一致性**：`prompt_tokens` 应随输入长度增长；长短输入 token 完全相同 → 计数疑似伪造。
3. **`max_tokens` 边界**：超限应 4xx（`maximum context length` 等）；非法模型名返回 200 → 网关未校验，可疑。
4. **`model` 回显**：Azure 兼容口回显的 `model` 常是部署名或映射后的模型名，与请求体不一致**不一定是掉包**（部署名映射所致），需结合能力探针。
5. **防掉包**：GPT 版本差异（gpt-5.4 vs 旧版）用能力探针（数感、知识截止、推理）判别；Azure 无"上限反查"类免费探针，主要靠行为差分。
6. **新版模型用 `max_completion_tokens`**：gpt-5.x 已弃用 `max_tokens`（400 `unsupported_parameter`，提示改用 `max_completion_tokens`）；脚本已自动回退（遇该 400 改用 `max_completion_tokens` 重试），手测 5.x 模型时直接传 `max_completion_tokens`。

---

## 六、隐式提示词缓存（OpenAI/Azure 自动缓存）

与 Anthropic 不同，**OpenAI / Azure 的提示词缓存是隐式自动的**——前缀 ≥ **1024 token** 时自动缓存，**无需 `cache_control` 断点**（Azure 也不支持 `prompt_cache_breakpoint` / `prompt_cache_options`）。

| 维度 | Anthropic Claude | OpenAI / Azure GPT |
|---|---|---|
| 开启 | 显式 `cache_control` 断点 | **隐式自动**，前缀 ≥1024 token |
| 命中字段 | `cache_read_input_tokens` | **`usage.prompt_tokens_details.cached_tokens`** |
| 写入字段 | `cache_creation_input_tokens` | gpt-5.6+ 新增 `cache_write_tokens` |
| 最小前缀 | 按模型 1024-4096 | 固定 **1024**，不足则 `cached_tokens=0` |
| 命中粒度 | 全前缀 | GPT-5.5 及更早：128-token 块递增匹配；**GPT-5.6 起：最小 1024 token 的扁平阈值，不再按 128 取整** |
| 计费 | 写 1.25× / 读 0.1× | 读约 0.25-0.5×；gpt-5.6+ 写 1.25×（计费比例来自 OpenAI 官方文档，脚本不验证） |

**判读**（连发同长前缀，读 `cached_tokens`，**至少 8–10 轮**）：

- 首轮 0、其余全部 >0 → **单一部署**，隐式缓存正常（健康）
- 命中轮数介于两者之间、且每轮只有 **≈0 或 ≈100%** 两种取值（无中间值）→ **多部署/多区域各自独立缓存**，命中率＝命中已预热部署的比例
- 恒 0 → ① 前缀不足 1024 token；② 多渠道负载均衡但都未预热；③ 网关未透传 `prompt_tokens_details`；④ 每次都路由到新部署
- 首轮即 >0 → 跨运行 / 跨租户残留缓存

**⚠ 轮数不足会得出错误结论（2026-10-28 实测）**

cc.relay.waninter.com 同一固定前缀（2494 token）连发 10 轮：

| 模型 | 逐轮命中 | 命中率 | 区域 |
|---|---|---|---|
| gpt-6.1-sol | 5/10 | **50%** | Australia East |
| gpt-5.6-sol | 5/10 | **50%** | Australia East（另有 East US 2） |

每轮 `cached` 只有两个值：`0` 或 `2491/2494`，**从无中间值**——这是多个 Azure 部署各自独立缓存的指纹。命中时几乎整段前缀都命中（99.7%），说明缓存机制本身工作正常，只是同一前缀每次被路由到不同部署。

同一站点用**旧版 3 轮探针**跑出来的判词是「有缓存字段但从不命中」，而实际上命中率是 50%。3 轮样本在 30–60% 命中概率下极易连续全 0 或全命中，**把随机事件判成了确定性结论**。现在脚本默认 10 轮，并按命中率+分布形态出判词。

> 与 Anthropic 一样：**只写不读 / 从不命中 ≠ 假模型**——多是渠道层多账号负载均衡稀释；缓存行为不作真伪判据，只影响成本与命中率。Azure 隐式缓存默认开启、无需配置，正常应能命中；恒 0 时优先怀疑多渠道或前缀不足。

**对成本的实际含义**：多部署站点的缓存省钱效果约等于命中率本身（上例约 50%），达不到接近 100%。要完全命中需网关做会话粘滞（sticky routing），这不是中转站的常规实现。

---

### 模型能力门控与证据分级

- 文本模型才执行 usage/隐式缓存/工具/结构化输出探针；`dall-e-*`、`gpt-image-*` 只执行生图能力探针，不能套文本模型判据。
- 不支持的参数、未透传的端点、配额或权限错误统一报告为“未验证/不适用”，不作为掉包结论。
- Azure 过滤字段、DeploymentNotFound、响应头只能形成“Azure 协议一致性迹象”；中转可改写，不能单独证明物理上游。**行为层探针（api-version / 内容审核 / model 回显）伪造代价更高，应作为主证据；但多维度自洽仍不等于数学证明**（见 3.4.6）。

## 七、配套脚本

同目录 **`AzureGPT鉴别测试.py`**（Python 3.8+，仅标准库）。VS Code 打开后在顶部 `CONFIG` 填 `base_url`/`api_key`/`models`，▶ Run 或 F5 即可；自动在同目录生成 **`AzureGPT鉴别报告.md` / `.json`**（字段中文）。探针：模型列表、成功响应结构 + Azure 过滤字段、错误判别（DeploymentNotFound vs model_not_found）、**api-version 敏感性（行为层：非法值是否引发分化）**、**model 回显（部署名 vs 请求名）**、**内容审核真拦截（行为层，最难伪造）**、max_tokens 边界、usage 一致性、**隐式缓存（`cached_tokens`，连发同前缀看命中）**；支持 `--only <探针>` / `--models a,b` 定向复测省额度；**`models` 留空时才拉取 `/models` 列表并手动选择要测的模型**（上游可能修改模型 id，不预先填写；非交互环境默认测试全部）；在配置区设 `mode`（`1`=仅文本 / `2`=仅生图 / `3`=两者，默认 3）选择测试类型。**文本/生图模型分开测**：`image_models` 列表（如 `gpt-image-1`/`dall-e-3`）单独走 `/images/generations`，检查返回 `b64_json`（PNG 魔数）或图片 URL——生图模型返回文本 → 路由错位/掉包。HTTP 层已带**浏览器 UA**（部分中转在 Cloudflare 后，默认 urllib UA 会被 403 拦截）。

**行为层三探针定向复测**（推荐在中转可改写字段时使用）：

```bash
python AzureGPT鉴别测试.py --config <cfg.json> --only apiversion
python AzureGPT鉴别测试.py --config <cfg.json> --only model_echo
python AzureGPT鉴别测试.py --config <cfg.json> --only content_filter
```

配合 `--models a,b,c` 可在多个模型上横向比对，观察有无个别模型行为不一致（渠道被单独替换的信号）。
