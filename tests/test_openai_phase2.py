"""Regression tests for Phase 2 OpenAI Chat Completions detection."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from relay_detector.core.detectors_base import ActiveDetector, PassiveDetector
from relay_detector.core.models import ExecutionConfig, Mode
from relay_detector.protocols.openai import (
    build_detectors,
    default_model,
    model_choices,
    tier_banner,
)
from relay_detector.protocols.openai.client import (
    OpenAIChatClient,
    normalize_openai_base_url,
)
from relay_detector.protocols.openai.detectors.basic_request import BasicRequestDetector
from relay_detector.protocols.openai.detectors.integrity import (
    _usage_close as _openai_stream_usage_close,
)
from relay_detector.protocols.openai.detectors.protocol import ProtocolDetector
from relay_detector.protocols.openai.detectors.token_billing import (
    _compare_reference,
    _stream_usage_compatible,
)
from relay_detector.protocols.openai.runner import Runner


def _chat_payload(
    *,
    model: str = "gpt-4o-mini",
    content: str = "pong",
    finish_reason: str = "stop",
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["content"] = None
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1741569952,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage or {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
        },
        "system_fingerprint": "fp_test",
    }


class FakeOpenAIBaseClient:
    async def chat_completions_create(self, **body: Any):
        model = body["model"]
        prompt = str(body.get("messages"))
        usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
        if "Reference text:" in prompt:
            usage = {"prompt_tokens": 90, "completion_tokens": 2, "total_tokens": 92}
        if body.get("tools"):
            response = _chat_payload(
                model=model,
                finish_reason="tool_calls",
                usage=usage,
                tool_calls=[
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "get_current_weather",
                            "arguments": '{"city":"Boston, MA","unit":"celsius"}',
                        },
                    }
                ],
            )
        elif body.get("response_format"):
            response = _chat_payload(
                model=model,
                content='{"ok":true,"nonce":"openai-detector"}',
                usage=usage,
            )
        elif "stream check" in prompt:
            response = _chat_payload(model=model, content="veridrop stream check", usage=usage)
        elif "HTTP status 418" in prompt:
            response = _chat_payload(
                model=model,
                content="HTTP 418 is an April Fools status code.",
                usage=usage,
            )
        else:
            response = _chat_payload(model=model, content="pong", usage=usage)
        return body, response, httpx.Headers({"x-request-id": "req_test"}), 12

    async def chat_completions_stream(self, **body: Any):
        _ = body
        yield {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1741569952,
            "model": body["model"],
            "choices": [{"index": 0, "delta": {"content": "veridrop "}}],
        }, 5
        yield {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1741569952,
            "model": body["model"],
            "choices": [{"index": 0, "delta": {"content": "stream check"}}],
        }, 7
        yield {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1741569952,
            "model": body["model"],
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
            },
        }, 9


class ReasoningOnlyBasicClient:
    async def chat_completions_create(self, **body: Any):
        return (
            body,
            _chat_payload(
                model=body["model"],
                content="",
                finish_reason="length",
                usage={
                    "prompt_tokens": 11,
                    "completion_tokens": 16,
                    "total_tokens": 27,
                    "completion_tokens_details": {"reasoning_tokens": 16},
                },
            ),
            httpx.Headers(),
            12,
        )


def test_openai_base_url_normalization_handles_v1_suffix():
    assert normalize_openai_base_url("https://api.example.com") == (
        "https://api.example.com/v1"
    )
    assert normalize_openai_base_url("https://api.example.com/v1/") == (
        "https://api.example.com/v1"
    )


@pytest.mark.asyncio
async def test_openai_client_strips_temperature_for_default_only_models():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        captured["body"] = _json.loads(request.content)
        return httpx.Response(200, json=_chat_payload(model="gpt-5.5-2026-04-23"))

    transport = httpx.MockTransport(handler)
    client = OpenAIChatClient("https://api.openai.com", "sk-test")
    client._client = httpx.AsyncClient(  # type: ignore[attr-defined]
        base_url=client.base_url,
        headers={"authorization": "Bearer sk-test", "content-type": "application/json"},
        transport=transport,
    )
    try:
        await client.chat_completions_create(
            model="gpt-5.5-2026-04-23",
            temperature=0,
            messages=[{"role": "user", "content": "hi"}],
        )
    finally:
        await client.aclose()

    assert captured["body"]["model"] == "gpt-5.5-2026-04-23"
    assert "temperature" not in captured["body"]


@pytest.mark.parametrize(
    "model,expect_stripped",
    [
        # Reasoning-tier models that reject temperature (HTTP 400 from OpenAI)
        ("gpt-5.5", True),
        ("gpt-5.5-pro", True),
        ("gpt-5.5-2026-04-23", True),
        ("gpt-5.4", True),
        ("gpt-5.4-pro", True),
        # Sub-variants of reasoning families ARE distinct (non-reasoning)
        # models that accept temperature — must NOT be stripped
        ("gpt-5.4-mini", False),
        ("gpt-5.4-nano", False),
        # Other GPT-5 lines accept temperature (5.1 with reasoning_effort=none)
        ("gpt-5.1", False),
        ("gpt-5.1-mini", False),
        # Legacy / non-reasoning families — never stripped
        ("gpt-4o", False),
        ("gpt-4o-mini", False),
        # Dot/hyphen/underscore canonicalization: same family bucket
        ("gpt-5-4", True),
        ("gpt-5_4", True),
        ("gpt-5-4-mini", False),
    ],
)
@pytest.mark.asyncio
async def test_openai_client_temperature_strip_per_model(model: str, expect_stripped: bool):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        captured["body"] = _json.loads(request.content)
        return httpx.Response(200, json=_chat_payload(model=model))

    transport = httpx.MockTransport(handler)
    client = OpenAIChatClient("https://api.openai.com", "sk-test")
    client._client = httpx.AsyncClient(  # type: ignore[attr-defined]
        base_url=client.base_url,
        headers={"authorization": "Bearer sk-test", "content-type": "application/json"},
        transport=transport,
    )
    try:
        await client.chat_completions_create(
            model=model,
            temperature=0,
            messages=[{"role": "user", "content": "hi"}],
        )
    finally:
        await client.aclose()

    if expect_stripped:
        assert "temperature" not in captured["body"], (
            f"{model} is a reasoning-tier model that rejects temperature — "
            "client must strip it before sending"
        )
    else:
        assert captured["body"].get("temperature") == 0, (
            f"{model} accepts temperature — client must NOT strip it "
            "(stripping would lose detector determinism)"
        )


def test_openai_detectors_use_core_base_classes():
    detectors = build_detectors()
    assert any(isinstance(detector, ActiveDetector) for detector in detectors)
    assert any(isinstance(detector, PassiveDetector) for detector in detectors)


def test_openai_tier_banner_disclaims_authenticity():
    title, message = tier_banner()
    assert title == "行为/协议级验证"
    assert "无法可靠区分" in message


def test_openai_default_model_choices_are_gpt5_series():
    assert model_choices() == [
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.3-codex",
        "gpt-5.4-nano",
        "gpt-5.4-mini",
    ]
    assert default_model() == "gpt-5.5"


def test_openai_protocol_detector_accepts_chat_completion_shape():
    detector = ProtocolDetector()
    request = {"model": "gpt-4o-mini"}
    response = _chat_payload(model="gpt-4o-mini")
    detector.observe(request, response, httpx.Headers({"x-request-id": "req"}), 10)
    result = detector.finalize()
    assert result.status == "pass"
    assert result.score == 100.0


@pytest.mark.asyncio
async def test_openai_standard_runner_completes_with_fake_client():
    cfg = ExecutionConfig.for_mode(Mode.STANDARD, max_concurrent=3)
    runner = Runner(FakeOpenAIBaseClient(), build_detectors(), cfg)
    outcome = await runner.run("gpt-4o-mini")
    by_name = {result.name: result for result in outcome.results}
    assert by_name["basic_request"].status == "pass"
    assert by_name["function_calling"].status == "pass"
    assert by_name["structured_output"].status == "pass"
    assert by_name["protocol"].status == "pass"
    assert by_name["token_billing"].status == "pass"
    assert "token_parity" not in by_name
    assert outcome.performance.request_count >= 6


@pytest.mark.asyncio
async def test_openai_basic_request_tolerates_reasoning_budget_exhaustion():
    result = await BasicRequestDetector().run(ReasoningOnlyBasicClient(), "gpt-5.5")
    assert result.status == "pass"
    assert result.score == 75.0
    assert result.details["reasoning_budget_exhausted"] is True


def test_openai_token_billing_accepts_lower_stream_completion_tokens():
    non_stream = {"prompt_tokens": 11, "completion_tokens": 8, "total_tokens": 19}
    stream = {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}
    assert _stream_usage_compatible(non_stream, stream) is True

    reference = {
        "collected_at": "2026-05-04T00:00:00+00:00",
        "ranges": {
            "short_prompt_tokens": {"min": 11, "max": 11},
            "short_completion_tokens": {"min": 8, "max": 8},
            "long_prompt_tokens": {"min": 95, "max": 95},
            "prompt_delta": {"min": 84, "max": 84},
            "stream_prompt_tokens": {"min": 11, "max": 11},
            "stream_completion_tokens": {"min": 8, "max": 8},
        },
    }
    result = _compare_reference(
        non_stream,
        {"prompt_tokens": 95, "completion_tokens": 8, "total_tokens": 103},
        stream,
        reference,
    )
    assert result["pass"] is True
    assert result["checks"]["stream_completion_tokens"]["pass"] is True
    assert result["checks"]["stream_completion_tokens"]["direction"] == "low"


def test_openai_integrity_accepts_lower_stream_completion_tokens():
    non_stream = {"prompt_tokens": 15, "completion_tokens": 19, "total_tokens": 34}
    stream = {"prompt_tokens": 15, "completion_tokens": 8, "total_tokens": 23}
    assert _openai_stream_usage_close(non_stream, stream) is True
