"""OpenAI stream vs non-stream response integrity detector."""

from __future__ import annotations

from typing import Any

from ....core.models import DetectorResult
from .base import ActiveDetector


PROMPT = "Reply with exactly: veridrop stream check"


class IntegrityDetector(ActiveDetector):
    name = "integrity"
    display_name = "流式一致性"
    weight = 15.0
    category = "consistency"
    description = "验证流式分片与结束标记"

    async def run(self, client, model: str) -> DetectorResult:
        details: dict[str, Any] = {}
        try:
            _req, non_stream, _h, _lat = await client.chat_completions_create(
                model=model,
                max_completion_tokens=32,
                temperature=0,
                messages=[{"role": "user", "content": PROMPT}],
            )
            stream = await _collect_stream(
                client,
                model=model,
                body={
                    "model": model,
                    "max_completion_tokens": 32,
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
                    model=model,
                    body={
                        "model": model,
                        "max_completion_tokens": 32,
                        "temperature": 0,
                        "messages": [{"role": "user", "content": PROMPT}],
                    },
                )
            except Exception as e:  # noqa: BLE001
                return self._result("error", 0.0, error=str(e), details=details)

        non_text = _message_text(non_stream).strip()
        stream_text = stream["text"].strip()
        non_finish = _finish_reason(non_stream)
        stream_finish = stream["finish_reason"]
        non_usage = non_stream.get("usage") if isinstance(non_stream.get("usage"), dict) else {}
        stream_usage = stream["usage"] if isinstance(stream["usage"], dict) else {}

        text_match = _normalize_text(non_text) == _normalize_text(stream_text)
        finish_match = stream_finish in (non_finish, None) or non_finish in (stream_finish, None)
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
            }
        )
        return self._result("pass" if score >= 70.0 else "fail", score, details)


async def _collect_stream(client, *, model: str, body: dict[str, Any]) -> dict[str, Any]:
    _ = model
    parts: list[str] = []
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    chunk_count = 0
    try:
        async for chunk, _elapsed_ms in client.chat_completions_stream(**body):
            chunk_count += 1
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            choices = chunk.get("choices")
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason") is not None:
                    finish_reason = choice.get("finish_reason")
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
        "finish_reason": finish_reason,
        "chunk_count": chunk_count,
        "error": None,
    }


def _message_text(resp: dict) -> str:
    choices = resp.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = msg.get("content") if isinstance(msg, dict) else ""
    return content if isinstance(content, str) else ""


def _finish_reason(resp: dict):
    choices = resp.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0].get("finish_reason")
    return None


def _normalize_text(value: str) -> str:
    return " ".join(value.lower().split()).strip(" .")


def _usage_close(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if not left or not right:
        return False
    left_prompt = _int(left.get("prompt_tokens"))
    right_prompt = _int(right.get("prompt_tokens"))
    if left_prompt is None or right_prompt is None or abs(left_prompt - right_prompt) > 1:
        return False

    left_completion = _int(left.get("completion_tokens"))
    right_completion = _int(right.get("completion_tokens"))
    if left_completion is None or right_completion is None or right_completion <= 0:
        return False

    # Reasoning-capable models may allocate hidden reasoning tokens differently
    # between stream and non-stream calls. Treat lower stream completion usage
    # as compatible; still reject suspicious over-reporting.
    over_tolerance = max(2, int(max(left_completion, 1) * 0.50))
    return right_completion <= left_completion + over_tolerance


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
