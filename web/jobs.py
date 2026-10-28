"""Job queue for HTTP-driven detections.

Wraps the same Runner the CLI uses. Single uvicorn worker, in-process state
guarded by an asyncio lock. Done jobs are persisted to disk so a server
restart keeps the shareable result URLs alive. API keys are NEVER persisted —
the masked form goes to disk; the raw key lives only in the Job until the run
finishes, then is dropped from memory.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from relay_detector.core.runner import ProgressCallback
from relay_detector.models import (
    DetectionReport,
    DetectionTier,
    DetectorResult,
    ExecutionConfig,
    Mode,
    Protocol,
    mask_api_key,
)
from relay_detector.scorer import (
    compute_total,
    effective_verdict,
    fatal_run_error,
    has_critical_issues,
    summary_text,
)

from .progress import bus


logger = logging.getLogger("veridrop")

JobStatus = Literal["queued", "running", "done", "error"]

# Production default; override via VERIDROP_JOBS_DIR in tests / dev so the
# import doesn't try to mkdir into /opt/veridrop on a developer laptop.
JOBS_DIR = Path(
    os.environ.get("VERIDROP_JOBS_DIR", "/opt/veridrop/web_data/jobs")
)
JOBS_DIR.mkdir(parents=True, exist_ok=True)

# Cap concurrent detections so a flood of submissions doesn't exhaust file
# descriptors or get the upstream Anthropic API rate-limited. Each detection
# already runs ~13 outbound requests in parallel, so 6 inflight = ~78 sockets.
_MAX_INFLIGHT = 6
_SEMA = asyncio.Semaphore(_MAX_INFLIGHT)


@dataclass
class Job:
    id: str
    protocol: str = "anthropic"
    status: JobStatus = "queued"
    base_url: str = ""
    target_model: str = ""
    mode: str = "full"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    report: dict[str, Any] | None = None
    error: str | None = None


_JOBS: dict[str, Job] = {}
_LOCK = asyncio.Lock()


def _new_job_id() -> str:
    # 8-char URL-safe id; secrets gives ~48 bits of entropy, fine for an
    # unguessable shareable link without auth.
    return secrets.token_urlsafe(6)


async def submit(
    base_url: str,
    api_key: str,
    model: str,
    mode: str,
    protocol: str = "anthropic",
    include_long_context: bool = False,
    include_long_context_extreme: bool = False,
) -> str:
    """Queue a detection job and return the job id immediately.

    Long-context probe is opt-in. Two tiers:
      - ``include_long_context`` (standard): 32k/100k/200k probes,
        $0.05–$0.50 upstream cost, 30–90s extra wall time.
      - ``include_long_context_extreme`` (adaptive): probes proportionally
        up to the model's advertised limit (e.g. 32k→500k→950k for 1M
        models). $0.05–$8 cost, 30s–5min wall time. Catches "advertised X
        but capped at Y<X" fraud that the standard tier misses on big
        models. Implies standard (it's a superset).
    """
    job_id = _new_job_id()
    job = Job(
        id=job_id,
        protocol=protocol,
        base_url=base_url,
        target_model=model,
        mode=mode,
    )
    async with _LOCK:
        _JOBS[job_id] = job
    # 事件必须在 create_task 之前发出：SSE 可能比 Runner 更早连上来，先缓存
    # queued 事件才能保证订阅者拿到完整序列（断线重连也能回放）。
    bus.publish(
        job_id,
        "queued",
        {
            "job_id": job_id,
            "protocol": protocol,
            "target_model": model,
            "mode": mode,
        },
    )
    asyncio.create_task(
        _run(
            job_id, base_url, api_key, model, mode, protocol,
            include_long_context, include_long_context_extreme,
        )
    )
    return job_id


async def get(job_id: str) -> Job | None:
    """Look up a job by id. Falls back to disk for jobs that survived a restart."""
    async with _LOCK:
        j = _JOBS.get(job_id)
    if j is not None:
        return j
    report = None
    for path in _report_candidates(job_id):
        if not path.exists():
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            break
        except (json.JSONDecodeError, OSError):
            continue
    if report is None:
        return None
    return Job(
        id=job_id,
        status="done",
        protocol=report.get("protocol", "anthropic"),
        base_url=report.get("base_url", ""),
        target_model=report.get("target_model", ""),
        mode=report.get("mode", "full"),
        created_at=time.time(),
        finished_at=time.time(),
        report=report,
    )


def report_path(job_id: str, protocol: str) -> Path:
    protocol_dir = JOBS_DIR / protocol
    protocol_dir.mkdir(parents=True, exist_ok=True)
    return protocol_dir / f"{job_id}.json"


def image_path(job_id: str, protocol: str) -> Path:
    protocol_dir = JOBS_DIR / protocol
    protocol_dir.mkdir(parents=True, exist_ok=True)
    return protocol_dir / f"{job_id}.jpg"


def _report_candidates(job_id: str) -> list[Path]:
    return [
        JOBS_DIR / f"{job_id}.json",
        JOBS_DIR / "anthropic" / f"{job_id}.json",
        JOBS_DIR / "openai" / f"{job_id}.json",
        JOBS_DIR / "gemini" / f"{job_id}.json",
    ]


async def _run(
    job_id: str,
    base_url: str,
    api_key: str,
    model: str,
    mode: str,
    protocol: str,
    include_long_context: bool = False,
    include_long_context_extreme: bool = False,
) -> None:
    async with _SEMA:
        async with _LOCK:
            j = _JOBS.get(job_id)
            if j is None:
                return
            j.status = "running"
            j.started_at = time.time()
            created_at = j.created_at
            started_at = j.started_at

        # Runner 已经负责发 run_started / probe_started / probe_finished，
        # 这里只做转发，绝不重复发同类型事件。回调是同步契约，publish 也是
        # 同步方法，异常由 Runner 吞掉，不会影响检测。
        def on_event(event_type: str, payload: dict[str, Any]) -> None:
            bus.publish(job_id, event_type, payload)

        try:
            cfg = ExecutionConfig.for_mode(Mode(mode), max_concurrent=3)
            cfg.include_long_context = include_long_context
            cfg.include_long_context_extreme = include_long_context_extreme
            # Long-context probes blow past the regular per-mode wall-clock
            # budget (60–180s). 1M-token requests alone take 2–4 minutes
            # upstream; the extreme path may also sleep 75s per tier waiting
            # for an OpenAI/Anthropic TPM window to reset before retrying
            # rate-limited probes (see _probe_tier_with_tpm_retry).
            # Without this bump asyncio.wait_for kills the runner
            # mid-detector and the user gets a misleading "fail" instead.
            if include_long_context_extreme:
                cfg.overall_timeout_s = max(cfg.overall_timeout_s, 900.0)
            elif include_long_context:
                cfg.overall_timeout_s = max(cfg.overall_timeout_s, 300.0)
            if protocol == "openai":
                outcome = await _run_openai(
                    base_url, api_key, model, cfg, on_event=on_event
                )
                report_protocol = Protocol.OPENAI
                report_tier = DetectionTier.BEHAVIORAL
                tier_title = "行为/协议级验证"
                tier_message = (
                    "本检测无法可靠区分高配模型真品与低配模型伪装。"
                    "我们检测的是中转站接口是否符合 OpenAI Chat Completions 协议规范、"
                    "能力是否完整、usage 字段是否符合官方响应形状。"
                )
            elif protocol == "gemini":
                outcome = await _run_gemini(
                    base_url, api_key, model, cfg, on_event=on_event
                )
                report_protocol = Protocol.GEMINI
                report_tier = DetectionTier.PROTOCOL
                tier_title = "协议级验证"
                tier_message = (
                    "本检测通过 OpenAI 兼容协议 (POST /chat/completions) 探测 Gemini 中转站,"
                    "验证响应字段、tool 调用、结构化输出、流式一致性和 usage 字段是否符合 OpenAI 规范。"
                    "它不提供加密级模型真伪证明。"
                )
            elif protocol == "anthropic":
                outcome = await _run_anthropic(
                    base_url, api_key, model, cfg, on_event=on_event
                )
                report_protocol = Protocol.ANTHROPIC
                report_tier = DetectionTier.CRYPTOGRAPHIC
                tier_title = "加密级验证"
                tier_message = (
                    "Claude thinking signature 来自 Anthropic 服务端签名。"
                    "通过该项时,它是当前检测集中最高可信度的真伪信号。"
                )
            else:
                raise ValueError(f"unsupported protocol: {protocol}")

            run_error = fatal_run_error(outcome.results)
            score = 0.0 if run_error else compute_total(outcome.results)
            verdict = effective_verdict(score, outcome.results)
            summary = run_error or summary_text(score, verdict)
            risk_level = _risk_level(outcome.results, score)

            self_id: str | None = None
            brands: list[str] = []
            for r in outcome.results:
                if r.name != "identity" or not isinstance(r.details, dict):
                    continue
                text = r.details.get("response_text")
                if isinstance(text, str) and text.strip():
                    self_id = text.strip()
                b = r.details.get("detected_non_anthropic_brands")
                if isinstance(b, list):
                    brands = [x for x in b if isinstance(x, str)]
                break

            report = DetectionReport(
                protocol=report_protocol,
                tier=report_tier,
                tier_title=tier_title,
                tier_message=tier_message,
                base_url=base_url,
                api_key_masked=mask_api_key(api_key),
                target_model=model,
                mode=Mode(mode),
                timestamp=datetime.now(timezone.utc),
                total_score=score,
                verdict=verdict,
                results=outcome.results,
                performance=outcome.performance,
                summary=summary,
                run_error=run_error,
                self_reported_identity=self_id,
                detected_non_anthropic_brands=brands,
            )
            report_dict = json.loads(report.model_dump_json())
            report_path(job_id, protocol).write_text(
                json.dumps(report_dict, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            probes = [
                {
                    "name": r.name,
                    "display_name": r.display_name,
                    "status": r.status,
                    "score": r.score,
                    "weight": r.weight,
                    "duration_ms": r.duration_ms,
                    "error": r.error,
                    "details": r.details,
                }
                for r in outcome.results
            ]
            probes_total = sum(1 for r in outcome.results if r.status != "skip")
            probes_passed = sum(1 for r in outcome.results if r.status == "pass")
            probes_failed = sum(
                1 for r in outcome.results if r.status in ("fail", "error")
            )

            finished_at = time.time()
            async with _LOCK:
                if job_id in _JOBS:
                    _JOBS[job_id].status = "done"
                    _JOBS[job_id].protocol = protocol
                    _JOBS[job_id].report = report_dict
                    _JOBS[job_id].finished_at = finished_at

            await _persist(
                job_id,
                protocol=protocol,
                base_url=base_url,
                target_model=model,
                mode=mode,
                # 只落掩码：报告里已经算好，原始 key 绝不进入持久化路径。
                api_key_masked=report_dict["api_key_masked"],
                status="done",
                created_at=created_at,
                started_at=started_at,
                finished_at=finished_at,
                risk_level=risk_level,
                report=report_dict,
                probes=probes,
                total_probes=probes_total,
                passed_probes=probes_passed,
                failed_probes=probes_failed,
            )

            bus.publish(
                job_id,
                "job_finished",
                {
                    "job_id": job_id,
                    "total_score": report_dict["total_score"],
                    "verdict": report_dict["verdict"],
                    "risk_level": risk_level,
                    "summary": report_dict.get("summary") or "",
                    "run_error": report_dict.get("run_error"),
                    "self_reported_identity": report_dict.get(
                        "self_reported_identity"
                    ),
                    "detected_brands": report_dict.get(
                        "detected_non_anthropic_brands"
                    )
                    or [],
                    "duration_ms": int((finished_at - started_at) * 1000),
                    "performance": report_dict.get("performance") or {},
                    "probes_total": probes_total,
                    "probes_passed": probes_passed,
                    "probes_failed": probes_failed,
                    "report_id": job_id,
                },
            )

        except Exception as e:  # noqa: BLE001 — bubble error into job state
            error_text = f"{type(e).__name__}: {e}"
            finished_at = time.time()
            async with _LOCK:
                if job_id in _JOBS:
                    _JOBS[job_id].status = "error"
                    _JOBS[job_id].error = error_text
                    _JOBS[job_id].finished_at = finished_at

            await _persist(
                job_id,
                protocol=protocol,
                base_url=base_url,
                target_model=model,
                mode=mode,
                # 失败分支没有 report，在调用点现算掩码，原始 key 不离开内存。
                api_key_masked=mask_api_key(api_key),
                status="error",
                created_at=created_at,
                started_at=started_at,
                finished_at=finished_at,
                error=error_text,
            )

            bus.publish(
                job_id,
                "job_error",
                {"job_id": job_id, "error": error_text},
            )
        finally:
            # 成功/失败都走这一条，保证订阅者只收到一个关闭哨兵。
            bus.close(job_id)


def _risk_level(results: list[DetectorResult], score: float) -> str:
    """critical 硬否决 → 高；否则按总分分档（50–69.99 为中）。"""
    if has_critical_issues(results) or score < 50:
        return "high"
    if score < 70:
        return "medium"
    return "low"


def _utc(ts: float | None) -> datetime | None:
    return None if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc)


async def _persist(
    job_id: str,
    *,
    protocol: str,
    base_url: str,
    target_model: str,
    mode: str,
    api_key_masked: str,
    status: str,
    created_at: float,
    started_at: float | None,
    finished_at: float,
    risk_level: str | None = None,
    report: dict[str, Any] | None = None,
    probes: list[dict[str, Any]] | None = None,
    total_probes: int = 0,
    passed_probes: int = 0,
    failed_probes: int = 0,
    error: str | None = None,
) -> None:
    """终态入库；任何异常只打 warning，绝不影响 Job 状态与 SSE。"""
    from . import repository

    try:
        await repository.save_detection(
            job_id,
            protocol=protocol,
            base_url=base_url,
            target_model=target_model,
            mode=mode,
            api_key_masked=api_key_masked,
            status=status,
            created_at=_utc(created_at),
            started_at=_utc(started_at),
            finished_at=_utc(finished_at),
            total_score=report.get("total_score") if report else None,
            verdict=report.get("verdict") if report else None,
            risk_level=risk_level,
            summary=(report.get("summary") or "") if report else "",
            run_error=(report.get("run_error") if report else error),
            self_reported_identity=(
                report.get("self_reported_identity") if report else None
            ),
            detected_brands=(
                report.get("detected_non_anthropic_brands") if report else None
            ),
            performance=(report.get("performance") if report else None),
            report=report,
            probes=probes,
            total_probes=total_probes,
            passed_probes=passed_probes,
            failed_probes=failed_probes,
        )
    except Exception as e:  # noqa: BLE001 — 落库失败不能拖垮作业
        logger.warning("db persist failed: %s", e)


async def _run_anthropic(
    base_url: str,
    api_key: str,
    model: str,
    cfg: ExecutionConfig,
    *,
    on_event: ProgressCallback | None = None,
):
    from relay_detector.protocols.anthropic import (
        build_detectors,
        build_runner,
        make_client,
    )

    async with make_client(base_url, api_key, timeout=cfg.request_timeout_s) as client:
        runner = build_runner(client, build_detectors(cfg.mode), cfg)
        return await runner.run(model, on_event=on_event)


async def _run_openai(
    base_url: str,
    api_key: str,
    model: str,
    cfg: ExecutionConfig,
    *,
    on_event: ProgressCallback | None = None,
):
    from relay_detector.protocols.openai import (
        build_detectors,
        build_runner,
        make_client,
    )

    async with make_client(base_url, api_key, timeout=cfg.request_timeout_s) as client:
        runner = build_runner(client, build_detectors(cfg.mode), cfg)
        return await runner.run(model, on_event=on_event)


async def _run_gemini(
    base_url: str,
    api_key: str,
    model: str,
    cfg: ExecutionConfig,
    *,
    on_event: ProgressCallback | None = None,
):
    from relay_detector.protocols.gemini import (
        build_detectors,
        build_runner,
        make_client,
    )

    async with make_client(base_url, api_key, timeout=cfg.request_timeout_s) as client:
        runner = build_runner(client, build_detectors(cfg.mode), cfg)
        return await runner.run(model, on_event=on_event)
