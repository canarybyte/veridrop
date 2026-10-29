# -*- coding: utf-8 -*-
"""离线自检: 用桩响应验证 AzureGPT 脚本的“原文留存 / 报告完整性 / 空目录清理”，
不发任何真实请求、不耗配额。

跑法: python "AzureGPT鉴别-离线自检.py"
改完 AzureGPT鉴别测试.py 后先跑本脚本。

覆盖范围: 本次改动新增的三块能力（原始报文落盘、失败项完整报错、空目录清理）。
  ⚠ 探针判定逻辑的回归不在此脚本——需真测或另行补桩。
"""
import importlib.util, io, json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "AzureGPT鉴别测试.py")

spec = importlib.util.spec_from_file_location("azuretest", TARGET)
M = importlib.util.module_from_spec(spec)
sys.modules["azuretest"] = M
spec.loader.exec_module(M)
print(f"已加载 {os.path.basename(TARGET)}")

ok_all = True


def chk(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print(("  ✅ " if cond else "  ❌ ") + name + ("  " + extra if extra else ""))


CFG = {"relay": {"base_url": "https://x.test/v1", "api_key": "sk-test", "model": "gpt-6.1-sol"},
       "request": {"max_tokens": 64, "timeout": 5, "verify_tls": True}}

_real_post = M.http_post


def stub_post(status, body, headers=None):
    """替掉 http_post：不回真实网络，但仍写 LAST_HEADERS（chat 依赖）。"""
    def _p(url, hdrs, payload, timeout, verify=True):
        M.LAST_HEADERS = dict(headers or {})
        return status, body
    return _p


def fresh():
    M.RAW_LOG.clear()
    M.PROBE_CONTEXT[0] = ""


print("\n[1] 原始报文留存")
# ① 失败的 400 完整错误体必须原样留存（最需要原文的场景）
fresh()
LONG_ERR = json.dumps({"error": {"code": "content_filter",
                                 "message": "The response was filtered due to the prompt triggering "
                                            "Azure OpenAI's content management policy."}},
                      ensure_ascii=False)
M.http_post = stub_post(400, LONG_ERR, {"apim-request-id": "req-abc", "x-ms-region": "East US"})
M.set_probe("内容审核拦截")
M.chat(CFG, "gpt-6.1-sol", [{"role": "user", "content": "hi"}])
chk("★400 错误体原文进 RAW_LOG（不截断）",
    M.RAW_LOG and M.RAW_LOG[-1]["响应体"] == LONG_ERR,
    f"长度={len(M.RAW_LOG[-1]['响应体']) if M.RAW_LOG else 0}")
chk("★记录了探针名与状态码",
    M.RAW_LOG[-1]["探针"] == "内容审核拦截" and M.RAW_LOG[-1]["状态码"] == 400)
chk("★响应头一并留存（Azure 原生头可作旁证）",
    M.RAW_LOG[-1]["响应头"].get("apim-request-id") == "req-abc"
    and M.RAW_LOG[-1]["响应头"].get("x-ms-region") == "East US")

# ② 超长响应体截断并标记
fresh()
M.http_post = stub_post(200, "A" * (M.MAX_RAW_BODY + 500))
M.set_probe("成功响应结构")
M.chat(CFG, "gpt-6.1-sol", [{"role": "user", "content": "hi"}])
chk("★超长响应体截断且带标记",
    M.RAW_LOG[-1]["响应体"].endswith("[_已截断]")
    and len(M.RAW_LOG[-1]["响应体"]) < M.MAX_RAW_BODY + 100)

# ③ max_tokens → max_completion_tokens 回退后，留存的请求体须是**最终生效**的那份
fresh()
_calls = []


def _fallback(url, hdrs, payload, timeout, verify=True):
    _calls.append(dict(payload))
    if "max_tokens" in payload:
        return 400, '{"error":{"message":"Use max_completion_tokens instead"}}'
    return 200, '{"choices":[{"message":{"content":"OK"}}],"usage":{"prompt_tokens":5,"completion_tokens":1}}'


M.http_post = _fallback
M.set_probe("max_tokens 边界")
M.chat(CFG, "gpt-6.1-sol", [{"role": "user", "content": "hi"}])
chk("★回退重试后留存的是生效请求体（含 max_completion_tokens）",
    M.RAW_LOG[-1]["请求体"].get("max_completion_tokens") is not None
    and "max_tokens" not in M.RAW_LOG[-1]["请求体"])

print("\n[2] 报告完整性")
fresh()
M.http_post = stub_post(503, '{"error":{"message":"分组 gpt-azure 下模型 X 无可用渠道（distributor）"}}',
                        {"x-oneapi-request-id": "20261015abc"})
M.chat(CFG, "gpt-6-sol", [{"role": "user", "content": "hi"}])   # 留 1 条原文
M.http_post = _real_post
M.RESULTS.clear()
M.CURRENT_MODEL = "gpt-6-sol"
M.record("api-version 敏感性", "SUSPICIOUS",
         "非法 api-version 与正常请求状态不同：非法=503 不带=200 合法=503")
M.record("工具调用闭环", "PASS", "正常")
_out = tempfile.mkdtemp(prefix="azure_report_")
_md, _json, _rd = M.MD_REPORT, M.JSON_REPORT, M.RESULT_OUT_DIR
M.RESULT_OUT_DIR = _out
M.MD_REPORT = os.path.join(_out, "r.md")
M.JSON_REPORT = os.path.join(_out, "r.json")
M.summarize({"relay": {"base_url": "https://x.test/v1"}, "models": ["gpt-6-sol"],
             "report": {"save_md": True, "save_json": True}}, False)
_md_text = io.open(M.MD_REPORT, encoding="utf-8").read()
chk("★报告为可疑项单列「失败/可疑项完整报错」章节",
    "失败/可疑项完整报错" in _md_text and "api-version 敏感性" in _md_text)
chk("★通过项不进报错章节", _md_text.count("工具调用闭环") == 1)
chk("★报告点名原始报文文件", "原始报文.json" in _md_text)
_rawf = os.path.join(_out, "原始报文.json")
chk("★原始报文.json 落在结果目录且含记录",
    os.path.exists(_rawf) and json.load(io.open(_rawf, encoding="utf-8"))["请求数"] == 1)
M.MD_REPORT, M.JSON_REPORT, M.RESULT_OUT_DIR = _md, _json, _rd

print("\n[3] 空目录清理")
_cdir = tempfile.mkdtemp(prefix="azure_clean_")
_empty = os.path.join(_cdir, "AzureGPT-鉴别结果-empty-1")
_kept = os.path.join(_cdir, "AzureGPT-鉴别结果-hasfile-2")
_other = os.path.join(_cdir, "随便一个别的目录")
for d in (_empty, _kept, _other):
    os.makedirs(d)
io.open(os.path.join(_kept, "AzureGPT鉴别报告.md"), "w", encoding="utf-8").write("x")
# ⚠ 别的目录必须**留空** —— 放了文件的话，删不掉是因为"非空"而非前缀守卫，
#   用例会假绿（实测踩过：变异掉前缀守卫后仍全绿，等于没测到）。
_removed = M.cleanup_empty_dirs(_cdir, "AzureGPT")
chk("★空结果目录被删除", not os.path.exists(_empty) and len(_removed) == 1)
chk("★有产物的结果目录被保留", os.path.exists(_kept))
chk("★空的、非本脚本前缀的目录不得被删（前缀守卫）", os.path.exists(_other))

# ★反面对照：清理函数绝不能删掉有报告或原始报文的目录（删错比留垃圾危险）
_cdir2 = tempfile.mkdtemp(prefix="azure_clean2_")
_d1 = os.path.join(_cdir2, "AzureGPT-鉴别结果-only-raw")
os.makedirs(_d1)
io.open(os.path.join(_d1, "原始报文.json"), "w", encoding="utf-8").write("[]")
M.cleanup_empty_dirs(_cdir2, "AzureGPT")
chk("★只含原始报文的目录不得被删（有产物即保留）", os.path.exists(_d1))

print("\n[4] 致命错误识别（key 失效 / 余额耗尽）")
# ★2026-10-16 实测踩坑★ key 跑测中途被封 → 剩余几十个探针逐个撞 401 并各自重试，
#   白耗十几分钟。这类错误「重试结果完全一样」，必须立即终止。
_f = M.fatal_error_kind
chk("★401 无效的令牌 → 鉴权失败",
    _f(401, '{"error":{"message":"无效的令牌","type":"new_api_error"}}') == "鉴权失败")
chk("★401 Invalid token（英文）→ 鉴权失败",
    _f(401, '{"error":{"message":"Invalid token"}}') == "鉴权失败")
chk("★403 余额不足 → 余额不足",
    _f(403, '{"error":{"message":"token quota is not enough, token remain quota: $1.37"}}') == "余额不足")
chk("★403 中文配额 → 余额不足", _f(403, '{"message":"配额不足"}') == "余额不足")
chk("★普通 400 不得判致命（避免误杀）", _f(400, '{"error":{"message":"bad param"}}') == "")
chk("★503 不得判致命（属可自愈的网络类）", _f(503, '{"error":{"message":"no channel"}}') == "")
chk("★Cloudflare 403(1010) 不得判致命（偶发拦截非 key 失效）",
    _f(403, "error code: 1010") == "")
M.ABORT_REASON = "鉴权失败（该错误重试与换模型均无意义，后续探针未执行）"
M.RESULTS.clear()
_abort_out = tempfile.mkdtemp(prefix="azure_abort_")
_md2, _rd2 = M.MD_REPORT, M.RESULT_OUT_DIR
M.RESULT_OUT_DIR = _abort_out
M.MD_REPORT = os.path.join(_abort_out, "abort.md")
M.summarize({"relay": {"base_url": "https://x.test/v1"}, "models": ["m"],
             "report": {"save_md": True, "save_json": False}}, False)
_abort_md = io.open(M.MD_REPORT, encoding="utf-8").read()
M.ABORT_REASON = ""
M.MD_REPORT, M.RESULT_OUT_DIR = _md2, _rd2
chk("★结论须点名终止原因（不得只说「无可判定项」）",
    "提前终止" in _abort_md and "鉴权失败" in _abort_md, _abort_md.split("结论：")[-1][:70])

# ⓑ 致命标记必须能被真正写进全局（防 _note 那类"赋值即变局部变量"的 UnboundLocalError）
#   ⚠ 用**真实本地 HTTP 服务**返回 401，而不是 monkeypatch urllib ——
#     真连接才能覆盖 try/except 分支，桩会再一次掩盖问题（这已是第三次栽在桩上）。
import http.server
import threading


class _H401(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        # ⚠ 必须发**真正的中文字节**，不能写成 b"...\\u65e0\\u6548..."——
        #   后者发出去是字面量 无 而非"无效"，判据匹配不上（实测踩过）。
        body = json.dumps({"error": {"message": "无效的令牌"}}, ensure_ascii=False).encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


_srv = http.server.HTTPServer(("127.0.0.1", 0), _H401)
threading.Thread(target=_srv.serve_forever, daemon=True).start()
_port = _srv.server_address[1]

_old_fatal = M.FATAL_ERROR
M.FATAL_ERROR = ""
try:
    _st, _bd = M.http_post(f"http://127.0.0.1:{_port}/messages", {}, {}, timeout=5)
    if _st == -1:
        # 沙箱/防火墙可能禁止本地回连 → 这是**环境限制**，不是源码问题，
        # 明确跳过而不是报失败（否则会把"跑不了"伪装成"代码坏了"）。
        print(f"      ⏭ 跳过：本地回环不可达（{str(_bd)[:60]}），无法验证真实 http_post 写全局")
    else:
        chk("★真实 http_post 拿到 401 不抛异常", _st == 401, f"HTTP {_st}")
        chk("★401 被写进 FATAL_ERROR（防 UnboundLocalError 回归）",
            M.FATAL_ERROR == "鉴权失败", f"FATAL_ERROR={M.FATAL_ERROR!r}")
except Exception as e:
    chk("★真实 http_post 拿到 401 不抛异常", False, f"{type(e).__name__}: {e}")
    chk("★401 被写进 FATAL_ERROR（防 UnboundLocalError 回归）", False, f"{type(e).__name__}: {e}")
finally:
    _srv.shutdown()
    M.FATAL_ERROR = _old_fatal

M.http_post = _real_post
print("\n" + ("全部通过 ✅" if ok_all else "存在失败 ❌"))
sys.exit(0 if ok_all else 1)
