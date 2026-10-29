"""OpenAI Chat Completions detector config."""

from __future__ import annotations

from ...core.models import Mode


DETECTOR_WEIGHTS: dict[str, float] = {
    "basic_request": 15.0,
    "model_consistency": 15.0,
    "function_calling": 15.0,
    "structured_output": 15.0,
    "protocol": 15.0,
    "integrity": 15.0,
    "token_billing": 10.0,
    # Heavier weight than other detectors because context-window fraud is
    # one of the highest-impact lies a relay can tell. Skipped (0 effective
    # weight) when ExecutionConfig.include_long_context is False.
    "long_context": 15.0,
    # AZ 探测:行为层 Azure 特征,仅 full 模式执行(见 MODE_DETECTORS)。
    "azure_api_version": 15.0,
    "azure_content_filter": 15.0,
    "azure_model_echo": 10.0,
}


MODE_DETECTORS: dict[Mode, set[str]] = {
    Mode.QUICK: {
        "basic_request",
        "model_consistency",
        "protocol",
    },
    Mode.STANDARD: {
        "basic_request",
        "model_consistency",
        "function_calling",
        "structured_output",
        "protocol",
        "integrity",
        "token_billing",
    },
    # full mode includes long_context, but the detector self-skips unless
    # ExecutionConfig.include_long_context is True. So default `--mode full`
    # stays cheap (~$0.005); users who want long-context probing must
    # explicitly opt in.
    Mode.FULL: set(DETECTOR_WEIGHTS.keys()),
}


OPENAI_MODEL_CHOICES = [
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gpt-5.4-nano",
    "gpt-5.4-mini",
]


def models_match(request_model: str, response_model: str) -> bool:
    """Bidirectional prefix match with dot-vs-hyphen normalization
    (e.g. `gpt-5.4-mini` ≡ `gpt-5-4-mini`)."""
    if not request_model or not response_model:
        return False
    a = request_model.replace(".", "-").replace("_", "-")
    b = response_model.replace(".", "-").replace("_", "-")
    return a == b or a.startswith(b) or b.startswith(a)
