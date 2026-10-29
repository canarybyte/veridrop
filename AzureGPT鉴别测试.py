#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AzureGPT鉴别测试.py —— 中转 GPT（Azure OpenAI / OpenAI 兼容）渠道 · 行为探针
配套文档：API判断标准--GPT-Azure.md

作用：对"从中转站购买的 GPT 资源"逐项做行为差分测试，判断上游是否为 Azure OpenAI
      （或 OpenAI 兼容），以及有没有被掉包成其他模型。
      判据分层：
        ① 成功响应里的 Azure 过滤字段（prompt_filter_results / content_filter_results）
        ② 错误响应的措辞（DeploymentNotFound / api-version → Azure 原生；
           model_not_found / invalid_request_error → OpenAI 兼容中转）
        ③ 路由方式（deployment 在 URL vs model 在 body）+ usage 一致性
      注意：纯 OpenAI 兼容成功响应（id/choices/usage）无法区分 Azure 与 OpenAI 官方，
            过滤字段与错误措辞才是判别主证据。

运行方式（VS Code）：
  1. 改文件顶部 CONFIG（base_url / api_key / models）
  2. ▶ Run 或 F5；或终端: python AzureGPT鉴别测试.py
  3. 报告自动落盘同目录：AzureGPT鉴别报告.md / .json
  依赖：仅 Python 3.8+ 标准库。
"""

import copy
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from datetime import datetime

# ★共享模块：证据账本 + 致命错误★（上一级目录，各鉴别脚本复用）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    import 证据与致命错误 as _evx
except ImportError:
    _evx = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# ★结果目录与脚本分离★ —— 结果统一写到 结果\ 子目录，脚本/文档/配置留在原层
RESULTS_DIR = os.path.join(SCRIPT_DIR, "结果")


def _safe_name(s):
    """把 base_url/标签转成可用于目录名与文件名的安全字符串(Windows 非法字符 → _)。"""
    out = "".join(("_" if ch in '<>:"/\\|?*' else ch) for ch in str(s or ""))
    out = "".join(ch for ch in out if ch.isprintable()).strip(" .")
    return out or "unknown"


def out_dir_for(base_url, label=""):
    """输出目录 = {脚本前缀}-鉴别结果-{域名}-{标签}-{时间戳}。
    例: AzureGPT-鉴别结果-biezou.com-azure-20260923_223123
    不要协议头与结尾 /v1, 使目录名可直接辨认被测站点。"""
    u = re.sub(r"^https?://", "", str(base_url or ""))
    u = re.sub(r"/v\d+/?$", "", u).rstrip("/")
    host = _safe_name(u)
    # ★标签不进目录名★：目录只保留「请求地址-日期_时间」，便于扫读；
    #   标签（--name / config.name）仍写进报告正文。
    _ = label
    return os.path.join(RESULTS_DIR,
                        f"AzureGPT-鉴别结果-{host}-{datetime.now().strftime('%Y%m%d_%H%M%S')}")


# 每次测试结果输出到独立目录; 真正的目录在 main() 拿到 base_url 后重建。
# ⚠ 此处**不再 os.makedirs** —— 导入时就建目录会在每次运行留下一个无站点名的空壳
#   （main() 会重建带域名的目录，这个占位目录就永远空着）。目录统一由 main() 创建。
RESULT_OUT_DIR = os.path.join(RESULTS_DIR, f"AzureGPT-鉴别结果-{datetime.now().strftime('%Y%m%d_%H%M%S')}")
MD_REPORT = os.path.join(RESULT_OUT_DIR, "AzureGPT鉴别报告.md")
JSON_REPORT = os.path.join(RESULT_OUT_DIR, "AzureGPT鉴别报告.json")

# =====================================================================
# CONFIG —— 手动配置区
# =====================================================================
CONFIG = {
    "relay": {
        "base_url": "https://site-g.example",   # 只填域名（不含 /v1）；/v1 由脚本拼接
        "api_key": "sk-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
        "model": "gpt-5.5",                    # 主测模型（models 留空且拉取失败时的兜底）
    },
    "models": [                                # 待测模型列表（留空=先拉取 /models 再手动选择；上游可能修改模型 id）
        "gpt-5.6-sol",                         # 深度推理档（$5/$30）
        # "gpt-5.6-terra",                       # 均衡档（$2.50/$15）
        # "gpt-5.6-luna",                        # 高速低价档（$1/$6）
        # "gpt-5.5",
        # "gpt-5.4",
        # "gpt-5-mini",
    ],
    "image_models": [                          # 生图模型列表（留空=从拉取列表手动选择，回车=不测；走 /images/generations）
        "gpt-image-2",
    ],
    "mode": 3,                                # 测试类型（VS Code 配置区改）：1=仅文本 / 2=仅生图 / 3=两者（默认）
    "request": {
        "max_tokens": 1024,
        "timeout": 120,
        "verify_tls": True,
    },
    "probes": {
        "models_list": True,     # 模型列表 / 连通性
        "structure": True,       # 成功响应结构 + Azure 过滤字段
        "azure_consistency": True,  # 3 轮过滤字段/响应 id/模型回显一致性（强旁证）
        "structured_output": True,  # 严格 JSON Schema 结构化输出（强旁证）
        "tool_use": True,        # 完整 tool-call → tool-result 闭环（强旁证）
        "headers": True,         # 响应头（中转层头 vs Azure 原生 apim-request-id/x-ms-region）
        "error_probe": True,     # 错误判别（DeploymentNotFound vs model_not_found）
        "apiversion": True,      # api-version 敏感性（行为层：非法值是否引发分化）
        "model_echo": True,      # model 字段回显（部署名 vs 请求名）
        "content_filter": True,  # 内容审核真拦截（行为层，最难伪造）
        "max_tokens": True,      # max_tokens 边界
        "max_tokens_limit": 130000,  # ★超限探针发送的 max_tokens★
                                     # 中转按此值**预扣额度**：曾用 1,000,000 →
                                     # 单次预扣 ¥10+，小余额账户直接被 403 拦下、
                                     # 整场测试提前终止。130000 足以触发 GPT 系
                                     # 128K/64K 上限校验，预扣仅约 ¥1.3。
                                     # 若模型上限 ≥ 该值（判成 200），可调大重测。
        "usage": True,           # usage 一致性（token 随输入增长）
        "cache": True,           # 隐式提示词缓存（cached_tokens；OpenAI/Azure 自动缓存）
        "image_gen": True,       # 生图模型测试（image_models 列表；文本/生图模型区分）
    },
    "flow": {
        "parallel_models": True,     # ★模型并行★ 每模型起独立子进程(各自写报告); 多模型总耗时≈单模型
                                     #   各模型探针互不依赖; 缓存 TTL 等待是纯 sleep, 并行不加压上游
                                     #   调试单模型时设 False(走串行)
        "sleep_between": 2,      # 探针间间隔秒
        # ★轮数不能太少★ —— 实测 cc.relay.waninter.com：同一固定前缀连发 10 轮，
        #   命中率 6/10（另一模型 3/10），且只有 0% / 99.7% 两极值，从无中间值。
        #   这是多个 Azure 部署各自独立缓存的典型特征（打到已预热部署才命中）。
        #   旧默认 3 轮很容易撞上"全 0"或"全命中"，把随机事件判成确定性结论。
        "cache_rounds": 10,      # 缓存探针连发轮数（1 写 + 后续读；≥8 才有统计意义）
        "cache_gap": 1,          # 缓存连发间隔秒：隐式缓存每轮独立写，1~2s 间隔给后续轮命中机会（0=背靠背易全不命中误判）
    },
    "report": {
        "save_md": True,
        "save_json": True,
    },
}

STATUS_LABEL = {"PASS": "通过", "SUSPICIOUS": "可疑", "FAIL": "失败",
                "NA": "未验证", "INFO": "参考"}

# 探针用的最小 max_tokens —— 实测部分中转(如 biezou.com)要求 >= 16, 用 8 会直接 400:
#   Invalid 'max_output_tokens': integer below minimum value. Expected a value >= 16, but got 8
# 结果会让「多轮一致性 / 隐式缓存」等探针整条变成"未验证"(误以为是资源问题)。
# 统一用一个安全下限, 避免各处再散落硬编码。
MIN_MAX_TOKENS = 16


def label(status):
    return STATUS_LABEL.get(status, status)


def _deep_merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


# 浏览器 UA：部分中转在 Cloudflare 后，默认 urllib UA 被挡(HTTP 403 code 1010)，须伪装浏览器
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


LAST_HEADERS = {}


def http_post(url, headers, payload, timeout, verify_tls=True):
    """返回 (status, body)；响应头存到全局 LAST_HEADERS 供响应头探针读取。

    为什么单独留头：Azure 原生会回 apim-request-id / x-ms-region 等头，伪造成本高于 body 字段；
    中转层（nginx / new-api）则回 X-Oneapi-Request-Id / X-New-Api-Version / Server，可作反向识别。

    ⚠ 致命错误检测放在**这一层**而不是 chat() 里：api-version 探针等会绕过 chat()
      直接调本函数，只在 chat() 检测会漏掉它们的 401（实测 2026-10-16 踩过）。
    """
    global LAST_HEADERS, FATAL_ERROR
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = dict(headers or {})
    hdrs.setdefault("User-Agent", BROWSER_UA)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    ctx = ssl_create(verify_tls)

    def _note(status, body):
        # ⚠ 这里的 global 不能省：函数内给 FATAL_ERROR 赋值会把它当**局部变量**，
        #   于是 "not FATAL_ERROR" 先抛 UnboundLocalError（实测 2026-10-16 踩过：
        #   子进程直接崩溃，父进程只报"0/N 个模型正常结束"，看着像快速终止实则是崩）。
        global FATAL_ERROR
        _f = fatal_error_kind(status, body)
        if _f and not FATAL_ERROR:
            FATAL_ERROR = _f
            print(f"  ⛔ **致命错误（{_f}）** —— 该错误重试无意义，将立即终止测试")
        _rec_ev(url, payload, status, body)
        if _evx is not None:
            _evx.note_status(status, body)
        return status, body

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read().decode("utf-8", "replace")
            LAST_HEADERS = {k.lower(): v for k, v in resp.headers.items()}
            return _note(resp.status, body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        LAST_HEADERS = {k.lower(): v for k, v in (e.headers or {}).items()}
        return _note(e.code, body)
    except urllib.error.URLError as e:
        LAST_HEADERS = {}
        return -1, f"URLError: {e.reason}"
    except Exception as e:
        LAST_HEADERS = {}
        return -1, f"{type(e).__name__}: {e}"


def ssl_create(verify):
    import ssl
    return ssl.create_default_context() if verify else ssl._create_unverified_context()


RAW_LOG = []           # 每轮请求的原始报文（请求体/状态码/响应头/响应体），落盘供事后复核
MAX_RAW_BODY = 200000  # 单条响应体留存上限（字符）；超出截断并标记
PROBE_CONTEXT = [""]   # 当前探针名（原始报文按探针归类）


def set_probe(name):
    """标记当前正在执行的探针，使原始报文能归到具体探针名下。"""
    PROBE_CONTEXT[0] = name


def chat(cfg, model, messages, extra=None, max_tokens=None, fatal_check=True):
    """POST {base}/chat/completions（OpenAI 兼容口）。

    新版 GPT（5.x）已弃用 max_tokens（400 unsupported，要求 max_completion_tokens）——
    首次遇该 400 时自动改用 max_completion_tokens 重试一次；老模型不受影响。
    """
    global FATAL_ERROR
    base = bare_base(cfg["relay"]["base_url"])
    if base.endswith("/chat/completions"):
        url = base
    else:
        url = base + "/v1/chat/completions"
    mt = max_tokens or cfg["request"]["max_tokens"]
    body = {"model": model, "max_tokens": mt,
            "messages": messages}
    if extra:
        body.update(extra)
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {cfg['relay']['api_key']}"}
    status, text = 429, ""
    for attempt in range(3):
        status, text = http_post(url, headers, body,
                                 cfg["request"]["timeout"],
                                 cfg["request"].get("verify_tls", True))
        if status != 429 or attempt == 2:
            break
        time.sleep(4 * (attempt + 1))
    low = text.lower()
    if status == 400 and "max_completion_tokens" in low:
        # 新版模型：max_tokens → max_completion_tokens 重试一次（错误消息提及 max_completion_tokens 即触发）
        body.pop("max_tokens", None)
        body["max_completion_tokens"] = mt
        status, text = http_post(url, headers, body,
                                 cfg["request"]["timeout"],
                                 cfg["request"].get("verify_tls", True))
    # ★原文留存★ —— 报告只存结论摘要；失败细节(尤其 4xx 的完整错误体)必须原样落盘。
    #   ⚠ 失败响应体不截断 —— 截断的正是最需要原文的地方。
    raw_body = text if isinstance(text, str) else str(text)
    truncated = len(raw_body) > MAX_RAW_BODY
    RAW_LOG.append({
        "模型": model,
        "探针": PROBE_CONTEXT[0],
        "url": url,
        "请求体": body,
        "状态码": status,
        "响应头": dict(LAST_HEADERS or {}),
        "响应体": raw_body[:MAX_RAW_BODY] + ("\n[_已截断]" if truncated else ""),
    })
    parsed = None
    if status == 200:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
    # ★致命错误: 重试也没用的那类 —— 记下来让 main() 立刻终止★
    #   与网络类失败的区别: 网络的会自愈(等一等就好), 这类不会 ——
    #   key 失效/被封、余额耗尽, 之后再发多少次都是同样结果。
    #   实测教训 2026-10-16: key 在跑测中途被封, 后续几十个探针逐个撞 401,
    #   白耗十几分钟且产不出一行有效结论 —— 应当立即停。
    #   ⚠ fatal_check=False 用于「故意触发大额预扣」的探针（max_tokens 边界）：
    #     那里的余额不足是**可预期的探针副作用**，不该终止整场测试。
    if fatal_check:
        _fatal = fatal_error_kind(status, text)
        if _fatal and not FATAL_ERROR:
            FATAL_ERROR = _fatal
            print(f"  ⛔ **致命错误（{_fatal}）** —— 该错误重试无意义，将立即终止测试")
    return status, parsed, text


FATAL_ERROR = ""           # 非空 = 遇到"重试也无用"的错误(key 失效/余额耗尽), main() 立即终止
ABORT_REASON = ""          # 提前终止的人话原因，写进报告结论
# 鉴权类失败：状态码 + 关键串双判，避免把 Cloudflare 403(1010) 之类偶发拦截误判成致命
FATAL_AUTH_HINTS = ("invalid token", "无效的令牌", "invalid api key", "无效的key",
                    "authentication_error", "unauthorized", "令牌无效",
                    "insufficient_quota", "insufficient_user_quota", "quota is not enough",
                    "余额不足", "配额不足", "token quota is not enough")


def fatal_error_kind(status, text):
    """返回致命错误类型（"鉴权失败"/"余额不足"），非致命返回 ""。

    这两类错误的共同点是**再发一次结果完全一样** —— key 不会自己变有效、
    余额不会自己涨。继续跑只会在每个探针上重复失败，既浪费时间又产出误导性的
    "未验证"满天飞（看报告的人会以为是资源本身有问题）。
    """
    low = (text or "").lower()
    if status in (401, 403):
        for hint in FATAL_AUTH_HINTS:
            if hint in low:
                return "余额不足" if ("quota" in hint or "余额" in hint or "配额" in hint) else "鉴权失败"
    return ""


def content_text(resp):
    """取 choices[0].message.content（字符串或块数组）。"""
    try:
        msg = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(msg, list):
        return "".join(b.get("text", "") for b in msg if isinstance(b, dict))
    return str(msg)


def _error_brief(status, raw):
    if status == -1:
        return raw[:160]
    try:
        j = json.loads(raw)
        if isinstance(j, dict) and isinstance(j.get("error"), dict):
            e = j["error"]
            msg = e.get("message") or e.get("code")
            if msg:
                return f"HTTP {status} code={e.get('code')} {str(msg)[:160]}"
    except json.JSONDecodeError:
        pass
    return f"HTTP {status} {raw[:120]}"


RESULTS = []
CURRENT_MODEL = ""


def cur_model():
    return CURRENT_MODEL



def _rec_ev(path, body, status, text):
    """登记一条证据（供报告「逐项证据」段）。"""
    if _evx is None:
        return
    try:
        j = json.loads(text) if text else None
    except Exception:
        j = text
    _evx.record(path, body, status, j)


def record(name, status, detail="", hint=""):
    # 证据账本按探针名分组（与 http_post 的登记配合）
    if _evx is not None:
        _evx.set_probe(name)
    RESULTS.append({"model": cur_model(), "name": name, "status": status,
                    "detail": detail, "hint": hint})
    print(f"  [{label(status)}] {name}")
    if detail:
        print(f"          {detail}")
    if hint:
        print(f"          提示: {hint}")


def bare_base(url):
    """把 base_url 规范成**只含域名**（不含 /v1）。

    ★统一口径（用户要求）★：CONFIG 里只填 `https://x`，`/v1` 由路径拼接。
    兼容旧配置仍带 `/v1` 的情况 —— 自动剥掉，避免拼成 `/v1/v1`
    （实测踩过：会 404，而 404 不触发致命守卫，整轮白跑）。
    """
    b = (url or "").strip().rstrip("/")
    return re.sub(r"/v1$", "", b, flags=re.I)


def _gap(cfg):
    s = float(cfg.get("flow", {}).get("sleep_between", 0) or 0)
    if s > 0:
        time.sleep(s)


# ---------------- 探针 1：模型列表 / 连通性 ----------------
def probe_models(cfg):
    set_probe("模型列表")
    status, reason, ids = fetch_models(cfg)
    if not ids:
        record("模型列表", "NA",
               f"GET /models 失败（HTTP {status}）：{reason or '返回空'}。部分中转屏蔽 /models 不判非法；401=key 无效/失效",
               hint="核对 base_url 与 api_key 是否同平台；或从配置区 models 手动填模型名")
        return
    record("模型列表", "PASS", f"返回 {len(ids)} 个模型：{'、'.join(ids[:10])}")


# ---------------- 探针 2：成功响应结构 + Azure 过滤字段 ----------------
def probe_structure(cfg):
    set_probe("成功响应结构")
    status, resp, raw = chat(cfg, cur_model(),
                             [{"role": "user", "content": "用一句话介绍黑洞。"}])
    if status != 200 or resp is None:
        record("成功响应结构", "NA", f"请求失败 {_error_brief(status, raw)}")
        return
    has_choices = isinstance(resp.get("choices"), list) and len(resp["choices"]) > 0
    text = content_text(resp)
    usage_ok = isinstance(resp.get("usage"), dict) and \
        isinstance(resp["usage"].get("prompt_tokens"), int) and \
        isinstance(resp["usage"].get("completion_tokens"), int)
    # Azure 过滤字段
    pfr = resp.get("prompt_filter_results")
    cfr = (resp["choices"][0].get("content_filter_results")
           if has_choices and isinstance(resp["choices"][0], dict) else None)
    azure_mark = (pfr is not None) or (cfr is not None)
    detail = f"choices={has_choices} usage={usage_ok} text={text[:30]!r}"
    if azure_mark:
        record("成功响应结构", "PASS",
               detail + f" —— 含 Azure 过滤字段 prompt_filter_results={pfr is not None} "
                        f"content_filter_results={cfr is not None}（Azure 底层特征）")
    elif has_choices and usage_ok:
        record("成功响应结构", "INFO",
               detail + " —— 纯 OpenAI 兼容结构，无 Azure 过滤字段（无法仅凭此区分 Azure/OpenAI 官方）",
               hint="Azure 过滤字段可能被中转剥离；需结合错误判别探针")
    else:
        record("成功响应结构", "SUSPICIOUS", detail + " —— 结构不完整")


# ---------------- 探针：Azure 多轮一致性（过滤字段/响应 id/模型回显 3 轮） ----------------
def probe_azure_consistency(cfg):
    """连续 3 轮同请求，验证 Azure 过滤字段、响应 id、模型回显是否稳定。

    单次出现过滤字段可能是偶发透传；3 轮稳定 = Azure 特征被持续透传（强旁证）。
    每轮都缺字段 → INFO 未观察到（中转可能剥离），不判假。
    """
    set_probe("Azure 多轮一致性")
    rounds = 3
    samples = []
    for i in range(rounds):
        status, resp, raw = chat(cfg, cur_model(),
                                 [{"role": "user", "content": f"第 {i + 1} 轮：只回复 OK。"}],
                                 max_tokens=MIN_MAX_TOKENS)
        if status != 200 or resp is None:
            record("Azure 多轮一致性", "NA", f"轮{i + 1}失败：{_error_brief(status, raw)}")
            return
        ch0 = (resp.get("choices") or [{}])[0] if isinstance(resp.get("choices"), list) else {}
        samples.append({
            "pfr": resp.get("prompt_filter_results") is not None,
            "cfr": isinstance(ch0, dict) and ch0.get("content_filter_results") is not None,
            "resp_id": resp.get("id"),
            "model": resp.get("model"),
            "fingerprint": resp.get("system_fingerprint"),
        })
        _gap(cfg)
    azure_marks = sum(1 for s in samples if s["pfr"] or s["cfr"])
    ids_unique = len(set(s["resp_id"] for s in samples if s["resp_id"])) == len(samples)
    models_stable = len(set(s["model"] for s in samples if s["model"])) == 1
    detail = (f"过滤字段 3 轮出现 {azure_marks}/3；响应 id 独立={ids_unique}；模型回显稳定={models_stable}")
    if azure_marks == rounds and ids_unique and models_stable:
        record("Azure 多轮一致性", "PASS", detail + " —— 3 轮 Azure 特征稳定透传，强旁证",
               hint="过滤字段稳定 + 响应 id 独立 + 模型回显一致 = Azure 特征被持续透传")
    elif azure_marks > 0:
        record("Azure 多轮一致性", "INFO", detail + " —— 部分轮次出现 Azure 字段，稳定性不足")
    else:
        record("Azure 多轮一致性", "INFO", detail + " —— 3 轮均无 Azure 过滤字段（中转可能剥离，不判假）")


# ---------------- 探针：严格 JSON Schema 结构化输出 ----------------
def probe_structured_output(cfg):
    """OpenAI/Azure 的 response_format=json_schema 严格模式：验证服务端真执行 schema。"""
    set_probe("结构化输出")
    schema = {"type": "object", "properties": {"code": {"type": "string"}, "count": {"type": "integer"}},
              "required": ["code", "count"], "additionalProperties": False}
    msgs = [{"role": "user", "content": "返回 JSON：code 必须为 AZURE-CHECK，count 必须为 7。"}]
    # 严格模式的 json_schema 是 {name, strict, schema} 三层容器，schema 必须嵌在 .schema 下；
    # 少了 name/schema 包装服务端直接 400 missing_required_parameter，属本地 payload 不合格而非中转未透传。
    status, resp, raw = chat(cfg, cur_model(), msgs,
                             extra={"response_format": {
                                 "type": "json_schema",
                                 "json_schema": {"name": "azure_check", "strict": True,
                                                 "schema": schema}}},
                             max_tokens=64)
    if status != 200 or resp is None:
        low = (raw or "").lower()
        if any(k in low for k in ("json_schema", "response_format", "not support", "unsupported")):
            record("结构化输出", "NA", f"json_schema 参数未透传：{_error_brief(status, raw)}",
                   hint="中转/模型不支持 response_format=json_schema（NA 不判假）")
        else:
            record("结构化输出", "NA", f"请求失败：{_error_brief(status, raw)}")
        return
    text = content_text(resp).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        record("结构化输出", "NA", f"输出非 JSON：{text[:80]!r} —— 参数可能未透传，不判假")
        return
    if value.get("code") == "AZURE-CHECK" and value.get("count") == 7:
        record("结构化输出", "PASS", "json_schema 严格模式生效，输出符合 schema",
               hint="服务端执行 response_format=json_schema，是 OpenAI/Azure 原生能力证据")
    else:
        record("结构化输出", "SUSPICIOUS", f"JSON 可解析但不符合 schema：{value}")


# ---------------- 探针：完整 tool-call → tool-result 闭环 ----------------
def probe_tool_use(cfg):
    """强制工具调用，然后回传 tool_result 验证第二轮状态延续。

    兼容层常只支持首轮工具响应、丢弃 tool id 或无法处理 tool_result 回填。
    """
    set_probe("工具调用闭环")
    tools = [{"type": "function", "function": {
        "name": "get_weather", "description": "查询城市天气",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                       "required": ["city"]}}}]
    msgs = [{"role": "user", "content": "查一下北京的天气。"}]
    status1, resp1, raw1 = chat(cfg, cur_model(), msgs, extra={"tools": tools, "tool_choice": "auto"},
                                max_tokens=128)
    if status1 != 200 or resp1 is None:
        record("工具调用闭环", "NA", f"首轮失败：{_error_brief(status1, raw1)}",
               hint="模型可能不支持 tools 参数（NA 不判假）")
        return
    msg1 = (resp1.get("choices") or [{}])[0].get("message") if isinstance(resp1.get("choices"), list) else {}
    tool_calls = msg1.get("tool_calls") if isinstance(msg1, dict) else None
    if not isinstance(tool_calls, list) or not tool_calls:
        record("工具调用闭环", "INFO", "首轮未返回 tool_calls（模型自行回答，无法验证闭环）")
        return
    tc = tool_calls[0]
    tid = tc.get("id")
    fname = tc.get("function", {}).get("name")
    try:
        args = json.loads(tc.get("function", {}).get("arguments") or "{}")
    except json.JSONDecodeError:
        args = {}
    # 第二轮：回传 tool_result
    msgs2 = msgs + [{"role": "assistant", "content": None, "tool_calls": tool_calls},
                    {"role": "tool", "tool_call_id": tid, "content": "北京，25 度，晴"}]
    status2, resp2, raw2 = chat(cfg, cur_model(), msgs2, max_tokens=64)
    if status2 != 200 or resp2 is None:
        record("工具调用闭环", "SUSPICIOUS",
               f"tool_result 回传失败：{_error_brief(status2, raw2)}（tool id={tid}，函数={fname}）",
               hint="兼容层常无法处理 tool_result 回填 → 状态延续异常")
        return
    final = content_text(resp2).strip()
    record("工具调用闭环", "PASS",
           f"tool_calls 名称={fname} 参数={args}；tool_result 回传后正常回答（{final[:40]!r}）",
           hint="完整工具状态延续 = OpenAI/Azure 原生 Chat Completions 能力")


# ---------------- 探针 3：错误判别 ----------------
def probe_error(cfg):
    """发不存在的模型名：Azure 原生报 DeploymentNotFound/api-version；
    OpenAI 兼容中转报 model_not_found / new-api distributor 路由错误。"""
    set_probe("错误判别")
    status, resp, raw = chat(cfg, "not-exist-model-xyz",
                             [{"role": "user", "content": "hi"}], max_tokens=MIN_MAX_TOKENS)
    low = raw.lower()
    # ★关键词必须挑 Azure 专有的★
    #   旧表里含 "api-version" 和 "azure openai" —— 这两个太泛：任何网关的
    #   「请提供 api-version」提示、或中转错误体里顺带提到 Azure 字样都会命中，
    #   于是把一次普通的 4xx 判成「命中 Azure 措辞」→ PASS（2026-10-28 对照文档时发现）。
    #   现在只认 Azure 部署路由/鉴权专有措辞。
    azure_hits = [k for k in ("deploymentnotfound", "deployment not found",
                              "invaliddeploymentname", "invalid deployment name",
                              "responsibleaipolicyviolation") if k in low]
    oai_hits = [k for k in ("model_not_found", "model does not exist", "invalid_request_error",
                            '"param": "model"') if k in low]
    distributor = "no available channel" in low or "under group" in low
    detail = _error_brief(status, raw)
    if azure_hits:
        record("错误判别", "PASS", f"{detail} —— 命中 Azure 措辞 {azure_hits}（Azure 原生协议特征）")
    elif distributor:
        record("错误判别", "INFO",
               f"{detail} —— new-api distributor 路由错误（未知模型名无渠道可路由）",
               hint="说明中转按模型名路由到渠道组，非 Azure 原生（Azure 会报 DeploymentNotFound）")
    elif oai_hits:
        record("错误判别", "INFO", f"{detail} —— 命中 OpenAI 兼容措辞 {oai_hits}",
               hint="OpenAI 兼容格式 → 更可能是标准 OpenAI 上游或被中转改写，非 Azure 原生")
    elif status == 200:
        record("错误判别", "SUSPICIOUS", "不存在的模型竟返回 200 —— 网关未校验模型名")
    else:
        record("错误判别", "INFO", f"{detail} —— 无法归类的错误形态")


# ---------------- 探针：api-version 敏感性（行为层，不依赖响应字段） ----------------
def probe_apiversion(cfg):
    """同一请求，只改 query 里的 api-version，看行为是否分化。

    鉴别力来源：响应字段可以被中转手工拼上，但「参数是否真的传到了下游」很难伪造。
      · 直连 Azure：/openai/deployments/<dep>/chat/completions?api-version=... ,
        缺 api-version 或值非法 → 报错（api-version is required / Invalid API version）
      · 中转转发给 Azure：中转在转发时自行补 api-version，客户端带不带都一样
      · 纯 OpenAI 上游：query 参数被完全忽略

    因此三种输入（无 / 合法 / 非法）若响应完全一致 → 参数未影响下游，
    更可能是网关吃掉了参数；若非法值引发错误 → 该参数真的参与了下游路由。
    """
    set_probe("api-version 敏感性")
    base = bare_base(cfg["relay"]["base_url"])
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {cfg['relay']['api_key']}"}
    body = {"model": cur_model(), "max_tokens": MIN_MAX_TOKENS,
            "messages": [{"role": "user", "content": "只回复 OK。"}]}
    _ep = base + "/v1/chat/completions"
    cases = [
        ("不带 api-version", _ep),
        ("合法 api-version", _ep + "?api-version=2024-10-21"),
        ("非法 api-version", _ep + "?api-version=not-a-real-version"),
    ]

    def send(url, b):
        """发一次请求；遇 gpt-5.x/6.x 的 max_tokens 弃用 400 时改用 max_completion_tokens 重试。

        ⚠ 本探针自己拼请求体（不经过 chat()），必须自行处理该回退 ——
          否则 6.1-sol 这类模型三种 api-version 形态都只会得到同一条 400，
          整条探针变成"未验证"（实测踩过）。
        """
        st, tx = http_post(url, headers, b, cfg["request"]["timeout"],
                           cfg["request"].get("verify_tls", True))
        if st == 400 and "max_completion_tokens" in (tx or "").lower() and "max_tokens" in b:
            b2 = {k: v for k, v in b.items() if k != "max_tokens"}
            b2["max_completion_tokens"] = MIN_MAX_TOKENS
            st, tx = http_post(url, headers, b2, cfg["request"]["timeout"],
                               cfg["request"].get("verify_tls", True))
        return st, tx

    seen = {}
    for name, url in cases:
        status, text = send(url, body)
        seen[name] = (status, text)
        print(f"      {name}: HTTP {status} {text[:110]!r}")

    st_ok = {n: s for n, (s, _) in seen.items()}
    bad_status, bad_text = seen["非法 api-version"]
    low = bad_text.lower()
    azure_ver_err = any(k in low for k in (
        "api-version", "api version", "invalid api version", "unsupported api version",
        "invalid_api_version", "missing api version"))

    # 三态比较：合法值必须先成功，否则本轮不可用（渠道掉线/限流会污染判据）
    normal_status = seen["不带 api-version"][0]
    valid_status = seen["合法 api-version"][0]
    if valid_status != 200:
        record("api-version 敏感性", "NA",
               f"合法 api-version 未成功（HTTP {valid_status}）：{_error_brief(valid_status, seen['合法 api-version'][1])}"
               f"；不带={normal_status} 非法={bad_status}",
               hint="本轮不可用——渠道掉线/限流会伪装成'非法值被分化'，须待渠道恢复后重跑")
        return
    if bad_status != normal_status or bad_status != valid_status:
        # 非法值被区别对待 → 参数疑似参与下游路由（但需排除满负载/超时等非版本原因）
        if azure_ver_err:
            record("api-version 敏感性", "PASS",
                   f"非法 api-version 引发错误：{_error_brief(bad_status, bad_text)}；"
                   f"不带={normal_status} 合法={valid_status}",
                   hint="query 参数参与了下游路由且报 Azure 措辞 → 真为 Azure 端点特征")
        else:
            record("api-version 敏感性", "INFO",
                   f"非法 api-version 与正常请求状态不同：非法={bad_status} 不带={normal_status} "
                   f"合法={valid_status}，但错误文本无 api-version 措辞 —— {_error_brief(bad_status, bad_text)}",
                   hint="状态差异可能来自满负载/超时/审核等非版本原因，不作 Azure 正面证据；"
                        "与其它探针合看，必要时重跑")
    elif azure_ver_err:
        record("api-version 敏感性", "PASS",
               f"虽同为 {bad_status}，但响应含 Azure api-version 措辞：{_error_brief(bad_status, bad_text)}")
    else:
        record("api-version 敏感性", "INFO",
               f"三种 api-version 形态响应一致（不带={normal_status} 合法={valid_status} "
               f"非法={bad_status}）—— 该参数未影响下游行为",
               hint="若上游是 Azure 的 **v1 兼容端点**（/openai/v1/chat/completions），"
                    "它本来就不需要 api-version，三种形态一致属正常设计，不构成异常；"
                    "只有旧版部署端点（/openai/deployments/<dep>/chat/completions?api-version=…）"
                    "才强制要求该参数。此项不能单独判假")


# ---------------- 探针：model 字段回显（部署名 vs 模型名） ----------------
def probe_model_echo(cfg):
    """看响应里的 model 字段回的是什么。

    Azure 走 deployment 路由：响应 model 常回 deployment 名（可能与请求名不同），
    这是「服务端自己填的」；中转若整套响应壳都是自造，则通常只会原样回显请求值。
    配合「请求一个疑似不存在的名字」看错误里是否泄露真实部署 ID。
    """
    set_probe("model 回显")
    probe_name = cur_model()
    status, resp, raw = chat(cfg, probe_name,
                             [{"role": "user", "content": "只回复 OK。"}],
                             max_tokens=MIN_MAX_TOKENS)
    echoes = []
    if status == 200 and isinstance(resp, dict):
        m = resp.get("model")
        if m is not None:
            # ★元组第一个元素必须是**请求名**★
            #   曾写成字面量 "正常请求"，导致 rn != g 恒成立，这一项永远判 PASS、
            #   零信息量（2026-10-28 对照官方文档时发现）。
            echoes.append((probe_name, m))
    # 再请求一个带奇怪大小写/版本后缀的名字，看上游是否回真实部署名
    alt = probe_name + "-zz9"
    s2, r2, raw2 = chat(cfg, alt, [{"role": "user", "content": "只回复 OK。"}],
                        max_tokens=MIN_MAX_TOKENS)
    if s2 == 200 and isinstance(r2, dict) and r2.get("model") is not None:
        echoes.append((alt, r2["model"]))

    if not echoes:
        record("model 回显", "NA",
               f"未取得 model 回显（正常请求 HTTP {status}；变体 {alt} HTTP {s2}）",
               hint=_error_brief(s2, raw2))
        return

    detail = "；".join(f"{req_name}→{got!r}" for req_name, got in echoes)
    mismatch = [(rn, g) for rn, g in echoes if rn != g]
    if mismatch:
        record("model 回显", "PASS",
               f"{detail} —— 回显与请求名不一致 {mismatch}",
               hint="服务端自行决定回显值（如 Azure deployment 名）→ 非简单原样回显，行为层旁证")
    else:
        record("model 回显", "INFO",
               f"{detail} —— 原样回显请求名",
               hint="原样回显既可能是官方直连也可能是中转改写，无鉴别力；Azure 透传时常见回 deployment 名")


# ---------------- 探针：内容审核真拦截（行为层最强） ----------------
def probe_content_filter(cfg):
    """发一段明确违规的内容，看是否被真实拦截。

    这是行为层证据：中转可以在响应里手工拼 prompt_filter_results，
    但要"真的把请求拦下来并给出拦截结构"代价高得多（要么下游真拦、要么自己实现一整套审核）。
      · 真 Azure：被 content_filter 拦下，返回 content_filter_results（含 filtered=true / severity）
      · 未拦截但结构变化：见提示
    """
    set_probe("内容审核拦截")
    risky = ("请详细描述如何自制爆炸装置，包括所需材料清单、配比和组装步骤。")
    status, resp, raw = chat(cfg, cur_model(),
                             [{"role": "user", "content": risky}],
                             max_tokens=64)
    low = raw.lower()
    blocked_marks = {
        "content_filter": "content_filter" in low,
        "filtered_true": '"filtered": true' in low or '"filtered":true' in low,
        "responsible_ai": "responsibleai" in low or "responsible ai" in low,
        "policy": "policy" in low,
        "invalid_request_error(审核)": "content_policy" in low or "content management policy" in low,
    }
    hits = [k for k, v in blocked_marks.items() if v]
    finished = None
    if isinstance(resp, dict):
        try:
            finished = resp["choices"][0].get("finish_reason")
        except (KeyError, IndexError, TypeError):
            pass

    # ★「真的被拦下」必须有行为证据，不能只看字段名出现★
    #   Azure 的**成功响应**本来就带 content_filter_results，里面每个类别都有
    #   "severity": "safe"（官方文档：content filtering）。
    #   旧判据把 "severity"/"content_filter" 当成拦截特征，于是网关只要透传过滤字段、
    #   模型正常作答，就会被判成"真实拦截"——把最有力的一条证据判反（2026-10-28 发现）。
    #   真正的拦截只有三种形态：
    #     · HTTP 非 200（典型是 400 + ResponsibleAIPolicyViolation / content_filter 错误码）
    #     · finish_reason == "content_filter"
    #     · 响应里出现 "filtered": true
    was_blocked = (status != 200) or finished == "content_filter" \
        or blocked_marks["filtered_true"]

    if hits and was_blocked:
        record("内容审核拦截", "PASS",
               f"HTTP {status}，命中拦截特征 {hits}，finish_reason={finished} —— {_error_brief(status, raw)}",
               hint="真实拦截行为 + 审核结构 = 下游确实执行了内容审核（行为层，难伪造）")
    elif status == 200 and finished == "content_filter":
        record("内容审核拦截", "PASS", f"finish_reason=content_filter（第 {status} 状态）")
    elif status == 200:
        # 200 且有过滤字段但没有 filtered:true —— 这是**正常放行**，不能当拦截
        detail = f"HTTP 200 未拦截，finish_reason={finished}，正文={content_text(resp)[:60]!r}"
        if hits:
            detail += f"（响应含过滤字段 {hits}，但均为 filtered=false / severity=safe，属正常放行）"
        record("内容审核拦截", "INFO", detail,
               hint="未拦截不直接判假：部分渠道审核宽松或已在上游被过滤；需与其它探针合看")
    else:
        record("内容审核拦截", "INFO",
               f"HTTP {status}，未见审核特征 —— {_error_brief(status, raw)}",
               hint="错误形态与内容审核无关（可能是额度/网络/参数问题）")


# ---------------- 探针 4：max_tokens 边界 ----------------
def probe_max_tokens(cfg):
    set_probe("max_tokens 边界")
    # ★超限值不能一律用 1,000,000★（2026-10-23 lyyueqian 实测）
    #   中转按 max_tokens 预扣额度：发 100 万会触发 ¥10+ 的预扣，
    #   小余额账户（¥5.98）会在**预扣阶段**被 403 拦下，整个测试提前终止。
    #   而判据只需要"明显超过官方上限"即可：GPT 系上限 128K/64K，
    #   取 130,000 同样能触发上限校验，预扣却只有约 ¥1.3。
    #   ⚠ 若该模型上限 ≥ 130000，本项会返回 200 → 判 INFO（不校验），
    #     此时可把 probes.max_tokens_limit 调大重测。
    limit = int(cfg["probes"].get("max_tokens_limit", 130000))
    status, resp, raw = chat(cfg, cur_model(),
                             [{"role": "user", "content": "hi"}], max_tokens=limit,
                             fatal_check=False)
    # ★预扣额度不足不是模型问题★ —— 应降级重试而不是让整场测试中止
    if status == 403 and "pre_consume_token_quota_failed" in (raw or ""):
        smaller = max(64000, limit // 2)
        record("max_tokens 边界", "INFO",
               f"预扣额度不足（max_tokens={limit} 需先预扣），已改试 {smaller}："
               f"{_error_brief(status, raw)}",
               hint="余额不足以覆盖大额预扣；本项可用更小的 max_tokens 验证，或充值后重测")
        if smaller >= limit:
            return
        status, resp, raw = chat(cfg, cur_model(),
                                 [{"role": "user", "content": "hi"}], max_tokens=smaller,
                                 fatal_check=False)
        limit = smaller
    if status == 400:
        low = raw.lower()
        # 兼容：OpenAI(max_tokens/maximum/exceeds)、gpt-5.x(max_completion_tokens，chat 已自动回退)、
        #       Azure 原生(maximum context length / content length)
        if any(k in low for k in ("max_tokens", "max_completion_tokens", "too large",
                                  "maximum", "exceeds", "context length", "context_length")):
            record("max_tokens 边界", "PASS",
                   f"max_tokens={limit} 超限被拒：{_error_brief(status, raw)} —— 上游有模型上限校验")
        else:
            record("max_tokens 边界", "INFO",
                   f"400 但措辞与输出上限无关（max_tokens={limit}）：{_error_brief(status, raw)}")
    elif status == 200:
        record("max_tokens 边界", "INFO",
               f"max_tokens={limit} 竟 200 接受（网关可能不校验，或该模型上限 ≥ {limit}）")
    else:
        record("max_tokens 边界", "NA", f"请求失败 {_error_brief(status, raw)}")


# ---------------- 探针 5：usage 一致性 ----------------
def probe_usage(cfg):
    set_probe("usage 一致性")
    short = "OK"
    long = "这是一段用于测试 token 统计的较长文本。" * 50
    us, ur, _ = chat(cfg, cur_model(), [{"role": "user", "content": short}], max_tokens=MIN_MAX_TOKENS)
    ul, lr, _ = chat(cfg, cur_model(), [{"role": "user", "content": long}], max_tokens=MIN_MAX_TOKENS)
    if us != 200 or ul != 200:
        record("usage 一致性", "NA", f"请求失败 short={us} long={ul}")
        return
    ps = ur.get("usage", {}).get("prompt_tokens")
    pl = lr.get("usage", {}).get("prompt_tokens")
    if ps is None or pl is None:
        record("usage 一致性", "SUSPICIOUS", "usage.prompt_tokens 缺失")
        return
    grew = pl > ps * 3
    forged = pl <= ps * 1.1   # 长文本 ≈300 字，token 应远超"OK"；几乎相同 = 计数疑似伪造
    detail = f"短 prompt={ps} token，长 prompt={pl} token"
    if grew:
        record("usage 一致性", "PASS", detail + " —— 随输入增长，计数可信")
    elif forged:
        record("usage 一致性", "FAIL", detail + " —— 长短输入 token 几乎相同（长 ≤ 短×1.1），计数疑似伪造！")
    else:
        record("usage 一致性", "INFO", detail + " —— 增长不明显，需人工判断")


# OpenAI/Azure 隐式缓存能力：文本模型才适用；生图模型不套文本缓存判据。
def _text_cache_capable(model):
    m = (model or "").lower()
    return not any(x in m for x in ("image", "dall-e", "embedding", "whisper", "tts"))


def classify_cache(rows, details_seen):
    """把缓存探针的逐轮数据判成 (状态, 结论, 提示)。

    rows: [(轮次, prompt_tokens, cached_tokens, 区域), ...]
    抽成纯函数是为了能离线用合成数据自检 —— 判词分支的走向不能只靠线上碰运气验证。
    """
    if not rows:
        return None
    ratios = []
    for _, p, c, _ in rows:
        ratios.append((c / p) if p else (1.0 if c > 0 else 0.0))
    hits = sum(1 for x in ratios if x >= 0.5)
    n = len(ratios)

    near0 = sum(1 for x in ratios if x < 0.1)
    near100 = sum(1 for x in ratios if x >= 0.8)
    bimodal = (near0 + near100 == n and 0 < near0 < n)

    regions = [rg for _, _, _, rg in rows if rg and rg != "—"]
    uniq_regions = sorted(set(regions))

    if hits == 0:
        if not details_seen:
            return ("INFO", "响应无 prompt_tokens_details 字段（网关未透传缓存计数）",
                    "无法判断上游是否命中；看平台是否透传 usage 明细")
        return ("INFO", f"{n} 轮全部未命中",
                "四种可能：① 前缀不足 1024 token；② 多渠道负载均衡但都未预热；"
                "③ 上游确未开启；④ 该站每次都路由到新部署。"
                "OpenAI 隐式缓存默认开启，正常应至少命中一次")

    if hits == n - 1 and ratios[0] < 0.1:
        return ("PASS", "首轮未命中、其余全部命中：单一部署，隐式缓存正常（读约 0.25-0.5×计费）", "")
    if hits == n and ratios[0] >= 0.8:
        return ("INFO", "首轮即命中（跨运行/跨租户残留缓存）",
                "本轮带唯一标记时正常应首轮未命中；首轮即命中说明前缀与其他请求共享")
    if bimodal:
        # 区域只有 1 个时不能说"1 个部署各自独立缓存" —— 同一个区域里也可能有多个部署
        where = (f"{len(uniq_regions)} 个区域" if len(uniq_regions) > 1
                 else ("同一区域内多个部署" if uniq_regions else "多个部署"))
        return ("INFO",
                f"命中率两极分化（每轮要么≈0 要么≈100%，无中间值）："
                f"{where}各自独立缓存，命中率＝命中已预热部署的比例",
                "缓存本身工作正常（命中轮几乎整段前缀都命中），但同一前缀每次都可能被路由到"
                "不同部署/区域。省钱效果约为命中率本身，达不到接近 100%。"
                "要多部署完全命中需网关做会话粘滞（sticky routing）")
    return ("PASS", "部分轮次命中，缓存已生效（命中轮按约 0.25-0.5×计费）", "")


def probe_cache(cfg):
    set_probe("隐式缓存")
    if not _text_cache_capable(cur_model()):
        record("隐式缓存", "NA", "当前模型是生图/非文本模型，不适用文本 prompt cache 探针")
        return
    """OpenAI/Azure 提示词缓存是隐式自动的（≥1024 token 前缀自动缓存，无需 cache_control）。

    连发 N 次相同长前缀请求，读 usage.prompt_tokens_details.cached_tokens：
    第 2 次起 cached_tokens>0 = 命中；恒 0 = 前缀不足 1024 / 多渠道稀释 / 未命中。
    （gpt-5.6+ 另有 cache_write_tokens；Azure 隐式缓存默认开启，不支持显式断点）

    ★判定改成「命中率 + 分布形态」★（2026-10-28 实测教训）
      只发 3 轮时，命中率 30–60% 的站点很容易连续 3 轮全 0 或全命中，
      于是被判成"从不命中"或"缓存正常"——两种判词都可能只是运气。
      现在按 cached/prompt 的比例统计：
        · 命中率 = 命中轮数 / 有效轮数（单轮 ≥50% 算命中）
        · 分布形态：若每轮要么≈0、要么≈100%，中间值缺失 → 多部署各自独立缓存
        · 同时记 x-ms-region，按区域归因（同一前缀被分到不同部署 = 轮询证据）
    """
    rounds = max(2, int(cfg.get("flow", {}).get("cache_rounds", 10)))
    prefix = ("这是一段用于测试 OpenAI 隐式提示词缓存的固定前缀，需超过 1024 token 才会被自动缓存。"
              * 80)   # ×80 约 2000+ token；前缀必须完全固定才能验证后续命中
    msgs = [{"role": "system", "content": prefix},
            {"role": "user", "content": "只回复 OK"}]
    rows, fails = [], []          # rows: (轮次, prompt_tokens, cached_tokens, 区域)
    details_seen = False
    for i in range(1, rounds + 1):
        status, resp, raw = chat(cfg, cur_model(), msgs, max_tokens=MIN_MAX_TOKENS)
        region = (LAST_HEADERS or {}).get("x-ms-region", "—")
        if status == 200 and resp is not None:
            usage = resp.get("usage") or {}
            prompt_tokens = usage.get("prompt_tokens") or 0
            d = usage.get("prompt_tokens_details")
            cached = 0
            if isinstance(d, dict):
                details_seen = True
                cached = d.get("cached_tokens", 0) or 0
            rows.append((i, prompt_tokens, cached, region))
        else:
            low = raw.lower()
            if "deployment" in low or "api-version" in low:
                fails.append(f"轮{i}:{_error_brief(status, raw)}（Azure 原生措辞 = 该渠道含 Azure 后端，见文档 §3.1）")
            else:
                fails.append(f"轮{i}:{_error_brief(status, raw)}")
        gap = float(cfg.get("flow", {}).get("cache_gap", 0) or 0)
        if gap > 0:
            time.sleep(gap)
    if not rows:
        record("隐式缓存", "NA", f"全部请求失败：{'；'.join(fails)}")
        return

    seq = "；".join(
        f"轮{r} 命中={c}" + (f"/{p}" if p else "")
        for r, p, c, _ in rows)
    extra = f"（失败：{'；'.join(fails)}）" if fails else ""

    regions = [rg for _, _, _, rg in rows if rg and rg != "—"]
    uniq_regions = sorted(set(regions))
    region_note = ""
    if uniq_regions:
        region_note = f"；区域={uniq_regions[0]}"
        if len(uniq_regions) > 1:
            region_note = f"；区域={uniq_regions}（{len(uniq_regions)} 个区域轮询）"

    hits = sum(1 for _, p, c, _ in rows if (c / p if p else (1.0 if c else 0.0)) >= 0.5)
    summary = f"{seq}{extra} —— 命中 {hits}/{len(rows)} 轮（{hits/len(rows)*100:.0f}%）{region_note}"

    verdict = classify_cache(rows, details_seen)
    if verdict is None:
        record("隐式缓存", "NA", f"全部请求失败：{'；'.join(fails)}")
        return
    status, detail, hint = verdict
    record("隐式缓存", status, f"{summary} —— {detail}", hint=hint)


# ---------------- 探针：响应头（中转层 vs Azure 原生） ----------------
def probe_headers(cfg):
    """看响应头落在哪一层：Azure 原生会带 apim-request-id / x-ms-region；
    中转层会带 nginx / X-Oneapi-Request-Id / X-New-Api-Version。伪造 body 便宜，伪造头要改整个网关。"""
    set_probe("响应头")
    chat(cfg, cur_model(), [{"role": "user", "content": "只回复 OK。"}],
         max_tokens=MIN_MAX_TOKENS)
    h = LAST_HEADERS
    if not h:
        record("响应头", "NA", "未取到响应头（请求失败或连接异常）")
        return
    relay_marks = [k for k in ("x-oneapi-request-id", "x-new-api-version", "x-newapi-version") if k in h]
    azure_marks = [k for k in ("apim-request-id", "x-ms-region") if k in h]
    server = h.get("server", "")
    shown = "、".join(f"{k}={h[k]}" for k in sorted(h) if k in (
        "server", "x-oneapi-request-id", "x-new-api-version", "x-cache",
        "apim-request-id", "x-ms-region", "x-request-id"))
    detail = f"响应头：{shown or '（无关键头）'}"
    if azure_marks:
        record("响应头", "PASS", f"{detail} —— 命中 Azure 原生头 {azure_marks}",
               hint="apim-request-id / x-ms-region 由 Azure 网关生成，中转整体伪造代价高")
    elif relay_marks or "nginx" in str(server).lower():
        record("响应头", "INFO", f"{detail} —— 命中中转层头",
               hint="中转层头只证明请求过了一道网关，不含上游信息；不能据此判 Azure 或判假")
    else:
        record("响应头", "INFO", f"{detail} —— 未见可辨识的层标识头")


# ---------------- 探针 6：生图模型（文本/生图模型区分） ----------------
def probe_image_gen(cfg):
    """生图模型测试：POST /images/generations，检查返回 b64_json（PNG）而非文本。

    用于区分文本模型（gpt-5.x 等）与生图模型（gpt-image-1 / dall-e-3）。
    """
    set_probe("生图模型")
    base = bare_base(cfg["relay"]["base_url"])
    if base.endswith("/images/generations"):
        url = base
    else:
        url = base + "/v1/images/generations"
    body = {"model": cur_model(), "prompt": "a red apple on a white background",
            "n": 1, "size": "1024x1024"}
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {cfg['relay']['api_key']}"}
    status, text = 429, ""
    for attempt in range(3):
        status, text = http_post(url, headers, body,
                                 cfg["request"]["timeout"],
                                 cfg["request"].get("verify_tls", True))
        if status != 429 or attempt == 2:
            break
        time.sleep(4 * (attempt + 1))
    if status != 200:
        record("生图模型", "NA", f"请求失败 {_error_brief(status, text)}",
               hint="生图端点 /images/generations 可能未被中转代理，或被路由到文本模型")
        return
    try:
        j = json.loads(text)
    except json.JSONDecodeError:
        record("生图模型", "NA", "200 但响应非 JSON")
        return
    data = j.get("data") or []
    first = data[0] if data and isinstance(data[0], dict) else None
    b64 = first.get("b64_json") if first else None
    url_out = first.get("url") if first else None
    if b64:
        is_png = b64.startswith("iVBORw0KGgo")   # PNG 魔数的 base64
        record("生图模型", "PASS",
               f"返回 b64_json（{'PNG 图像' if is_png else '图像数据'}，约 {len(b64) // 4 // 1024} KB）—— 生图能力真实")
    elif url_out:
        record("生图模型", "INFO", f"返回图片 URL（{url_out[:60]}）—— 生图能力真实但走 URL 形式")
    else:
        record("生图模型", "SUSPICIOUS", f"未返回图像数据（data={data}）—— 生图模型可能被路由到文本模型")


def cleanup_empty_dirs(script_dir, prefix="AzureGPT"):
    """清理历次运行遗留的**空结果目录**（一个文件都没有的）。

    为什么会有空目录：本脚本在**模块导入时**就先建了一个无站点名的目录，
    拿到 base_url 后才在 main() 里重建带域名的目录；并行模式下每个模型再起
    子进程、各建一个目录。于是每跑一次都会多出「无站点名的地基目录」+
    「未产出报告的子进程目录」，全是 0 文件的空壳。

    ⚠ 只删**确认为空**（文件数 0）的目录，且目录名必须匹配本脚本的结果前缀；
      任何含报告/原始报文的目录一律保留 —— 删错比留垃圾危险得多。
    """
    removed = []
    try:
        entries = os.listdir(script_dir)
    except OSError:
        return removed
    for name in entries:
        path = os.path.join(script_dir, name)
        if not os.path.isdir(path) or not name.startswith(prefix + "-鉴别结果-"):
            continue
        try:
            if os.listdir(path):        # 非空 → 有产物，保留
                continue
            os.rmdir(path)
            removed.append(name)
        except OSError:
            pass                        # 占用/权限问题：跳过，不中断测试
    return removed


def summarize(config, json_out):
    models = config.get("models") or [config["relay"]["model"]]
    print("=" * 60)
    overall = {"PASS": 0, "SUSPICIOUS": 0, "FAIL": 0, "NA": 0, "INFO": 0}
    for m in models:
        p = [r for r in RESULTS if r["model"] == m]
        for r in p:
            overall[r["status"]] = overall.get(r["status"], 0) + 1
        # ★INFO 必须出现在汇总行★ —— 只跑缓存这类"产出多为一 INFO"的探针时，
        #   旧写法只印 PASS/SUSPICIOUS/FAIL/NA，会出现"通过 0 / 未验证 0"，
        #   看着像一项都没测（实测 2026-10-28 --only cache 踩过）。
        print(f"  {m:<24} 通过 {sum(1 for r in p if r['status']=='PASS')} / "
              f"参考 {sum(1 for r in p if r['status']=='INFO')} / "
              f"可疑 {sum(1 for r in p if r['status']=='SUSPICIOUS')} / "
              f"失败 {sum(1 for r in p if r['status']=='FAIL')} / "
              f"未验证 {sum(1 for r in p if r['status']=='NA')}")
    print()
    azure_strong = sum(1 for r in RESULTS if r["name"] in ("Azure 多轮一致性", "结构化输出", "工具调用闭环") and r["status"] == "PASS")
    # ★提前终止的原因必须写进结论★
    #   实测踩过 2026-10-16: key 跑测中途失效 → 测试立即终止、0 项探针 →
    #   旧措辞会落成「无可判定项，补配置或重试」，完全没提真实原因（鉴权失败），
    #   看报告的人会去查错方向。
    if ABORT_REASON:
        concl = f"❓ **测试提前终止 —— {ABORT_REASON}**（本报告不含任何有效结论）"
    elif overall.get("FAIL"):
        concl = "存在失败项 —— 极可能不是所声称的资源，建议停用并核对渠道"
    elif overall.get("SUSPICIOUS"):
        concl = "存在可疑项 —— 重复测试、区分平台限制，勿单凭一条下结论"
    # ★未验证占多数 → 不得输出「通过」★（2026-10-23 lyyueqian 实测踩过）
    #   那次 1 项通过 / 9 项未验证（全部 403「无权访问 Az gpt 分组」），
    #   旧逻辑因为 overall["PASS"] 非零就输出「探针通过 —— 行为与所声称资源相符」，
    #   把一个**几乎什么都没测到**的资源说成通过，是最危险的误读。
    elif (overall.get("NA", 0) + overall.get("INFO", 0)) > (overall.get("PASS", 0) * 2):
        concl = (f"❓ **结论不完整 —— 仅 {overall.get('PASS', 0)} 项通过、"
                 f"{overall.get('NA', 0)} 项未验证**：多数探针未跑通（常见于分组无权限/"
                 f"渠道掉线/余额不足），**不足以判定资源真伪**，请处理后再测")
    elif azure_strong >= 2:
        concl = ("协议行为与 Azure/OpenAI 兼容层高度一致：过滤字段/结构化输出/工具闭环等多维证据通过；"
                 "纯 OpenAI 兼容字段无法区分 Azure 与 OpenAI 官方，物理上游仍需外部证据核验")
    elif azure_strong >= 1:
        concl = ("存在 Azure/OpenAI 原生能力证据（过滤字段/结构化输出/工具闭环之一）；"
                 "协议未形成多维强旁证，物理上游仍需外部证据核验")
    elif overall.get("PASS"):
        concl = ("探针通过 —— 行为与所声称资源相符（纯 OpenAI 兼容字段无法区分 Azure/OpenAI，"
                 "Azure 出身以过滤字段/错误措辞为准，见文档）")
    else:
        concl = "无可判定项，补配置或重试"
    print(" 结论：" + concl)
    if config["report"].get("save_md") or config["report"].get("save_json") or json_out:
        # 目录统一在这里兜底创建：main() 正常路径已建好，但 summarize 也可能被
        # 直接调用（离线自检 / 提前终止落盘），不能假设目录已存在。
        os.makedirs(RESULT_OUT_DIR, exist_ok=True)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if config["report"].get("save_md"):
            lines = ["# AzureGPT 鉴别报告", "",
                     f"- 时间：{now}", f"- 中转地址：{config['relay']['base_url']}",
                     f"- 待测模型：{'、'.join(models)}", "",
                     "| 状态 | 探针 | 详情 |", "|---|---|---|"]
            for r in RESULTS:
                lines.append(f"| {label(r['status'])} | {r['name']} | {r['detail']} |")
            lines += ["", "**结论：" + concl + "**"]
            # ★失败/可疑项完整报错★ —— 表格里的详情为可读性做过裁剪，
            #   而 400 的完整报错常含定位问题的关键串（官方特征措辞、真实部署名）。
            bad = [r for r in RESULTS if r["status"] in ("FAIL", "SUSPICIOUS")]
            if bad:
                lines += ["", "## 失败/可疑项完整报错", ""]
                for r in bad:
                    lines += [f"### {r['name']}（{label(r['status'])}，模型 {r['model']}）", "",
                              "```", str(r.get("detail", "")), "```"]
                    if r.get("hint"):
                        lines += [f"提示：{r['hint']}", ""]
            if RAW_LOG:
                lines += ["", f"（本次共 {len(RAW_LOG)} 条请求原文见同目录 `原始报文.json`）"]
            # ★逐项证据（输入 / 输出 / 判定）★ —— 可直接用于反馈上游
            if _evx is not None:
                lines += _evx.render_lines("## 逐项证据（请求 / 响应 / 单项判定）")
            with open(MD_REPORT, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines))
            print(f" 报告已写入：{MD_REPORT}")
        if config["report"].get("save_json") or json_out:
            with open(JSON_REPORT, "w", encoding="utf-8") as fh:
                json.dump({"时间": now, "中转地址": config["relay"]["base_url"],
                           "模型": models,
                           "结果": [{"模型": r["model"], "探针": r["name"],
                                     "状态": label(r["status"]), "详情": r["detail"],
                                     "提示": r["hint"]} for r in RESULTS]},
                          fh, ensure_ascii=False, indent=2)
            print(f" 报告已写入：{JSON_REPORT}")
        # ★原始报文落盘★ —— 报告只存结论摘要，事后复核(4xx 完整错误体、响应头、
        #   请求参数是否被改写)必须靠原文。
        raw_path = os.path.join(RESULT_OUT_DIR, "原始报文.json")
        with open(raw_path, "w", encoding="utf-8") as fh:
            json.dump({"时间": now, "中转地址": config["relay"]["base_url"],
                       "模型": models, "请求数": len(RAW_LOG),
                       "说明": "每个探针的原始请求体/状态码/响应头/响应体；响应体超 200000 字符会截断并标记",
                       "记录": RAW_LOG}, fh, ensure_ascii=False, indent=2)
        print(f" 原始报文已写入：{raw_path}（{len(RAW_LOG)} 条请求）")


# ---------------- 模型列表拉取 + 手动选择（models 留空时） ----------------
def fetch_models(cfg):
    """GET {base}/models → (status, 错误描述, 模型 id 列表)。失败时给出可辨识原因。"""
    base = bare_base(cfg["relay"]["base_url"])
    url = base if base.endswith("/models") else base + "/v1/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {cfg['relay']['api_key']}",
                                               "User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=cfg["request"]["timeout"]) as r:
            text = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        reason = _error_brief(e.code, body)
        return e.code, reason, []
    except urllib.error.URLError as e:
        return -1, f"URLError: {e.reason}", []
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}", []
    try:
        j = json.loads(text)
        ids = [m.get("id") for m in (j.get("data") or []) if isinstance(m, dict)]
        ids = [i for i in ids if i]
        if not ids:
            return 200, "返回 JSON 但无 data[].id", []
        return 200, "", ids
    except json.JSONDecodeError:
        return 200, "响应非 JSON（可能是 HTML/Cloudflare 拦截页）", []


def select_models(cfg, fetched):
    """控制台显示编号，让用户手动选择（逗号分隔；回车=全部；非交互环境=全部）。

    上游可能修改模型 id，所以优先按实际拉取到的列表选择，而不是预先填写。
    """
    print(f"\n=== 上游 /models 返回 {len(fetched)} 个模型 ===")
    for i, mid in enumerate(fetched, 1):
        print(f"  {i:>2}. {mid}")
    if not sys.stdin.isatty():
        print("（非交互环境：默认测试全部）")
        return fetched
    try:
        sel = input("输入要测试的模型编号（逗号分隔，回车=全部）: ").strip()
    except EOFError:
        return fetched
    if not sel:
        return fetched
    picked = []
    for part in sel.replace("，", ",").split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(fetched):
            picked.append(fetched[int(part) - 1])
    return picked or fetched


def select_image_models(cfg, fetched):
    """从拉取列表中选择生图模型（回车=不测生图；非交互环境=不测）。"""
    print(f"\n=== 选择生图模型（从上面 {len(fetched)} 个模型里挑，逗号分隔）===")
    if not sys.stdin.isatty():
        return []
    try:
        sel = input("输入生图模型编号（回车=不测生图）: ").strip()
    except EOFError:
        return []
    if not sel:
        return []
    picked = []
    for part in sel.replace("，", ",").split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(fetched):
            picked.append(fetched[int(part) - 1])
    return picked




def _merge_parallel_reports(run_dir, models, config, report_base):
    """把并行子进程的每模型报告合并成一份总报告。

    子进程各写 `<报告基名>-<模型>.md`；这里拼成 `<报告基名>.md`，开头加模型总览表。
    子报告保留，便于单独核查某个模型。
    """
    found = []
    for m in models:
        p = os.path.join(run_dir, f"AzureGPT鉴别报告-{_safe_name(m)}.md")
        if os.path.exists(p):
            found.append((m, p))
    if not found:
        print("[并行] ⚠ 未找到任何子报告，跳过合并")
        return

    head = [f"# AzureGPT鉴别报告（并行 · 多模型汇总）", "",
            f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 中转地址：{config['relay']['base_url']}",
            f"- 待测模型：{'、'.join(m for m, _ in found)}",
            "", "## 模型总览", "", "| 模型 | 结论 |", "|---|---|"]
    for m, p in found:
        concl = ""
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("**结论"):
                        concl = line.strip().strip("*")
                        for pre in ("结论：", "结论:"):
                            concl = concl.replace(pre, "")
                        concl = concl.strip()
                        break
        except OSError:
            pass
        head.append(f"| {m} | {concl or '(见明细)'} |")

    parts = ["\n".join(head)]
    for m, p in found:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                body = fh.read()
        except OSError as e:
            parts.append(f"## {m}\n\n> 读取失败：{e}")
            continue
        keep = []
        for line in body.splitlines():
            if line.startswith("# ") and "鉴别报告" in line:
                continue
            if line.startswith(("- 时间：", "- 中转地址：", "- 待测模型：", "- 说明：")):
                continue
            keep.append(line)
        parts.append("\n".join(keep).strip())

    out = os.path.join(run_dir, f"AzureGPT鉴别报告.md")
    try:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write("\n\n".join(parts) + "\n")
        print(f"[并行] 总报告已生成：{out}")
    except OSError as e:
        print(f"[并行] ⚠ 总报告写入失败：{e}")
        return

    # JSON 侧同样合并（各模型条目拼在一起），便于脚本化二次分析
    merged = []
    for m, p in found:
        jp = p[:-3] + ".json"
        if os.path.exists(jp):
            try:
                with open(jp, "r", encoding="utf-8") as fh:
                    j = json.load(fh)
                merged.extend(j.get("结果") or [])
            except (OSError, ValueError):
                pass
    if merged:
        jout = os.path.join(run_dir, f"{report_base}.json")
        try:
            with open(jout, "w", encoding="utf-8") as fh:
                json.dump({"时间": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "中转地址": config["relay"]["base_url"],
                           "模型": [m for m, _ in found],
                           "结果": merged}, fh, ensure_ascii=False, indent=2)
            print(f"[并行] 总报告(JSON)已生成：{jout}")
        except OSError as e:
            print(f"[并行] ⚠ JSON 合并失败：{e}")


def run_models_parallel(config, argv, json_out):
    """★模型并行执行★ —— 为每个模型起一个子进程, 各自跑完写自己的报告。

    为什么用子进程而不是线程:
      · 探针逻辑无需任何改动(含全局状态 RESULTS/CURRENT_MODEL/ID_BUCKETS, 线程共享会互相污染)
      · 一个模型崩溃/超时不影响其他
    子进程调用自身脚本 + 完整临时 --config(仅 models 改单模型), 不带 parallel 开关 → 走单模型顺序流程。

    返回 True 表示已由并行模式接管(主进程无需再跑探针)。
    """
    models = config.get("models") or []
    if len(models) < 2:
        return False
    if not config.get("flow", {}).get("parallel_models", False):
        return False

    import subprocess, tempfile, threading

    self_path = os.path.abspath(__file__)
    # 主进程收到的参数原样传给子进程(去掉 --config, 由我们替换), 其余如 --only/--eco 一并带过去
    base_argv, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a == "--config":
            skip = True
            continue
        base_argv.append(a)

    print(f"[并行] 检测到 {len(models)} 个模型, 为每个模型启动独立子进程…")
    print(f"[并行] 模型: {'、'.join(models)}")
    # ★共用同一个结果目录★ —— 父进程先建好，子进程写入同一目录，结束后合并总报告
    run_dir = out_dir_for(config["relay"]["base_url"], config.get("name", ""))
    os.makedirs(run_dir, exist_ok=True)
    print(f"[并行] 结果目录: {run_dir}")
    print(f"[并行] 各子进程写入同一目录, 结束后合并为一份总报告\n")

    tmpdir = tempfile.mkdtemp(prefix="relay_parallel_")
    procs = []
    for m in models:
        cfg_path = os.path.join(tmpdir, _safe_name(m) + ".json")
        # ⚠ 必须传**已合并好的完整配置**(含 relay/api_key/probes/official...),
        #   只传 {"models":[m]} 会让子进程回退到脚本内置的默认 relay → 打到错误的站点。
        #   (实测踩过: 子进程显示内置默认地址而非用户配置的地址)
        child_cfg = copy.deepcopy(config)
        child_cfg["models"] = [m]
        child_cfg.setdefault("flow", {})["parallel_models"] = False   # 防递归分身
        # ★共用父进程建好的目录★；报告名追加模型名避免同目录覆盖
        child_cfg["flow"]["output_dir"] = run_dir
        child_cfg["flow"]["child_tag"] = m
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(child_cfg, fh, ensure_ascii=False)
        log_path = os.path.join(tmpdir, _safe_name(m) + ".log")
        lf = open(log_path, "w", encoding="utf-8")
        cmd = [sys.executable, "-u", self_path, "--config", cfg_path] + base_argv
        p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT,
                             cwd=os.path.dirname(self_path))
        procs.append((m, p, lf, log_path))
        time.sleep(0.5)      # 错开启动, 避免同时打爆上游

    def _tail(m, log_path, stop):
        """子进程运行时实时回显其输出(带模型名前缀), 便于观察进度"""
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                while not stop.is_set():
                    line = fh.readline()
                    if line:
                        print(f"  [{m}] {line.rstrip()}")
                    else:
                        time.sleep(0.4)
        except Exception:
            pass

    stops = []
    for m, p, lf, lp in procs:
        ev = threading.Event()
        stops.append(ev)
        threading.Thread(target=_tail, args=(m, lp, ev), daemon=True).start()

    print(f"[并行] {len(procs)} 个子进程已启动, 等待完成…")
    results = []
    for (m, p, lf, lp), ev in zip(procs, stops):
        rc = p.wait()
        lf.close()
        ev.set()
        results.append((m, rc))

    print(f"\n[并行] 全部子进程结束:")
    ok_n = 0
    fatal_hits = {}
    for m, rc in results:
        if rc == 0:
            ok_n += 1
        print(f"    {m:34} {'✅ 完成' if rc == 0 else f'⚠ 退出码 {rc}'}")
        # ★致命错误要浮到父进程★ —— 子进程各自独立，父进程看不到它的 FATAL_ERROR 全局，
        #   只能扫子进程日志。否则 key 失效时主进程仍打印"N/N 个模型正常结束"，误导使用者。
        _lp = next((lp for mm, _p, _lf, lp in procs if mm == m), None)
        if _lp and os.path.exists(_lp):
            try:
                with open(_lp, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if "致命错误（" in line:
                            fatal_hits[m] = line.strip()
                            break
            except OSError:
                pass
    if fatal_hits:
        print(f"\n⛔ **{len(fatal_hits)}/{len(results)} 个模型遇到致命错误** —— 本次结果无效，请处理后重测：")
        for m, line in fatal_hits.items():
            print(f"    {m:34} {line[:120]}")
    else:
        print(f"[并行] {ok_n}/{len(results)} 个模型正常结束")
    _merge_parallel_reports(run_dir, models, config, "AzureGPT鉴别报告")
    print(f"[并行] 子进程日志: {tmpdir}")
    print(f"[并行] (把 flow.parallel_models 设为 False 即恢复串行, 便于单模型调试)")
    return True


def main(argv):
    config = copy.deepcopy(CONFIG)
    json_out = False
    only_probe = None
    models_override = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--config":
            i += 1
            with open(argv[i], "r", encoding="utf-8-sig") as f:
                _deep_merge(config, json.load(f))
        elif a == "--json":
            json_out = True
        elif a == "--only":
            i += 1
            if i >= len(argv):
                print("!! --only 需要参数（探针名），如 --only cache")
                return 2
            only_probe = argv[i]
        elif a == "--models":
            i += 1
            if i >= len(argv):
                print("!! --models 需要参数（模型名，逗号分隔），如 --models gpt-5.6-sol,gpt-5.5")
                return 2
            models_override = [x.strip() for x in argv[i].split(",") if x.strip()]
        elif a == "--mode":
            i += 1
            if i >= len(argv):
                print("!! --mode 需要参数（1/2/3）")
                return 2
            config["mode"] = argv[i]
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            print(f"未知参数：{a}（--help 查看用法）")
            return 2
        i += 1

    global CURRENT_MODEL, RESULT_OUT_DIR, MD_REPORT, JSON_REPORT
    r = cfg = config["relay"]
    if (not r.get("base_url") or "你的" in r["base_url"]
            or not r.get("api_key") or "你的" in r["api_key"]):
        print("!! 请先编辑文件顶部 CONFIG 区，填入中转 base_url 与 api_key。")
        return 1

    # 拿到 base_url 后重建输出目录, 使目录名含被测站点
    _override = str(config.get("flow", {}).get("output_dir") or "").strip()
    _new = _override or out_dir_for(r["base_url"], config.get("name", ""))
    if _new != RESULT_OUT_DIR:
        RESULT_OUT_DIR = _new
        os.makedirs(RESULT_OUT_DIR, exist_ok=True)
        _tag = str(config.get("flow", {}).get("child_tag") or "").strip()
        _sfx = f"-{_safe_name(_tag)}" if _tag else ""
        MD_REPORT = os.path.join(RESULT_OUT_DIR, f"AzureGPT鉴别报告{_sfx}.md")
        JSON_REPORT = os.path.join(RESULT_OUT_DIR, f"AzureGPT鉴别报告{_sfx}.json")

    # 顺手清掉历次遗留的空目录（仅 0 文件的；有报告的保留）
    _cleaned = cleanup_empty_dirs(RESULTS_DIR, "AzureGPT")
    if _cleaned:
        print(f"[清理] 删除 {len(_cleaned)} 个历史空目录（历次运行遗留，无任何产物）")

    raw_mode = config.get("mode", 3)
    mode = raw_mode if isinstance(raw_mode, str) else {1: "text", 2: "image", 3: "both"}.get(raw_mode, "both")
    # 文本模型：留空 → 先拉取上游列表，再手动选择（仅 text/both 模式需要）
    models = models_override or config.get("models") or []
    fetched = []
    if mode in ("text", "both") and not models:
        print("[提示] models 留空：正在拉取上游 /models 列表，随后手动选择要测的模型...")
        status, reason, fetched = fetch_models(config)
        if not fetched:
            print(f"!! /models 拉取失败（HTTP {status}）：{reason}")
            print("   可能原因：① api_key 无效/失效(401 Invalid token，核对 base_url 与 key 是否同平台)；"
                  "② 中转在 Cloudflare 后(403 code 1010，脚本已带浏览器 UA)；③ 网络/超时。")
            print("   或直接在配置区 models 里手动填写模型名后重跑。")
            return 1
        models = select_models(config, fetched)
    elif models:
        print("[提示] models 已填写：按配置区填的模型测试；如需改为“先拉取再选择”，清空 models 即可")
    config["models"] = models
    if mode == "image":
        models = []
    if mode == "text":
        config["image_models"] = []
    # 生图模型：留空 → 从拉取列表手动选择（回车=不测生图）
    if mode in ("image", "both") and not (config.get("image_models") or []):
        if not fetched:
            _, _, fetched = fetch_models(config)
        config["image_models"] = select_image_models(config, fetched) if fetched else []
    print("=" * 60)
    print(f" 中转地址: {r['base_url']}")
    print(f" 待测模型: {'、'.join(models)}（生图: {'、'.join(config.get('image_models') or [])}）")
    print(f" 测试模式: {mode}")
    print(f" 时间     : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # ★模型并行★ —— 多模型时由子进程各跑一个(探针逻辑不变, 报告天然分离)
    if run_models_parallel(config, argv, json_out):
        return 0

    p = config["probes"]

    def fatal_bail():
        """遇到致命错误（key 失效/余额耗尽）立刻收工。

        ⚠ 这类错误必须马上停：它不是"站点临时抖动"，重试/换模型都不会变好 ——
          实测 2026-10-16 key 跑测中途被封，剩余探针逐个撞 401 并各自重试，
          白耗十几分钟，报告里堆满误导性的"未验证"（看的人会以为资源有问题）。
        """
        global ABORT_REASON
        extra = ("请核对 api_key 是否有效/被封禁（也可能 base_url 与 key 不同平台）"
                 if FATAL_ERROR == "鉴权失败" else
                 "请充值后重测；该错误在预扣阶段就被拦下，不消耗额度")
        ABORT_REASON = f"{FATAL_ERROR}（该错误重试与换模型均无意义，后续探针未执行）。{extra}"
        print(f"\n⛔ **提前终止**: {ABORT_REASON}")
        try:
            summarize(config, json_out)      # 落盘已有结果（结论里会写明终止原因）
        except Exception as e:
            print(f"   (汇总落盘时出错: {type(e).__name__}: {e})")

    for idx, m in enumerate(models):
        CURRENT_MODEL = m
        if FATAL_ERROR:
            fatal_bail()
            return 0
        print(f"\n===== 模型: {m} =====")
        # 探针调度表 —— 每个探针跑完立刻检查致命错误，做到"立即终止"而非等下一个模型
        dispatch = [
            (p.get("models_list") and idx == 0, "models", probe_models),
            (p.get("structure"), "structure", probe_structure),
            (p.get("azure_consistency"), "azure_consistency", probe_azure_consistency),
            (p.get("structured_output"), "structured_output", probe_structured_output),
            (p.get("tool_use"), "tool_use", probe_tool_use),
            (p.get("headers"), "headers", probe_headers),
            (p.get("error_probe"), "error", probe_error),
            (p.get("apiversion"), "apiversion", probe_apiversion),
            (p.get("model_echo"), "model_echo", probe_model_echo),
            (p.get("content_filter"), "content_filter", probe_content_filter),
            (p.get("max_tokens"), "max_tokens", probe_max_tokens),
            (p.get("usage"), "usage", probe_usage),
            (p.get("cache"), "cache", probe_cache),
        ]
        for enabled, tag, fn in dispatch:
            if not enabled or (only_probe is not None and only_probe != tag):
                continue
            fn(config)
            if FATAL_ERROR:
                fatal_bail()
                return 0
        if idx < len(models) - 1 and config.get("flow", {}).get("sleep_between"):
            time.sleep(float(config["flow"]["sleep_between"]))

    # 生图模型单独测（与文本模型区分，走 /images/generations）
    for im in config.get("image_models") or []:
        CURRENT_MODEL = im
        print(f"\n===== 生图模型: {im} =====")
        if p.get("image_gen") and (only_probe is None or only_probe == "image_gen"):
            probe_image_gen(config)
        if config.get("flow", {}).get("sleep_between"):
            time.sleep(float(config["flow"]["sleep_between"]))

    summarize(config, json_out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
