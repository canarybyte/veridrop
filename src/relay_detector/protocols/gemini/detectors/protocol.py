"""Passive Chat Completions response shape validator.

Observes every successful response (non-stream and synthesized stream) and
checks the OpenAI Chat Completions envelope: id prefix `chatcmpl-`,
object="chat.completion", required top-level keys, finish_reason in the
allowed enum, usage fields are integers when present.
"""

from __future__ import annotations

from typing import Any

import httpx

from ....core.models import DetectorResult
from .base import PassiveDetector


REQUIRED_TOP_LEVEL = ("id", "object", "model", "choices")
ALLOWED_FINISH_REASONS = (
    None,
    "stop",
    "length",
    "tool_calls",
    "content_filter",
    "function_call",
)
ALLOWED_OBJECT = "chat.completion"
ID_PREFIX = "chatcmpl-"


class ProtocolDetector(PassiveDetector):
    name = "protocol"
    display_name = "协议规范性"
    weight = 15.0
    category = "basic"
    description = "被动检查 OpenAI 兼容协议字段"

    def __init__(self) -> None:
        self._observations: list[dict[str, Any]] = []

    def observe(
        self,
        request: dict[str, Any],
        response: dict[str, Any],
        headers: httpx.Headers,
        latency_ms: int,
    ) -> None:
        score, issues = _shape_score(response)
        self._observations.append(
            {
                "request_model": request.get("model"),
                "latency_ms": latency_ms,
                "score": score,
                "issues": issues,
                "id": response.get("id"),
                "object": response.get("object"),
            }
        )

    def finalize(self) -> DetectorResult:
        if not self._observations:
            return self.skip("no-observations")

        score = sum(float(obs["score"]) for obs in self._observations) / len(
            self._observations
        )
        issues = [
            issue
            for obs in self._observations
            for issue in obs.get("issues", [])
        ]
        critical_count = sum(
            1 for issue in issues if issue.get("severity") == "critical"
        )
        passed = score >= 80.0 and critical_count == 0
        return self._result(
            "pass" if passed else "fail",
            score,
            {
                "observation_count": len(self._observations),
                "critical_issue_count": critical_count,
                "issues": issues[:30],
                "evaluation_zh": (
                    "Chat Completions 响应字段、id 前缀、finish_reason 等均符合 OpenAI 规范。"
                    if passed
                    else "响应不完全符合 OpenAI Chat Completions 规范,中转站可能改写了部分字段。"
                ),
            },
        )


def _shape_score(response: dict[str, Any]) -> tuple[float, list[dict[str, str]]]:
    """Score a single Chat Completions response 0–100 and list any issues."""
    issues: list[dict[str, str]] = []

    def add_issue(severity: str, code: str, message: str) -> None:
        issues.append({"severity": severity, "code": code, "message": message})

    checks_total = 0
    checks_passed = 0

    for key in REQUIRED_TOP_LEVEL:
        checks_total += 1
        if key in response:
            checks_passed += 1
        else:
            add_issue("critical", f"missing_{key}", f"响应缺少 {key} 字段")

    checks_total += 1
    response_id = response.get("id")
    if isinstance(response_id, str) and response_id.startswith(ID_PREFIX):
        checks_passed += 1
    else:
        add_issue(
            "major",
            "bad_id_prefix",
            f"response.id 应以 '{ID_PREFIX}' 开头,实际为 {response_id!r}",
        )

    checks_total += 1
    obj = response.get("object")
    if obj == ALLOWED_OBJECT:
        checks_passed += 1
    else:
        add_issue(
            "major",
            "bad_object",
            f"response.object 应为 '{ALLOWED_OBJECT}',实际为 {obj!r}",
        )

    checks_total += 1
    choices = response.get("choices")
    first_choice: dict[str, Any] | None = None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        first_choice = choices[0]
        checks_passed += 1
    else:
        add_issue("critical", "bad_choices", "choices 不是非空数组")

    checks_total += 1
    if first_choice is not None:
        finish = first_choice.get("finish_reason")
        if finish in ALLOWED_FINISH_REASONS:
            checks_passed += 1
        else:
            add_issue(
                "major",
                "bad_finish_reason",
                f"finish_reason {finish!r} 不在 OpenAI 允许的枚举里",
            )
    else:
        add_issue("major", "bad_finish_reason", "缺少 finish_reason")

    checks_total += 1
    if first_choice is not None:
        message = first_choice.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            checks_passed += 1
        else:
            add_issue(
                "major",
                "bad_message",
                "choices[0].message 缺失或 role 不是 assistant",
            )
    else:
        add_issue("major", "bad_message", "缺少 message")

    checks_total += 1
    usage = response.get("usage")
    if isinstance(usage, dict):
        checks_passed += 1
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            checks_total += 1
            value = usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                checks_passed += 1
            else:
                add_issue(
                    "minor",
                    f"bad_usage_{field}",
                    f"usage.{field} 不是整数: {value!r}",
                )
    else:
        add_issue("major", "missing_usage", "响应缺少 usage 对象")

    score = (checks_passed / checks_total * 100.0) if checks_total else 0.0
    return score, issues
