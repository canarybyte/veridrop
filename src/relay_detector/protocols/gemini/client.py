"""Gemini OpenAI-compatible HTTP client.

Veridrop tests Gemini relays through the OpenAI Chat Completions wire format
exclusively. Google's official OpenAI-compatible endpoint lives at
`https://generativelanguage.googleapis.com/v1beta/openai/chat/completions`;
third-party relays usually expose `/v1/chat/completions`. Both speak the same
wire protocol, so this client is a thin OpenAI Chat Completions client with
Gemini-flavored defaults — no Gemini-native `/v1beta/models/X:generateContent`
support, no request/response translation.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from ...core import trace
from ...core.models import UsageMetrics

if TYPE_CHECKING:
    from ...core.detectors_base import PassiveDetector


DEFAULT_GEMINI_OPENAI_BASE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/openai"
)
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_CONCURRENT = 3
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_BACKOFF_S = 30.0
MAX_RETRIES = 3


def normalize_gemini_base_url(base_url: str) -> str:
    """Normalize a Gemini OpenAI-compat base URL.

    Preserve explicit prefixes such as `/v1` and `/v1beta/openai`. If the
    user enters only a host root, assume the common OpenAI-compatible `/v1`
    prefix so providers like B.AI work with their documented base URL.
    """
    normalized = base_url.rstrip("/")
    parts = urlsplit(normalized)
    if parts.scheme and parts.netloc and parts.path in ("", "/"):
        return urlunsplit((parts.scheme, parts.netloc, "/v1", "", ""))
    return normalized


class GeminiAPIError(Exception):
    def __init__(self, status: int, body: str, headers: httpx.Headers | None = None):
        self.status = status
        self.body = body
        self.headers = headers
        super().__init__(f"HTTP {status}: {body[:200]}")


class GeminiClient:
    """Raw httpx client for the Gemini OpenAI-compatible Chat Completions endpoint."""

    def __init__(
        self,
        base_url: str = DEFAULT_GEMINI_OPENAI_BASE_URL,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        extra_headers: dict[str, str] | None = None,
    ):
        self.base_url = normalize_gemini_base_url(base_url)
        self.api_key = api_key or ""
        headers = {
            "authorization": f"Bearer {self.api_key}",
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

    async def __aenter__(self) -> GeminiClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def chat_completions_create(
        self, **body: Any
    ) -> tuple[dict[str, Any], dict[str, Any], httpx.Headers, int]:
        body.pop("stream", None)
        start = time.perf_counter()
        resp = await self._client.post("/chat/completions", json=body)
        latency_ms = int((time.perf_counter() - start) * 1000)
        if resp.status_code >= 400:
            raise GeminiAPIError(resp.status_code, resp.text, resp.headers)
        return body, resp.json(), resp.headers, latency_ms

    async def chat_completions_stream(
        self, **body: Any
    ) -> AsyncIterator[tuple[dict[str, Any], int]]:
        body["stream"] = True
        start = time.perf_counter()
        async with self._client.stream("POST", "/chat/completions", json=body) as resp:
            if resp.status_code >= 400:
                err_body = (await resp.aread()).decode("utf-8", errors="replace")
                raise GeminiAPIError(resp.status_code, err_body, resp.headers)
            async for chunk in _parse_openai_sse(resp.aiter_lines()):
                elapsed = int((time.perf_counter() - start) * 1000)
                yield chunk, elapsed


async def _parse_openai_sse(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    """Minimal SSE parser for OpenAI-shape Chat Completions streams.

    Treats `data: [DONE]` as a normal chunk with `_done=True` so callers can
    observe end-of-stream if they care. Multi-line `data:` payloads are joined
    with newlines per the SSE spec.
    """
    data_lines: list[str] = []
    async for line in lines:
        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                data_lines = []
                if payload.strip() == "[DONE]":
                    yield {"_done": True}
                    continue
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    yield {"_raw": payload, "_parse_error": True}
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        payload = "\n".join(data_lines)
        if payload.strip() == "[DONE]":
            yield {"_done": True}
        else:
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                yield {"_raw": payload, "_parse_error": True}


class ThrottledGeminiClient:
    """Semaphore + backoff wrapper. Mirrors the OpenAI throttled client shape.

    Detectors call .chat_completions_create / .chat_completions_stream; passive
    detectors are notified via .observe() after each successful non-stream call
    and after each stream finishes (with the final usage chunk merged in).
    """

    def __init__(
        self,
        base: GeminiClient,
        passive_detectors: list[PassiveDetector] | None = None,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    ):
        self._base = base
        self._passive = passive_detectors or []
        self._sema = asyncio.Semaphore(max_concurrent)
        self._backoff_until = 0.0
        self._backoff_lock = asyncio.Lock()
        self.request_count = 0
        self.backoff_events = 0
        self.total_usage = UsageMetrics()
        # First content delta on each stream, in ms from request start. Min
        # across streams ≈ best-case relay first-token latency.
        self._ttft_samples_ms: list[int] = []

    async def _wait_for_backoff(self) -> None:
        while True:
            wait = self._backoff_until - time.monotonic()
            if wait <= 0:
                return
            await asyncio.sleep(wait)

    async def _trigger_backoff(self, retry_after: float) -> None:
        async with self._backoff_lock:
            until = time.monotonic() + retry_after
            if until > self._backoff_until:
                self._backoff_until = until
                self.backoff_events += 1

    def _retry_after_seconds(self, exc: GeminiAPIError, attempt: int) -> float:
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
        for detector in self._passive:
            try:
                detector.observe(request, response, headers, latency_ms)
            except Exception:
                pass

    def _absorb_response_usage(self, response: dict[str, Any]) -> None:
        usage = response.get("usage")
        if not isinstance(usage, dict):
            return
        delta = UsageMetrics()
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if isinstance(prompt, int) and not isinstance(prompt, bool):
            delta.input_tokens = prompt
        if isinstance(completion, int) and not isinstance(completion, bool):
            delta.output_tokens = completion
        self.total_usage.add(delta)

    async def chat_completions_create(
        self, **body: Any
    ) -> tuple[dict[str, Any], dict[str, Any], httpx.Headers, int]:
        return await self._with_retry(
            lambda: self._base.chat_completions_create(**body),
            broadcast=True,
        )

    async def chat_completions_stream(
        self, **body: Any
    ) -> AsyncIterator[tuple[dict[str, Any], int]]:
        await self._wait_for_backoff()
        async with self._sema:
            self.request_count += 1
            usage: dict[str, Any] | None = None
            ttft_recorded = False
            chunks_for_broadcast: list[dict[str, Any]] = []
            request_started = time.monotonic()
            async for chunk, elapsed_ms in self._base.chat_completions_stream(**body):
                if chunk.get("_done") or chunk.get("_parse_error"):
                    continue
                if not ttft_recorded and _chunk_has_text_delta(chunk):
                    self._ttft_samples_ms.append(elapsed_ms)
                    ttft_recorded = True
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
                chunks_for_broadcast.append(chunk)
                yield chunk, elapsed_ms
            if usage:
                self._absorb_response_usage({"usage": usage})
            latency = int((time.monotonic() - request_started) * 1000)
            # Synthesize a non-stream-shaped response from the stream so passive
            # detectors observe ALL traffic, not just non-stream calls.
            if chunks_for_broadcast:
                synthesized = _synthesize_stream_response(chunks_for_broadcast, usage)
                self._broadcast(body, synthesized, httpx.Headers(), latency)
                trace.record_exchange(
                    request=body,
                    response=synthesized,
                    latency_ms=latency,
                    stream=True,
                    events=chunks_for_broadcast,
                )
            else:
                trace.record_exchange(
                    request=body,
                    response=None,
                    latency_ms=latency,
                    stream=True,
                    events=chunks_for_broadcast,
                )

    async def _with_retry(
        self,
        op: Callable[[], Awaitable[tuple[dict[str, Any], dict[str, Any], httpx.Headers, int]]],
        broadcast: bool,
    ) -> tuple[dict[str, Any], dict[str, Any], httpx.Headers, int]:
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            await self._wait_for_backoff()
            async with self._sema:
                self.request_count += 1
                try:
                    req, resp, headers, latency = await op()
                except GeminiAPIError as e:
                    last_exc = e
                    if e.status in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                        await self._trigger_backoff(self._retry_after_seconds(e, attempt))
                        continue
                    trace.record_exchange(
                        error={
                            "type": type(e).__name__,
                            "status": getattr(e, "status", None),
                            "body": getattr(e, "body", None),
                        },
                        status_code=getattr(e, "status", None),
                    )
                    raise
                except (httpx.TimeoutException, httpx.NetworkError) as e:
                    last_exc = e
                    if attempt < MAX_RETRIES:
                        await self._trigger_backoff(min(2.0 ** attempt, MAX_BACKOFF_S))
                        continue
                    raise
                trace.record_exchange(request=req, response=resp, latency_ms=latency)
                self._absorb_response_usage(resp)
                if broadcast:
                    self._broadcast(req, resp, headers, latency)
                return req, resp, headers, latency
        raise last_exc if last_exc else RuntimeError("retry loop exhausted")


def _chunk_has_text_delta(chunk: dict[str, Any]) -> bool:
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict) and delta.get("content"):
            return True
    return False


def _synthesize_stream_response(
    chunks: list[dict[str, Any]],
    final_usage: dict[str, Any] | None,
) -> dict[str, Any]:
    """Reduce a stream's chunks into one Chat Completions–shaped dict.

    Top-level id/model/object come from the first chunk. Content is the
    concatenation of all delta.content. finish_reason is the last non-null one
    seen. usage is the final usage chunk (if include_usage was honored).
    Tool-call deltas are intentionally not reassembled here — passive detectors
    observe the structural envelope, not function-call arguments.
    """
    head = chunks[0] if chunks else {}
    text_parts: list[str] = []
    finish_reason: Any = None
    for chunk in chunks:
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        first = choices[0]
        if not isinstance(first, dict):
            continue
        delta = first.get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str):
            text_parts.append(content)
        if first.get("finish_reason") is not None:
            finish_reason = first.get("finish_reason")
    out = {
        "id": head.get("id"),
        "object": "chat.completion",
        "model": head.get("model"),
        "created": head.get("created"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "".join(text_parts)},
                "finish_reason": finish_reason,
            }
        ],
    }
    if isinstance(final_usage, dict):
        out["usage"] = final_usage
    return out
