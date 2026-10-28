"""Detector orchestrator — see DESIGN.md §6.2.

Responsibility split:
- Filter detectors by mode + applies_to(model).
- Split into Active vs Passive.
- Wrap base client in ThrottledClient that broadcasts to passives and
  accumulates per-request usage automatically.
- asyncio.gather() the actives; finalize() the passives at the end.
- Wall-clock the entire run for total_latency_ms.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .detectors_base import ActiveDetector, BaseDetector, PassiveDetector
from .models import (
    DetectorResult,
    ExecutionConfig,
    PerformanceMetrics,
)


ThrottledClientFactory = Callable[
    [Any, list[PassiveDetector], int],
    Any,
]
TTFTProbe = Callable[[Any, str], Awaitable[None]]
# 进度回调：(事件类型, payload)。回调异常会被 Runner 吞掉，绝不影响检测。
ProgressCallback = Callable[[str, dict[str, Any]], None]


@dataclass
class RunOutcome:
    results: list[DetectorResult]
    performance: PerformanceMetrics


class Runner:
    def __init__(
        self,
        base_client: Any,
        detectors: list[BaseDetector],
        config: ExecutionConfig,
        *,
        mode_detectors: Mapping[Any, set[str]],
        throttled_client_factory: ThrottledClientFactory,
        ttft_probe: TTFTProbe | None = None,
    ):
        self._base_client = base_client
        self._all_detectors = detectors
        self._config = config
        self._mode_detectors = mode_detectors
        self._throttled_client_factory = throttled_client_factory
        self._ttft_probe = ttft_probe

    @staticmethod
    def _emit(
        on_event: ProgressCallback | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """发送进度事件；未传回调时直接返回，回调抛错一律吞掉。"""
        if on_event is None:
            return
        try:
            on_event(event_type, payload)
        except Exception:  # noqa: BLE001 — 进度回调绝不能影响检测
            pass

    @staticmethod
    def _describe(d: BaseDetector) -> dict[str, Any]:
        return {
            "name": d.name,
            "display_name": d.display_name,
            "weight": d.weight,
            "passive": isinstance(d, PassiveDetector),
        }

    @staticmethod
    def _finished_payload(
        result: DetectorResult,
        *,
        passive: bool,
        skip_reason: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": result.name,
            "display_name": result.display_name,
            "weight": result.weight,
            "status": result.status,
            "score": result.score,
            "duration_ms": result.duration_ms,
            "error": result.error,
            "passive": passive,
        }
        if skip_reason is not None:
            payload["skip_reason"] = skip_reason
        return payload

    def _select_detectors(self, model: str) -> list[BaseDetector]:
        mode_set = self._mode_detectors[self._config.mode]
        selected: list[BaseDetector] = []
        for d in self._all_detectors:
            if d.name not in mode_set:
                continue
            if not d.applies_to(model):
                continue
            selected.append(d)
        return selected

    async def run(
        self,
        model: str,
        on_event: ProgressCallback | None = None,
    ) -> RunOutcome:
        selected = self._select_detectors(model)
        active = [d for d in selected if isinstance(d, ActiveDetector)]
        passive = [d for d in selected if isinstance(d, PassiveDetector)]

        self._emit(
            on_event,
            "run_started",
            {
                "total": len(selected),
                "detectors": [self._describe(d) for d in selected],
            },
        )

        client = self._throttled_client_factory(
            self._base_client,
            passive,
            self._config.max_concurrent,
        )

        skipped: list[DetectorResult] = []
        selected_names = {d.name for d in selected}
        for d in self._all_detectors:
            if d.name in selected_names:
                continue
            reason = (
                "mode-excluded"
                if d.name not in self._mode_detectors[self._config.mode]
                else "model-excluded"
            )
            result = d.skip(reason)
            skipped.append(result)
            self._emit(
                on_event,
                "probe_finished",
                self._finished_payload(
                    result,
                    passive=isinstance(d, PassiveDetector),
                    skip_reason=reason,
                ),
            )

        # 被动探针不自己发请求，run_started 之后先告知前端它们在被动观察中。
        for d in passive:
            self._emit(
                on_event,
                "probe_started",
                {
                    "name": d.name,
                    "display_name": d.display_name,
                    "passive": True,
                },
            )

        # Inject config for detectors that branch on mode (e.g. ConsistencyDetector)
        for d in selected:
            d.config = self._config

        finished_active: set[str] = set()

        async def run_one(d: ActiveDetector) -> DetectorResult:
            self._emit(
                on_event,
                "probe_started",
                {
                    "name": d.name,
                    "display_name": d.display_name,
                    "passive": False,
                },
            )
            try:
                result = await d._timed_run(client, model)
            except asyncio.CancelledError:
                # 整体超时导致 wait_for 取消：不把“取消”当作探针异常上报，
                # 也不吞掉取消本身；超时分支会统一生成 error 结果并补发事件。
                raise
            finished_active.add(d.name)
            self._emit(
                on_event,
                "probe_finished",
                self._finished_payload(result, passive=False),
            )
            return result

        run_started = time.perf_counter()
        try:
            active_results = await asyncio.wait_for(
                asyncio.gather(*(run_one(d) for d in active)),
                timeout=self._config.overall_timeout_s,
            )
        except asyncio.TimeoutError:
            active_results = [
                d._result(
                    "error",
                    0.0,
                    {"timeout_s": self._config.overall_timeout_s},
                    error="overall timeout",
                )
                for d in active
            ]
            # 超时前已完成的探针已在 run_one 里发过事件，这里只补发其余探针。
            for d, result in zip(active, active_results):
                if d.name in finished_active:
                    continue
                self._emit(
                    on_event,
                    "probe_finished",
                    self._finished_payload(result, passive=False),
                )
        total_latency_ms = int((time.perf_counter() - run_started) * 1000)

        # Quick mode contains no streaming detector (integrity is the only one,
        # and it's standard/full only). Without a streaming request the
        # ThrottledClient has no chance to record a content_block_delta time,
        # so TTFT would show as null. Fire one minimal streaming probe here as
        # a fallback — costs ~5 output tokens (~$0.0005 on Haiku). Skip when
        # samples already exist so standard/full pay nothing extra.
        if self._ttft_probe is not None and not getattr(client, "_ttft_samples_ms", []):
            try:
                await self._ttft_probe(client, model)
            except Exception:  # noqa: BLE001 — probe failures must not crash run
                pass

        passive_results: list[DetectorResult] = []
        for d in passive:
            result = d.finalize()
            passive_results.append(result)
            self._emit(
                on_event,
                "probe_finished",
                self._finished_payload(result, passive=True),
            )

        # TTFT: best-case first-token latency observed across all streamed
        # requests. None when no detector did a stream (e.g. quick mode with
        # only non-stream probes selected).
        ttft_ms: int | None = (
            min(client._ttft_samples_ms)
            if getattr(client, "_ttft_samples_ms", None)
            else None
        )
        perf = PerformanceMetrics(
            usage=getattr(client, "total_usage", PerformanceMetrics().usage),
            request_count=getattr(client, "request_count", 0),
            backoff_events=getattr(client, "backoff_events", 0),
            total_latency_ms=total_latency_ms,
            ttft_ms=ttft_ms,
        )

        return RunOutcome(
            results=[*active_results, *passive_results, *skipped],
            performance=perf,
        )
