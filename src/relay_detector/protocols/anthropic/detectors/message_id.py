"""MessageIDDetector — DESIGN.md §3.10.

Passive: validates id-prefix conventions across all observed responses.

Per official docs, Anthropic only guarantees the *prefix* of these IDs
('msg_', 'toolu_', 'srvtoolu_', 'file_'); length and suffix charset
"may change over time", so we deliberately do not check those.
"""

from __future__ import annotations

from typing import Any

import httpx

from ....core.models import DetectorResult
from .base import PassiveDetector


# Each base check (id, type, role, model) is worth 25.
# Each non-base prefix violation deducts 25 once.
BASE_CHECK_WEIGHT = 25.0
NESTED_VIOLATION_PENALTY = 25.0


class MessageIDDetector(PassiveDetector):
    name = "message_id"
    display_name = "消息标识规范"
    weight = 5.0
    category = "basic"
    description = "被动检查 message id 前缀与格式"

    def __init__(self) -> None:
        # Tracking violations as labels so duplicates collapse.
        self._violations: set[str] = set()
        self._observations = 0
        self._sample_violations: dict[str, str] = {}  # label -> example

    def observe(
        self,
        request: dict[str, Any],
        response: dict[str, Any],
        headers: httpx.Headers,
        latency_ms: int,
    ) -> None:
        self._observations += 1

        rid = response.get("id")
        if not (isinstance(rid, str) and rid.startswith("msg_") and len(rid) >= 8):
            self._record("id_prefix_invalid", repr(rid)[:80])

        if response.get("type") != "message":
            self._record("type_not_message", repr(response.get("type"))[:80])

        if response.get("role") != "assistant":
            self._record("role_not_assistant", repr(response.get("role"))[:80])

        model = response.get("model")
        if not isinstance(model, str) or "claude" not in model.lower():
            self._record("model_not_claude", repr(model)[:80])

        for block in response.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            bid = block.get("id")
            if btype == "tool_use" and isinstance(bid, str):
                if not bid.startswith("toolu_"):
                    self._record("tool_use_id_prefix_invalid", repr(bid)[:80])
            elif btype == "server_tool_use" and isinstance(bid, str):
                if not bid.startswith("srvtoolu_"):
                    self._record(
                        "server_tool_use_id_prefix_invalid", repr(bid)[:80]
                    )

    def _record(self, label: str, example: str) -> None:
        self._violations.add(label)
        self._sample_violations.setdefault(label, example)

    def finalize(self) -> DetectorResult:
        if self._observations == 0:
            return self.skip("no observations")

        score = 100.0
        for label in self._violations:
            score -= (
                BASE_CHECK_WEIGHT
                if label
                in (
                    "id_prefix_invalid",
                    "type_not_message",
                    "role_not_assistant",
                    "model_not_claude",
                )
                else NESTED_VIOLATION_PENALTY
            )
        score = max(0.0, score)
        status = "pass" if score >= 70 else "fail"
        return self._result(
            status,
            score,
            details={
                "observation_count": self._observations,
                "violations": sorted(self._violations),
                "samples": dict(self._sample_violations),
            },
        )
