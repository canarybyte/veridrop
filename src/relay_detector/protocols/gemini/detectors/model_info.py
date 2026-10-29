"""Model identity + deterministic stability check.

In OpenAI-compat mode, Gemini relays return the model id in `response.model`.
The relay's job is to faithfully echo (or canonically map) the requested model.
We also probe deterministic completion length stability across runs to flag
models that silently fall back to a different routing target between calls.
"""

from __future__ import annotations

import statistics

from ....core.models import DetectorResult, Mode
from ..config import models_match
from .base import ActiveDetector
from .utils import int_value


class ModelInfoDetector(ActiveDetector):
    name = "model_info"
    display_name = "模型响应形状"
    weight = 15.0
    category = "basic"
    description = "检查响应 model/形状是否符合声明"

    async def run(self, client, model: str) -> DetectorResult:
        quick = self.config is not None and self.config.mode == Mode.QUICK
        runs = 1 if quick else 3
        responses = []
        try:
            for _ in range(runs):
                _req, resp, _h, _lat = await client.chat_completions_create(
                    model=model,
                    max_completion_tokens=60,
                    temperature=0,
                    messages=[
                        {
                            "role": "user",
                            "content": "In one sentence, explain HTTP status 418.",
                        }
                    ],
                )
                responses.append(resp)
        except Exception as e:  # noqa: BLE001
            return self._result("error", 0.0, error=str(e))

        first = responses[0]
        response_model = str(first.get("model") or "")
        match = models_match(model, response_model)
        score = 60.0 if match else 0.0
        details = {
            "request_model": model,
            "response_model": response_model,
            "model_match": match,
            "response_id": first.get("id"),
            "response_object": first.get("object"),
            "n_runs": runs,
        }
        if runs == 1:
            score += 40.0
            details["stability_label"] = "skipped_quick_mode"
        else:
            tokens = [
                int_value((r.get("usage") or {}).get("completion_tokens")) or 0
                for r in responses
            ]
            details["completion_tokens_seq"] = tokens
            mean = statistics.mean(tokens) if tokens else 0
            if mean:
                cv = statistics.pstdev(tokens) / mean
                details["stability_cv"] = round(cv, 3)
                if cv < 0.10:
                    score += 40.0
                    details["stability_label"] = "stable"
                elif cv < 0.30:
                    score += 20.0
                    details["stability_label"] = "suspicious"
                else:
                    details["stability_label"] = "highly_anomalous"
            else:
                details["stability_label"] = "no_completion_usage"
        return self._result("pass" if score >= 70.0 else "fail", score, details)
