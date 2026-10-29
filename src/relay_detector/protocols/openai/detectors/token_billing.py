"""OpenAI token billing sanity detector.

This detector does not try to prove the provider's invoice. It checks whether
the token counts returned by the relay are internally consistent and plausible
for controlled requests:

1. usage fields exist and total = prompt + completion
2. adding a fixed block of simple text increases prompt_tokens by a sane amount
3. tiny fixed outputs are not reported as huge completion token counts
4. stream and non-stream usage are compatible for the same short prompt

The public report should phrase this as "Token 数是否可信", not as tokenizer
or provider-internal implementation detail.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ....core.models import DetectorResult
from ..client import OpenAIChatClient
from .base import ActiveDetector


SHORT_PROMPT = "Reply with exactly: ok"
LONG_EXTRA_WORD = " apple"
LONG_EXTRA_REPEATS = 80
LONG_PROMPT = SHORT_PROMPT + "\n\nReference text:" + (LONG_EXTRA_WORD * LONG_EXTRA_REPEATS)
MAX_TOKENS = 8

# The added text is intentionally simple English where modern OpenAI tokenizers
# are close to one token per repeated word. Keep a generous window so we flag
# clear accounting problems rather than harmless tokenizer/version drift.
DELTA_MIN = 45
DELTA_MAX = 130
COMPLETION_MAX_FOR_OK = 12
REFERENCE_VERSION = 1
REFERENCE_ROUNDS = 3


class TokenBillingDetector(ActiveDetector):
    name = "token_billing"
    display_name = "Token 计费"
    weight = 10.0
    category = "billing"
    description = "检测 token 计费是否被放大/篡改"

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

        short_usage = _usage(short_resp)
        long_usage = _usage(long_resp)
        stream_usage = stream.get("usage") if isinstance(stream.get("usage"), dict) else {}
        short_text = _message_text(short_resp).strip()
        long_text = _message_text(long_resp).strip()

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
            _usage_arithmetic_ok(short_usage)
            and _usage_arithmetic_ok(long_usage)
            and (not stream_usage or _usage_arithmetic_ok(stream_usage))
        )
        sub["usage_arithmetic"] = {
            "pass": arithmetic_ok,
            "note": "total_tokens 应等于 prompt_tokens + completion_tokens",
        }
        if arithmetic_ok:
            score += 20.0

        prompt_delta = None
        delta_ok = False
        sp = _int(short_usage.get("prompt_tokens"))
        lp = _int(long_usage.get("prompt_tokens"))
        if sp is not None and lp is not None:
            prompt_delta = lp - sp
            delta_ok = DELTA_MIN <= prompt_delta <= DELTA_MAX
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

        stream_ok = False
        stream_note = ""
        if not stream_usage:
            stream_note = "接口没有返回流式 usage,无法用流式结果交叉验证"
        else:
            stream_ok = _stream_usage_compatible(short_usage, stream_usage)
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

        reference = await _get_reference(model)
        reference_result = _compare_reference(
            short_usage,
            long_usage,
            stream_usage,
            reference,
        )
        sub["normal_usage"] = reference_result
        if reference_result.get("comparison_available"):
            reference_score = float(reference_result.get("score") or 0.0)
            if reference_score < 50:
                score = min(score, 45.0)
            elif reference_score < 75:
                score = min(score, 70.0)

        passed_checks = sum(1 for item in sub.values() if item.get("pass"))
        if not usage_present or passed_checks <= 1:
            return self.skip("insufficient-token-usage")

        risk_level = (
            "low" if score >= 90
            else "medium" if score >= 70
            else "high"
        )
        evaluation_zh = _evaluation_text(score, stream_usage, reference_result)
        details = {
            "sub_checks": sub,
            "risk_level": risk_level,
            "evaluation_zh": evaluation_zh,
        }
        return self._result("pass" if score >= 90.0 else "fail", score, details)


async def _collect_stream_usage(client, body: dict[str, Any]) -> dict[str, Any]:
    usage: dict[str, Any] | None = None
    chunk_count = 0
    try:
        async for chunk, _elapsed_ms in client.chat_completions_stream(**body):
            chunk_count += 1
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
    except Exception as e:  # noqa: BLE001
        return {"usage": None, "chunk_count": chunk_count, "error": str(e)}
    return {"usage": usage, "chunk_count": chunk_count, "error": None}


def _usage(resp: dict[str, Any]) -> dict[str, Any]:
    usage = resp.get("usage")
    return usage if isinstance(usage, dict) else {}


def _message_text(resp: dict[str, Any]) -> str:
    choices = resp.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = msg.get("content") if isinstance(msg, dict) else ""
    return content if isinstance(content, str) else ""


def _usage_arithmetic_ok(usage: dict[str, Any]) -> bool:
    prompt = _int(usage.get("prompt_tokens"))
    completion = _int(usage.get("completion_tokens"))
    total = _int(usage.get("total_tokens"))
    return (
        prompt is not None
        and completion is not None
        and total is not None
        and prompt + completion == total
    )


def _completion_sane(usage: dict[str, Any]) -> bool:
    completion = _int(usage.get("completion_tokens"))
    return completion is not None and 0 < completion <= COMPLETION_MAX_FOR_OK


def _usage_close(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if not left or not right:
        return False
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        lv = _int(left.get(key))
        rv = _int(right.get(key))
        if lv is None or rv is None:
            return False
        if abs(lv - rv) > 1:
            return False
    return True


def _stream_usage_compatible(
    non_stream_usage: dict[str, Any], stream_usage: dict[str, Any]
) -> bool:
    """Return whether stream usage looks compatible with a non-stream probe.

    For reasoning-capable models, the same short prompt can spend a different
    number of completion tokens on hidden reasoning in stream vs non-stream
    calls, especially when the model only supports the default temperature.
    Prompt tokens should still line up; completion tokens should be present,
    positive, and not suspiciously larger than the non-stream count.
    """
    if not non_stream_usage or not stream_usage:
        return False
    left_prompt = _int(non_stream_usage.get("prompt_tokens"))
    right_prompt = _int(stream_usage.get("prompt_tokens"))
    if left_prompt is None or right_prompt is None or abs(left_prompt - right_prompt) > 1:
        return False
    left_completion = _int(non_stream_usage.get("completion_tokens"))
    right_completion = _int(stream_usage.get("completion_tokens"))
    if left_completion is None or right_completion is None or right_completion <= 0:
        return False
    if right_completion > COMPLETION_MAX_FOR_OK:
        return False
    over_tolerance = max(2, int(max(left_completion, 1) * 0.50))
    return right_completion <= left_completion + over_tolerance


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _evaluation_text(
    score: float,
    stream_usage: dict[str, Any],
    reference_result: dict[str, Any],
) -> str:
    if score >= 90:
        if reference_result.get("comparison_available"):
            return "Token 数基本可信: 返回的 Token 统计自洽,并且落在正常范围内。"
        return "Token 数基本可信: usage 加法自洽,加长文本带来的 token 增量合理,流式和非流式统计也能对上。"
    if score >= 70:
        if reference_result.get("comparison_available"):
            return "Token 数有偏差: 返回的 Token 统计有部分项目超出正常范围,建议留意是否存在多算或适配层统计误差。"
        if not stream_usage:
            return "Token 数有偏差: 基础统计基本可用,但接口没有给出完整流式 usage,无法做更强交叉验证。"
        return "Token 数有偏差: 有部分检查没有对上,建议留意是否存在额外提示词、适配层统计误差或轻微多算。"
    return "Token 数明显异常: usage 字段、token 增量或流式/非流式统计存在明显问题,有虚报或统计错误风险。"


async def _get_reference(model: str) -> dict[str, Any] | None:
    cache = _read_reference_cache()
    models = cache.setdefault("models", {})
    cached = models.get(model)
    if isinstance(cached, dict) and cached.get("version") == REFERENCE_VERSION:
        return cached

    api_key = _openai_api_key()
    if not api_key:
        return None

    collected = await _collect_reference(model, api_key)
    if collected is None:
        return None
    models[model] = collected
    _write_reference_cache(cache)
    return collected


async def _collect_reference(model: str, api_key: str) -> dict[str, Any] | None:
    samples: list[dict[str, Any]] = []
    async with OpenAIChatClient(
        "https://api.openai.com/v1",
        api_key,
        timeout=30.0,
    ) as client:
        try:
            for _ in range(REFERENCE_ROUNDS):
                _req1, short_resp, _h1, _lat1 = await client.chat_completions_create(
                    model=model,
                    max_completion_tokens=MAX_TOKENS,
                    temperature=0,
                    messages=[{"role": "user", "content": SHORT_PROMPT}],
                )
                _req2, long_resp, _h2, _lat2 = await client.chat_completions_create(
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
                short_usage = _usage(short_resp)
                long_usage = _usage(long_resp)
                stream_usage = (
                    stream.get("usage")
                    if isinstance(stream.get("usage"), dict)
                    else {}
                )
                if not (short_usage and long_usage):
                    return None
                samples.append(
                    {
                        "short_usage": _usage_ints(short_usage),
                        "long_usage": _usage_ints(long_usage),
                        "stream_usage": _usage_ints(stream_usage),
                    }
                )
        except Exception:
            return None

    return {
        "version": REFERENCE_VERSION,
        "model": model,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "rounds": len(samples),
        "ranges": {
            "short_prompt_tokens": _range(
                s["short_usage"].get("prompt_tokens") for s in samples
            ),
            "short_completion_tokens": _range(
                s["short_usage"].get("completion_tokens") for s in samples
            ),
            "short_total_tokens": _range(
                s["short_usage"].get("total_tokens") for s in samples
            ),
            "long_prompt_tokens": _range(
                s["long_usage"].get("prompt_tokens") for s in samples
            ),
            "prompt_delta": _range(
                (s["long_usage"].get("prompt_tokens") or 0)
                - (s["short_usage"].get("prompt_tokens") or 0)
                for s in samples
            ),
            "stream_prompt_tokens": _range(
                s["stream_usage"].get("prompt_tokens") for s in samples
            ),
            "stream_completion_tokens": _range(
                s["stream_usage"].get("completion_tokens") for s in samples
            ),
            "stream_total_tokens": _range(
                s["stream_usage"].get("total_tokens") for s in samples
            ),
        },
    }


def _compare_reference(
    short_usage: dict[str, Any],
    long_usage: dict[str, Any],
    stream_usage: dict[str, Any],
    reference: dict[str, Any] | None,
) -> dict[str, Any]:
    if not reference:
        return {
            "comparison_available": False,
            "pass": True,
            "score": 100.0,
            "note": "仅做本次响应自洽检查",
        }

    ranges = reference.get("ranges") if isinstance(reference.get("ranges"), dict) else {}
    checks = {
        "short_prompt_tokens": _in_range(
            _int(short_usage.get("prompt_tokens")),
            ranges.get("short_prompt_tokens"),
        ),
        "short_completion_tokens": _in_completion_range(
            _int(short_usage.get("completion_tokens")),
            ranges.get("short_completion_tokens"),
        ),
        "long_prompt_tokens": _in_range(
            _int(long_usage.get("prompt_tokens")),
            ranges.get("long_prompt_tokens"),
        ),
        "prompt_delta": _in_range(
            (
                _int(long_usage.get("prompt_tokens"))
                - _int(short_usage.get("prompt_tokens"))
                if _int(long_usage.get("prompt_tokens")) is not None
                and _int(short_usage.get("prompt_tokens")) is not None
                else None
            ),
            ranges.get("prompt_delta"),
        ),
    }
    if stream_usage:
        checks["stream_prompt_tokens"] = _in_range(
            _int(stream_usage.get("prompt_tokens")),
            ranges.get("stream_prompt_tokens"),
        )
        checks["stream_completion_tokens"] = _in_completion_range(
            _int(stream_usage.get("completion_tokens")),
            ranges.get("stream_completion_tokens"),
        )

    passed = sum(1 for item in checks.values() if item["pass"])
    total = len(checks)
    score = passed / total * 100.0 if total else 0.0
    return {
        "comparison_available": True,
        "pass": score >= 75.0,
        "score": round(score, 1),
        "checks": checks,
        "updated_at": reference.get("collected_at"),
    }


def _in_range(value: int | None, range_info: Any) -> dict[str, Any]:
    if value is None or not isinstance(range_info, dict):
        return {"value": value, "pass": False, "range": range_info}
    low = _int(range_info.get("min"))
    high = _int(range_info.get("max"))
    if low is None or high is None:
        return {"value": value, "pass": False, "range": range_info}
    tolerance = max(2, int(max(abs(high), 1) * 0.10))
    ok = low - tolerance <= value <= high + tolerance
    return {
        "value": value,
        "range": {"min": low, "max": high, "tolerance": tolerance},
        "pass": ok,
        "direction": "high" if value > high + tolerance else "low" if value < low - tolerance else "ok",
    }


def _in_completion_range(value: int | None, range_info: Any) -> dict[str, Any]:
    result = _in_range(value, range_info)
    if value is None or not isinstance(range_info, dict):
        return result
    high = _int(range_info.get("max"))
    if high is None:
        return result
    tolerance = max(2, int(max(abs(high), 1) * 0.10))
    if 0 < value <= high + tolerance:
        result["pass"] = True
        result["direction"] = "low" if value < (_int(range_info.get("min")) or 0) else "ok"
    return result


def _usage_ints(usage: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = _int(usage.get(key))
        if value is not None:
            out[key] = value
    return out


def _range(values) -> dict[str, Any]:
    clean = [v for v in values if isinstance(v, int) and not isinstance(v, bool)]
    if not clean:
        return {"min": None, "max": None, "values": []}
    return {"min": min(clean), "max": max(clean), "values": clean}


def _reference_path() -> Path:
    configured = os.environ.get("VERIDROP_OPENAI_TOKEN_REFERENCE_PATH")
    if configured:
        return Path(configured)
    return Path("web_data/openai_token_reference.json")


def _read_reference_cache() -> dict[str, Any]:
    path = _reference_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", REFERENCE_VERSION)
    data.setdefault("models", {})
    return data


def _write_reference_cache(cache: dict[str, Any]) -> None:
    path = _reference_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _openai_api_key() -> str | None:
    # Unit tests must never make live OpenAI calls just because a developer or
    # server has OPENAI_API_KEY configured.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    for path in (Path.cwd() / ".env", Path("/opt/veridrop/.env")):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                if name.strip() == "OPENAI_API_KEY":
                    value = value.strip().strip('"').strip("'")
                    return value or None
        except OSError:
            continue
    return None
