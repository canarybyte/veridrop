"""Anthropic long-context detector tests — mirrors OpenAI variant.

Both detectors share the same core/long_context.py primitives; tests here
focus on the protocol-specific surface (messages_create vs
chat_completions_create, content[].text vs choices[0].message.content,
input_tokens vs prompt_tokens) without re-testing the core helpers."""

from __future__ import annotations

import pytest

from relay_detector.core.long_context import ANSWER_RE
from relay_detector.core.models import ExecutionConfig, Mode
from relay_detector.protocols.anthropic.detectors import long_context as anthropic_lc
from relay_detector.protocols.anthropic.detectors.long_context import (
    LongContextDetector,
)


class _MockClient:
    def __init__(self, base_url: str = "https://mock.anthropic.example"):
        self.base_url = base_url
        self.calls: list[dict] = []
        self.messages_create = None  # set in tests


def _build_resp(text: str, input_tokens: int = 1000) -> dict:
    """Anthropic Messages API response shape: content blocks + usage."""
    return {
        "id": "msg_mock",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": 50,
        },
    }


@pytest.mark.asyncio
async def test_anthropic_long_context_skips_when_not_opted_in():
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(Mode.FULL, include_long_context=False)
    client = _MockClient()
    client.messages_create = lambda **k: (_ for _ in ()).throw(
        AssertionError("should not call API when not opted in")
    )
    result = await det.run(client, "claude-haiku-4-5")
    assert result.status == "skip"
    assert "可选" in result.details["skip_reason"]


@pytest.mark.asyncio
async def test_anthropic_long_context_passes_when_all_needles_recalled():
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(Mode.FULL, include_long_context=True)
    client = _MockClient()

    async def smart_response(**kwargs):
        # Extract canonical answers from the embedded prompt and echo them back
        prompt = kwargs["messages"][0]["content"]
        ids = ANSWER_RE.findall(prompt.upper())
        return ({}, _build_resp("\n".join(ids[:3])), {}, 0)

    client.messages_create = smart_response
    # claude-haiku-4-5 has 200k context — all three tiers probe.
    result = await det.run(client, "claude-haiku-4-5")
    assert result.status == "pass"
    assert result.score == 100.0
    tiers = result.details["tiers_tested"]
    assert len(tiers) == 3
    for t in tiers:
        assert t["status"] == "pass"
        assert t["needles_found"] == 3
    assert result.details["model_context_limit"] == 200_000


@pytest.mark.asyncio
async def test_anthropic_long_context_fails_at_first_tier_when_truncated():
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(Mode.FULL, include_long_context=True)
    client = _MockClient()

    async def truncated_response(**kwargs):
        # Severe truncation: model can't see any needles
        return ({}, _build_resp("NOT FOUND\nNOT FOUND\nNOT FOUND"), {}, 0)

    client.messages_create = truncated_response
    result = await det.run(client, "claude-haiku-4-5")
    assert result.status == "fail"
    # Stop on first failure — only 32k tier probed
    assert len(result.details["tiers_tested"]) == 1
    assert result.details["tiers_tested"][0]["target_tokens"] == 32_000
    assert result.details["truncation_inferred_at_tokens"] is not None


@pytest.mark.asyncio
async def test_anthropic_long_context_request_error_treated_as_truncation():
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(Mode.FULL, include_long_context=True)
    client = _MockClient()

    async def too_large(**kwargs):
        raise RuntimeError("413 Payload Too Large")

    client.messages_create = too_large
    result = await det.run(client, "claude-haiku-4-5")
    assert result.status == "fail"
    assert "413" in result.details["tiers_tested"][0]["error"]
    assert result.details["tiers_tested"][0]["estimated_cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_anthropic_long_context_passes_haiku_with_200k_clamp():
    """200k tier on a 200k-context model must be probed (not skipped) by
    clamping the haystack to leave room for the question. Catches the bug
    where naive `target > limit` skips the highest tier on every 200k
    Anthropic model (Haiku 4.5, Opus 4.5, Sonnet 4.5, etc.) and silently
    lowers Veridrop's coverage."""
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(Mode.FULL, include_long_context=True)
    client = _MockClient()

    async def smart_response(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        ids = ANSWER_RE.findall(prompt.upper())
        return ({}, _build_resp("\n".join(ids[:3])), {}, 0)

    client.messages_create = smart_response
    result = await det.run(client, "claude-haiku-4-5")  # 200k context
    tiers = result.details["tiers_tested"]
    # No skip — all three tiers actually probed even though 200k tier ==
    # model limit.
    assert all(t["status"] == "pass" for t in tiers)
    assert tiers[2]["target_tokens"] == 200_000


@pytest.mark.asyncio
async def test_anthropic_long_context_uses_correct_api_shape():
    """Sanity check: detector calls messages_create (not chat_completions),
    sends max_tokens (not max_completion_tokens), and extracts content[].text."""
    det = LongContextDetector()
    det.config = ExecutionConfig.for_mode(Mode.FULL, include_long_context=True)
    client = _MockClient()

    captured_kwargs: list[dict] = []

    async def capture(**kwargs):
        captured_kwargs.append(kwargs)
        prompt = kwargs["messages"][0]["content"]
        ids = ANSWER_RE.findall(prompt.upper())
        return ({}, _build_resp("\n".join(ids[:3])), {}, 0)

    client.messages_create = capture
    await det.run(client, "claude-haiku-4-5")

    assert len(captured_kwargs) >= 1
    first = captured_kwargs[0]
    assert "max_tokens" in first
    assert "max_completion_tokens" not in first
    assert first["temperature"] == 0
    assert first["model"] == "claude-haiku-4-5"
    assert first["messages"][0]["role"] == "user"


def test_anthropic_1m_near_limit_initial_target_is_conservative():
    target = anthropic_lc._initial_haystack_target(950_000, 1_000_000)
    assert 580_000 <= target <= 590_000
    assert anthropic_lc._initial_haystack_target(500_000, 1_000_000) == 498_500


@pytest.mark.asyncio
async def test_anthropic_long_context_hits_real_950k_target_in_one_count(
    monkeypatch,
):
    det = LongContextDetector()
    client = _MockClient()
    assemble_targets: list[int] = []

    def fake_assemble(target_tokens, needles, seed, protocol=None):
        assemble_targets.append(target_tokens)
        return "\n".join(n.sentence for n in needles)

    async def count_tokens(**kwargs):
        return ({}, {"input_tokens": 946_135}, {}, 0)

    async def messages_create(**kwargs):
        client.calls.append(kwargs)
        prompt = kwargs["messages"][0]["content"]
        ids = ANSWER_RE.findall(prompt.upper())
        return ({}, _build_resp("\n".join(ids[:3]), input_tokens=946_135), {}, 0)

    monkeypatch.setattr(anthropic_lc, "assemble_haystack", fake_assemble)
    client.count_tokens = count_tokens
    client.messages_create = messages_create

    result = await det._probe_tier(
        client, "claude-opus-4-7", 950_000, "seed", 1_000_000
    )

    assert result["status"] == "pass"
    assert result["input_tokens_precounted"] == 946_135
    assert result["count_tokens_attempts"] == 1
    assert result["sizing_iterations"] == 0
    assert len(client.calls) == 1
    assert len(assemble_targets) == 1


@pytest.mark.asyncio
async def test_anthropic_long_context_grows_to_real_950k_target(monkeypatch):
    det = LongContextDetector()
    client = _MockClient()
    assemble_targets: list[int] = []
    counted = [700_000, 949_000]
    count_calls = 0

    def fake_assemble(target_tokens, needles, seed, protocol=None):
        assemble_targets.append(target_tokens)
        return "\n".join(n.sentence for n in needles)

    async def count_tokens(**kwargs):
        nonlocal count_calls
        count_calls += 1
        return ({}, {"input_tokens": counted[min(count_calls - 1, 1)]}, {}, 0)

    async def messages_create(**kwargs):
        client.calls.append(kwargs)
        prompt = kwargs["messages"][0]["content"]
        ids = ANSWER_RE.findall(prompt.upper())
        return ({}, _build_resp("\n".join(ids[:3]), input_tokens=949_000), {}, 0)

    monkeypatch.setattr(anthropic_lc, "assemble_haystack", fake_assemble)
    client.count_tokens = count_tokens
    client.messages_create = messages_create

    result = await det._probe_tier(
        client, "claude-opus-4-7", 950_000, "seed", 1_000_000
    )

    assert result["status"] == "pass"
    assert result["input_tokens_precounted"] == 949_000
    assert result["count_tokens_attempts"] == 2
    assert result["sizing_iterations"] == 1
    assert len(client.calls) == 1
    assert len(assemble_targets) == 2
    assert 580_000 <= assemble_targets[0] <= 590_000
    assert assemble_targets[1] > assemble_targets[0]


@pytest.mark.asyncio
async def test_anthropic_long_context_recounts_after_oversized_precount(monkeypatch):
    det = LongContextDetector()
    client = _MockClient()
    assemble_targets: list[int] = []
    counted = [1_559_737, 949_000]
    count_calls = 0

    def fake_assemble(target_tokens, needles, seed, protocol=None):
        assemble_targets.append(target_tokens)
        return "\n".join(n.sentence for n in needles)

    async def count_tokens(**kwargs):
        nonlocal count_calls
        count_calls += 1
        return ({}, {"input_tokens": counted[min(count_calls - 1, 1)]}, {}, 0)

    async def messages_create(**kwargs):
        client.calls.append(kwargs)
        prompt = kwargs["messages"][0]["content"]
        ids = ANSWER_RE.findall(prompt.upper())
        return ({}, _build_resp("\n".join(ids[:3]), input_tokens=949_000), {}, 0)

    monkeypatch.setattr(anthropic_lc, "assemble_haystack", fake_assemble)
    client.count_tokens = count_tokens
    client.messages_create = messages_create

    result = await det._probe_tier(
        client, "claude-opus-4-7", 950_000, "seed", 1_000_000
    )

    assert result["status"] == "pass"
    assert result["input_tokens_precounted"] == 949_000
    assert result["count_tokens_attempts"] == 2
    assert result["sizing_iterations"] == 1
    assert len(client.calls) == 1
    assert len(assemble_targets) == 2
    assert assemble_targets[1] < assemble_targets[0]


@pytest.mark.asyncio
async def test_anthropic_long_context_skips_near_limit_without_count_tokens(
    monkeypatch,
):
    det = LongContextDetector()
    client = _MockClient()

    def fake_assemble(target_tokens, needles, seed, protocol=None):
        return "\n".join(n.sentence for n in needles)

    async def count_tokens(**kwargs):
        raise RuntimeError("count_tokens unavailable")

    async def messages_create(**kwargs):
        raise AssertionError("near-limit prompt must not be sent without count")

    monkeypatch.setattr(anthropic_lc, "assemble_haystack", fake_assemble)
    client.count_tokens = count_tokens
    client.messages_create = messages_create

    result = await det._probe_tier(
        client, "claude-opus-4-7", 950_000, "seed", 1_000_000
    )

    assert result["status"] == "skip"
    assert "count_tokens" in result["skip_reason"]
    assert result["count_tokens_attempts"] == 1


@pytest.mark.asyncio
async def test_anthropic_long_context_provider_prompt_overflow_is_skip(
    monkeypatch,
):
    det = LongContextDetector()
    client = _MockClient()

    class PromptOverflow(Exception):
        status = 400
        body = "prompt is too long: 1559737 tokens > 1000000 maximum"

    def fake_assemble(target_tokens, needles, seed, protocol=None):
        return "\n".join(n.sentence for n in needles)

    async def count_tokens(**kwargs):
        return ({}, {"input_tokens": 949_000}, {}, 0)

    async def messages_create(**kwargs):
        raise PromptOverflow()

    monkeypatch.setattr(anthropic_lc, "assemble_haystack", fake_assemble)
    client.count_tokens = count_tokens
    client.messages_create = messages_create

    result = await det._probe_tier(
        client, "claude-opus-4-7", 950_000, "seed", 1_000_000
    )

    assert result["status"] == "skip"
    assert "prompt overflow" in result["skip_reason"]
    assert "1559737 tokens > 1000000 maximum" in result["error"]
