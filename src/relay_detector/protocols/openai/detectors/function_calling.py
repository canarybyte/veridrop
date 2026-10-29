"""OpenAI function/tool calling detector."""

from __future__ import annotations

import json

from ....core.models import DetectorResult
from .base import ActiveDetector


TOOL_NAME = "get_current_weather"
TOOL_DEF = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "Get current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
            },
            "required": ["city", "unit"],
            "additionalProperties": False,
        },
    },
}


class FunctionCallingDetector(ActiveDetector):
    name = "function_calling"
    display_name = "函数调用"
    weight = 15.0
    category = "capability"
    description = "验证 tool_calls 结构与执行"

    async def run(self, client, model: str) -> DetectorResult:
        try:
            _req, resp, _h, _lat = await client.chat_completions_create(
                model=model,
                max_completion_tokens=128,
                temperature=0,
                messages=[
                    {
                        "role": "user",
                        "content": "Use get_current_weather for Boston, MA in celsius. Do not answer directly.",
                    }
                ],
                tools=[TOOL_DEF],
                tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
            )
        except Exception as e:  # noqa: BLE001
            return self._result("error", 0.0, error=str(e))

        score = 0.0
        sub: dict[str, dict] = {}
        tool_calls = _tool_calls(resp)
        has_call = bool(tool_calls)
        sub["has_tool_call"] = {"value": has_call, "pass": has_call}
        if has_call:
            score += 20
        else:
            return self._result(
                "fail",
                score,
                {
                    "sub_checks": sub,
                    "finish_reason": _finish_reason(resp),
                    "message_text": _message_text(resp)[:300],
                },
            )

        call = tool_calls[0]
        cid = call.get("id")
        ok = isinstance(cid, str) and cid.startswith("call_")
        sub["id_prefix"] = {"value": cid, "pass": ok}
        if ok:
            score += 20

        ok = call.get("type") == "function"
        sub["type"] = {"value": call.get("type"), "pass": ok}
        if ok:
            score += 20

        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = fn.get("name")
        ok = name == TOOL_NAME
        sub["name"] = {"value": name, "pass": ok}
        if ok:
            score += 20

        args_raw = fn.get("arguments")
        parsed = None
        try:
            parsed = json.loads(args_raw) if isinstance(args_raw, str) else None
        except json.JSONDecodeError:
            parsed = None
        ok = (
            isinstance(parsed, dict)
            and isinstance(parsed.get("city"), str)
            and parsed.get("unit") in ("celsius", "fahrenheit")
        )
        sub["arguments_json"] = {"value": parsed if parsed is not None else args_raw, "pass": ok}
        if ok:
            score += 20

        return self._result(
            "pass" if score >= 70 else "fail",
            score,
            {"sub_checks": sub, "finish_reason": _finish_reason(resp)},
        )


def _tool_calls(resp: dict) -> list[dict]:
    choices = resp.get("choices")
    if not isinstance(choices, list) or not choices:
        return []
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    calls = msg.get("tool_calls") if isinstance(msg, dict) else None
    return [c for c in calls if isinstance(c, dict)] if isinstance(calls, list) else []


def _finish_reason(resp: dict):
    choices = resp.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0].get("finish_reason")
    return None


def _message_text(resp: dict) -> str:
    choices = resp.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = msg.get("content") if isinstance(msg, dict) else ""
    return content if isinstance(content, str) else ""
