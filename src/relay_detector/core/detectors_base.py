"""Detector base classes — see DESIGN.md §4.3 / §6.2.

Three layers:
  BaseDetector            metadata + applies_to filter
  ├── ActiveDetector      runs its own request(s)
  └── PassiveDetector     observes other detectors' (req, resp) for free
"""

from __future__ import annotations

import time
import traceback
from abc import ABC, abstractmethod
from typing import Any

import httpx

from .models import DetectorResult, DetectorStatus, ExecutionConfig, Mode


class BaseDetector(ABC):
    name: str = ""
    display_name: str = ""
    weight: float = 0.0
    # 探针分类(见 core/probes.py 的 PROBE_CATEGORIES)与一句话中文说明,
    # 供 Web 的探针目录接口与前端分组勾选使用,不参与评分逻辑。
    category: str = "basic"
    description: str = ""
    modes: set[Mode] = {Mode.QUICK, Mode.STANDARD, Mode.FULL}
    # Injected by Runner before run() is called. Detectors that need to
    # branch on mode (e.g. simplified vs full) read this.
    config: ExecutionConfig | None = None

    def applies_to(self, model: str) -> bool:
        """Return False to skip this detector for the given model."""
        return True

    def _result(
        self,
        status: DetectorStatus,
        score: float,
        details: dict[str, Any] | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> DetectorResult:
        return DetectorResult(
            name=self.name,
            display_name=self.display_name,
            status=status,
            score=score,
            weight=self.weight,
            details=details or {},
            duration_ms=duration_ms,
            error=error,
        )

    def skip(self, reason: str) -> DetectorResult:
        return self._result("skip", 0.0, {"skip_reason": reason})


class ActiveDetector(BaseDetector):
    """Issues its own API requests."""

    @abstractmethod
    async def run(self, client: Any, model: str) -> DetectorResult:
        ...

    async def _timed_run(
        self, client: Any, model: str
    ) -> DetectorResult:
        start = time.perf_counter()
        try:
            result = await self.run(client, model)
        except Exception as e:  # noqa: BLE001 — detector must never crash runner
            elapsed = int((time.perf_counter() - start) * 1000)
            return self._result(
                "error",
                0.0,
                details={
                    "exception_type": type(e).__name__,
                    # Truncated traceback so the file/line surfacing the bug
                    # is visible in the JSON report when this happens again.
                    "traceback": traceback.format_exc()[:3000],
                },
                duration_ms=elapsed,
                error=str(e),
            )
        if result.duration_ms is None:
            result.duration_ms = int((time.perf_counter() - start) * 1000)
        return result


class PassiveDetector(BaseDetector):
    """Observes (request, response) tuples from active detectors and accumulates.

    Does NOT issue its own requests — zero token cost. Called by ThrottledClient
    via .observe() after each successful active request, then .finalize() at the
    end to produce the result.
    """

    def observe(
        self,
        request: dict[str, Any],
        response: dict[str, Any],
        headers: httpx.Headers,
        latency_ms: int,
    ) -> None:
        """Override to accumulate state across many requests."""
        ...

    @abstractmethod
    def finalize(self) -> DetectorResult:
        """Produce the result from accumulated observations."""
        ...
