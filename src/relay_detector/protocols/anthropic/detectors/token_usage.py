"""Anthropic token usage sanity detector.

This detector checks whether a Claude-compatible relay reports token usage
that is internally consistent and plausible for controlled requests. It is not
a billing audit, but it catches common over-reporting patterns:

1. usage exists on Messages responses
2. adding a fixed simple text block increases input_tokens by a sane amount
3. tiny fixed outputs do not report output_tokens above the request cap
4. streaming usage agrees with non-stream usage for the same short prompt
5. when available, /v1/messages/count_tokens aligns with actual input_tokens
"""

from __future__ import annotations

from typing import Any

from ....core.models import DetectorResult
from ..config import lookup_model
from .base import ActiveDetector


SHORT_PROMPT = "Reply with exactly: ok"
LONG_EXTRA_WORD = " apple"
LONG_EXTRA_REPEATS = 80
LONG_PROMPT = SHORT_PROMPT + "\n\nReference text:" + (LONG_EXTRA_WORD * LONG_EXTRA_REPEATS)
MAX_TOKENS = 16

# Anthropic tokenizers should count the 80 repeated simple words close to one
# token each. Keep this deliberately wide to flag clear fraud, not drift.
DELTA_MIN = 45
DELTA_MAX = 140
NEW_TOKENIZER_DELTA_MIN = 90
NEW_TOKENIZER_DELTA_MAX = 230

OUTPUT_TOKEN_TOLERANCE = 4
INPUT_CLOSE_FRAC = 0.20
INPUT_CLOSE_FLOOR = 4
OUTPUT_CLOSE_FRAC = 0.50
OUTPUT_CLOSE_FLOOR = 3


class TokenUsageDetector(ActiveDetector):
    name = "token_usage"
    display_name = "Token 用量"
    weight = 10.0
    category = "billing"
    description = "检查 usage token 字段的真实性"

    async def run(self, client, model: str) -> DetectorResult:
        short_body = {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "temperature": 0,
            "messages": [{"role": "user", "content": SHORT_PROMPT}],
        }
        long_body = {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "temperature": 0,
            "messages": [{"role": "user", "content": LONG_PROMPT}],
        }

        try:
            _short_req, short_resp, _h1, _lat1 = await client.messages_create(
                **short_body
            )
            _long_req, long_resp, _h2, _lat2 = await client.messages_create(
                **long_body
            )
            stream = await _collect_stream_usage(client, short_body)
        except Exception as e:  # noqa: BLE001
            return self._result("error", 0.0, error=str(e))

        count_result = await _count_tokens(client, short_body)

        short_usage = _usage(short_resp)
        long_usage = _usage(long_resp)
        short_text = _join_text_blocks(short_resp.get("content")).strip()
        long_text = _join_text_blocks(long_resp.get("content")).strip()

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

        short_input = _int(short_usage.get("input_tokens"))
        long_input = _int(long_usage.get("input_tokens"))
        delta = long_input - short_input if short_input is not None and long_input is not None else None
        delta_min, delta_max = _delta_range(model)
        delta_ok = delta is not None and delta_min <= delta <= delta_max
        sub["input_token_delta"] = {
            "short_input_tokens": short_input,
            "long_input_tokens": long_input,
            "delta": delta,
            "expected_range": [delta_min, delta_max],
            "pass": delta_ok,
        }
        if delta_ok:
            score += 25.0

        output_ok = _output_sane(short_usage) and _output_sane(long_usage)
        sub["output_tokens"] = {
            "short_output_tokens": short_usage.get("output_tokens"),
            "long_output_tokens": long_usage.get("output_tokens"),
            "max_tokens": MAX_TOKENS,
            "short_text": short_text[:80],
            "long_text": long_text[:80],
            "pass": output_ok,
        }
        if output_ok:
            score += 15.0

        stream_ok = _stream_usage_close(short_usage, stream)
        sub["stream_usage"] = {
            "non_stream_usage": short_usage,
            "stream_input_tokens": stream.get("input_tokens"),
            "stream_output_tokens": stream.get("output_tokens"),
            "stream_chunk_count": stream.get("chunk_count"),
            "stream_error": stream.get("error"),
            "pass": stream_ok,
        }
        if stream_ok:
            score += 25.0

        count_ok = _count_close(short_usage, count_result)
        sub["count_tokens"] = {
            "count_tokens": count_result.get("input_tokens"),
            "count_error": count_result.get("error"),
            "actual_input_tokens": short_input,
            "pass": count_ok,
            "note": (
                "count_tokens 与实际 Messages usage 接近"
                if count_ok
                else "count_tokens 不可用或与实际 usage 偏差过大"
            ),
        }
        if count_ok:
            score += 15.0

        if not usage_present:
            return self.skip("missing-usage")

        details = {
            "sub_checks": sub,
            "evaluation_zh": _evaluation(score, count_result),
            "risk_level": "low" if score >= 90 else "medium" if score >= 80 else "high",
        }
        return self._result("pass" if score >= 80.0 else "fail", score, details)


async def _collect_stream_usage(client, body: dict[str, Any]) -> dict[str, Any]:
    stream_input: int | None = None
    stream_output: int | None = None
    chunk_count = 0
    try:
        async for ev, _elapsed in client.messages_stream(**body):
            chunk_count += 1
            if ev.event == "message_start":
                msg = ev.data.get("message") or {}
                usage = msg.get("usage") or {}
                stream_input = _int(usage.get("input_tokens"))
            elif ev.event == "message_delta":
                usage = ev.data.get("usage") or {}
                out = _int(usage.get("output_tokens"))
                if out is not None:
                    stream_output = out
    except Exception as e:  # noqa: BLE001
        return {
            "input_tokens": stream_input,
            "output_tokens": stream_output,
            "chunk_count": chunk_count,
            "error": str(e),
        }
    return {
        "input_tokens": stream_input,
        "output_tokens": stream_output,
        "chunk_count": chunk_count,
        "error": None,
    }


async def _count_tokens(client, body: dict[str, Any]) -> dict[str, Any]:
    try:
        _req, resp, _h, _lat = await client.count_tokens(**body)
    except Exception as e:  # noqa: BLE001
        return {"input_tokens": None, "error": str(e)}
    return {
        "input_tokens": _int(resp.get("input_tokens")),
        "error": None,
        "raw": resp,
    }


def _usage(resp: dict[str, Any]) -> dict[str, Any]:
    usage = resp.get("usage")
    return usage if isinstance(usage, dict) else {}


def _join_text_blocks(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _output_sane(usage: dict[str, Any]) -> bool:
    output = _int(usage.get("output_tokens"))
    return output is not None and 0 <= output <= MAX_TOKENS + OUTPUT_TOKEN_TOLERANCE


def _delta_range(model: str) -> tuple[int, int]:
    info = lookup_model(model)
    if info is not None and info.new_tokenizer:
        return NEW_TOKENIZER_DELTA_MIN, NEW_TOKENIZER_DELTA_MAX
    return DELTA_MIN, DELTA_MAX


def _stream_usage_close(non_stream: dict[str, Any], stream: dict[str, Any]) -> bool:
    ns_input = _int(non_stream.get("input_tokens"))
    ns_output = _int(non_stream.get("output_tokens"))
    st_input = _int(stream.get("input_tokens"))
    st_output = _int(stream.get("output_tokens"))
    if ns_input is None or ns_output is None or st_input is None or st_output is None:
        return False
    input_tol = max(INPUT_CLOSE_FLOOR, int(ns_input * INPUT_CLOSE_FRAC))
    output_tol = max(OUTPUT_CLOSE_FLOOR, int(ns_output * OUTPUT_CLOSE_FRAC))
    return (
        abs(ns_input - st_input) <= input_tol
        and abs(ns_output - st_output) <= output_tol
        and st_output >= 0
    )


def _count_close(non_stream: dict[str, Any], count_result: dict[str, Any]) -> bool:
    actual = _int(non_stream.get("input_tokens"))
    counted = _int(count_result.get("input_tokens"))
    if actual is None or counted is None:
        return False
    tolerance = max(INPUT_CLOSE_FLOOR, int(actual * INPUT_CLOSE_FRAC))
    return abs(actual - counted) <= tolerance


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _evaluation(score: float, count_result: dict[str, Any]) -> str:
    if score >= 90:
        return "Token 用量基本可信: usage 字段完整,长短 prompt 增量合理,stream 与 non-stream 统计一致,且 count_tokens 对得上。"
    if score >= 80:
        if count_result.get("error"):
            return "Token 用量基本可信: usage 自洽且流式/非流式一致,但该中转站没有可用的 count_tokens 端点,无法做最强输入 token 交叉验证。"
        return "Token 用量基本可信: 核心 usage 检查通过,但有一个辅助交叉验证不完整。"
    return "Token 用量存在风险: usage 字段缺失、长短 prompt 增量异常、输出 token 超出请求上限,或 stream 与 non-stream token 统计不一致。"
