"""Tests for core/long_context.py + OpenAI long_context detector."""

from __future__ import annotations

import pytest

from relay_detector.core.long_context import (
    ANSWER_RE,
    Needle,
    assemble_haystack,
    build_question,
    estimate_cost_usd,
    evaluate_recalls,
    make_needles,
    model_context_limit,
)
from relay_detector.core.models import ExecutionConfig, Mode
from relay_detector.protocols.openai.detectors.long_context import (
    LongContextDetector,
)


# ---------- Core helpers ----------


def test_make_needles_returns_three_distinct_answers():
    needles = make_needles("test-seed-1")
    assert len(needles) == 3
    answers = {n.answer for n in needles}
    assert len(answers) == 3, "Each needle must have a unique answer"
    for n in needles:
        assert ANSWER_RE.match(n.answer), f"answer {n.answer} doesn't match expected ID format"


def test_make_needles_deterministic_per_seed():
    a = make_needles("seed-A")
    b = make_needles("seed-A")
    assert [n.answer for n in a] == [n.answer for n in b]


def test_make_needles_differ_per_seed():
    a = make_needles("seed-A")
    b = make_needles("seed-B")
    assert [n.answer for n in a] != [n.answer for n in b]


def test_needles_at_distinct_positions():
    needles = make_needles("seed")
    positions = [n.position_pct for n in needles]
    # Three positions: head, middle, tail
    assert positions == sorted(positions)
    assert positions[0] < 0.2
    assert 0.4 < positions[1] < 0.6
    assert positions[2] > 0.8


def test_assemble_haystack_approximates_target_size():
    needles = make_needles("test")
    # Aim for 1000 tokens × 6 chars/token = ~6000 chars. Live measured 6.05
    # against gpt-4o-mini at three tiers, so tolerance ±15%.
    text = assemble_haystack(1000, needles, "test")
    assert 5100 <= len(text) <= 6900, f"got {len(text)} chars, expected ~6000"


def test_assemble_haystack_contains_all_needles():
    needles = make_needles("test")
    text = assemble_haystack(2000, needles, "test")
    for n in needles:
        assert n.answer in text, f"needle {n.answer} not embedded in haystack"
        assert n.sentence in text


def test_assemble_haystack_deterministic():
    needles = make_needles("test")
    a = assemble_haystack(500, needles, "test-seed")
    b = assemble_haystack(500, needles, "test-seed")
    assert a == b


def test_build_question_lists_all_needle_labels():
    needles = make_needles("test")
    q = build_question(needles)
    for n in needles:
        assert n.label in q
    assert "NOT FOUND" in q  # explicit miss instruction prevents fabrication


def test_evaluate_recalls_full_match():
    needles = make_needles("test")
    response = " ".join(n.answer for n in needles)
    assert evaluate_recalls(response, needles) == [True, True, True]


def test_evaluate_recalls_partial_match():
    needles = make_needles("test")
    # Model returns only first and third, says NOT FOUND for second.
    response = f"1) {needles[0].answer}\n2) NOT FOUND\n3) {needles[2].answer}"
    assert evaluate_recalls(response, needles) == [True, False, True]


def test_evaluate_recalls_case_insensitive():
    needles = make_needles("test")
    response = needles[0].answer.lower()  # model lower-cased
    recalls = evaluate_recalls(response, needles)
    assert recalls[0] is True
    assert recalls[1] is False
    assert recalls[2] is False


def test_evaluate_recalls_empty_response():
    needles = make_needles("test")
    assert evaluate_recalls("", needles) == [False, False, False]


def test_evaluate_recalls_no_collision_with_filler():
    """Sanity check that needle answers don't accidentally appear in filler."""
    needles = make_needles("test-1")
    # Build a haystack with DIFFERENT seed for needles vs filler
    other_needles = make_needles("test-2")
    haystack_only = assemble_haystack(500, [], "test-1:haystack-only")
    # The "test-1" needles should NOT appear in a haystack built from a
    # different seed and no needles inserted.
    assert evaluate_recalls(haystack_only, needles) == [False, False, False]
    _ = other_needles  # silence unused


# ---------- Cost estimation ----------


def test_estimate_cost_known_model():
    cost = estimate_cost_usd(100_000, "gpt-4o-mini")
    # 100k tokens × $0.15 / 1M = $0.015
    assert 0.014 <= cost <= 0.016


def test_estimate_cost_unknown_model_falls_back():
    cost = estimate_cost_usd(100_000, "totally-fake-model")
    assert cost > 0  # uses default rate, doesn't crash


def test_estimate_cost_handles_snapshot_suffix():
    # gpt-4o-mini-2024-07-18 should match gpt-4o-mini's rate
    cost = estimate_cost_usd(100_000, "gpt-4o-mini-2024-07-18")
    assert 0.014 <= cost <= 0.016


# ---------- Model context limit ----------


def test_model_context_limit_known_models():
    """Values verified against official docs 2026-05-05 — see
    _MODEL_CONTEXT_LIMITS docstring for sources. Update both the table and
    this test together so drift surfaces in CI rather than as silent
    coverage loss."""
    # OpenAI
    assert model_context_limit("gpt-4o-mini") == 128_000
    assert model_context_limit("gpt-4o") == 128_000
    assert model_context_limit("gpt-4.1") == 1_047_576
    assert model_context_limit("gpt-5") == 272_000
    assert model_context_limit("o3-mini") == 200_000  # was 128k pre-2025-04
    assert model_context_limit("o1-mini") == 128_000
    # Anthropic — 1M is GA on Opus/Sonnet 4.6+
    assert model_context_limit("claude-haiku-4-5") == 200_000
    assert model_context_limit("claude-sonnet-4-6") == 1_000_000
    assert model_context_limit("claude-opus-4-6") == 1_000_000
    assert model_context_limit("claude-opus-4-7") == 1_000_000
    assert model_context_limit("claude-opus-4-8") == 1_000_000
    assert model_context_limit("claude-opus-4-5") == 200_000
    # Gemini — all 1,048,576 (1MB binary) per ai.google.dev model pages
    assert model_context_limit("gemini-2.5-pro") == 1_048_576
    assert model_context_limit("gemini-2.5-flash") == 1_048_576
    assert model_context_limit("gemini-3.1-pro") == 1_048_576


def test_model_context_limit_snapshot_suffix():
    # Snapshot IDs like gpt-4o-mini-2024-07-18 should resolve via prefix.
    assert model_context_limit("gpt-4o-mini-2024-07-18") == 128_000
    assert model_context_limit("claude-haiku-4-5-20251001") == 200_000
    assert model_context_limit("claude-sonnet-4-6-20251101") == 1_000_000
    assert model_context_limit("claude-opus-4-8-20260609") == 1_000_000


def test_model_context_limit_unknown_falls_back_conservatively():
    # Unknown model defaults to 128k — better to skip a probe tier than
    # send 200k to a 16k model and flag the natural error as truncation.
    assert model_context_limit("unknown-model-xyz") == 128_000
    assert model_context_limit("") == 128_000


# ---------- Adaptive tiers (extreme strategy) ----------


def test_tiers_for_model_1m():
    """1M model gets ~(32k, 500k, 950k) — covers all three of the
    transport / mid-tier / high-end fraud surfaces. Without adaptive
    tiers we'd be blind to the 200k–1M range on Sonnet 4.6 / GPT-4.1
    / Gemini 2.5 Pro / Opus 4.7 etc."""
    from relay_detector.core.long_context import tiers_for_model
    tiers = tiers_for_model(1_000_000)
    assert len(tiers) == 3
    assert tiers[0] == 32_000  # capped at 32k floor for cheap transport check
    assert 450_000 <= tiers[1] <= 550_000
    assert 900_000 <= tiers[2] <= 1_000_000
    # Strictly ascending
    assert tiers[0] < tiers[1] < tiers[2]


def test_tiers_for_model_200k():
    """200k model gets ~(32k, 100k, 190k) — same effective coverage as
    the old hardcoded standard tiers."""
    from relay_detector.core.long_context import tiers_for_model
    tiers = tiers_for_model(200_000)
    assert len(tiers) == 3
    assert tiers[0] == 32_000
    assert 90_000 <= tiers[1] <= 110_000
    assert 180_000 <= tiers[2] <= 200_000


def test_tiers_for_model_128k():
    from relay_detector.core.long_context import tiers_for_model
    tiers = tiers_for_model(128_000)
    assert tiers[0] == 32_000
    assert tiers[-1] <= 128_000


def test_tiers_for_model_tiny_16k():
    """gpt-3.5-turbo class — 32k floor doesn't apply, scales down."""
    from relay_detector.core.long_context import tiers_for_model
    tiers = tiers_for_model(16_000)
    assert tiers[0] < 32_000  # floor disabled for small models
    assert tiers[-1] <= 16_000
    assert all(tiers[i] < tiers[i+1] for i in range(len(tiers)-1))


def test_tiers_for_model_dedupes_close_tiers():
    """For ~64k models the proportional tiers (32k, 32k, 60.8k) would
    have duplicates — the 1.5x spread filter drops them."""
    from relay_detector.core.long_context import tiers_for_model
    tiers = tiers_for_model(64_000)
    # Should have at most 2 distinct tiers (no duplicates within 1.5x)
    assert all(tiers[i] * 1.5 <= tiers[i+1] for i in range(len(tiers)-1))


# ---------- Detector behaviour ----------


class _MockClient:
    """Stub that records each chat_completions_create call and returns
    a configurable response. Used to drive the detector through pass/
    partial/fail paths without burning API credits."""

    def __init__(self, base_url: str = "https://mock.example.com"):
        self.base_url = base_url
        self.calls: list[dict] = []
        self.responses: list[dict] = []

    async def chat_completions_create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise RuntimeError("no canned response for call #" + str(len(self.calls)))
        resp = self.responses.pop(0)
        return ({}, resp, {}, 0)


def _build_resp(text: str, prompt_tokens: int = 1000) -> dict:
    return {
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 50,
            "total_tokens": prompt_tokens + 50,
        },
    }


@pytest.fixture(autouse=True)
def _mock_long_context_sleep(monkeypatch):
    """Replace the 75s TPM-reset sleep in the detector with a no-op so
    tests that exercise rate_limited paths don't actually wait minutes.
    Individual tests that need to assert the sleep duration override
    this with their own monkeypatch on the same target."""
    from relay_detector.protocols.openai.detectors import long_context as _lc_oa

    async def _no_sleep(_s):
        pass

    monkeypatch.setattr(_lc_oa.asyncio, "sleep", _no_sleep)


@pytest.mark.asyncio
async def test_long_context_skips_when_not_opted_in():
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(Mode.FULL, include_long_context=False)
    client = _MockClient()
    result = await det.run(client, "gpt-4o-mini")
    assert result.status == "skip"
    assert "可选" in result.details["skip_reason"]
    assert client.calls == []  # didn't burn any tokens


@pytest.mark.asyncio
async def test_long_context_passes_when_all_needles_recalled():
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(Mode.FULL, include_long_context=True)
    client = _MockClient()

    # We need to set up the right answers for each tier's needles.
    # The detector seeds per-tier with f"{seed}:{target_tokens}", and the
    # base seed includes time.time() so we don't know it ahead of time.
    # Workaround: monkey-patch make_needles to return predictable answers
    # we can echo back. But that's messy. Easier: use a callable response
    # builder that inspects the request and returns matching answers.

    # Simpler approach: drive the detector through real make_needles output
    # by overriding the mock's behaviour to extract needles from the prompt.
    async def smart_response(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        # Extract all unique IDs from the prompt — those are the answers.
        ids = ANSWER_RE.findall(prompt.upper())
        # Return them as the response
        return ({}, _build_resp("\n".join(ids[:3])), {}, 0)

    client.chat_completions_create = smart_response
    # gpt-4.1-mini has 1M context, so all three tiers probe (no skip).
    result = await det.run(client, "gpt-4.1-mini")
    assert result.status == "pass"
    assert result.score == 100.0
    assert len(result.details["tiers_tested"]) == 3
    for tier in result.details["tiers_tested"]:
        assert tier["status"] == "pass"
        assert tier["needles_found"] == 3


@pytest.mark.asyncio
async def test_long_context_fails_at_first_tier_when_truncated():
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(Mode.FULL, include_long_context=True)
    client = _MockClient()

    # Simulate severe truncation — model says "NOT FOUND" for all.
    async def truncated_response(**kwargs):
        return ({}, _build_resp("NOT FOUND\nNOT FOUND\nNOT FOUND"), {}, 0)

    client.chat_completions_create = truncated_response
    result = await det.run(client, "gpt-4o-mini")
    assert result.status == "fail"
    # Only the first tier should have been probed (stop-on-first-failure)
    assert len(result.details["tiers_tested"]) == 1
    assert result.details["highest_tier_reached"] == 32_000
    assert result.details["truncation_inferred_at_tokens"] is not None
    # Score: first tier 0pct, rest untested → 0/3 = 0
    assert result.score == 0.0


@pytest.mark.asyncio
async def test_long_context_request_error_treated_as_truncation():
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(Mode.FULL, include_long_context=True)
    client = _MockClient()

    async def boom(**kwargs):
        raise RuntimeError("413 Payload Too Large")

    client.chat_completions_create = boom
    result = await det.run(client, "gpt-4o-mini")
    assert result.status == "fail"
    assert "413" in result.details["tiers_tested"][0]["error"]
    # Failed tier still counts — just costs $0 since request errored
    assert result.details["tiers_tested"][0]["estimated_cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_long_context_partial_recall_passes_with_warning():
    """Live data 2026-05-05 against api.anthropic.com directly: claude-haiku-4-5
    at 200k tier hit ATLAS (50% middle position) miss while ALPHA (10%) and
    BRAVO (90%) found — classic lost-in-the-middle. Hard-failing on partial
    would reject the ground-truth API. Policy: partial at any tier =
    pass-with-warning (score still reflects 2/3 = 66% of that tier).
    Loop still stops on partial to avoid wasting probes on the same wobble."""
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(Mode.FULL, include_long_context=True)
    client = _MockClient()

    call_count = {"n": 0}

    async def degrading_response(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        ids = ANSWER_RE.findall(prompt.upper())
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First tier (32k): all three
            return ({}, _build_resp("\n".join(ids)), {}, 0)
        else:
            # Second tier (100k): only first two
            return ({}, _build_resp("\n".join(ids[:2]) + "\nNOT FOUND"), {}, 0)

    client.chat_completions_create = degrading_response
    result = await det.run(client, "gpt-4o-mini")
    # Partial → pass overall (lost-in-the-middle is not truncation)
    assert result.status == "pass"
    tiers = result.details["tiers_tested"]
    assert tiers[0]["status"] == "pass"
    assert tiers[1]["status"] == "partial"
    # Loop still stops on partial — saves cost
    assert len(tiers) == 2
    # Truncation is NOT inferred (partial isn't truncation evidence)
    assert result.details["truncation_inferred_at_tokens"] is None
    # Summary surfaces the wobble for transparency
    assert "中段" in result.details["summary"] or "召回" in result.details["summary"]


@pytest.mark.asyncio
async def test_long_context_estimated_cost_reported():
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(Mode.FULL, include_long_context=True)
    client = _MockClient()

    async def smart_response(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        ids = ANSWER_RE.findall(prompt.upper())
        return ({}, _build_resp("\n".join(ids[:3])), {}, 0)

    client.chat_completions_create = smart_response
    # gpt-4.1-mini @ $0.40/M, all 3 tiers probed (1M context limit), 332k → ~$0.13
    result = await det.run(client, "gpt-4.1-mini")
    cost = result.details["estimated_cost_usd"]
    assert 0.12 <= cost <= 0.14


@pytest.mark.asyncio
async def test_long_context_skips_tier_above_model_limit():
    """gpt-4o-mini has 128k context — the 200k tier must be skipped, not failed.
    Otherwise a non-fraudulent OpenAI key would always show false-positive
    truncation, since the model itself rejects 200k input."""
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(Mode.FULL, include_long_context=True)
    client = _MockClient()

    async def smart_response(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        ids = ANSWER_RE.findall(prompt.upper())
        return ({}, _build_resp("\n".join(ids[:3])), {}, 0)

    client.chat_completions_create = smart_response
    result = await det.run(client, "gpt-4o-mini")  # 128k context

    # 32k and 100k probed → pass. 200k skipped (over 128k * 0.95 budget).
    tiers = result.details["tiers_tested"]
    assert len(tiers) == 3
    assert tiers[0]["status"] == "pass"
    assert tiers[1]["status"] == "pass"
    assert tiers[2]["status"] == "skip"
    assert "上限" in tiers[2]["skip_reason"]
    # Aggregate: status pass (only probed tiers count for verdict)
    assert result.status == "pass"
    assert result.score == 100.0
    # Summary mentions the skip
    assert "更高档因模型自身" in result.details["summary"]


@pytest.mark.asyncio
async def test_long_context_skip_overall_when_model_too_small():
    """gpt-3.5-turbo has 16k context — every probe tier is over the limit.
    Detector returns overall skip rather than misleading fail."""
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(Mode.FULL, include_long_context=True)
    client = _MockClient()

    # No client calls expected — every tier should skip without probing.
    result = await det.run(client, "gpt-3.5-turbo")
    assert result.status == "skip"
    assert client.calls == []  # spent zero
    tiers = result.details["tiers_tested"]
    assert all(t["status"] == "skip" for t in tiers)


@pytest.mark.asyncio
async def test_long_context_extreme_uses_adaptive_tiers_for_1m_model():
    """include_long_context_extreme=True on a 1M model probes proportionally
    (~32k, ~500k, ~950k) instead of hardcoded (32k, 100k, 200k). This is
    the WHOLE POINT of extreme mode — without it big models are tested at
    only 20% of advertised capacity, which lets a "1M-claimed but capped
    at 256k" relay slip through with a green badge."""
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(
        Mode.FULL,
        include_long_context=False,
        include_long_context_extreme=True,
    )
    client = _MockClient()

    async def smart_response(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        ids = ANSWER_RE.findall(prompt.upper())
        return ({}, _build_resp("\n".join(ids[:3])), {}, 0)

    client.chat_completions_create = smart_response
    result = await det.run(client, "gpt-4.1-mini")  # 1,047,576 context
    tiers = result.details["tiers_tested"]
    assert len(tiers) == 3
    # Tier 1 still 32k (transport floor); tier 3 near model limit
    assert tiers[0]["target_tokens"] == 32_000
    assert tiers[2]["target_tokens"] >= 900_000  # ~95% of 1M
    assert result.details["tier_strategy"] == "extreme"


@pytest.mark.asyncio
async def test_long_context_standard_tiers_unchanged():
    """Default standard mode (only include_long_context=True) keeps
    hardcoded (32k, 100k, 200k) tiers — backward compatibility for
    everyone who liked the old behavior."""
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(
        Mode.FULL,
        include_long_context=True,
        include_long_context_extreme=False,
    )
    client = _MockClient()

    async def smart_response(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        ids = ANSWER_RE.findall(prompt.upper())
        return ({}, _build_resp("\n".join(ids[:3])), {}, 0)

    client.chat_completions_create = smart_response
    result = await det.run(client, "gpt-4.1-mini")  # 1M model
    tiers = result.details["tiers_tested"]
    targets = [t["target_tokens"] for t in tiers]
    assert targets == [32_000, 100_000, 200_000]  # not adaptive
    assert result.details["tier_strategy"] == "standard"


@pytest.mark.asyncio
async def test_long_context_passes_timeout_to_client():
    """Big probes need explicit timeout > the 30s default — verify the
    detector plumbs request_timeout_s through to the client call. Without
    this kwarg, httpx 30s timeout would kill any 200k+ probe regardless
    of model speed."""
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(
        Mode.FULL, include_long_context_extreme=True,
    )
    client = _MockClient()
    captured: list[dict] = []

    async def capture(**kwargs):
        captured.append(kwargs)
        prompt = kwargs["messages"][0]["content"]
        ids = ANSWER_RE.findall(prompt.upper())
        return ({}, _build_resp("\n".join(ids[:3])), {}, 0)

    client.chat_completions_create = capture
    await det.run(client, "gpt-4.1-mini")  # 1M context, all 3 tiers probe

    assert len(captured) == 3
    for kwargs in captured:
        assert "request_timeout_s" in kwargs, "tier must pass timeout"
        assert kwargs["request_timeout_s"] >= 120.0, "timeout floor 120s"
    # Tier 3 (~950k) gets a much larger timeout than tier 1 (32k)
    assert captured[2]["request_timeout_s"] > captured[0]["request_timeout_s"]
    assert captured[2]["request_timeout_s"] >= 200.0  # ~240s for 950k


@pytest.mark.asyncio
async def test_long_context_429_treated_as_rate_limited_not_truncation():
    """OpenAI exposes long-context as a separate SKU
    (`gpt-4.1-mini-long-context`) with its own TPM cap. Hitting that cap
    raises HTTP 429 — which is RATE LIMITING, not truncation. Marking it
    fail would falsely flag a compliant relay. This is the bug we caught
    in live validation 2026-05-05 ($0.22 worth)."""
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(
        Mode.FULL, include_long_context_extreme=True,
    )
    client = _MockClient()

    call_count = {"n": 0}

    async def degrading(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # 32k probe passes
            prompt = kwargs["messages"][0]["content"]
            ids = ANSWER_RE.findall(prompt.upper())
            return ({}, _build_resp("\n".join(ids[:3])), {}, 0)
        # Subsequent tiers hit OpenAI's actual TPM error
        raise RuntimeError(
            "HTTP 429: Request too large for gpt-4.1-mini "
            "(for limit gpt-4.1-mini-long-context) on tokens per min "
            "(TPM): Limit 1000000"
        )

    client.chat_completions_create = degrading
    result = await det.run(client, "gpt-4.1-mini")  # 1M extreme tiers

    tiers = result.details["tiers_tested"]
    # First tier passes. Second tier (~524k) hits 429 and stops the loop.
    assert tiers[0]["status"] == "pass"
    assert tiers[1]["status"] == "rate_limited"
    assert "TPM" in tiers[1]["error"] or "429" in tiers[1]["error"]
    # NO third tier — loop broke on rate_limited
    assert len(tiers) == 2
    # Detector overall is PASS, not fail — 32k tier proved no truncation,
    # and the 429 doesn't count against the relay.
    assert result.status == "pass"
    # Score reflects only probed tiers (just the 32k pass)
    assert result.score == 100.0
    assert "TPM" in result.details["summary"]


@pytest.mark.asyncio
async def test_long_context_429_retried_after_tpm_window_reset(monkeypatch):
    """When a tier hits 429/TPM, detector waits ~75s for the sliding
    window to reset and retries once. This unblocks legit big probes
    (e.g. 995k on a 1M TPM tier where the prior 524k hasn't aged out
    yet). Mock asyncio.sleep so the test doesn't actually wait."""
    sleeps_recorded: list[float] = []

    async def fake_sleep(s):
        sleeps_recorded.append(s)

    # Patch asyncio.sleep on the module object that long_context imported,
    # so the detector's `asyncio.sleep(75)` call hits our fake (string-path
    # monkeypatch doesn't traverse nested module attrs reliably).
    from relay_detector.protocols.openai.detectors import long_context as _lc_oa
    monkeypatch.setattr(_lc_oa.asyncio, "sleep", fake_sleep)

    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(
        Mode.FULL, include_long_context=True,
    )
    client = _MockClient()

    # First call to a tier: 429. Subsequent calls (after retry): success.
    call_state = {"tier_first_call_seen": set()}

    async def smart(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        # Use prompt length as a proxy for which tier we're on
        size_bucket = len(prompt) // 100_000
        first_call = size_bucket not in call_state["tier_first_call_seen"]
        if first_call and size_bucket > 0:
            # Big tiers hit TPM on first attempt only
            call_state["tier_first_call_seen"].add(size_bucket)
            raise RuntimeError(
                "HTTP 429: tokens per min (TPM): Limit 1000000"
            )
        ids = ANSWER_RE.findall(prompt.upper())
        return ({}, _build_resp("\n".join(ids[:3])), {}, 0)

    client.chat_completions_create = smart
    result = await det.run(client, "gpt-4.1-mini")  # 1M model

    # All three tiers should ultimately succeed via retry
    tiers = result.details["tiers_tested"]
    assert all(t["status"] == "pass" for t in tiers if t["status"] != "skip")
    assert result.status == "pass"
    # Verify retry path was taken: at least one ~75s sleep recorded
    assert any(70 <= s <= 80 for s in sleeps_recorded), (
        f"expected ~75s TPM-reset sleep, got {sleeps_recorded}"
    )
    # Verify retry metadata recorded on the tiers that needed it
    retried_tiers = [t for t in tiers if t.get("tpm_retry_attempted")]
    assert len(retried_tiers) >= 1


@pytest.mark.asyncio
async def test_long_context_429_persists_after_retry_marks_rate_limited(monkeypatch):
    """If a tier is rate-limited on BOTH initial attempt AND retry, give up
    and mark rate_limited (don't loop forever). This is the case where the
    user's TPM tier is genuinely too low to fit the probe size."""
    async def fake_sleep(s):
        pass

    monkeypatch.setattr(
        "relay_detector.protocols.openai.detectors.long_context.asyncio.sleep",
        fake_sleep,
    )

    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(
        Mode.FULL, include_long_context=True,
    )
    client = _MockClient()

    async def always_429(**kwargs):
        raise RuntimeError("HTTP 429: tokens per min (TPM): Limit 100000")

    client.chat_completions_create = always_429
    result = await det.run(client, "gpt-4.1-mini")
    # All probed tiers rate-limited → overall skip
    assert result.status == "skip"
    tiers = result.details["tiers_tested"]
    # First tier exists, was retried, still rate-limited
    assert tiers[0]["status"] == "rate_limited"
    assert tiers[0].get("tpm_retry_attempted") is True


@pytest.mark.asyncio
async def test_long_context_all_tiers_rate_limited_returns_skip():
    """If every tier hits rate-limit (e.g. user's API key has TPM lower
    than even our smallest probe), the detector returns skip rather
    than a misleading pass or fail."""
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(
        Mode.FULL, include_long_context=True,
    )
    client = _MockClient()

    async def always_429(**kwargs):
        raise RuntimeError("HTTP 429: rate_limit_exceeded - please slow down")

    client.chat_completions_create = always_429
    result = await det.run(client, "gpt-4.1-mini")
    assert result.status == "skip"
    assert "rate limit" in result.details["summary"].lower()


def test_looks_rate_limited_catches_known_patterns():
    from relay_detector.protocols.openai.detectors.long_context import (
        _looks_rate_limited,
    )
    assert _looks_rate_limited("HTTP 429: Request too large")
    assert _looks_rate_limited("Error: rate_limit_exceeded")
    assert _looks_rate_limited("on tokens per min (TPM): Limit 1000000")
    assert _looks_rate_limited("requests per min exceeded")
    # NOT rate-limited:
    assert not _looks_rate_limited("HTTP 413: Payload Too Large")
    assert not _looks_rate_limited("ReadTimeout")
    assert not _looks_rate_limited("context_length_exceeded")
    assert not _looks_rate_limited("")


@pytest.mark.asyncio
async def test_long_context_extreme_implies_standard():
    """If both flags are set, extreme wins (it's a superset). If only
    extreme is set, the detector still runs (extreme acts as enabler)."""
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(
        Mode.FULL,
        include_long_context=False,
        include_long_context_extreme=True,
    )
    client = _MockClient()

    async def smart_response(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        ids = ANSWER_RE.findall(prompt.upper())
        return ({}, _build_resp("\n".join(ids[:3])), {}, 0)

    client.chat_completions_create = smart_response
    result = await det.run(client, "gpt-4o-mini")
    # Did NOT skip even though include_long_context=False — extreme alone
    # is enough to enable the detector.
    assert result.status != "skip"
    assert result.details["tier_strategy"] == "extreme"
