"""FAQ content for /faq.

Single source of truth: drives both the rendered HTML page AND the FAQPage
JSON-LD schema embedded in <head>. Editing one place keeps the two in sync.

Authoring rules (see docs/SEO_AI_GEO_PLAN.md §3.2 for why):

* Each answer is **definition-first**: state the concrete answer in the
  first sentence, then expand with specifics or numbers.
* Keep answers ≤ ~280 Chinese chars — long enough to give an LLM something
  to cite, short enough that the FAQPage schema doesn't bloat.
* Use real numbers (token counts, percentages, signature lengths) wherever
  possible — pages with concrete numbers get cited 33% more by AI engines.
* Anchors (id) become deep-linkable via /faq#<category>-<question> — never
  rename them after publication; that breaks external links.
* The order inside a category is the order users see; put the highest-
  intent / most-searched-for question first.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FAQEntry:
    id: str
    q: str
    a: str


@dataclass(frozen=True)
class FAQCategory:
    id: str
    title: str
    intro: str
    entries: tuple[FAQEntry, ...]


FAQ_CATEGORIES: tuple[FAQCategory, ...] = (
    FAQCategory(
        id="basics",
        title="中转站基础",
        intro=(
            "「AI API 中转站」是第三方搭建的 API 转发服务,把你的请求代理给 OpenAI / Anthropic / "
            "Google 官方,再把响应返回给你。中转站千差万别 — 有的 1:1 透传,有的会改写字段、"
            "替换模型、注入 system prompt。这一组问题先讲清楚什么是中转站、为什么需要、风险在哪。"
        ),
        entries=(
            FAQEntry(
                "what-is-relay",
                "什么是 AI API 中转站?",
                "中转站(relay,又叫「镜像」「代理」)是第三方搭建的 API 转发服务,"
                "把你的请求转给 OpenAI / Anthropic / Google 官方,再把响应原样返回给你。"
                "常见出现原因:国内无法直连官方域名、便宜的批发价、统一一个接口调多家模型。",
            ),
            FAQEntry(
                "why-use-relay",
                "为什么需要中转站?直接用官方不行吗?",
                "三个常见原因:① 国内访问 anthropic.com / openai.com 需要科学上网;"
                "② 部分中转站把多家厂商的批发额度打包零售,价格比官方低 30-70%;"
                "③ 一些中转站把 Claude / GPT / Gemini 全统一成 OpenAI 兼容接口,代码只用维护一套。",
            ),
            FAQEntry(
                "relay-vs-official",
                "中转站和官方 API 有什么区别?",
                "官方 API 字段、签名、错误格式都是确定的;中转站之间差异巨大 — "
                "有的 1:1 透传,有的会改写 usage 字段、剥离 thinking 块、用别的模型替换、"
                "甚至注入额外 system prompt。Veridrop 检测的就是这种「中间到底动了什么」。",
            ),
            FAQEntry(
                "relay-legal",
                "中转站合法吗?用了会不会出问题?",
                "法律上灰色地带,看具体合作模式。真正的实务风险有三:"
                "① <strong>跑路风险</strong> — 中转站随时可能停服、涨价、卷款消失,大额充值要慎重;"
                "② <strong>模型响应跟官方不一致</strong> — 中转站可能在中间动手脚"
                "(替换模型、剥离能力、虚报 token),影响你下游业务的稳定性;"
                "③ <strong>prompt 内容被记录</strong> — 涉及商业机密 / 用户数据的对话最好避开"
                "来路不明的中转站。建议小额测试,选信誉好的运营方,大额场景考虑直连官方。",
            ),
            FAQEntry(
                "relay-steal-key",
                "用中转站的 API key 有什么风险?",
                "Veridrop 实测过很多家中转站,常见的坑有 5 种(都见过真实案例):"
                "① <strong>偷偷换成别的模型</strong> — 你买的是 GPT,实际给你跑的可能是 Claude 或其他"
                "更便宜的模型。表面上看不出来,但写出来的内容风格、思路、解题习惯都不对,"
                "用来做正经事很容易翻车。"
                "② <strong>高级功能用不了</strong> — 比如让模型严格按指定格式回话、"
                "让它读 PDF、调用工具这些进阶能力,在被「偷换」的中转站上经常失灵 — "
                "因为后台换上来的便宜模型根本不会这一套。"
                "③ <strong>计费数字不可信</strong> — 同样一句话问几次,每次扣的钱都不一样;"
                "有的中转站干脆不告诉你这次用了多少 token,你根本对不上账。"
                "充值的钱很可能比官方贵不少,却没有任何凭证可查。"
                "④ <strong>菜单上的模型实际没货</strong> — 它对外宣称支持几十个模型,"
                "你真去用其中一些,直接报「没有这个模型」。Veridrop 在你提交检测前会先帮你"
                "试一下,免得你白等半分钟拿到一份全 0 分的报告。"
                "⑤ <strong>规则说变就变</strong> — 同一把 key,今天用得好好的,"
                "第二天可能突然提示「需要升级套餐」「分组失效」甚至直接封号。"
                "中转站后台规则可以无通知调整,大额充值之前最好先小额试用,"
                "并跑一次 Veridrop 看看实际表现。",
            ),
        ),
    ),
    FAQCategory(
        id="authenticity",
        title="真伪识别",
        intro=(
            "中转站「假货」是行业最大的痛点 — 你以为在用 Claude,实际可能跑的是 Kiro / Amazon Q;"
            "你以为在用 GPT-4o,实际后端是 Claude Haiku。这一组问题讲清楚作假手段和检测方法。"
        ),
        entries=(
            FAQEntry(
                "verify-authentic",
                "怎么知道我用的是真 Claude / GPT / Gemini 而不是替身?",
                "三个方向:① 协议字段(id 前缀、object、finish_reason 是否符合官方规范);"
                "② 能力指纹(thinking signature 加密签名、PDF 多模态、function calling shape);"
                "③ 用量字段(usage 里有没有混入异源痕迹如 claude_cache_creation_*)。"
                "Veridrop 把这三类合成 7-10 项检测。",
            ),
            FAQEntry(
                "common-fake-tactics",
                "中转站常见的「作假」手段有哪些?",
                "5 种常见:① 用 Kiro / Amazon Q / Bedrock 跑 Claude 替身;"
                "② 用 Claude 后端跑 GPT 请求(usage 字段会暴露 claude_* 残留);"
                "③ 把 GPT-4o 偷换成 GPT-4o-mini 省成本;④ 剥离 thinking 块、PDF 多模态等高级能力;"
                "⑤ 注入额外 system prompt 改写身份回答。",
            ),
            FAQEntry(
                "ask-who-are-you",
                "直接问「你是谁」能验证模型真假吗?",
                "不能。现代模型都被训练得知道怎么回答身份问题,而且中转站可以注入 system prompt "
                "让模型说「我是 Claude」。Veridrop 把「身份一致性」检测的权重设为 5%,"
                "只作为最弱的辅助信号 — 真正可信的是 thinking signature 这种加密级指标。",
            ),
            FAQEntry(
                "kiro-amazon-q",
                "Kiro / Amazon Q 假冒 Claude 怎么识破?",
                "Kiro 和 Amazon Q 是 Amazon 的 Claude 替身网关,响应没有 Claude thinking signature "
                "(它们走的是 AWS Bedrock 的简化接口,不返回服务端签名)。"
                "Veridrop 在 thinking_signature 检测上会直接判 0 分,fail 整体检测。",
            ),
            FAQEntry(
                "gpt-becomes-claude",
                "中转站把 GPT 请求转给 Claude 后端,怎么发现?",
                "看响应的 usage 字段。原生 OpenAI 只有 prompt_tokens / completion_tokens / total_tokens "
                "三个键。如果出现 claude_cache_creation_5_m_tokens、usage_source: anthropic 等字段,"
                "基本可以判定中转站在做协议转换。Veridrop 把这种指纹标记为 critical 级别。",
            ),
        ),
    ),
    FAQCategory(
        id="claude",
        title="Claude API 中转站",
        intro=(
            "Claude 是 Veridrop 检测最深入的协议 — 因为 Claude 有 thinking signature 这种加密级"
            "可验证指标,业内目前只有 Veridrop 利用了这个信号。"
        ),
        entries=(
            FAQEntry(
                "thinking-signature",
                "thinking signature 是什么?为什么是真伪验证的金标准?",
                "thinking signature 是 Claude 启用扩展思考时,响应里 signature 字段返回的"
                "服务端加密产物,长度 500-2000 字符。这个签名由 Anthropic 服务端生成,"
                "带加密验证 — 中转站理论上无法伪造。Veridrop 把它作为 25% 权重的核心检测项。",
            ),
            FAQEntry(
                "claude-code-relay",
                "Claude code 中转站怎么挑?",
                "关键看 4 点:① 跑一次 Veridrop 看 thinking signature 是否拿到 100 分;"
                "② 看 usage 里有没有 claude_* 之外的异源字段;"
                "③ 看 stream / non-stream 一致性(中转站常常在两条路径上做不同处理);"
                "④ 看消息 id 是否符合 msg_ / toolu_ / srvtoolu_ 前缀规范。",
            ),
            FAQEntry(
                "claude-detector-coverage",
                "Veridrop 的 Claude 检测覆盖哪些维度?",
                "11 项,按权重:thinking signature (25%) > behavioral signature (15%) > "
                "structured output (12%) > consistency / knowledge / token usage (各 10%) > pdf (8%) > "
                "identity / protocol / integrity / message_id (各 5%)。"
                "可在控制台手动勾选探针(全部适用探针共 11 项,约 75 秒)。"
            ),
            FAQEntry(
                "claude-stream-no-thinking",
                "Claude 中转站 stream 模式没返回 thinking 怎么回事?",
                "这是 Claude Opus 4.7 已知的 API drift — adaptive thinking + streaming + summarized "
                "的组合下,SSE 流里不出现 thinking 块(non-stream 正常)。"
                "Veridrop 的 thinking_signature detector 已经针对这个切到非流式,确保检测可靠。",
            ),
            FAQEntry(
                "claude-cost",
                "检测时为什么要消耗 token?成本多少?",
                "检测发真请求,调用次数取决于你勾选的探针数量(全选约 12 个 API 调用),token 总消耗 ~3000-5000 "
                "(取决于 thinking 是否触发)。按 Haiku 价格 ~$0.012,Sonnet ~$0.05,"
                "Opus ~$0.20。Veridrop 自己不收费 — 你付的是上游 API 的真实 token 钱。",
            ),
            FAQEntry(
                "claude-zero-score",
                "检测出 0 分是不是这家中转站完全没用?",
                "看具体情况。常见 0 分原因:① API key 失效或额度耗尽(检测无效 banner 会提示);"
                "② 中转站不支持你选的模型(preflight 会提前 422 拒绝);③ 真的是非常垃圾的中转站。"
                "看每项 detector 的 details 而不是只看总分。",
            ),
        ),
    ),
    FAQCategory(
        id="openai",
        title="OpenAI 中转站",
        intro=(
            "OpenAI 协议没有像 thinking signature 这样的加密级指标,Veridrop 在 OpenAI 上主要做"
            "「协议合规 + 适配层指纹识别」 — 抓中转站用 Anthropic / Google 后端伪装 GPT 的痕迹。"
        ),
        entries=(
            FAQEntry(
                "openai-fake-evidence",
                "OpenAI 中转站把 GPT 偷换成 Claude 有什么直接证据?",
                "响应的 usage 字段是最直接的证据。真原生 OpenAI 只有 prompt_tokens / "
                "completion_tokens / total_tokens。如果发现 claude_cache_creation_5_m_tokens、"
                "usage_source: anthropic、或 input_tokens / output_tokens(Anthropic 命名),"
                "基本可以判定中转站在协议转换。Veridrop 标为 critical,直接拒绝该项 detector。",
            ),
            FAQEntry(
                "openai-75-marginal",
                "我的 GPT-4o 检测出 75 分,verdict 是 marginal,怎么解读?",
                "75 分本身在「通过」区间,但如果有任何 critical 级别的 issues "
                "(尤其 protocol detector 报多个 critical),Veridrop 会把 verdict 砍到 marginal — "
                "圆圈变黄、提示「存在风险」。这通常意味着接口能用但中转站在做协议转换,"
                "实际跑的可能不是真的 GPT 模型。",
            ),
            FAQEntry(
                "openai-strict-json",
                "response_format=json_schema strict 不生效是什么意思?",
                "OpenAI strict 模式应该返回纯 JSON,不能包 Markdown 代码块。"
                "如果返回 ```json {...} ``` 格式,有两种可能:① 中转站没透传 response_format 参数;"
                "② 透传了但底层模型(很可能不是 GPT)不理解 strict 模式。"
                "Veridrop 通过 markdown_json_seen 标志区分这两种情况。",
            ),
            FAQEntry(
                "openai-model-mismatch",
                "我用的中转站 model 字段返回 gpt-4o,但响应明显不像 GPT,怎么办?",
                "model 字段只是字符串,中转站可以填任何值。真伪要看其他指标:"
                "① Veridrop 协议规范性 detector 会扫 usage 字段指纹;"
                "② 行为差异(GPT 偏简洁,Claude 偏礼貌啰嗦);"
                "③ 多次调用的 token 数 CV(模型一致性 detector)。",
            ),
            FAQEntry(
                "openai-no-models",
                "OpenAI 中转站测出来 0 个模型可用怎么办?",
                "Veridrop 的预探测会列出该 key 在中转站上有多少模型属于本协议。"
                "如果当前页是 OpenAI 但 0 个 GPT 模型,会显示一个黄色卡片提示"
                "「该 key 在此中转站没有 GPT 模型,但有 X 个 Claude 模型 / Y 个 Gemini 模型 → "
                "一键跳转测那边」。",
            ),
        ),
    ),
    FAQCategory(
        id="gemini",
        title="Gemini API 中转站",
        intro=(
            "Veridrop 的 Gemini 检测只走 OpenAI 兼容路径(POST /chat/completions),"
            "因为 99% 的第三方 Gemini 中转站都用这条。Gemini 3 系列默认开 thinking,"
            "对 max_tokens 设置和 token 用量统计有特殊要求。"
        ),
        entries=(
            FAQEntry(
                "gemini-model-not-found",
                "为什么 Gemini API 中转站经常 model_not_found?",
                "三种可能:① 中转站只代理部分 Gemini 模型(很多中转站只有 3.x preview 系列,没有 2.5);"
                "② 模型名拼写差异(gemini-2.5-flash vs models/gemini-2.5-flash);③ 模型已下架。"
                "Veridrop 提交前会做 preflight,500ms 内识别死模型并给出可用列表替代。",
            ),
            FAQEntry(
                "gemini-thinking-default",
                "Gemini 3.x preview 检测时 max_completion_tokens 应该填多少?",
                "至少 64,推荐 128+。Gemini 3 默认开 thinking,会消耗 30-60 reasoning_tokens 才输出文本。"
                "如果 max 太小(< 32),thinking 占满后没空间出文本,响应就是空字符串 + "
                "finish_reason=length。Veridrop 的 detector 已经把 max 调到 64-384。",
            ),
            FAQEntry(
                "gemini-no-native",
                "Veridrop 为什么不支持 Gemini 原生 /v1beta/models/X:generateContent 路径?",
                "99% 的第三方 Gemini 中转站只暴露 OpenAI 兼容协议,Google 官方也提供 OpenAI 兼容"
                "端点 /v1beta/openai。维护两套独立的检测逻辑成本高且容易引入翻译层导致结果失真,"
                "所以 Veridrop 集中在 OpenAI 兼容路径上做透。",
            ),
            FAQEntry(
                "gemini-pick",
                "Gemini 中转站怎么挑?",
                "三档:① 必须支持 gemini-2.5-flash 或 gemini-3-flash-preview(2026 stable);"
                "② Veridrop 上跑出 protocol 检测 ≥ 80;"
                "③ usage 字段没有 gemini_* 之外的异源残留(意味着没在转协议)。"
                "Veridrop 自动按推荐顺序排序中转站的模型列表。",
            ),
            FAQEntry(
                "gemini-google-official",
                "Google 官方 Gemini API 也能用 Veridrop 测吗?",
                "可以。base_url 填 https://generativelanguage.googleapis.com/v1beta/openai,"
                "api_key 用 Google AI Studio 申请的 AIza... key。Google 官方端点应该全 7 项 100 分,"
                "可作为基线对比第三方中转站。",
            ),
        ),
    ),
    FAQCategory(
        id="veridrop",
        title="Veridrop 工具使用",
        intro="工具本身的使用、报告解读、自托管等问题。",
        entries=(
            FAQEntry(
                "how-to-use",
                "Veridrop 怎么用?最简流程?",
                "三步:① 打开 veridrop.org/claude(或 /openai、/gemini);"
                "② 填中转站 base_url + api_key,选模型(下拉自动列出该 key 在该中转站可用的模型);"
                "③ 点「开始检测」,30-60 秒拿到报告。报告 URL 永久可分享(/r/{job_id}),也能下载 JPG。",
            ),
            FAQEntry(
                "probe-selection",
                "检测时怎么选探针?",
                "在控制台的「探针选择」区手动勾选:可按厂商页签切换,按分类批量全选或清空,"
                "「全选可用」只勾选当前模型适用的探针。至少勾选一个探针才能开始检测。",
            ),
            FAQEntry(
                "how-to-read-score",
                "报告分数怎么解读?",
                "总分加权:≥85 优秀(绿)/ 70-84 通过(浅绿)/ 50-69 marginal(黄)/ <50 未达标(红)。"
                "但任意 detector 报 critical 级 issue,verdict 会被砍到 marginal(圆圈变黄)— "
                "即使分数够 70 也不绿。点每项 detector 看 details 里的 sub_checks 找扣分原因。",
            ),
            FAQEntry(
                "report-link-public",
                "检测报告链接 /r/xxx 谁能看到?",
                "任何人拿到链接都能看(匿名、不加密)。报告里不含你的 API key 明文,"
                "只有脱敏形式(sk-y7xU••••••0h)。如果你测的是私有中转站不想公开,不要分享 URL。"
                "后续会加 opt-in 的「私有报告」开关。",
            ),
            FAQEntry(
                "self-host",
                "可以本地自托管 Veridrop 吗?",
                "可以,Veridrop 完全开源。clone 仓库 → pip install -e .[web] → "
                "uvicorn web.server:app 即可。CLI 也可以单独用:relay-detector detect "
                "--base-url ... --api-key ... --mode full。详见 README。",
            ),
        ),
    ),
    FAQCategory(
        id="privacy",
        title="隐私与安全",
        intro=(
            "把 API key 交给一个检测工具是有风险的事,所以 Veridrop 在数据处理上的承诺需要"
            "可验证 — 我们的代码完全开源,你可以审计、可以自托管。"
        ),
        entries=(
            FAQEntry(
                "key-stored",
                "Veridrop 会记录我的 API key 吗?",
                "不会。API key 只存在 job 内存对象里,job 跑完(成功或失败)立即清空,"
                "不写报告 JSON、不写日志、不写磁盘。报告里 key 是脱敏形式 (sk-y7xU••••••0h),"
                "代码开源可验证。",
            ),
            FAQEntry(
                "vs-cctest-privacy",
                "跟 cctest.ai 在数据安全上有什么区别?",
                "cctest.ai 也声称不上传 key,但代码闭源无法验证。Veridrop 完全开源,"
                "你可以 clone 代码自己跑(git clone ... && uvicorn web.server:app),"
                "或者审计服务端代码(GitHub 上完全公开)。",
            ),
            FAQEntry(
                "upstream-cost",
                "检测过程会调用上游产生费用吗?",
                "会,但很少。调用次数取决于勾选的探针,全选约 12 个真实请求,token 总消耗 3000-5000。"
                "按 Haiku 大约 $0.012,Sonnet $0.05,Opus $0.20。"
                "这个钱付给上游(中转站或官方),Veridrop 自身不收费。",
            ),
            FAQEntry(
                "personal-info",
                "报告里有什么个人信息?可以删除吗?",
                "报告只包含:① 脱敏后的 base_url;② 脱敏后的 api_key;③ 检测结果。"
                "没有个人姓名、邮箱、IP。如果需要删除特定报告,GitHub issue 联系即可。",
            ),
        ),
    ),
    FAQCategory(
        id="compare",
        title="工具对比",
        intro=(
            "中转站检测领域不止 Veridrop 一家,这一组讲清楚 Veridrop 跟同类工具的差异、"
            "以及怎么挑中转站。"
        ),
        entries=(
            FAQEntry(
                "vs-cctest",
                "Veridrop 跟 cctest.ai 有什么区别?",
                "cctest 只做 Claude(单协议),用「黑盒检测」对抗规避,但维度有限。"
                "Veridrop 三协议(Claude / OpenAI / Gemini)+ 加密级 thinking signature + "
                "跨协议自动跳转 + 预提交死模型识别 + 完全开源,且 OpenAI 上能识别"
                "「GPT 实为 Claude」的协议适配层指纹。",
            ),
            FAQEntry(
                "vs-hvoy",
                "Veridrop 跟 hvoy.ai 有什么区别?",
                "hvoy 也支持三协议,但检测维度浅(主要是协议合规)。"
                "Veridrop 在 Claude 上有加密级 thinking signature 验证(独家)、"
                "OpenAI 上有协议转换指纹识别(usage_source 等 critical 级)、"
                "Gemini 上适配 thinking-by-default 模型,深度更够。",
            ),
            FAQEntry(
                "vs-leaderboards",
                "中转站对比榜单(aiapipk.com / 知乎评测)跟 Veridrop 是什么关系?",
                "那些是「主观评测」或「广告导向」,Veridrop 是「客观技术检测」。"
                "可以互补:用榜单初筛信誉好的中转站,再用 Veridrop 做技术验证。",
            ),
            FAQEntry(
                "how-to-pick-relay",
                "怎么挑中转站?有什么硬性指标?",
                "Veridrop 推荐 5 条硬指标:① thinking signature ≥ 100;"
                "② protocol critical_issue_count = 0;③ stream / non-stream usage 一致;"
                "④ 模型 id 字段匹配请求模型(model_consistency pass);"
                "⑤ 多次请求 completion_tokens CV < 0.10(稳定性)。",
            ),
        ),
    ),
)


def total_question_count() -> int:
    return sum(len(c.entries) for c in FAQ_CATEGORIES)


def faqpage_jsonld() -> dict:
    """Build a single FAQPage schema.org JSON-LD covering every entry.

    Schema spec recommends one FAQPage per page with all Q/A as mainEntity.
    Multiple FAQPages on the same URL are NOT recommended — they confuse
    the LLM extractor about which set is canonical.
    """
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": entry.q,
                "acceptedAnswer": {"@type": "Answer", "text": entry.a},
            }
            for cat in FAQ_CATEGORIES
            for entry in cat.entries
        ],
    }


def find_entry(anchor: str) -> tuple[FAQCategory, FAQEntry] | None:
    """Look up a Q by its anchor (e.g. 'claude-thinking-signature').

    Anchor format: '<category-id>-<entry-id>'. Used by product pages that
    deep-link to specific FAQ entries.
    """
    for cat in FAQ_CATEGORIES:
        prefix = cat.id + "-"
        if not anchor.startswith(prefix):
            continue
        entry_id = anchor[len(prefix):]
        for e in cat.entries:
            if e.id == entry_id:
                return cat, e
    return None
