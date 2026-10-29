"""Function/tool calling round trip via OpenAI Chat Completions tools schema."""

from __future__ import annotations

import json

from ....core.models import DetectorResult
from .base import ActiveDetector
from .utils import finish_reason, message_text, tool_calls


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
    description = "验证函数调用结构"

    async def run(self, client, model: str) -> DetectorResult:
        try:
            _req, resp, _h, _lat = await client.chat_completions_create(
                model=model,
                max_completion_tokens=128,
                temperature=0,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Use get_current_weather for Boston, MA in celsius. "
                            "Do not answer directly."
                        ),
                    }
                ],
                tools=[TOOL_DEF],
                tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
            )
        except Exception as e:  # noqa: BLE001
            return self._result("error", 0.0, error=str(e))

        sub: dict[str, dict] = {}
        score = 0.0
        calls = tool_calls(resp)
        has_call = bool(calls)
        sub["has_tool_call"] = {"value": has_call, "pass": has_call}
        if not has_call:
            return self._result(
                "fail",
                score,
                {
                    "sub_checks": sub,
                    "finish_reason": finish_reason(resp),
                    "message_text": message_text(resp)[:300],
                    "evaluation_zh": (
                        "函数调用没有真正生效: 请求强制了 tool_choice,"
                        "但响应里没有 tool_calls 数组。中转站可能没透传 OpenAI tool 字段。"
                    ),
                },
            )
        score += 20.0

        call = calls[0]
        cid = call.get("id")
        ok = isinstance(cid, str) and cid.startswith("call_")
        sub["id_prefix"] = {"value": cid, "pass": ok}
        if ok:
            score += 20.0

        ok = call.get("type") == "function"
        sub["type"] = {"value": call.get("type"), "pass": ok}
        if ok:
            score += 20.0

        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = fn.get("name")
        ok = name == TOOL_NAME
        sub["name"] = {"value": name, "pass": ok}
        if ok:
            score += 20.0

        args_raw = fn.get("arguments")
        parsed = None
        if isinstance(args_raw, str):
            try:
                parsed = json.loads(args_raw)
            except json.JSONDecodeError:
                parsed = None
        ok = (
            isinstance(parsed, dict)
            and isinstance(parsed.get("city"), str)
            and parsed.get("unit") in ("celsius", "fahrenheit")
        )
        sub["arguments_json"] = {
            "value": parsed if parsed is not None else args_raw,
            "pass": ok,
        }
        if ok:
            score += 20.0

        return self._result(
            "pass" if score >= 70.0 else "fail",
            score,
            {
                "sub_checks": sub,
                "finish_reason": finish_reason(resp),
                "evaluation_zh": (
                    "函数调用正常: 返回的 tool_calls 字段、call_ ID、function name 和 arguments JSON 都符合 OpenAI 规范。"
                    if score >= 70.0
                    else "函数调用部分通过: 中转站返回了 tool_calls,但其中某些子字段不符合 OpenAI 规范。"
                ),
            },
        )
