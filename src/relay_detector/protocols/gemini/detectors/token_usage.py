"""Token usage sanity check.

Verifies the relay's `usage` accounting is internally consistent and plausible:

1. usage object is present on both short and long requests
2. total_tokens = prompt_tokens + completion_tokens (arithmetic identity)
3. adding ~80 simple words to the prompt increases prompt_tokens by a sane
   amount (45–130 tokens — wide enough to forgive tokenizer drift between
   Gemini variants, narrow enough to flag bogus accounting)
4. tiny outputs aren't reported as huge completion token counts
5. stream usage (when include_usage is honored) agrees with non-stream usage

This is a sanity check, not an audit — we don't try to prove the provider's
invoice, only that the numbers they report are self-consistent.
"""

from __future__ import annotations

from typing import Any

from ....core.models import DetectorResult
from .base import ActiveDetector
from .utils import int_value, message_text, usage


SHORT_PROMPT = "Reply with exactly: ok"
LONG_EXTRA_WORD = " apple"
LONG_EXTRA_REPEATS = 80
LONG_PROMPT = SHORT_PROMPT + "\n\nReference text:" + (LONG_EXTRA_WORD * LONG_EXTRA_REPEATS)
# 128 leaves room for reasoning tokens. Gemini 3 series defaults to thinking-on
# and burns 30–60 reasoning tokens before any text is emitted; with 8 we'd get
# finish_reason=length on every probe and could not measure usage at all.
MAX_TOKENS = 128

# Wide range to absorb Gemini's tokenizer changes between model versions.
DELTA_MIN = 45
DELTA_MAX = 140


class TokenUsageDetector(ActiveDetector):
    name = "token_usage"
    display_name = "Token 用量"
    weight = 10.0
    category = "billing"
    description = "检查 usage 字段真实性"

    async def run(self, client, model: str) -> DetectorResult:
        try:
            _short_req, short_resp, _h1, _lat1 = await client.chat_completions_create(
                model=model,
                max_completion_tokens=MAX_TOKENS,
                temperature=0,
                messages=[{"role": "user", "content": SHORT_PROMPT}],
            )
            _long_req, long_resp, _h2, _lat2 = await client.chat_completions_create(
                model=model,
                max_completion_tokens=MAX_TOKENS,
                temperature=0,
                messages=[{"role": "user", "content": LONG_PROMPT}],
            )
            stream = await _collect_stream_usage(
                client,
                {
                    "model": model,
                    "max_completion_tokens": MAX_TOKENS,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": SHORT_PROMPT}],
                    "stream_options": {"include_usage": True},
                },
            )
        except Exception as e:  # noqa: BLE001
            return self._result("error", 0.0, error=str(e))

        short_usage = usage(short_resp)
        long_usage = usage(long_resp)
        stream_usage = (
            stream.get("usage") if isinstance(stream.get("usage"), dict) else {}
        )
        short_text = message_text(short_resp).strip()
        long_text = message_text(long_resp).strip()

        sub: dict[str, dict[str, Any]] = {}
        score = 0.0

        usage_present = bool(short_usage and long_usage)
        sub["usage_present"] = {
            "pass": usage_present,
            "short_usage": short_usage,
            "long_usage": long_usage,
        }
        if usage_present:
            score += 20.0

        arithmetic_ok = (
            _arithmetic_ok(short_usage)
            and _arithmetic_ok(long_usage)
            and (not stream_usage or _arithmetic_ok(stream_usage))
        )
        sub["usage_arithmetic"] = {
            "pass": arithmetic_ok,
            "note": "total_tokens 应等于 prompt_tokens + completion_tokens",
        }
        if arithmetic_ok:
            score += 20.0

        sp = int_value(short_usage.get("prompt_tokens"))
        lp = int_value(long_usage.get("prompt_tokens"))
        prompt_delta = lp - sp if sp is not None and lp is not None else None
        delta_ok = prompt_delta is not None and DELTA_MIN <= prompt_delta <= DELTA_MAX
        sub["prompt_token_delta"] = {
            "short_prompt_tokens": sp,
            "long_prompt_tokens": lp,
            "delta": prompt_delta,
            "expected_range": [DELTA_MIN, DELTA_MAX],
            "pass": delta_ok,
        }
        if delta_ok:
            score += 25.0

        completion_ok = _completion_sane(short_usage) and _completion_sane(long_usage)
        sub["completion_tokens"] = {
            "short_completion_tokens": short_usage.get("completion_tokens"),
            "long_completion_tokens": long_usage.get("completion_tokens"),
            "short_text": short_text[:80],
            "long_text": long_text[:80],
            "pass": completion_ok,
        }
        if completion_ok:
            score += 15.0

        stream_ok = bool(stream_usage) and _usage_close(short_usage, stream_usage)
        if not stream_usage:
            stream_note = "接口没有返回流式 usage,无法用流式结果交叉验证"
        elif stream_ok:
            stream_note = "stream 与 non-stream 的 usage 接近"
        else:
            stream_note = "stream 与 non-stream 的 usage 偏差超出容忍"
        sub["stream_usage"] = {
            "non_stream_usage": short_usage,
            "stream_usage": stream_usage,
            "stream_chunk_count": stream.get("chunk_count"),
            "stream_error": stream.get("error"),
            "pass": stream_ok,
            "note": stream_note,
        }
        if stream_ok:
            score += 20.0

        return self._result(
            "pass" if score >= 80.0 else "fail",
            score,
            {
                "sub_checks": sub,
                "evaluation_zh": _evaluation(score, usage_present),
            },
        )


def _evaluation(score: float, usage_present: bool) -> str:
    if score >= 80.0:
        return "Token 用量基本可信: usage 字段完整,total = prompt + completion 自洽,长 prompt 增量合理。"
    if not usage_present:
        return "Token 用量无法判断: 接口没有返回完整 usage 字段。"
    return "Token 用量存在风险: usage 不自洽,或长短 prompt 的 token 增量不合理,或流式与非流式不一致。"


def _arithmetic_ok(u: dict[str, Any]) -> bool:
    if not u:
        return False
    p = int_value(u.get("prompt_tokens"))
    c = int_value(u.get("completion_tokens"))
    t = int_value(u.get("total_tokens"))
    if p is None or c is None or t is None:
        return False
    # Allow up to 5 tokens of slack — some relays add overhead tokens for
    # system messages or reasoning summaries.
    return abs(t - (p + c)) <= 5


def _completion_sane(u: dict[str, Any]) -> bool:
    """completion_tokens must not exceed the request cap.

    For thinking models (e.g. Gemini 3) the field includes reasoning tokens and
    can fill the entire MAX_TOKENS budget on a "say ok" prompt — that's
    expected, not fraud. We only flag values clearly above the cap, which would
    indicate the relay is fabricating numbers.
    """
    c = int_value(u.get("completion_tokens"))
    return c is not None and 0 <= c <= MAX_TOKENS + 5


def _usage_close(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if not left or not right:
        return False
    matched = 0
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        lv = int_value(left.get(key))
        rv = int_value(right.get(key))
        if lv is not None and rv is not None and abs(lv - rv) <= 2:
            matched += 1
    return matched >= 2


async def _collect_stream_usage(client, body: dict[str, Any]) -> dict[str, Any]:
    stream_usage: dict[str, Any] | None = None
    chunk_count = 0
    try:
        async for chunk, _elapsed in client.chat_completions_stream(**body):
            chunk_count += 1
            if chunk.get("_done") or chunk.get("_parse_error"):
                continue
            if isinstance(chunk.get("usage"), dict):
                stream_usage = chunk["usage"]
    except Exception as e:  # noqa: BLE001
        return {"usage": stream_usage, "chunk_count": chunk_count, "error": str(e)}
    return {"usage": stream_usage, "chunk_count": chunk_count, "error": None}
