"""Raw OpenAI Chat Completions client and throttled wrapper."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx

from ...core.models import UsageMetrics

if TYPE_CHECKING:
    from ...core.detectors_base import PassiveDetector


DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_CONCURRENT = 3
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_BACKOFF_S = 30.0
MAX_RETRIES = 3
# Reasoning-tier model families that REJECT sampling parameters. Sending
# temperature (even temperature=0) returns HTTP 400 "Unsupported value:
# 'temperature' does not support 0 with this model. Only the default (1)
# value is supported." The safe action is to strip the field entirely.
#
# IMPORTANT: -mini / -nano sub-variants of these families are distinct
# (non-reasoning) models that DO accept temperature — they must NOT be
# stripped, otherwise consistency / model_consistency detectors lose
# determinism and start flapping.
#
# Sources (May 2026):
#   - https://community.openai.com/t/temperature-in-gpt-5-models/1337133
#   - https://github.com/mem0ai/mem0/issues/4738 (gpt-5.4-mini accepts temp)
#   - https://github.com/BerriAI/litellm/issues/27351 (gpt-5.1 reasoning_effort=none accepts temp)
_TEMPERATURE_REJECTING_FAMILIES = (
    "gpt-5.5",  # 5.5 / 5.5-pro / 5.5-2026-04-23 (no -mini/-nano variant exists yet)
    "gpt-5.4",  # 5.4 / 5.4-pro — but NOT 5.4-mini / 5.4-nano
)
_TEMPERATURE_OK_SUB_VARIANTS = ("-mini", "-nano")


def normalize_openai_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return normalized
    return normalized + "/v1"


def _normalize_openai_model_id(model_id: str) -> str:
    """Same dot/underscore→hyphen canonicalization used by models_match in
    config.py, so users typing `gpt-5_4` or `gpt-5-4` map to the same
    family bucket as `gpt-5.4`."""
    return model_id.replace(".", "-").replace("_", "-")


def _rejects_temperature(model_id: str) -> bool:
    normalized = _normalize_openai_model_id(model_id)
    for family in _TEMPERATURE_REJECTING_FAMILIES:
        nf = _normalize_openai_model_id(family)
        if not normalized.startswith(nf):
            continue
        tail = normalized[len(nf):]
        if any(tail.startswith(suf) for suf in _TEMPERATURE_OK_SUB_VARIANTS):
            return False
        return True
    return False


def _sanitize_body(body: dict[str, Any]) -> dict[str, Any]:
    model = body.get("model")
    if isinstance(model, str) and _rejects_temperature(model):
        body.pop("temperature", None)
    return body


class OpenAIAPIError(Exception):
    def __init__(self, status: int, body: str, headers: httpx.Headers | None = None):
        self.status = status
        self.body = body
        self.headers = headers
        super().__init__(f"HTTP {status}: {body[:200]}")


class OpenAIChatClient:
    """Raw httpx client for OpenAI-compatible endpoints."""

    def __init__(
        self,
        base_url: str = DEFAULT_OPENAI_BASE_URL,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        extra_headers: dict[str, str] | None = None,
    ):
        self.base_url = normalize_openai_base_url(base_url)
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

    async def __aenter__(self) -> OpenAIChatClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def responses_create(
        self, **body: Any
    ) -> tuple[dict[str, Any], dict[str, Any], httpx.Headers, int]:
        body.pop("stream", None)
        body = _sanitize_body(body)
        start = time.perf_counter()
        resp = await self._client.post("/responses", json=body)
        latency_ms = int((time.perf_counter() - start) * 1000)
        if resp.status_code >= 400:
            raise OpenAIAPIError(resp.status_code, resp.text, resp.headers)
        return body, resp.json(), resp.headers, latency_ms

    async def chat_completions_create(
        self, **body: Any
    ) -> tuple[dict[str, Any], dict[str, Any], httpx.Headers, int]:
        body.pop("stream", None)
        # Per-request timeout override — long-context detector passes a
        # tier-scaled value (e.g. 240s for a 950k probe) so big inputs
        # don't get killed by the 30s default. Pop before _sanitize_body
        # so it never reaches the OpenAI API as a JSON field.
        timeout = body.pop("request_timeout_s", None)
        body = _sanitize_body(body)
        start = time.perf_counter()
        kwargs: dict[str, Any] = {"json": body}
        if timeout is not None:
            kwargs["timeout"] = float(timeout)
        resp = await self._client.post("/chat/completions", **kwargs)
        latency_ms = int((time.perf_counter() - start) * 1000)
        if resp.status_code >= 400:
            raise OpenAIAPIError(resp.status_code, resp.text, resp.headers)
        return body, resp.json(), resp.headers, latency_ms

    async def chat_completions_stream(
        self, **body: Any
    ) -> AsyncIterator[tuple[dict[str, Any], int]]:
        body["stream"] = True
        body = _sanitize_body(body)
        start = time.perf_counter()
        async with self._client.stream("POST", "/chat/completions", json=body) as resp:
            if resp.status_code >= 400:
                err_body = (await resp.aread()).decode("utf-8", errors="replace")
                raise OpenAIAPIError(resp.status_code, err_body, resp.headers)
            async for chunk in _parse_openai_sse(resp.aiter_lines()):
                elapsed = int((time.perf_counter() - start) * 1000)
                yield chunk, elapsed


async def _parse_openai_sse(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
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


class ThrottledOpenAIClient:
    def __init__(
        self,
        base: OpenAIChatClient,
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
        self._ttft_samples_ms: list[int] = []

    @property
    def base_url(self) -> str:
        """Pass-through so detectors treat this wrapper as the underlying client."""
        return self._base.base_url

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

    def _retry_after_seconds(self, exc: OpenAIAPIError, attempt: int) -> float:
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
            async for chunk, elapsed_ms in self._base.chat_completions_stream(**body):
                if not ttft_recorded and _chunk_has_text_delta(chunk):
                    self._ttft_samples_ms.append(elapsed_ms)
                    ttft_recorded = True
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
                yield chunk, elapsed_ms
            if usage:
                self._absorb_response_usage({"usage": usage})

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
                except OpenAIAPIError as e:
                    last_exc = e
                    if e.status in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                        await self._trigger_backoff(self._retry_after_seconds(e, attempt))
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


OpenAIClient = OpenAIChatClient
