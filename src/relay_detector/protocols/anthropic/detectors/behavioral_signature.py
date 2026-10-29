"""BehavioralSignatureDetector — DESIGN.md §3.2.

Active. Sends N short, deterministic probes that elicit Claude-typical
behaviors (refusal style, markdown formatting, hedging, structure preference,
identity-injection resistance). Each signature has expected/unexpected regex
patterns; a signature 'hits' when expected match AND no unexpected match.

Score = sum(weight where hit) / sum(weight) × 100.

Limitations: behaviors can be coaxed by sufficient prompt engineering, which
is why the weight is 15% (not 25%). The detector is a reasonable indicator,
not proof.
"""

from __future__ import annotations

import importlib.resources as resources
import json
import re

from ....core.models import DetectorResult
from .base import ActiveDetector


MAX_TOKENS = 350


def _load_signatures() -> list[dict]:
    raw = (
        resources.files("relay_detector.protocols.anthropic.data")
        .joinpath("behavioral_signatures.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(raw).get("signatures", [])


class BehavioralSignatureDetector(ActiveDetector):
    name = "behavioral_signature"
    display_name = "行为签名验证"
    weight = 15.0
    category = "identity"
    description = "用行为指纹判断是否为官方模型族"

    async def run(self, client, model: str) -> DetectorResult:
        signatures = _load_signatures()
        if not signatures:
            return self._result("skip", 0.0, {"reason": "no signatures loaded"})

        results: list[dict] = []
        weighted_hits = 0.0
        max_weight = 0.0

        for sig in signatures:
            w = float(sig.get("weight", 1.0))
            max_weight += w
            messages: list[dict] = []
            kwargs: dict = {
                "model": model,
                "max_tokens": MAX_TOKENS,
                "temperature": 0,
                "messages": [{"role": "user", "content": sig["prompt"]}],
            }
            if sig.get("system"):
                kwargs["system"] = sig["system"]
            try:
                _req, resp, _h, _lat = await client.messages_create(**kwargs)
            except Exception as e:  # noqa: BLE001
                results.append(
                    {"id": sig["id"], "hit": False, "error": str(e), "weight": w}
                )
                continue
            text = _join_text(resp.get("content"))
            hit = _evaluate(text, sig)
            results.append(
                {
                    "id": sig["id"],
                    "hit": hit,
                    "weight": w,
                    "response_excerpt": text[:200],
                }
            )
            if hit:
                weighted_hits += w

        score = (weighted_hits / max_weight * 100.0) if max_weight else 0.0
        status = "pass" if score >= 70 else "fail"
        return self._result(
            status,
            score,
            {
                "signatures": results,
                "hits": sum(1 for r in results if r.get("hit")),
                "total": len(results),
            },
        )


def _evaluate(text: str, sig: dict) -> bool:
    expected = sig.get("expected_patterns") or []
    unexpected = sig.get("unexpected_patterns") or []
    match_mode = sig.get("expected_match", "all")

    def rx_match(pattern: str) -> bool:
        return re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE) is not None

    if any(rx_match(p) for p in unexpected):
        return False
    if not expected:
        return True
    if match_mode == "any":
        return any(rx_match(p) for p in expected)
    return all(rx_match(p) for p in expected)


def _join_text(content) -> str:
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text")
            if isinstance(t, str):
                parts.append(t)
    return "".join(parts)
