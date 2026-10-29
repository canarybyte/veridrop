"""OpenAI token usage parity against an optional official baseline."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ....core.models import DetectorResult, Mode
from .base import ActiveDetector


PROMPT = "Reply with exactly: pong"


class TokenParityDetector(ActiveDetector):
    name = "token_parity"
    display_name = "Token 字段对齐"
    weight = 10.0
    category = "consistency"
    description = "比对 usage 与官方字段形状"

    async def run(self, client, model: str) -> DetectorResult:
        baseline = _load_baseline_usage(model, self.config.mode if self.config else None)
        if baseline is None:
            return self.skip("no-openai-official-baseline")

        try:
            _req, resp, _h, _lat = await client.chat_completions_create(
                model=model,
                messages=[{"role": "user", "content": PROMPT}],
                max_completion_tokens=32,
                temperature=0,
                store=False,
            )
        except Exception as e:  # noqa: BLE001
            return self._result("error", 0.0, error=str(e))

        usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
        score, diffs = _score_usage(usage, baseline)
        return self._result(
            "pass" if score >= 80.0 else "fail",
            score,
            {
                "usage": usage,
                "baseline_usage": baseline,
                "diffs": diffs,
            },
        )


def _load_baseline_usage(model: str, mode: Mode | None) -> dict[str, int] | None:
    candidates: list[Path] = []
    explicit = os.environ.get("VERIDROP_OPENAI_BASELINE_PATH")
    if explicit:
        candidates.append(Path(explicit))
    suffixes = [
        "chat_text",
        "full",
        mode.value if mode else "",
    ]
    for suffix in suffixes:
        if suffix:
            candidates.append(Path("data/baselines/openai") / f"{model}_{suffix}.json")

    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        usage = _extract_usage(data)
        if usage is not None:
            return usage
    return None


def _extract_usage(data: Any) -> dict[str, int] | None:
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("usage"), dict):
        return _usage_ints(data["usage"])
    for probe in data.get("probes") or []:
        if not isinstance(probe, dict):
            continue
        if probe.get("wire_api") != "chat_completions":
            continue
        response = probe.get("response")
        if isinstance(response, dict) and isinstance(response.get("usage"), dict):
            usage = _usage_ints(response["usage"])
            if usage:
                return usage
    return None


def _usage_ints(usage: dict[str, Any]) -> dict[str, int] | None:
    out = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        out[key] = value
    return out


def _score_usage(
    usage: dict[str, Any],
    baseline: dict[str, int],
) -> tuple[float, dict[str, dict[str, Any]]]:
    diffs: dict[str, dict[str, Any]] = {}
    score = 100.0
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        observed = usage.get(key)
        expected = baseline.get(key)
        if not isinstance(observed, int) or isinstance(observed, bool):
            diffs[key] = {
                "observed": observed,
                "expected": expected,
                "delta": None,
                "pass": False,
            }
            score -= 35.0
            continue
        delta = observed - expected
        ok = abs(delta) <= (1 if key != "total_tokens" else 2)
        diffs[key] = {
            "observed": observed,
            "expected": expected,
            "delta": delta,
            "pass": ok,
        }
        if not ok:
            score -= 25.0 if key == "prompt_tokens" else 15.0
    return max(score, 0.0), diffs
