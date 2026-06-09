"""M1 smoke tests for the HTTP layer."""

from __future__ import annotations

import httpx
import pytest
import respx

from relay_detector.client import (
    AnthropicAPIError,
    AnthropicClient,
    ThrottledClient,
    _normalize_base_url,
    _parse_sse,
)


BASE_URL = "https://api.example.com"


def test_normalize_base_url_accepts_trailing_v1():
    assert _normalize_base_url("https://api.example.com/v1") == "https://api.example.com"
    assert _normalize_base_url("https://api.example.com/v1/") == "https://api.example.com"
    assert _normalize_base_url("https://api.example.com/api/v1") == "https://api.example.com/api"
    assert _normalize_base_url("https://api.example.com") == "https://api.example.com"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude-opus-4-7", "claude-opus-4-8"])
async def test_messages_create_strips_temperature_for_new_opus(model):
    """New Opus models reject `temperature`; client must strip it."""
    sample = {
        "id": "msg_x", "type": "message", "role": "assistant",
        "model": model, "content": [],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        captured.append(_json.loads(request.content))
        return httpx.Response(200, json=sample)

    async with respx.mock(base_url=BASE_URL) as router:
        router.post("/v1/messages").mock(side_effect=handler)
        async with AnthropicClient(BASE_URL, "sk-test") as client:
            await client.messages_create(
                model=model,
                max_tokens=10,
                temperature=0,
                messages=[{"role": "user", "content": "x"}],
            )
    assert len(captured) == 1
    sent = captured[0]
    assert "temperature" not in sent, "temperature must be stripped for new Opus"
    assert sent["model"] == model
    assert sent["max_tokens"] == 10  # other fields untouched


@pytest.mark.asyncio
async def test_sanitize_body_tolerates_dotted_opus_4_7():
    """Users frequently type `claude-opus-4.7` (dotted) in the web form.
    The deprecation strip must still kick in — otherwise temperature leaks
    through and Anthropic returns HTTP 400 for every active detector.
    Real diagnosis: api.b.ai opus 4.7 report wsvN91HA on 2026-05-06.
    """
    sample = {
        "id": "msg_x", "type": "message", "role": "assistant",
        "model": "claude-opus-4-7", "content": [],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        captured.append(_json.loads(request.content))
        return httpx.Response(200, json=sample)

    async with respx.mock(base_url=BASE_URL) as router:
        router.post("/v1/messages").mock(side_effect=handler)
        async with AnthropicClient(BASE_URL, "sk-test") as client:
            await client.messages_create(
                model="claude-opus-4.7",  # dotted form
                max_tokens=10,
                temperature=0,
                messages=[{"role": "user", "content": "x"}],
            )
    sent = captured[0]
    assert "temperature" not in sent, (
        "dotted form must also strip temperature — otherwise every active "
        "detector 400s on Opus 4.7"
    )


@pytest.mark.asyncio
async def test_messages_create_keeps_temperature_for_haiku():
    """Models without a deprecation rule keep their temperature."""
    sample = {
        "id": "msg_x", "type": "message", "role": "assistant",
        "model": "claude-haiku-4-5", "content": [],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        captured.append(_json.loads(request.content))
        return httpx.Response(200, json=sample)

    async with respx.mock(base_url=BASE_URL) as router:
        router.post("/v1/messages").mock(side_effect=handler)
        async with AnthropicClient(BASE_URL, "sk-test") as client:
            await client.messages_create(
                model="claude-haiku-4-5",
                max_tokens=10,
                temperature=0.5,
                messages=[{"role": "user", "content": "x"}],
            )
    assert captured[0]["temperature"] == 0.5


@pytest.mark.asyncio
async def test_messages_create_returns_dict_unchanged():
    sample = {
        "id": "msg_abc123",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5",
        "content": [{"type": "text", "text": "pong"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 1},
    }
    async with respx.mock(base_url=BASE_URL) as router:
        router.post("/v1/messages").mock(
            return_value=httpx.Response(200, json=sample)
        )
        async with AnthropicClient(BASE_URL, "sk-test") as client:
            req, resp, headers, latency = await client.messages_create(
                model="claude-haiku-4-5",
                max_tokens=10,
                messages=[{"role": "user", "content": "hi"}],
            )
    # raw response must be preserved exactly (not normalized by an SDK)
    assert resp == sample
    assert resp["id"] == "msg_abc123"
    assert resp["usage"]["input_tokens"] == 10
    assert latency >= 0


@pytest.mark.asyncio
async def test_count_tokens_strips_generation_only_fields():
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        captured.append(_json.loads(request.content))
        return httpx.Response(200, json={"input_tokens": 12})

    async with respx.mock(base_url=BASE_URL) as router:
        router.post("/v1/messages/count_tokens").mock(side_effect=handler)
        async with AnthropicClient(BASE_URL, "sk-test") as client:
            _req, resp, _headers, _latency = await client.count_tokens(
                model="claude-haiku-4-5",
                max_tokens=16,
                temperature=0,
                stream=True,
                messages=[{"role": "user", "content": "hi"}],
            )

    assert resp == {"input_tokens": 12}
    sent = captured[0]
    assert "max_tokens" not in sent
    assert "temperature" not in sent
    assert "stream" not in sent
    assert sent["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_messages_create_raises_on_4xx():
    async with respx.mock(base_url=BASE_URL) as router:
        router.post("/v1/messages").mock(
            return_value=httpx.Response(401, json={"error": "bad key"})
        )
        async with AnthropicClient(BASE_URL, "sk-test") as client:
            with pytest.raises(AnthropicAPIError) as ei:
                await client.messages_create(
                    model="m", max_tokens=1, messages=[]
                )
    assert ei.value.status == 401


@pytest.mark.asyncio
async def test_throttled_retries_on_429_with_backoff():
    """First call returns 429, second returns 200. ThrottledClient must retry."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                429,
                json={"error": "rate limited"},
                headers={"retry-after": "0"},
            )
        return httpx.Response(
            200,
            json={
                "id": "msg_x",
                "type": "message",
                "role": "assistant",
                "model": "m",
                "content": [],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    async with respx.mock(base_url=BASE_URL) as router:
        router.post("/v1/messages").mock(side_effect=handler)
        async with AnthropicClient(BASE_URL, "sk-test") as base:
            throttled = ThrottledClient(base, max_concurrent=2)
            req, resp, headers, latency = await throttled.messages_create(
                model="m", max_tokens=1, messages=[{"role": "user", "content": "x"}]
            )
    assert call_count == 2
    assert throttled.backoff_events == 1
    assert throttled.request_count == 2
    assert resp["id"] == "msg_x"


@pytest.mark.asyncio
async def test_throttled_broadcasts_to_passive_detectors():
    sample = {
        "id": "msg_p",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5",
        "content": [],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 5},
    }
    observations: list[tuple] = []

    class _Spy:
        def observe(self, request, response, headers, latency_ms):
            observations.append((request, response, latency_ms))

        def finalize(self):  # not used here
            ...

    async with respx.mock(base_url=BASE_URL) as router:
        router.post("/v1/messages").mock(
            return_value=httpx.Response(200, json=sample)
        )
        async with AnthropicClient(BASE_URL, "sk-test") as base:
            throttled = ThrottledClient(base, passive_detectors=[_Spy()])  # type: ignore[list-item]
            await throttled.messages_create(
                model="m", max_tokens=1, messages=[{"role": "user", "content": "x"}]
            )

    assert len(observations) == 1
    req_seen, resp_seen, latency_seen = observations[0]
    assert resp_seen == sample
    assert req_seen["model"] == "m"
    assert latency_seen >= 0


@pytest.mark.asyncio
async def test_throttled_accumulates_usage_from_non_stream():
    """ThrottledClient.total_usage absorbs usage from each non-stream response."""
    sample = {
        "id": "msg_x",
        "type": "message",
        "role": "assistant",
        "model": "m",
        "content": [],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 12,
            "output_tokens": 7,
            "cache_read_input_tokens": 3,
        },
    }
    async with respx.mock(base_url=BASE_URL) as router:
        router.post("/v1/messages").mock(
            return_value=httpx.Response(200, json=sample)
        )
        async with AnthropicClient(BASE_URL, "sk-test") as base:
            throttled = ThrottledClient(base)
            await throttled.messages_create(
                model="m", max_tokens=1, messages=[{"role": "user", "content": "x"}]
            )
            await throttled.messages_create(
                model="m", max_tokens=1, messages=[{"role": "user", "content": "x"}]
            )
    # Two calls, accumulated.
    assert throttled.total_usage.input_tokens == 24
    assert throttled.total_usage.output_tokens == 14
    assert throttled.total_usage.cache_read_input_tokens == 6


@pytest.mark.asyncio
async def test_throttled_broadcasts_synthetic_final_response_for_stream():
    """Bug B fix: passive detectors must observe streaming requests too.
    ThrottledClient builds a final response dict from message_start +
    message_delta events and broadcasts it after the stream completes."""
    sse = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"id":"msg_streamed",'
        b'"type":"message","role":"assistant","model":"claude-haiku-4-5",'
        b'"content":[],"stop_reason":null,"stop_sequence":null,'
        b'"usage":{"input_tokens":11,"output_tokens":1}}}\n\n'
        b'event: content_block_start\n'
        b'data: {"type":"content_block_start","index":0,'
        b'"content_block":{"type":"text","text":""}}\n\n'
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"text_delta","text":"hi"}}\n\n'
        b'event: content_block_stop\n'
        b'data: {"type":"content_block_stop","index":0}\n\n'
        b'event: message_delta\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn",'
        b'"stop_sequence":null},"usage":{"output_tokens":4}}\n\n'
        b'event: message_stop\n'
        b'data: {"type":"message_stop"}\n\n'
    )
    observations: list[tuple] = []

    class _Spy:
        def observe(self, request, response, headers, latency_ms):
            observations.append((request, response, dict(headers), latency_ms))

        def finalize(self):  # not exercised here
            ...

    async with respx.mock(base_url=BASE_URL) as router:
        router.post("/v1/messages").mock(
            return_value=httpx.Response(
                200,
                content=sse,
                headers={
                    "content-type": "text/event-stream",
                    "anthropic-request-id": "req_streamed_xyz",
                },
            )
        )
        async with AnthropicClient(BASE_URL, "sk-test") as base:
            throttled = ThrottledClient(base, passive_detectors=[_Spy()])  # type: ignore[list-item]
            async for _ in throttled.messages_stream(
                model="m", max_tokens=10,
                messages=[{"role": "user", "content": "x"}]
            ):
                pass

    assert len(observations) == 1, "passive must see the streamed request once"
    req, resp, headers, latency = observations[0]
    # Synthesized response carries the message_start fields...
    assert resp["id"] == "msg_streamed"
    assert resp["type"] == "message"
    assert resp["role"] == "assistant"
    assert resp["model"] == "claude-haiku-4-5"
    # ...stop_reason / stop_sequence from message_delta...
    assert resp["stop_reason"] == "end_turn"
    assert resp["stop_sequence"] is None
    # ...and usage merged (input from start, output from delta cumulative).
    assert resp["usage"]["input_tokens"] == 11
    assert resp["usage"]["output_tokens"] == 4
    # Headers from the response are forwarded.
    assert headers.get("anthropic-request-id") == "req_streamed_xyz"


@pytest.mark.asyncio
async def test_throttled_accumulates_usage_from_stream():
    """ThrottledClient absorbs message_start input + message_delta output
    (cumulative final value) across a streamed response."""
    sse = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"id":"msg_1","model":"m",'
        b'"usage":{"input_tokens":42,"output_tokens":1}}}\n\n'
        b'event: content_block_start\n'
        b'data: {"type":"content_block_start","index":0,'
        b'"content_block":{"type":"text","text":""}}\n\n'
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"text_delta","text":"hi"}}\n\n'
        b'event: content_block_stop\n'
        b'data: {"type":"content_block_stop","index":0}\n\n'
        b'event: message_delta\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn",'
        b'"stop_sequence":null},"usage":{"output_tokens":7}}\n\n'
        b'event: message_stop\n'
        b'data: {"type":"message_stop"}\n\n'
    )
    async with respx.mock(base_url=BASE_URL) as router:
        router.post("/v1/messages").mock(
            return_value=httpx.Response(
                200,
                content=sse,
                headers={"content-type": "text/event-stream"},
            )
        )
        async with AnthropicClient(BASE_URL, "sk-test") as base:
            throttled = ThrottledClient(base)
            received = []
            async for ev_pair in throttled.messages_stream(
                model="m", max_tokens=10,
                messages=[{"role": "user", "content": "x"}]
            ):
                received.append(ev_pair[0].event)
    assert "message_start" in received
    assert "message_delta" in received
    # input_tokens from message_start, output_tokens from message_delta.
    assert throttled.total_usage.input_tokens == 42
    assert throttled.total_usage.output_tokens == 7


@pytest.mark.asyncio
async def test_sse_parser_handles_full_stream():
    """Replicate a minimal Anthropic-like SSE stream and verify event parsing."""
    raw = (
        "event: message_start\n"
        'data: {"type":"message_start","message":{"id":"msg_1","model":"m"}}\n'
        "\n"
        "event: ping\n"
        'data: {"type":"ping"}\n'
        "\n"
        "event: content_block_start\n"
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"text","text":""}}\n'
        "\n"
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"hi"}}\n'
        "\n"
        "event: content_block_stop\n"
        'data: {"type":"content_block_stop","index":0}\n'
        "\n"
        "event: message_delta\n"
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn",'
        '"stop_sequence":null},"usage":{"output_tokens":2}}\n'
        "\n"
        "event: message_stop\n"
        'data: {"type":"message_stop"}\n'
        "\n"
    )

    async def line_iter():
        for line in raw.split("\n"):
            yield line

    events = []
    async for ev in _parse_sse(line_iter()):
        events.append(ev)

    types = [e.event for e in events]
    assert types == [
        "message_start",
        "ping",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    # text_delta inside content_block_delta is parsed
    delta_event = events[3]
    assert delta_event.data["delta"]["text"] == "hi"
    # message_delta carries cumulative usage
    msg_delta = events[5]
    assert msg_delta.data["usage"]["output_tokens"] == 2
