"""ProtocolDetector — DESIGN.md §3.8.

Passive: validates Anthropic Messages API response schema across all observed
responses. Each *kind* of issue counts once (not once per observation), so the
detector measures protocol compliance, not how often a problem reoccurred.
"""

from __future__ import annotations

from typing import Any

import httpx

from ....core.models import DetectorResult
from .base import PassiveDetector


VALID_STOP_REASONS = {"end_turn", "max_tokens", "stop_sequence", "tool_use", None}
VALID_CONTENT_BLOCK_TYPES = {
    "text",
    "tool_use",
    "thinking",
    "redacted_thinking",
    "server_tool_use",
    "web_search_tool_result",
}

# Per design §3.8: each missing/wrong field -10. Cap at 0.
ISSUE_PENALTY = 10.0


class ProtocolDetector(PassiveDetector):
    name = "protocol"
    display_name = "协议规范性"
    weight = 5.0
    category = "basic"
    description = "被动检查响应字段是否符合协议规范"

    def __init__(self) -> None:
        self._issues: set[str] = set()
        self._observations = 0

    def observe(
        self,
        request: dict[str, Any],
        response: dict[str, Any],
        headers: httpx.Headers,
        latency_ms: int,
    ) -> None:
        self._observations += 1

        rid = response.get("id")
        if not isinstance(rid, str) or not rid:
            self._issues.add("id_missing_or_not_string")

        if response.get("type") != "message":
            self._issues.add(f"type_invalid:{response.get('type')!r}")

        if response.get("role") != "assistant":
            self._issues.add(f"role_invalid:{response.get('role')!r}")

        model = response.get("model")
        if not isinstance(model, str) or not model:
            self._issues.add("model_missing_or_not_string")

        content = response.get("content")
        if not isinstance(content, list):
            self._issues.add("content_not_array")
        else:
            for block in content:
                if not isinstance(block, dict):
                    self._issues.add("content_block_not_object")
                    continue
                btype = block.get("type")
                if not isinstance(btype, str):
                    self._issues.add("content_block_missing_type")
                elif btype not in VALID_CONTENT_BLOCK_TYPES:
                    self._issues.add(f"content_block_unknown_type:{btype!r}")

        if response.get("stop_reason") not in VALID_STOP_REASONS:
            self._issues.add(
                f"stop_reason_invalid:{response.get('stop_reason')!r}"
            )

        ss = response.get("stop_sequence")
        if not (ss is None or isinstance(ss, str)):
            self._issues.add("stop_sequence_wrong_type")

        usage = response.get("usage")
        if not isinstance(usage, dict):
            self._issues.add("usage_missing_or_not_object")
        else:
            if not _is_nonneg_int(usage.get("input_tokens")):
                self._issues.add("usage_input_tokens_invalid")
            if not _is_nonneg_int(usage.get("output_tokens")):
                self._issues.add("usage_output_tokens_invalid")
            for opt in (
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            ):
                if opt in usage and not _is_nonneg_int(usage[opt]):
                    self._issues.add(f"{opt}_wrong_type")
        # Note: we do NOT check for anthropic-request-id header — empirical
        # testing showed even api.anthropic.com does not return it on the
        # /v1/messages endpoint, so flagging its absence was a false positive.

    def finalize(self) -> DetectorResult:
        if self._observations == 0:
            return self.skip("no observations")

        score = max(0.0, 100.0 - len(self._issues) * ISSUE_PENALTY)
        status = "pass" if score >= 70 else "fail"
        return self._result(
            status,
            score,
            details={
                "observation_count": self._observations,
                "issue_count": len(self._issues),
                "issues": sorted(self._issues),
            },
        )


def _is_nonneg_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0
