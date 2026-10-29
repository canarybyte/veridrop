"""ConsistencyDetector — DESIGN.md §3.4.

Active. Two layers:
  1. response.model field matches the requested model (bidirectional prefix)
  2. With temperature=0, output_tokens are stable across N runs (CV < 0.1)

Quick mode simplifies to a single call (just the model-field check); the
stability sub-score is recorded as "skipped" and given full credit so the
detector's max stays 100. Standard/Full do the full 3-run stability test.
"""

from __future__ import annotations

import statistics

from ..config import models_match
from ....core.models import DetectorResult, Mode
from .base import ActiveDetector


PROBE_PROMPT = (
    "Reply in 30 words explaining what HTTP status 418 means. "
    "Do not include any preamble."
)
MAX_TOKENS = 100
RUNS_FULL = 3
RUNS_QUICK = 1


class ConsistencyDetector(ActiveDetector):
    name = "consistency"
    display_name = "模型一致性"
    weight = 10.0
    category = "consistency"
    description = "重复请求比对输出与模型字段是否稳定"

    async def run(self, client, model: str) -> DetectorResult:
        is_quick = self.config is not None and self.config.mode == Mode.QUICK
        n_runs = RUNS_QUICK if is_quick else RUNS_FULL

        responses = []
        try:
            for _ in range(n_runs):
                _req, resp, _h, _lat = await client.messages_create(
                    model=model,
                    max_tokens=MAX_TOKENS,
                    temperature=0,
                    messages=[{"role": "user", "content": PROBE_PROMPT}],
                )
                responses.append(resp)
        except Exception as e:  # noqa: BLE001
            return self._result("error", 0.0, error=str(e))

        first = responses[0]
        response_model = first.get("model") or ""
        match = models_match(model, response_model)
        model_score = 60.0 if match else 0.0

        details: dict = {
            "request_model": model,
            "response_model": response_model,
            "model_match": match,
            "n_runs": n_runs,
        }

        if n_runs > 1:
            output_tokens = [
                _safe_int(r.get("usage", {}).get("output_tokens"))
                for r in responses
            ]
            details["output_tokens_seq"] = output_tokens
            mean = statistics.mean(output_tokens) if output_tokens else 0
            if mean > 0:
                stdev = statistics.pstdev(output_tokens)
                cv = stdev / mean
                details["stability_cv"] = round(cv, 3)
                if cv < 0.10:
                    stability_score = 40.0
                    details["stability_label"] = "stable"
                elif cv < 0.30:
                    stability_score = 20.0
                    details["stability_label"] = "suspicious"
                else:
                    stability_score = 0.0
                    details["stability_label"] = "highly_anomalous"
            else:
                stability_score = 0.0
                details["stability_label"] = "no_output"
        else:
            # Quick mode: skip stability test, full credit, mark as skipped.
            stability_score = 40.0
            details["stability_label"] = "skipped_quick_mode"

        score = model_score + stability_score
        status = "pass" if score >= 70 else "fail"
        return self._result(status, score, details)


def _safe_int(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else 0
