"""StructuredOutputDetector — DESIGN.md §3.7.

Active. Forces a tool_use call with a deterministic prompt and validates:
  1. response contains a tool_use block
  2. tool_use.id starts with 'toolu_' (per official prefix convention)
  3. tool_use.name equals our defined tool name
  4. tool_use.input is a dict with city (string) and unit ∈ {celsius, fahrenheit}
  5. stop_reason == 'tool_use' (per spec when a client tool is called)
  6. tool_use.caller (if present) is one of the known enum values

Score: 5 mandatory checks × 20 = 100. caller invalid = -10.
"""

from __future__ import annotations

from ....core.models import DetectorResult
from .base import ActiveDetector


TOOL_NAME = "get_weather"
TOOL_DEF = {
    "name": TOOL_NAME,
    "description": "Get the current weather for a city.",
    "input_schema": {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "The city to look up, e.g. 'Tokyo' or 'San Francisco'.",
            },
            "unit": {
                "type": "string",
                "enum": ["celsius", "fahrenheit"],
                "description": "Temperature unit.",
            },
        },
        "required": ["city", "unit"],
    },
}
PROMPT = "What's the current weather in Tokyo? Use celsius."
MAX_TOKENS = 200
VALID_CALLERS = {"direct", "code_execution_20250825", "code_execution_20260120"}


class StructuredOutputDetector(ActiveDetector):
    name = "structured_output"
    display_name = "结构化输出"
    weight = 12.0
    category = "capability"
    description = "验证 tool/JSON 结构化输出"

    async def run(self, client, model: str) -> DetectorResult:
        try:
            _req, resp, _h, _lat = await client.messages_create(
                model=model,
                max_tokens=MAX_TOKENS,
                temperature=0,
                tools=[TOOL_DEF],
                tool_choice={"type": "any"},
                messages=[{"role": "user", "content": PROMPT}],
            )
        except Exception as e:  # noqa: BLE001
            return self._result("error", 0.0, error=str(e))

        sub: dict[str, dict] = {}
        score = 0.0

        content = resp.get("content") or []
        tool_blocks = [
            b for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use"
        ]
        block_types = [
            b.get("type") for b in content if isinstance(b, dict)
        ]

        # Check 1: tool_use block present
        has_block = bool(tool_blocks)
        sub["has_tool_use_block"] = {"value": has_block, "pass": has_block}
        if has_block:
            score += 20.0

        if not has_block:
            # When tool_use is absent, record the text response so we can see
            # *why* the model didn't call the tool (e.g. relay station's
            # system prompt told it "I can't help with tool calls" — that
            # text is the smoking gun).
            text_excerpt = "".join(
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
                and isinstance(b.get("text"), str)
            )[:400]
            return self._result(
                "fail",
                score,
                {
                    "sub_checks": sub,
                    "content_block_types": block_types,
                    "stop_reason": resp.get("stop_reason"),
                    "text_response": text_excerpt,
                },
            )

        tool = tool_blocks[0]

        # Check 2: id prefix
        bid = tool.get("id")
        ok = isinstance(bid, str) and bid.startswith("toolu_")
        sub["id_prefix"] = {"value": bid, "pass": ok}
        if ok:
            score += 20.0

        # Check 3: tool name
        name = tool.get("name")
        ok = name == TOOL_NAME
        sub["name"] = {"value": name, "pass": ok}
        if ok:
            score += 20.0

        # Check 4: input schema fit
        inp = tool.get("input")
        ok = (
            isinstance(inp, dict)
            and isinstance(inp.get("city"), str)
            and bool(inp.get("city"))
            and inp.get("unit") in ("celsius", "fahrenheit")
        )
        sub["input_schema"] = {"value": inp, "pass": ok}
        if ok:
            score += 20.0

        # Check 5: stop_reason
        sr = resp.get("stop_reason")
        ok = sr == "tool_use"
        sub["stop_reason"] = {"value": sr, "pass": ok}
        if ok:
            score += 20.0

        # Optional `caller` field (per docs: enum string of "direct" /
        # "code_execution_*"). Empirically the official API has been observed
        # returning a dict here, so we ONLY penalize when caller is a string
        # AND not in our known enum — anything else is recorded as-is without
        # penalty so doc/spec drift doesn't ding a working API.
        if "caller" in tool:
            caller = tool.get("caller")
            if isinstance(caller, str):
                is_valid = caller in VALID_CALLERS
                sub["caller"] = {"value": caller, "pass": is_valid}
                if not is_valid:
                    score = max(0.0, score - 10.0)
            elif isinstance(caller, dict):
                sub["caller"] = {
                    "value": {
                        "<type>": "dict",
                        "<keys>": sorted(caller.keys())[:8],
                    },
                    "pass": True,
                    "note": "non-string caller (Anthropic API drift) — recorded, not penalized",
                }
            else:
                sub["caller"] = {
                    "value": f"<{type(caller).__name__}>",
                    "pass": True,
                    "note": "non-string caller — recorded, not penalized",
                }

        status = "pass" if score >= 70 else "fail"
        return self._result(
            status,
            score,
            {
                "sub_checks": sub,
                "content_block_types": block_types,
                "stop_reason": resp.get("stop_reason"),
            },
        )
