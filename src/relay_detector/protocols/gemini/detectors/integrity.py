"""Stream vs non-stream consistency check.

Sends the same prompt twice — once non-stream, once with `stream=true` — and
compares the assistant text, finish_reason, and usage. Some relays return
plausible non-stream responses but truncate / reorder / drop usage in stream
mode; this is the only Gemini OpenAI-compat detector that exercises the SSE
path.

If the relay rejects `stream_options.include_usage` (some clones don't
implement it), we retry once without it before giving up.
"""

from __future__ import annotations

from typing import Any

from ....core.models import DetectorResult
from .base import ActiveDetector
from .utils import finish_reason, message_text


PROMPT = "Reply with exactly: veridrop stream check"


class IntegrityDetector(ActiveDetector):
    name = "integrity"
    display_name = "流式一致性"
    weight = 15.0
    category = "consistency"
    description = "验证流式响应完整性"

    async def run(self, client, model: str) -> DetectorResult:
        details: dict[str, Any] = {}
        try:
            _req, non_stream, _h, _lat = await client.chat_completions_create(
                model=model,
                max_completion_tokens=128,
                temperature=0,
                messages=[{"role": "user", "content": PROMPT}],
            )
            stream = await _collect_stream(
                client,
                {
                    "model": model,
                    "max_completion_tokens": 128,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": PROMPT}],
                    "stream_options": {"include_usage": True},
                },
            )
        except Exception as e:  # noqa: BLE001
            return self._result("error", 0.0, error=str(e))

        if stream["error"]:
            details["stream_options_error"] = stream["error"]
            try:
                stream = await _collect_stream(
                    client,
                    {
                        "model": model,
                        "max_completion_tokens": 128,
                        "temperature": 0,
                        "messages": [{"role": "user", "content": PROMPT}],
                    },
                )
            except Exception as e:  # noqa: BLE001
                return self._result("error", 0.0, error=str(e), details=details)

        non_text = message_text(non_stream).strip()
        stream_text = stream["text"].strip()
        non_finish = finish_reason(non_stream)
        stream_finish = stream["finish_reason"]
        non_usage = (
            non_stream.get("usage") if isinstance(non_stream.get("usage"), dict) else {}
        )
        stream_usage = (
            stream["usage"] if isinstance(stream["usage"], dict) else {}
        )

        text_match = _normalize_text(non_text) == _normalize_text(stream_text)
        finish_match = stream_finish in (non_finish, None) or non_finish in (
            stream_finish,
            None,
        )
        usage_match = _usage_close(non_usage, stream_usage)

        score = 0.0
        if non_text:
            score += 15.0
        if stream_text:
            score += 20.0
        if text_match:
            score += 30.0
        if finish_match:
            score += 15.0
        if stream_usage:
            score += 10.0
        if usage_match:
            score += 10.0

        details.update(
            {
                "non_stream_text": non_text[:300],
                "stream_text": stream_text[:300],
                "text_match": text_match,
                "non_stream_finish_reason": non_finish,
                "stream_finish_reason": stream_finish,
                "finish_match": finish_match,
                "non_stream_usage": non_usage,
                "stream_usage": stream_usage,
                "usage_match": usage_match,
                "stream_chunk_count": stream["chunk_count"],
                "evaluation_zh": (
                    "流式与非流式响应基本一致: 文本、结束原因和 usage 三者吻合。"
                    if score >= 70.0
                    else "stream 与 non-stream 在文本、结束原因或 usage 字段上不对齐,中转站可能在两条路径上做了不同处理。"
                ),
            }
        )
        return self._result("pass" if score >= 70.0 else "fail", score, details)


async def _collect_stream(client, body: dict[str, Any]) -> dict[str, Any]:
    parts: list[str] = []
    usage: dict[str, Any] | None = None
    finish: str | None = None
    chunk_count = 0
    try:
        async for chunk, _elapsed in client.chat_completions_stream(**body):
            chunk_count += 1
            if chunk.get("_done") or chunk.get("_parse_error"):
                continue
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            choices = chunk.get("choices")
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason") is not None:
                    finish = choice.get("finish_reason")
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    continue
                content = delta.get("content")
                if isinstance(content, str):
                    parts.append(content)
    except Exception as e:  # noqa: BLE001
        return {
            "text": "",
            "usage": None,
            "finish_reason": None,
            "chunk_count": chunk_count,
            "error": str(e),
        }
    return {
        "text": "".join(parts),
        "usage": usage,
        "finish_reason": finish,
        "chunk_count": chunk_count,
        "error": None,
    }


def _normalize_text(value: str) -> str:
    return " ".join(value.lower().split()).strip(" .")


def _usage_close(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if not left or not right:
        return False
    pairs = ("prompt_tokens", "completion_tokens", "total_tokens")
    matched = 0
    for key in pairs:
        lv = left.get(key)
        rv = right.get(key)
        if (
            isinstance(lv, int)
            and not isinstance(lv, bool)
            and isinstance(rv, int)
            and not isinstance(rv, bool)
            and abs(lv - rv) <= 1
        ):
            matched += 1
    return matched >= 2
