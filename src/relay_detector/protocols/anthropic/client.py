"""HTTP clients for the Anthropic Messages API — see DESIGN.md §4.4 / §6.3.

Two layers:
- AnthropicClient: raw httpx wrapper, no SDK. Returns dict responses to expose
  whatever the relay station actually emits (no field normalization).
- ThrottledClient: wraps AnthropicClient with semaphore concurrency cap, global
  backoff on 429/503, and broadcast-to-passive-detector hooks.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx

from ...core.models import StreamEvent, UsageMetrics

if TYPE_CHECKING:
    from ...core.detectors_base import PassiveDetector


ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_CONCURRENT = 3
RETRYABLE_STATUS = {429, 500, 502, 503, 504, 529}
MAX_BACKOFF_S = 30.0
MAX_RETRIES = 4

# Per-model parameter deprecations: when the model alias starts with the key,
# the listed body fields are stripped before sending. Anthropic occasionally
# deprecates parameters silently in newer models — Opus 4.7/4.8 reject
# requests that include `temperature` with HTTP 400 "deprecated for this model".
# Adding entries here keeps detector code model-agnostic.
PARAM_DEPRECATIONS: dict[str, tuple[str, ...]] = {
    "claude-opus-4-8": ("temperature",),
    "claude-opus-4-7": ("temperature",),
}


def _sanitize_body(body: dict[str, Any]) -> dict[str, Any]:
    """Strip body fields the target model is known to reject. Tolerates the
    common dot-vs-hyphen typo (e.g. user types `claude-opus-4.7`) by
    normalizing both sides before the prefix check — without this, Opus 4.7
    requests with the dotted form leak `temperature` and trip HTTP 400."""
    model = body.get("model")
    if not isinstance(model, str):
        return body
    normalized_model = model.replace(".", "-").replace("_", "-")
    for prefix, deprecated in PARAM_DEPRECATIONS.items():
        if normalized_model.startswith(prefix):
            for k in deprecated:
                body.pop(k, None)
    return body


def _normalize_base_url(base_url: str) -> str:
    """Normalize Anthropic-compatible base URLs to the API root.

    Users often paste OpenAI-style relay roots ending in `/v1`. This client
    posts to `/v1/messages` itself, so keeping a trailing `/v1` would produce
    `/v1/v1/messages`.
    """
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        normalized = normalized[:-3].rstrip("/")
    return normalized


class AnthropicAPIError(Exception):
    """Raised on non-2xx HTTP response. Holds status + body for inspection."""

    def __init__(self, status: int, body: str, headers: httpx.Headers | None = None):
        self.status = status
        self.body = body
        self.headers = headers
        super().__init__(f"HTTP {status}: {body[:200]}")


class AnthropicClient:
    """Raw httpx-based client. Returns dicts unchanged from the wire."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT,
        anthropic_version: str = ANTHROPIC_VERSION,
        extra_headers: dict[str, str] | None = None,
    ):
        self.base_url = _normalize_base_url(base_url)
        self.api_key = api_key
        headers = {
            "x-api-key": api_key,
            "anthropic-version": anthropic_version,
            "content-type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AnthropicClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def messages_create(
        self, **body: Any
    ) -> tuple[dict[str, Any], dict[str, Any], httpx.Headers, int]:
        """Non-streaming POST /v1/messages.

        Returns: (request_body, response_dict, response_headers, latency_ms).
        Raises AnthropicAPIError on non-2xx.

        Pass ``request_timeout_s=<seconds>`` to override the client's default
        per-request timeout — the long-context detector uses this for the
        big tiers (950k tokens can take 3+ minutes upstream).
        """
        body.pop("stream", None)
        timeout = body.pop("request_timeout_s", None)
        body = _sanitize_body(body)
        start = time.perf_counter()
        kwargs: dict[str, Any] = {"json": body}
        if timeout is not None:
            kwargs["timeout"] = float(timeout)
        resp = await self._client.post("/v1/messages", **kwargs)
        latency_ms = int((time.perf_counter() - start) * 1000)
        if resp.status_code >= 400:
            raise AnthropicAPIError(resp.status_code, resp.text, resp.headers)
        return body, resp.json(), resp.headers, latency_ms

    async def count_tokens(
        self, **body: Any
    ) -> tuple[dict[str, Any], dict[str, Any], httpx.Headers, int]:
        """POST /v1/messages/count_tokens.

        Anthropic's count endpoint estimates input tokens for the same request
        shape used by Messages. Relays may omit it; callers decide whether that
        is a hard failure or a weaker confidence signal.
        """
        body.pop("stream", None)
        body.pop("max_tokens", None)
        body.pop("temperature", None)
        body = _sanitize_body(body)
        start = time.perf_counter()
        resp = await self._client.post("/v1/messages/count_tokens", json=body)
        latency_ms = int((time.perf_counter() - start) * 1000)
        if resp.status_code >= 400:
            raise AnthropicAPIError(resp.status_code, resp.text, resp.headers)
        return body, resp.json(), resp.headers, latency_ms

    async def messages_stream(
        self,
        *,
        on_headers: Callable[[httpx.Headers], None] | None = None,
        **body: Any,
    ) -> AsyncIterator[tuple[StreamEvent, int]]:
        """Streaming POST /v1/messages, yields (event, elapsed_ms_since_request_start).

        Caller is responsible for the entire SSE lifecycle. We do NOT accumulate
        a Message object — detectors that need that build it themselves.

        on_headers, if given, is invoked once with the response headers before
        the first SSE event. Used by ThrottledClient to feed PassiveDetector
        observers with the streamed request's headers (anthropic-request-id etc).
        """
        body["stream"] = True
        body = _sanitize_body(body)
        start = time.perf_counter()
        async with self._client.stream("POST", "/v1/messages", json=body) as resp:
            if resp.status_code >= 400:
                err_body = (await resp.aread()).decode("utf-8", errors="replace")
                raise AnthropicAPIError(resp.status_code, err_body, resp.headers)
            if on_headers is not None:
                on_headers(resp.headers)
            async for event in _parse_sse(resp.aiter_lines()):
                elapsed = int((time.perf_counter() - start) * 1000)
                yield event, elapsed


async def _parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[StreamEvent]:
    """Minimal SSE parser. Each event is a block of lines terminated by blank line.

    We only care about `event:` and `data:` lines. `data:` may span multiple
    lines per spec — we join them with newlines before json-decoding.
    """
    event_name: str | None = None
    data_lines: list[str] = []
    async for line in lines:
        # httpx aiter_lines strips the trailing newline but keeps the content.
        if line == "":
            if event_name is not None:
                payload = "\n".join(data_lines)
                try:
                    data = json.loads(payload) if payload else {}
                except json.JSONDecodeError:
                    data = {"_raw": payload, "_parse_error": True}
                yield StreamEvent(event=event_name, data=data)
            event_name = None
            data_lines = []
            continue
        if line.startswith(":"):
            # SSE comment, ignore
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        # other field types (id:, retry:) ignored
    # final flush if stream ended without trailing blank line
    if event_name is not None:
        payload = "\n".join(data_lines)
        try:
            data = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            data = {"_raw": payload, "_parse_error": True}
        yield StreamEvent(event=event_name, data=data)


# --- Throttled wrapper ----------------------------------------------------


PassiveCallback = Callable[
    [dict[str, Any], dict[str, Any], httpx.Headers, int], None
]


class ThrottledClient:
    """Semaphore + global-backoff wrapper around AnthropicClient.

    - max_concurrent caps simultaneous in-flight requests to the relay.
    - 429/503 triggers a *global* backoff (other concurrent calls wait too).
    - Successful requests are broadcast to passive detectors via .observe().
    """

    def __init__(
        self,
        base: AnthropicClient,
        passive_detectors: list[PassiveDetector] | None = None,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    ):
        self._base = base
        self._passive = passive_detectors or []
        self._sema = asyncio.Semaphore(max_concurrent)
        self._backoff_until = 0.0  # monotonic seconds
        self._backoff_lock = asyncio.Lock()
        self.request_count = 0
        self.backoff_events = 0
        # Cumulative usage across all requests (stream + non-stream).
        self.total_usage = UsageMetrics()
        # Time-to-first-token samples: ms from request start to the first
        # content_block_delta event of each stream. We use content_block_delta
        # rather than message_start because message_start is just the connect
        # ack — content_block_delta is the first real model token. Min across
        # all streams ≈ best-case relay first-token latency.
        self._ttft_samples_ms: list[int] = []

    @property
    def base_url(self) -> str:
        """Pass-through so detectors treat this wrapper as the underlying client."""
        return self._base.base_url

    async def _wait_for_backoff(self) -> None:
        while True:
            now = time.monotonic()
            wait = self._backoff_until - now
            if wait <= 0:
                return
            await asyncio.sleep(wait)

    async def _trigger_backoff(self, retry_after: float) -> None:
        async with self._backoff_lock:
            until = time.monotonic() + retry_after
            if until > self._backoff_until:
                self._backoff_until = until
                self.backoff_events += 1

    def _retry_after_seconds(self, exc: AnthropicAPIError, attempt: int) -> float:
        # Honor Retry-After header if present, else exponential backoff.
        if exc.headers is not None:
            ra = exc.headers.get("retry-after")
            if ra:
                try:
                    return min(float(ra), MAX_BACKOFF_S)
                except ValueError:
                    pass
        return min(2.0 ** attempt, MAX_BACKOFF_S)

    def _broadcast(
        self,
        request: dict[str, Any],
        response: dict[str, Any],
        headers: httpx.Headers,
        latency_ms: int,
    ) -> None:
        for d in self._passive:
            try:
                d.observe(request, response, headers, latency_ms)
            except Exception:
                # passive observation must not break the pipeline
                pass

    def _absorb_response_usage(self, response: dict[str, Any]) -> None:
        usage = response.get("usage")
        if not isinstance(usage, dict):
            return
        delta = UsageMetrics()
        for k in ("input_tokens", "output_tokens"):
            v = usage.get(k)
            if isinstance(v, int) and not isinstance(v, bool):
                setattr(delta, k, v)
        for k in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            v = usage.get(k)
            if isinstance(v, int) and not isinstance(v, bool):
                setattr(delta, k, v)
        self.total_usage.add(delta)

    async def messages_create(
        self, **body: Any
    ) -> tuple[dict[str, Any], dict[str, Any], httpx.Headers, int]:
        return await self._with_retry(
            lambda: self._base.messages_create(**body),
            broadcast=True,
        )

    async def count_tokens(
        self, **body: Any
    ) -> tuple[dict[str, Any], dict[str, Any], httpx.Headers, int]:
        return await self._with_retry(
            lambda: self._base.count_tokens(**body),
            broadcast=False,
        )

    async def messages_stream(
        self, **body: Any
    ) -> AsyncIterator[tuple[StreamEvent, int]]:
        """Streaming variant.

        Note: streaming requests still respect the semaphore, but retry logic
        only applies to the initial connect — once events start flowing we
        don't retry mid-stream. Per-stream usage is captured by intercepting
        message_start (true input_tokens) and message_delta (cumulative
        output_tokens) and committed to total_usage when the stream ends.

        At stream end we also synthesize a non-stream-equivalent response dict
        from message_start + message_delta and broadcast it to passive
        detectors so they observe ALL requests, not just non-stream ones.
        """
        await self._wait_for_backoff()
        async with self._sema:
            self.request_count += 1
            local = UsageMetrics()
            captured_headers: list[httpx.Headers] = []
            message_start_msg: dict[str, Any] | None = None
            delta_stop_reason: Any = None
            delta_stop_sequence: Any = None
            delta_usage: dict[str, Any] | None = None

            def _capture_headers(h: httpx.Headers) -> None:
                captured_headers.append(h)

            request_started = time.monotonic()
            ttft_recorded = False
            try:
                async for ev_pair in self._base.messages_stream(
                    on_headers=_capture_headers, **body
                ):
                    ev, elapsed_ms = ev_pair
                    # First content_block_delta = real first token. Capture
                    # once per stream; subsequent deltas don't refine the
                    # measurement.
                    if not ttft_recorded and ev.event == "content_block_delta":
                        self._ttft_samples_ms.append(elapsed_ms)
                        ttft_recorded = True
                    self._absorb_stream_event(ev, local)
                    if ev.event == "message_start" and message_start_msg is None:
                        msg = ev.data.get("message")
                        if isinstance(msg, dict):
                            message_start_msg = msg
                    elif ev.event == "message_delta":
                        d = ev.data.get("delta") or {}
                        if "stop_reason" in d:
                            delta_stop_reason = d["stop_reason"]
                        if "stop_sequence" in d:
                            delta_stop_sequence = d["stop_sequence"]
                        u = ev.data.get("usage")
                        if isinstance(u, dict):
                            delta_usage = u
                    yield ev_pair
            finally:
                self.total_usage.add(local)
                if message_start_msg is not None:
                    final = self._synthesize_stream_response(
                        message_start_msg,
                        delta_stop_reason,
                        delta_stop_sequence,
                        delta_usage,
                    )
                    headers = captured_headers[0] if captured_headers else httpx.Headers()
                    latency = int((time.monotonic() - request_started) * 1000)
                    self._broadcast(body, final, headers, latency)

    @staticmethod
    def _synthesize_stream_response(
        message_start_msg: dict[str, Any],
        stop_reason: Any,
        stop_sequence: Any,
        delta_usage: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Reconstruct a non-stream-equivalent dict from streamed events.

        Used to feed PassiveDetector.observe() for streamed requests. Top-level
        fields (id/type/role/model/usage) come from message_start; stop_reason
        and stop_sequence come from message_delta. Content blocks are NOT
        rebuilt — passive checks focus on top-level shape, not block content.
        """
        final = dict(message_start_msg)
        final["stop_reason"] = stop_reason
        final["stop_sequence"] = stop_sequence
        if delta_usage:
            base_usage = dict(message_start_msg.get("usage") or {})
            base_usage.update(delta_usage)
            final["usage"] = base_usage
        return final

    @staticmethod
    def _absorb_stream_event(ev: StreamEvent, local: UsageMetrics) -> None:
        """Update per-stream local usage from an event.

        message_start carries true input_tokens; message_delta carries
        cumulative output_tokens (per Anthropic streaming spec — not delta).
        Multiple message_delta events overwrite, so we always use the latest.
        """
        if ev.event == "message_start":
            msg = ev.data.get("message") or {}
            usage = msg.get("usage") or {}
            v = usage.get("input_tokens")
            if isinstance(v, int) and not isinstance(v, bool):
                local.input_tokens = v
            for k in (
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            ):
                cv = usage.get(k)
                if isinstance(cv, int) and not isinstance(cv, bool):
                    setattr(local, k, cv)
        elif ev.event == "message_delta":
            usage = ev.data.get("usage") or {}
            v = usage.get("output_tokens")
            if isinstance(v, int) and not isinstance(v, bool):
                local.output_tokens = v

    async def _with_retry(
        self,
        op: Callable[[], Awaitable[
            tuple[dict[str, Any], dict[str, Any], httpx.Headers, int]
        ]],
        broadcast: bool,
    ) -> tuple[dict[str, Any], dict[str, Any], httpx.Headers, int]:
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            await self._wait_for_backoff()
            async with self._sema:
                self.request_count += 1
                try:
                    req, resp, headers, latency = await op()
                except AnthropicAPIError as e:
                    last_exc = e
                    if e.status in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                        delay = self._retry_after_seconds(e, attempt)
                        await self._trigger_backoff(delay)
                        continue
                    raise
                except (httpx.TimeoutException, httpx.NetworkError) as e:
                    last_exc = e
                    if attempt < MAX_RETRIES:
                        await self._trigger_backoff(min(2.0 ** attempt, MAX_BACKOFF_S))
                        continue
                    raise
                self._absorb_response_usage(resp)
                if broadcast:
                    self._broadcast(req, resp, headers, latency)
                return req, resp, headers, latency
        # unreachable; either returned or raised above
        raise last_exc if last_exc else RuntimeError("retry loop exhausted")
