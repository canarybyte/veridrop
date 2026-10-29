"""Basic OpenAI Chat Completions request detector."""

from __future__ import annotations

from ....core.models import DetectorResult
from .base import ActiveDetector


class BasicRequestDetector(ActiveDetector):
    name = "basic_request"
    display_name = "基础请求"
    weight = 15.0
    category = "basic"
    description = "最小对话请求验证基本可用性"
    max_completion_tokens = 96

    async def run(self, client, model: str) -> DetectorResult:
        try:
            _req, resp, _h, _lat = await client.chat_completions_create(
                model=model,
                max_completion_tokens=self.max_completion_tokens,
                temperature=0,
                messages=[
                    {
                        "role": "user",
                        "content": "Reply only with the single word: pong",
                    }
                ],
            )
        except Exception as e:  # noqa: BLE001
            return self._result("error", 0.0, error=str(e))

        text = _message_text(resp)
        finish_reason = _finish_reason(resp)
        usage = resp.get("usage")
        reasoning_exhausted = _looks_like_reasoning_budget_exhausted(
            usage, finish_reason, text
        )
        ok = "pong" in text.lower()
        score = 100.0 if ok else 80.0 if text else 75.0 if reasoning_exhausted else 0.0
        return self._result(
            "pass" if score >= 70 else "fail",
            score,
            {
                "response_text": text[:300],
                "object": resp.get("object"),
                "model": resp.get("model"),
                "finish_reason": finish_reason,
                "usage": usage,
                "reasoning_budget_exhausted": reasoning_exhausted,
            },
        )


def _message_text(resp: dict) -> str:
    choices = resp.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    return content if isinstance(content, str) else ""


def _finish_reason(resp: dict):
    choices = resp.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0].get("finish_reason")
    return None


def _looks_like_reasoning_budget_exhausted(
    usage: object, finish_reason: object, text: str
) -> bool:
    if text or finish_reason != "length" or not isinstance(usage, dict):
        return False
    completion_tokens = usage.get("completion_tokens")
    details = usage.get("completion_tokens_details")
    if not isinstance(completion_tokens, int) or completion_tokens <= 0:
        return False
    if not isinstance(details, dict):
        return False
    reasoning_tokens = details.get("reasoning_tokens")
    return isinstance(reasoning_tokens, int) and reasoning_tokens >= completion_tokens
