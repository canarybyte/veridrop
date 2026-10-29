"""Pydantic data models — see DESIGN.md §4.2."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Mode(str, Enum):
    QUICK = "quick"
    STANDARD = "standard"
    FULL = "full"


class Protocol(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"


class DetectionTier(str, Enum):
    CRYPTOGRAPHIC = "cryptographic"
    BEHAVIORAL = "behavioral"
    PROTOCOL = "protocol"


class ExecutionConfig(BaseModel):
    mode: Mode = Mode.QUICK
    max_concurrent: int = 3
    request_timeout_s: float = 30.0
    overall_timeout_s: float = 60.0
    strict_signature: bool = False
    use_cache: bool = True
    persist_cache: bool = False
    # Opt-in long-context (needle-in-haystack) probe. Off by default because
    # a single full run can cost $0.10–$0.50 (vs ~$0.005 for the rest of the
    # full mode). Detectors that respect this flag must self-skip when False.
    include_long_context: bool = False
    # "Extreme" tier: probes proportionally up to the model's advertised
    # context limit (e.g. 32k → 500k → 950k for a 1M model). Catches the
    # "advertised X tokens but actually capped at Y < X" fraud that the
    # standard tier (capped at 200k) misses. Cost ranges to ~$8 for 1M
    # Opus runs — must be a separately confirmed opt-in. When True, takes
    # precedence over `include_long_context` (it's a superset).
    include_long_context_extreme: bool = False

    @classmethod
    def for_mode(cls, mode: Mode, **overrides: Any) -> ExecutionConfig:
        defaults = {
            Mode.QUICK: 60.0,
            Mode.STANDARD: 120.0,
            Mode.FULL: 180.0,
        }
        return cls(mode=mode, overall_timeout_s=defaults[mode], **overrides)


DetectorStatus = Literal["pass", "fail", "skip", "error"]
Verdict = Literal["passed", "marginal", "failed"]


class DetectorResult(BaseModel):
    name: str
    display_name: str
    status: DetectorStatus
    score: float = Field(ge=0.0, le=100.0)
    weight: float = Field(ge=0.0)
    details: dict[str, Any] = Field(default_factory=dict)
    # null for passive detectors (no own request) and for skipped/error
    # results that never started timing.
    duration_ms: int | None = None
    error: str | None = None


class UsageMetrics(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    server_tool_use: dict[str, Any] | None = None

    def add(self, other: UsageMetrics) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        if other.cache_read_input_tokens is not None:
            self.cache_read_input_tokens = (
                self.cache_read_input_tokens or 0
            ) + other.cache_read_input_tokens
        if other.cache_creation_input_tokens is not None:
            self.cache_creation_input_tokens = (
                self.cache_creation_input_tokens or 0
            ) + other.cache_creation_input_tokens


class PerformanceMetrics(BaseModel):
    total_latency_ms: int = 0
    ttft_ms: int | None = None
    tokens_per_second: float | None = None
    usage: UsageMetrics = Field(default_factory=UsageMetrics)
    request_count: int = 0
    backoff_events: int = 0


class DetectionReport(BaseModel):
    # Defaults preserve compatibility with historical Claude-only reports.
    protocol: Protocol = Protocol.ANTHROPIC
    tier: DetectionTier = DetectionTier.CRYPTOGRAPHIC
    tier_title: str = ""
    tier_message: str = ""
    base_url: str
    api_key_masked: str
    target_model: str
    mode: Mode
    timestamp: datetime
    total_score: float = Field(ge=0.0, le=100.0)
    verdict: Verdict
    results: list[DetectorResult] = Field(default_factory=list)
    performance: PerformanceMetrics = Field(default_factory=PerformanceMetrics)
    summary: str = ""
    run_error: str | None = None
    # Top-level shortcut to the model's self-reported identity (raw text from
    # IdentityDetector). Surfaced here so callers don't need to dig into
    # results[0].details.response_text — and so compare() can put the
    # baseline-vs-relay self-id side-by-side in the report header. None when
    # IdentityDetector was skipped or errored.
    self_reported_identity: str | None = None
    # 本次实际参与评分/执行的探针名(排序后)。显式勾选检测时记录所选集合,
    # 按 mode 默认选中时记录实际选中的探测器。旧报告没有该字段,默认空。
    selected_probes: list[str] = Field(default_factory=list)
    # Detected non-Anthropic backend brand labels found in the identity
    # response (e.g. ["Amazon Q", "AWS"]). Empty for genuine Anthropic.
    # Populated by IdentityDetector and lifted here for fast top-level reads
    # — comparator treats any new brand vs baseline as a CRITICAL signal.
    detected_non_anthropic_brands: list[str] = Field(default_factory=list)


class StreamEvent(BaseModel):
    """One server-sent event from the streaming Messages API."""

    event: str  # "message_start" / "content_block_delta" / "ping" / etc.
    data: dict[str, Any]


def mask_api_key(key: str) -> str:
    """Mask API key for display: 'sk-y7xUabc...0h' -> 'sk-y7xU••••••0h'."""
    if not key:
        return ""
    if len(key) <= 8:
        return key[:2] + "•" * (len(key) - 2)
    return f"{key[:6]}{'•' * 6}{key[-2:]}"
