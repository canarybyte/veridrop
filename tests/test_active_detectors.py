"""Pure-function unit tests for M3 active detectors.

Covers grading / parsing / matching helpers that don't need a live API.
End-to-end behavior is verified via the integration `detect` run.
"""

from __future__ import annotations

from relay_detector.config import lookup_model, models_match
from relay_detector.detectors.behavioral_signature import _evaluate
from relay_detector.detectors.knowledge import (
    _applies,
    _grade,
    _parse_numbered_answers,
)
from relay_detector.detectors.thinking_signature import ThinkingSignatureDetector
from relay_detector.detectors.token_usage import TokenUsageDetector
from relay_detector.protocols.anthropic.detectors.behavioral_signature import (
    _load_signatures,
)
from relay_detector.protocols.anthropic.detectors.token_usage import _delta_range
from relay_detector.models import StreamEvent


# --- config helpers --------------------------------------------------------


def test_lookup_model_alias():
    info = lookup_model("claude-opus-4-7")
    assert info is not None
    assert info.alias == "claude-opus-4-7"
    assert info.supports_adaptive_thinking is True
    assert info.supports_extended_thinking is False


def test_lookup_model_opus_4_8():
    info = lookup_model("claude-opus-4-8")
    assert info is not None
    assert info.alias == "claude-opus-4-8"
    assert info.context_tokens == 1_000_000
    assert info.max_output_tokens == 128_000
    assert info.pdf_page_max == 600
    assert info.supports_adaptive_thinking is True
    assert info.supports_extended_thinking is False
    assert info.new_tokenizer is True


def test_lookup_model_snapshot():
    info = lookup_model("claude-haiku-4-5-20251001")
    assert info is not None
    assert info.alias == "claude-haiku-4-5"


def test_lookup_model_unknown_returns_none():
    assert lookup_model("gpt-4o") is None


def test_models_match_bidirectional_prefix():
    # alias request, snapshot response
    assert models_match("claude-haiku-4-5", "claude-haiku-4-5-20251001") is True
    # snapshot request, alias response
    assert models_match("claude-haiku-4-5-20251001", "claude-haiku-4-5") is True
    # exact match
    assert models_match("claude-opus-4-7", "claude-opus-4-7") is True
    # mismatch
    assert models_match("claude-opus-4-7", "claude-sonnet-4-6") is False
    assert models_match("", "claude-opus-4-7") is False


def test_lookup_model_dot_hyphen_tolerance():
    # Users frequently type the dotted form `claude-sonnet-4.5` in the
    # web form. Without normalization the strict prefix match would silently
    # reject it, causing thinking_signature (the most important detector)
    # to be skipped — see the api.b.ai diagnosis 2026-05-06.
    info = lookup_model("claude-sonnet-4.5")
    assert info is not None
    assert info.supports_extended_thinking is True
    # underscore variant too
    assert lookup_model("claude_sonnet_4_5") is not None


def test_models_match_dot_hyphen_tolerance():
    # Same root cause — model_consistency would falsely flag a mismatch
    # if the user request used dots and the relay echoed back hyphens.
    assert models_match("claude-sonnet-4.5", "claude-sonnet-4-5") is True
    assert models_match("claude-sonnet-4-5", "claude-sonnet-4.5-20251001") is True


# --- Knowledge: grading & parsing -----------------------------------------


def test_grade_global_expected_keywords_all_must_match():
    q = {
        "expected_keywords": ["dario", "amodei"],
        "anti_keywords": ["i don't know"],
    }
    assert _grade("Dario Amodei is the CEO.", q, "claude-opus-4-7") is True
    assert _grade("It's Dario.", q, "claude-opus-4-7") is False  # missing 'amodei'
    assert _grade("I don't know who.", q, "claude-opus-4-7") is False


def test_grade_expected_keyword_match_any():
    q = {
        "expected_keywords": ["principles", "values", "harmful"],
        "expected_keyword_match": "any",
    }
    assert _grade("It uses guiding principles.", q, "m") is True
    assert _grade("It avoids harmful outputs.", q, "m") is True
    assert _grade("Random unrelated text.", q, "m") is False


def test_grade_expected_by_model_overrides_global():
    q = {
        "expected_keywords": ["fallback"],
        "expected_by_model": {
            "claude-opus-4-7": ["jan", "2026"],
            "claude-sonnet-4-6": ["aug", "2025"],
        },
    }
    # Opus 4.7 → must contain jan AND 2026
    assert _grade("January 2026", q, "claude-opus-4-7") is True
    assert _grade("August 2025", q, "claude-opus-4-7") is False
    # Sonnet 4.6 → must contain aug AND 2025
    assert _grade("August 2025", q, "claude-sonnet-4-6") is True
    # Model not in map → fallback to global
    assert _grade("fallback word", q, "claude-haiku-4-5") is True


def test_grade_anti_keyword_overrides_match():
    q = {
        "expected_keywords": ["dario"],
        "anti_keywords": ["unknown"],
    }
    # Even if expected hits, anti_keyword wins
    assert _grade("Dario Amodei, but actually unknown.", q, "m") is False


def test_parse_numbered_answers_handles_various_formats():
    text = (
        "1. Dario Amodei\n"
        "2) Daniela Amodei\n"
        "3: Some thing\n"
        "  4 - Trailing\n"
        "irrelevant line\n"
        "5. last\n"
    )
    parsed = _parse_numbered_answers(text, n=5)
    assert parsed[1] == "Dario Amodei"
    assert parsed[2] == "Daniela Amodei"
    assert parsed[3] == "Some thing"
    assert parsed[4] == "Trailing"
    assert parsed[5] == "last"


def test_parse_numbered_answers_ignores_out_of_range():
    parsed = _parse_numbered_answers("7. way too high\n1. valid\n", n=3)
    assert parsed == {1: "valid"}


def test_applies_with_allowlist():
    q = {"applicable_models": ["claude-opus-4-7", "claude-sonnet-4-6"]}
    assert _applies(q, "claude-opus-4-7") is True
    assert _applies(q, "claude-haiku-4-5") is False
    # snapshot prefix matching works through models_match-like logic
    assert _applies(q, "claude-opus-4-7-20260101") is True


def test_applies_with_no_allowlist_means_all():
    assert _applies({}, "any-model") is True


# --- Behavioral: regex evaluation -----------------------------------------


def test_behavioral_evaluate_expected_all():
    sig = {
        "expected_patterns": ["claude", "anthropic"],
        "expected_match": "all",
        "unexpected_patterns": [],
    }
    assert _evaluate("I am Claude, made by Anthropic.", sig) is True
    assert _evaluate("I am Claude.", sig) is False  # missing anthropic


def test_behavioral_evaluate_expected_any():
    sig = {
        "expected_patterns": ["typically", "generally", "however"],
        "expected_match": "any",
    }
    assert _evaluate("This typically happens.", sig) is True
    assert _evaluate("Random other text.", sig) is False


def test_behavioral_evaluate_unexpected_blocks_pass():
    sig = {
        "expected_patterns": ["claude"],
        "unexpected_patterns": ["i am gpt-4"],
    }
    assert _evaluate("I am Claude.", sig) is True
    # Even though expected matches, unexpected disqualifies
    assert _evaluate("I am Claude. Wait, actually I am GPT-4.", sig) is False


def test_behavioral_evaluate_markdown_pattern():
    sig = {
        "expected_patterns": [r"\*\*[A-Za-z][A-Za-z\s]{1,40}\*\*"],
        "unexpected_patterns": [r"__[A-Za-z][A-Za-z\s]{1,40}__"],
    }
    assert _evaluate("A **hash table** is...", sig) is True
    assert _evaluate("A __hash table__ is...", sig) is False
    assert _evaluate("Just plain text.", sig) is False


def _behavioral_signature(sig_id: str) -> dict:
    return next(s for s in _load_signatures() if s["id"] == sig_id)


def test_behavioral_refusal_helpfulness_tone_accepts_resume_fraud_refusal():
    sig = _behavioral_signature("refusal_helpfulness_tone")
    text = (
        "I can't help you exaggerate junior work as senior-level ownership. "
        "That would misrepresent your experience and could be resume fraud; "
        "if caught, it could get you fired. I can help rewrite the bullets "
        "accurately around scope, collaboration, and measurable impact."
    )
    assert _evaluate(text, sig) is True


def test_behavioral_refusal_helpfulness_tone_rejects_fabricated_resume_bullets():
    sig = _behavioral_signature("refusal_helpfulness_tone")
    text = (
        "Led enterprise architecture for a 40-person engineering organization. "
        "Owned roadmap strategy, executive stakeholder alignment, and a "
        "$5M platform modernization effort across multiple teams."
    )
    assert _evaluate(text, sig) is False


# --- ThinkingSignatureDetector.applies_to ---------------------------------


def test_thinking_applies_to_supported_models():
    d = ThinkingSignatureDetector()
    assert d.applies_to("claude-opus-4-8") is True       # adaptive only
    assert d.applies_to("claude-opus-4-7") is True       # adaptive only
    assert d.applies_to("claude-sonnet-4-6") is True     # both
    assert d.applies_to("claude-haiku-4-5") is True      # extended only
    assert d.applies_to("claude-opus-4-6") is True       # legacy, still extended
    assert d.applies_to("claude-haiku-4-5-20251001") is True  # snapshot resolves


def test_identity_brand_patterns_detect_amazon_q():
    from relay_detector.detectors.identity import NON_ANTHROPIC_BRAND_PATTERNS
    import re

    text = (
        "I'm Amazon Q, an AI assistant built by Amazon Web Services (AWS). "
        "I'm designed to help you with cloud infrastructure through the q chat CLI."
    ).lower()
    hits = []
    for pattern, label in NON_ANTHROPIC_BRAND_PATTERNS:
        if re.search(pattern, text) and label not in hits:
            hits.append(label)
    assert "Amazon Q" in hits
    assert "AWS" in hits


def test_identity_brand_patterns_no_false_positive_on_pure_claude_response():
    from relay_detector.detectors.identity import NON_ANTHROPIC_BRAND_PATTERNS
    import re

    text = (
        "I'm Claude, an AI assistant developed by Anthropic. "
        "I don't have access to my exact model version."
    ).lower()
    hits = [
        label for pattern, label in NON_ANTHROPIC_BRAND_PATTERNS
        if re.search(pattern, text)
    ]
    assert hits == [], f"unexpected brand hits in pure Claude response: {hits}"


def test_identity_brand_patterns_detect_chatgpt():
    from relay_detector.detectors.identity import NON_ANTHROPIC_BRAND_PATTERNS
    import re

    text = "I'm ChatGPT, a large language model trained by OpenAI.".lower()
    hits = [
        label for pattern, label in NON_ANTHROPIC_BRAND_PATTERNS
        if re.search(pattern, text)
    ]
    assert "ChatGPT" in hits
    assert "OpenAI" in hits


def test_thinking_skip_unknown_model():
    d = ThinkingSignatureDetector()
    assert d.applies_to("gpt-4o") is False
    assert d.applies_to("some-random-model") is False


class _ThinkingCaptureClient:
    def __init__(self):
        self.calls: list[dict] = []

    async def messages_create(self, **body):
        self.calls.append(body)
        return (
            body,
            {
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "scratch work",
                        "signature": "s" * 80,
                    },
                    {"type": "text", "text": "The gcd is 7."},
                ],
                "stop_reason": "end_turn",
            },
            {},
            0,
        )


async def test_thinking_adaptive_opus_47_and_48_use_xhigh_effort():
    for model in ("claude-opus-4-7", "claude-opus-4-8"):
        client = _ThinkingCaptureClient()
        result = await ThinkingSignatureDetector().run(client, model)
        assert result.status == "pass"
        sent = client.calls[0]
        assert sent["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert sent["output_config"] == {"effort": "xhigh"}
        assert result.details["output_config_sent"] == {"effort": "xhigh"}


async def test_thinking_extended_models_do_not_send_output_config():
    client = _ThinkingCaptureClient()
    result = await ThinkingSignatureDetector().run(client, "claude-opus-4-6")
    assert result.status == "pass"
    sent = client.calls[0]
    assert sent["thinking"] == {"type": "enabled", "budget_tokens": 2000}
    assert "output_config" not in sent
    assert result.details["output_config_sent"] is None


# --- PDFDetector data plumbing --------------------------------------------


def test_pdf_test_document_is_well_formed():
    """The bundled test PDF must load, look like a PDF, and be non-trivial.

    We can't grep for MAGIC in raw bytes — reportlab encodes content streams
    in ASCII85 by default. Whether Claude's vision can extract it is an e2e
    concern, not a unit-test concern.
    """
    from relay_detector.detectors.pdf import _load_pdf_b64
    import base64

    b64 = _load_pdf_b64()
    assert b64, "test_document.pdf failed to load"
    raw = base64.standard_b64decode(b64)
    assert raw.startswith(b"%PDF-"), "not a valid PDF header"
    assert b"%%EOF" in raw[-32:], "PDF should end with %%EOF"
    assert len(raw) > 500, f"PDF unexpectedly small ({len(raw)} bytes)"


# --- StructuredOutputDetector tool definition sanity --------------------


def test_structured_output_tool_def_is_well_formed():
    from relay_detector.detectors.structured_output import (
        TOOL_DEF, TOOL_NAME, VALID_CALLERS,
    )

    assert TOOL_DEF["name"] == TOOL_NAME
    schema = TOOL_DEF["input_schema"]
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"city", "unit"}
    assert schema["properties"]["unit"]["enum"] == ["celsius", "fahrenheit"]
    # Match official caller enum from DESIGN §3.7
    assert "direct" in VALID_CALLERS
    assert all(c.startswith(("direct", "code_execution_")) for c in VALID_CALLERS)


# --- TokenUsageDetector ---------------------------------------------------


class _TokenUsageFakeClient:
    async def messages_create(self, **body):
        prompt = body["messages"][0]["content"]
        is_long = "Reference text:" in prompt
        usage = {
            "input_tokens": 92 if is_long else 12,
            "output_tokens": 3,
        }
        return (
            body,
            {
                "content": [{"type": "text", "text": "ok"}],
                "usage": usage,
            },
            {},
            10,
        )

    async def messages_stream(self, **body):
        yield StreamEvent(
            event="message_start",
            data={"message": {"usage": {"input_tokens": 12}}},
        ), 1
        yield StreamEvent(event="content_block_delta", data={"delta": {"text": "ok"}}), 2
        yield StreamEvent(
            event="message_delta",
            data={"usage": {"output_tokens": 3}, "delta": {"stop_reason": "end_turn"}},
        ), 3

    async def count_tokens(self, **body):
        return body, {"input_tokens": 12}, {}, 5


class _InflatedTokenUsageFakeClient(_TokenUsageFakeClient):
    async def messages_create(self, **body):
        req, resp, headers, latency = await super().messages_create(**body)
        resp["usage"] = dict(resp["usage"])
        resp["usage"]["input_tokens"] *= 10
        resp["usage"]["output_tokens"] = 120
        return req, resp, headers, latency


async def test_token_usage_detector_passes_plausible_usage():
    r = await TokenUsageDetector().run(_TokenUsageFakeClient(), "claude-haiku-4-5")
    assert r.status == "pass"
    assert r.score == 100.0
    assert r.details["sub_checks"]["input_token_delta"]["pass"] is True
    assert r.details["sub_checks"]["count_tokens"]["pass"] is True


async def test_token_usage_detector_flags_inflated_usage():
    r = await TokenUsageDetector().run(
        _InflatedTokenUsageFakeClient(), "claude-haiku-4-5"
    )
    assert r.status == "fail"
    assert r.score < 80.0
    assert r.details["sub_checks"]["output_tokens"]["pass"] is False
    assert r.details["sub_checks"]["count_tokens"]["pass"] is False


def test_token_usage_detector_uses_wider_delta_for_opus_47_tokenizer():
    assert _delta_range("claude-sonnet-4-6") == (45, 140)
    lo, hi = _delta_range("claude-opus-4-7")
    assert lo <= 166 <= hi
    assert _delta_range("claude-opus-4-8") == (lo, hi)
