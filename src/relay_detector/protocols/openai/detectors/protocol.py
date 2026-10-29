"""OpenAI Chat Completions protocol-shape detector."""

from __future__ import annotations

from typing import Any

import httpx

from ....core.models import DetectorResult
from ..baseline import extract_openai_features, sanitize_openai_headers
from ..protocol_templates import validate_chat_completion
from .base import PassiveDetector


class ProtocolDetector(PassiveDetector):
    name = "protocol"
    display_name = "协议规范性"
    weight = 15.0
    category = "basic"
    description = "被动检查 OpenAI Chat Completions 字段"

    def __init__(self) -> None:
        self._observations: list[dict[str, Any]] = []

    def observe(
        self,
        request: dict[str, Any],
        response: dict[str, Any],
        headers: httpx.Headers,
        latency_ms: int,
    ) -> None:
        validation = validate_chat_completion(
            response,
            request_model=str(request.get("model") or ""),
        )
        safe_headers = sanitize_openai_headers(headers)
        self._observations.append(
            {
                "request_model": request.get("model"),
                "latency_ms": latency_ms,
                "validation": validation.to_dict(),
                "features": extract_openai_features(
                    "chat_completions",
                    response,
                    safe_headers,
                ),
            }
        )

    def finalize(self) -> DetectorResult:
        if not self._observations:
            return self.skip("no-observations")

        scores = [
            float(obs["validation"]["score"])
            for obs in self._observations
            if isinstance(obs.get("validation"), dict)
        ]
        score = sum(scores) / len(scores) if scores else 0.0
        issues = [
            issue
            for obs in self._observations
            for issue in obs["validation"].get("issues", [])
        ]
        critical_count = sum(1 for issue in issues if issue.get("severity") == "critical")
        major_count = sum(1 for issue in issues if issue.get("severity") == "major")
        details = {
            "observation_count": len(self._observations),
            "critical_issue_count": critical_count,
            "major_issue_count": major_count,
            "issues": issues[:30],
            "fingerprints": [
                obs["validation"].get("fingerprints", {})
                for obs in self._observations[:10]
            ],
        }
        passed = score >= 80.0 and critical_count == 0
        return self._result("pass" if passed else "fail", score, details)
