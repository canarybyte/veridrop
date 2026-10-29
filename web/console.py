"""React 探针控制台 API + 外部 agent 只读 API。

路由分两组：

- ``/app/api/*``  —— 前端同源接口，免密钥（浏览器不持有任何服务端密钥）。
- ``/api/v1/*``   —— 外部 agent 只读接口，需要 ``X-API-Key``（也接受
  ``Authorization: Bearer <key>``），密钥来自 ``VERIDROP_AGENT_API_KEY``。

时间字段约定（前端不依赖具体格式，但同一接口内类型必须稳定）：

- ``/app/api/detect/*``（作业状态 / SSE）统一用 **epoch 秒（float）**，
  与 ``web/jobs.py`` 的 ``Job`` 字段一致；
- ``/app/api/reports/*`` 与 ``/api/v1/reports/*`` 统一用 **ISO 8601 字符串**，
  与 ``web/repository.py`` 的输出一致（磁盘回落时取报告里的 timestamp）。

本模块只依赖 jobs / progress / repository，不反向依赖 server，避免循环导入。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from . import jobs, probe, repository
from .credentials import (
    CredentialDecryptError,
    decrypt_api_key,
    encrypt_api_key,
    mask_key,
)
from .probe_catalog import build_catalog
from .progress import bus


logger = logging.getLogger("veridrop")

HERE = Path(__file__).resolve().parent
APP_DIR = HERE / "static" / "app"
INDEX_FILE = APP_DIR / "index.html"

# SSE 心跳间隔（秒）——见 spec「连接保活」。
_SSE_PING_INTERVAL = 15.0
_TERMINAL_EVENT_TYPES = ("job_finished", "job_error")

_FRONTEND_MISSING = "frontend not built; run npm run build in web-ui"


# --------------------------------------------------------------------- 小工具


def _host_from_url(base_url: str) -> str:
    """从 base_url 推导域名；畸形 URL 返回空字符串。"""
    try:
        return urlparse(base_url).hostname or ""
    except ValueError:
        return ""


def _report_results(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    """报告里的 results 是 dict 列表；过滤掉非 dict 项。"""
    if not isinstance(report, dict):
        return []
    results = report.get("results")
    if not isinstance(results, list):
        return []
    return [item for item in results if isinstance(item, dict)]


def _has_critical_issues(results: list[dict[str, Any]]) -> bool:
    """等价于 relay_detector.scorer.has_critical_issues，但作用于 dict 结果。

    私有函数 ``jobs._risk_level`` 需要 DetectorResult 对象，而 API 层手里
    只有 JSON dict，所以这里按同样的两条判定复刻，避免为判风险去反序列化。
    """
    for result in results:
        if result.get("status") == "skip":
            continue
        details = result.get("details")
        if not isinstance(details, dict):
            continue
        count = details.get("critical_issue_count")
        if isinstance(count, int) and count > 0:
            return True
        for issue in details.get("issues") or []:
            if isinstance(issue, dict) and issue.get("severity") == "critical":
                return True
    return False


def _risk_level_from_report(report: dict[str, Any] | None) -> str | None:
    """与 jobs._risk_level 相同规则：critical 或 <50 → high；<70 → medium。"""
    if not isinstance(report, dict):
        return None
    score = report.get("total_score")
    score = float(score) if isinstance(score, (int, float)) else 0.0
    results = _report_results(report)
    if _has_critical_issues(results) or score < 50:
        return "high"
    if score < 70:
        return "medium"
    return "low"


def _probe_counts(results: list[dict[str, Any]]) -> tuple[int, int, int]:
    """(total, passed, failed)；total 只数非 skip 的探针。"""
    total = sum(1 for r in results if r.get("status") != "skip")
    passed = sum(1 for r in results if r.get("status") == "pass")
    failed = sum(1 for r in results if r.get("status") in ("fail", "error"))
    return total, passed, failed


def _progress_from_events(events: list[dict[str, Any]]) -> dict[str, int]:
    """从事件总线缓存推导进度：total 取 run_started.total，done 数 probe_finished。"""
    total = done = passed = failed = 0
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if event.get("type") == "run_started":
            value = payload.get("total")
            if isinstance(value, int):
                total = value
        elif event.get("type") == "probe_finished":
            done += 1
            status = payload.get("status")
            if status == "pass":
                passed += 1
            elif status in ("fail", "error"):
                failed += 1
    return {"total": total, "done": done, "passed": passed, "failed": failed}


def _progress_for_job(
    job_id: str,
    report: dict[str, Any] | None,
) -> dict[str, int]:
    """优先用实时事件；重启后没有事件时退回报告里的逐探针结果。"""
    events = bus.get_events(job_id)
    if events:
        return _progress_from_events(events)
    results = _report_results(report)
    if results:
        total, passed, failed = _probe_counts(results)
        # 报告里所有探针都已终结，但 total 只计非 skip，done 与之对齐。
        return {"total": total, "done": passed + failed, "passed": passed, "failed": failed}
    return {"total": 0, "done": 0, "passed": 0, "failed": 0}


def _format_sse(event: dict[str, Any]) -> str:
    """严格的 SSE 帧：id / event / data 三行 + 空行结尾。"""
    return (
        f"id: {event['seq']}\n"
        f"event: {event['type']}\n"
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    )


def _parse_last_event_id(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return None


def _synthetic_terminal_event(job: jobs.Job, seq: int) -> dict[str, Any] | None:
    """为「已结束但缓存里没有终态事件」的 job 合成一条终态事件。

    典型场景：服务重启后从磁盘读回的旧 job、或内存状态与总线之间的极端竞态。
    合成事件只发给当前 SSE 连接，不写回 bus（避免污染缓存与 seq）。
    """
    if not isinstance(job.report, dict):
        if job.status == "error" or job.error:
            return {
                "seq": seq,
                "ts": time.time(),
                "type": "job_error",
                "payload": {
                    "job_id": job.id,
                    "error": job.error or "检测任务异常结束",
                },
            }
        return None

    report = job.report
    results = _report_results(report)
    total, passed, failed = _probe_counts(results)
    return {
        "seq": seq,
        "ts": time.time(),
        "type": "job_finished",
        "payload": {
            "job_id": job.id,
            "total_score": report.get("total_score"),
            "verdict": report.get("verdict"),
            "risk_level": _risk_level_from_report(report),
            "summary": report.get("summary") or "",
            "run_error": report.get("run_error"),
            "self_reported_identity": report.get("self_reported_identity"),
            "detected_brands": list(
                report.get("detected_non_anthropic_brands") or []
            ),
            "selected_probes": list(report.get("selected_probes") or []),
            # 磁盘读回的旧 job 没有可靠起止时间，给 0 保证字段类型稳定。
            "duration_ms": 0,
            "performance": report.get("performance") or {},
            "probes_total": total,
            "probes_passed": passed,
            "probes_failed": failed,
            "report_id": job.id,
        },
    }


def _db_replay_events(
    record: dict[str, Any], include_deep: bool = True
) -> list[dict[str, Any]]:
    """把数据库里的历史检测还原成与实时流同形状的事件序列。

    只用于内存与磁盘都找不到的历史任务：探针结果 → probe_finished，
    终态 → job_finished / job_error；有深测结果时追加 deep_started / deep_progress /
    deep_finished。运行中（服务重启时未结束）的记录给出中断提示。
    include_deep=False 时不还原深测（补跑深测的回放会自行衔接新的深测事件）。
    """
    job_id = record["id"]
    probes = record.get("probes") or []
    deep_report = record.get("deep_report") if isinstance(record.get("deep_report"), dict) else None
    has_deep = include_deep and deep_report is not None
    events: list[dict[str, Any]] = []

    def add(event_type: str, payload: dict[str, Any]) -> None:
        events.append(
            {"seq": len(events) + 1, "ts": 0, "type": event_type, "payload": payload}
        )

    add(
        "run_started",
        {
            "total": len(probes),
            "detectors": [
                {
                    "name": p["name"],
                    "display_name": p["display_name"],
                    "weight": p["weight"],
                    "passive": False,
                }
                for p in probes
            ],
        },
    )
    for p in probes:
        add(
            "probe_finished",
            {
                "name": p["name"],
                "display_name": p["display_name"],
                "weight": p["weight"],
                "status": p["status"],
                "score": p["score"],
                "duration_ms": p["duration_ms"],
                "error": p["error"],
                "passive": False,
            },
        )

    status = record.get("status")
    if status == "done":
        report = record.get("report") or {}
        add(
            "job_finished",
            {
                "job_id": job_id,
                "total_score": record.get("total_score"),
                "verdict": record.get("verdict"),
                "risk_level": record.get("risk_level"),
                "summary": record.get("summary") or "",
                "run_error": record.get("run_error"),
                "self_reported_identity": record.get("self_reported_identity"),
                "detected_brands": list(record.get("detected_brands") or []),
                "selected_probes": list(report.get("selected_probes") or []),
                "duration_ms": _duration_ms(record),
                "performance": record.get("performance") or {},
                "probes_total": record.get("total_probes") or 0,
                "probes_passed": record.get("passed_probes") or 0,
                "probes_failed": record.get("failed_probes") or 0,
                "report_id": job_id,
                # 有深测结果才声明 deep_enabled,前端据此等待随后的 deep_* 事件。
                "deep_enabled": include_deep and has_deep,
                "deep_status": record.get("deep_status") if has_deep else "skipped",
            },
        )
    elif status == "error":
        add(
            "job_error",
            {
                "job_id": job_id,
                "error": record.get("run_error") or record.get("summary") or "检测任务异常结束",
            },
        )
    else:
        add(
            "job_error",
            {"job_id": job_id, "error": "检测未完成：服务重启时该任务已中断。"},
        )
    if has_deep:
        checks = sorted(
            (c for c in (deep_report.get("checks") or []) if isinstance(c, dict)),
            key=lambda c: int(c.get("id") or 0),
        )
        add(
            "deep_started",
            {
                "total": len(checks) or 13,
                "checks": [{"id": c.get("id"), "title": c.get("title")} for c in checks],
            },
        )
        for check in checks:
            add("deep_progress", check)
        add(
            "deep_finished",
            {
                "status": "error" if record.get("deep_status") == "error" else "done",
                "error": deep_report.get("error"),
                "duration_ms": deep_report.get("duration_ms") or 0,
                "metrics": deep_report.get("metrics") or {},
                "checks": checks,
            },
        )
    return events


def _duration_ms(record: dict[str, Any]) -> int:
    """历史记录的耗时；缺少起止时间时给 0。"""
    started, finished = record.get("started_at"), record.get("finished_at")
    if not started or not finished:
        return 0
    try:
        delta = datetime.fromisoformat(finished) - datetime.fromisoformat(started)
    except ValueError:
        return 0
    return max(0, int(delta.total_seconds() * 1000))


async def _replay_stream(events: list[dict[str, Any]]) -> AsyncIterator[str]:
    for event in events:
        yield _format_sse(event)


async def _event_stream(
    job_id: str,
    job: jobs.Job,
    last_event_id: int | None,
) -> AsyncIterator[str]:
    """SSE 生成器：回放 → （必要时合成终态）→ 实时推送 / 心跳。"""
    subscription = bus.subscribe(job_id, last_event_id)
    try:
        cached = bus.get_events(job_id)
        last_cached_seq = cached[-1]["seq"] if cached else 0
        cached_has_terminal = any(
            event.get("type") in _TERMINAL_EVENT_TYPES for event in cached
        )

        for event in subscription.replay:
            yield _format_sse(event)

        # job 已终结（总线已 close，或磁盘读回后 status 就是 done/error），但
        # 缓存里没有终态事件时补发一条，然后立即收流，不让客户端空等。
        if (
            not cached_has_terminal
            and (bus.is_closed(job_id) or job.status in ("done", "error"))
        ):
            synthetic = _synthetic_terminal_event(job, last_cached_seq + 1)
            if synthetic is not None:
                yield _format_sse(synthetic)
            return

        while True:
            try:
                event = await asyncio.wait_for(
                    subscription.queue.get(), timeout=_SSE_PING_INTERVAL
                )
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if event is None:
                return
            yield _format_sse(event)
    finally:
        # 客户端断开（CancelledError）或正常结束都走这里，取消订阅。
        subscription.close()


# --------------------------------------------------------------- 外部 agent 鉴权


def require_agent_key(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    """校验外部 agent 的服务端密钥。

    未配置 VERIDROP_AGENT_API_KEY → 503（接口整体关闭）；缺失或错误 → 401。
    比较用 compare_digest，避免时序侧信道。
    """
    expected = os.environ.get("VERIDROP_AGENT_API_KEY")
    if not expected:
        raise HTTPException(status_code=503, detail="agent api disabled")

    provided = (x_api_key or "").strip()
    if not provided and authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer":
            provided = token.strip()

    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=401, detail="invalid or missing api key"
        )


router = APIRouter()
agent_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_agent_key)])


# ------------------------------------------------------------------ 请求模型


def _strip_nonempty(value: str) -> str:
    """去空白后必须非空;失败由 pydantic 转 422。"""
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be empty")
    return stripped


def _validate_base_url(value: str) -> str:
    """只接受 http(s) 前缀;协议之外不做更严格的结构校验。"""
    stripped = _strip_nonempty(value)
    if not stripped.startswith(("http://", "https://")):
        raise ValueError("base_url must start with http:// or https://")
    return stripped


def _validate_name(value: str) -> str:
    stripped = _strip_nonempty(value)
    if len(stripped) > 64:
        raise ValueError("name must be at most 64 characters")
    return stripped


class DetectRequest(BaseModel):
    """前端提交检测的 JSON body（凭据引用 + 模型 + 探针选择）。

    base_url / api_key 已从请求体移除:检测对象只能是已保存的凭据,明文 key
    不再经过前端。
    """

    credential_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    include_long_context: bool = False
    include_long_context_extreme: bool = False
    # 深度诊断(13–25 项)手动开关,默认关闭。
    deep: bool = False
    # 用户手动勾选的探针名;缺失或空列表由路由层统一返回 422。
    probes: list[str] = Field(default_factory=list)

    @field_validator("credential_id", "model")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        """去空白后再校验非空；校验失败交给 FastAPI 默认 422。"""
        return _strip_nonempty(value)


class CredentialCreate(BaseModel):
    """新建凭据的请求体；api_key 只在此处出现一次,之后只存密文与掩码。"""

    name: str
    base_url: str
    api_key: str

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _validate_name(value)

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        return _validate_base_url(value)

    @field_validator("api_key")
    @classmethod
    def _check_api_key(cls, value: str) -> str:
        return _strip_nonempty(value)


class CredentialUpdate(BaseModel):
    """PATCH 凭据:全部可选,但至少要传一项。"""

    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str | None) -> str | None:
        return None if value is None else _validate_name(value)

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_base_url(value)

    @field_validator("api_key")
    @classmethod
    def _check_api_key(cls, value: str | None) -> str | None:
        return None if value is None else _strip_nonempty(value)

    @model_validator(mode="after")
    def _require_one(self) -> "CredentialUpdate":
        if self.name is None and self.base_url is None and self.api_key is None:
            raise ValueError("at least one field is required")
        return self


def _protocol_from_model(model: str) -> str:
    """按模型名前缀推导协议,与前端下拉的语义保持一致。"""
    name = model.strip().lower()
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith("gemini"):
        return "gemini"
    if name.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    return "anthropic"


# ---------------------------------------------------------- /app/api/credentials


async def _load_credential_secret(credential_id: str) -> dict[str, Any]:
    """取含密文的凭据;库不可用 503,不存在 404。"""
    try:
        return await repository.get_credential_secret(credential_id)  # type: ignore[return-value]
    except SQLAlchemyError as exc:
        logger.warning("db read failed: %s", exc)
        raise HTTPException(status_code=503, detail="database unavailable")


@router.get("/app/api/credentials")
async def app_credentials() -> dict[str, Any]:
    try:
        items = await repository.list_credentials()
    except SQLAlchemyError as exc:
        logger.warning("db read failed: %s", exc)
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"items": items}


@router.post("/app/api/credentials", status_code=201)
async def app_create_credential(payload: CredentialCreate) -> dict[str, Any]:
    encrypted = encrypt_api_key(payload.api_key)
    masked = mask_key(payload.api_key)
    try:
        return await repository.create_credential(
            name=payload.name,
            base_url=payload.base_url,
            api_key_enc=encrypted,
            api_key_masked=masked,
        )
    except IntegrityError:
        raise HTTPException(
            status_code=409, detail="credential name already exists"
        )
    except SQLAlchemyError as exc:
        logger.warning("db write failed: %s", exc)
        raise HTTPException(status_code=503, detail="database unavailable")


@router.patch("/app/api/credentials/{credential_id}")
async def app_update_credential(
    credential_id: str, payload: CredentialUpdate
) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if payload.name is not None:
        updates["name"] = payload.name
    if payload.base_url is not None:
        updates["base_url"] = payload.base_url
    if payload.api_key is not None:
        # 换 key 时密文与掩码必须一起刷新,否则展示与解密会指向不同的 key。
        updates["api_key_enc"] = encrypt_api_key(payload.api_key)
        updates["api_key_masked"] = mask_key(payload.api_key)

    try:
        credential = await repository.update_credential(
            credential_id, **updates
        )
    except IntegrityError:
        raise HTTPException(
            status_code=409, detail="credential name already exists"
        )
    except SQLAlchemyError as exc:
        logger.warning("db write failed: %s", exc)
        raise HTTPException(status_code=503, detail="database unavailable")

    if credential is None:
        raise HTTPException(status_code=404, detail="credential not found")
    return credential


@router.delete("/app/api/credentials/{credential_id}", status_code=204)
async def app_delete_credential(credential_id: str) -> Response:
    try:
        deleted = await repository.delete_credential(credential_id)
    except SQLAlchemyError as exc:
        logger.warning("db write failed: %s", exc)
        raise HTTPException(status_code=503, detail="database unavailable")
    if not deleted:
        raise HTTPException(status_code=404, detail="credential not found")
    return Response(status_code=204)


@router.post("/app/api/credentials/{credential_id}/models")
async def app_credential_models(credential_id: str) -> dict[str, Any]:
    """用已保存的凭据调用中转站 /models(服务端代发,前端拿不到明文 key)。"""
    credential = await _load_credential_secret(credential_id)
    if credential is None:
        raise HTTPException(status_code=404, detail="credential not found")

    try:
        plain_key = decrypt_api_key(credential["api_key_enc"])
    except CredentialDecryptError:
        raise HTTPException(
            status_code=503, detail="credential cannot be decrypted"
        )

    result = await probe.probe_relay(credential["base_url"], plain_key)
    try:
        await repository.touch_credential(credential_id)
    except SQLAlchemyError as exc:
        # 记录使用时间失败不影响本次结果。
        logger.warning("touch credential failed: %s", exc)
    return result


# ---------------------------------------------------------- /app/api/probes


@router.get("/app/api/probes")
async def app_probes(
    protocol: Literal["anthropic", "openai", "gemini"] = Query(...),
    model: str | None = Query(default=None),
) -> dict[str, Any]:
    """探针目录:分类与探针清单(含 applies 适用标记)。"""
    try:
        return build_catalog(protocol, model or None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# ---------------------------------------------------------- /app/api/detect


def _validate_selected_probes(
    probes: list[str], protocol: str, model: str
) -> list[str]:
    """校验用户勾选的探针名:至少一个,且必须是该协议目录内的已知探针。"""
    if not probes:
        raise HTTPException(status_code=422, detail="至少选择一个探针")
    known = {p["name"] for p in build_catalog(protocol, model)["probes"]}
    for name in probes:
        if name not in known:
            raise HTTPException(
                status_code=422, detail=f"unknown probe: {name}"
            )
    return probes


@router.post("/app/api/detect")
async def app_detect(payload: DetectRequest) -> dict[str, str]:
    credential = await _load_credential_secret(payload.credential_id)
    if credential is None:
        raise HTTPException(status_code=404, detail="credential not found")

    try:
        plain_key = decrypt_api_key(credential["api_key_enc"])
    except CredentialDecryptError:
        raise HTTPException(
            status_code=503, detail="credential cannot be decrypted"
        )

    protocol = _protocol_from_model(payload.model)
    selected_probes = _validate_selected_probes(
        payload.probes, protocol, payload.model
    )

    # 明文 key 只作为局部变量传给 jobs;Job 结束后即丢弃,不入库。
    job_id = await jobs.submit(
        credential["base_url"],
        plain_key,
        payload.model,
        protocol=protocol,
        include_long_context=payload.include_long_context,
        include_long_context_extreme=payload.include_long_context_extreme,
        credential_id=credential["id"],
        credential_name=credential["name"],
        deep=payload.deep,
        selected_probes=selected_probes,
    )
    return {"job_id": job_id, "status": "queued"}


@router.get("/app/api/detect/{job_id}")
async def app_detect_status(job_id: str) -> dict[str, Any]:
    job = await jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    report = job.report if isinstance(job.report, dict) else None
    return {
        "job_id": job.id,
        "status": job.status,
        "protocol": job.protocol,
        "base_url": job.base_url,
        "target_model": job.target_model,
        # epoch 秒（float），与 Job 字段一致。
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "error": job.error,
        "progress": _progress_for_job(job_id, report),
        # 深测状态:running/done/error/skipped;磁盘回落的旧 job 为 null。
        "deep": bool(job.deep),
        "deep_status": job.deep_status,
        "total_score": report.get("total_score") if report else None,
        "verdict": report.get("verdict") if report else None,
        "risk_level": _risk_level_from_report(report),
        "summary": report.get("summary") if report else None,
    }


@router.get("/app/api/detect/{job_id}/events")
async def app_detect_events(
    job_id: str,
    last_event_id_header: str | None = Header(default=None, alias="Last-Event-ID"),
    last_event_id: int | None = Query(default=None),
) -> StreamingResponse:
    # 历史记录优先从数据库还原:磁盘 JSON 只含终态,无法回放探针与深测事件。
    # 仅内存中仍存活(运行中或刚结束、总线仍有缓存)的任务走实时流。
    if not jobs.is_live(job_id) and not bus.get_events(job_id):
        try:
            record = await repository.get_detection(job_id)
        except SQLAlchemyError:
            record = None
        if record is not None:
            return StreamingResponse(
                _replay_stream(_db_replay_events(record)),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

    job = await jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    # EventSource 断线重连会自动带 Last-Event-ID；query 参数作为手工调试的
    # 兼容入口，两者都有时以 header 为准。
    resume_from = _parse_last_event_id(last_event_id_header)
    if resume_from is None:
        resume_from = last_event_id

    return StreamingResponse(
        _event_stream(job_id, job, resume_from),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------- 报告读取公共逻辑


async def _reports_page(
    *,
    protocol: str | None,
    model: str | None,
    verdict: str | None,
    min_score: float | None,
    limit: int,
    offset: int,
    credential_id: str | None = None,
) -> dict[str, Any]:
    """分页列表；数据库不可用统一 503，不暴露 500。"""
    try:
        items = await repository.list_detections(
            protocol=protocol,
            model=model,
            verdict=verdict,
            min_score=min_score,
            limit=limit,
            offset=offset,
            credential_id=credential_id,
        )
        total = await repository.count_detections(
            protocol=protocol,
            model=model,
            verdict=verdict,
            min_score=min_score,
            credential_id=credential_id,
        )
    except SQLAlchemyError as exc:
        logger.warning("db read failed: %s", exc)
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    """补齐报告详情的契约字段，缺失值给 null / [] / {}。"""
    out = dict(record)
    base_url = str(out.get("base_url") or "")
    out["host"] = out.get("host") or _host_from_url(base_url)
    out["detected_brands"] = list(out.get("detected_brands") or [])
    out["performance"] = out.get("performance") or {}
    out["probes"] = list(out.get("probes") or [])

    report = out.get("report")
    out["report"] = report if isinstance(report, dict) else None
    # 本次实际参与的探针名:报告内有则透出,旧报告统一给空列表。
    out["selected_probes"] = list(
        (report.get("selected_probes") if isinstance(report, dict) else None)
        or []
    )
    # 深测结果:旧报告(磁盘回落 / 未开启深测)没有这两个字段,统一给 null。
    deep_report = out.get("deep_report")
    out["deep_report"] = deep_report if isinstance(deep_report, dict) else None
    out.setdefault("deep_status", None)
    if out.get("risk_level") is None and isinstance(report, dict):
        out["risk_level"] = _risk_level_from_report(report)

    # 磁盘回落的旧 job 没有交换快照,统一给 0 保证字段类型稳定。
    out["exchange_count"] = int(out.get("exchange_count") or 0)

    for key in (
        "id",
        "protocol",
        "base_url",
        "target_model",
        "status",
        "api_key_masked",
        "total_score",
        "verdict",
        "summary",
        "run_error",
        "self_reported_identity",
        "created_at",
        "started_at",
        "finished_at",
        "total_probes",
        "passed_probes",
        "failed_probes",
        "credential_id",
        "credential_name",
    ):
        out.setdefault(key, None)
    return out


def _record_from_job(job: jobs.Job) -> dict[str, Any]:
    """把磁盘回落的 Job 组装成与数据库记录同形状的 dict。"""
    report = job.report if isinstance(job.report, dict) else {}
    results = _report_results(report)
    total, passed, failed = _probe_counts(results)
    timestamp = report.get("timestamp")
    base_url = job.base_url or str(report.get("base_url") or "")

    return {
        "id": job.id,
        "protocol": job.protocol or report.get("protocol") or "anthropic",
        "base_url": base_url,
        "host": _host_from_url(base_url),
        "target_model": job.target_model or report.get("target_model") or "",
        "status": job.status,
        "api_key_masked": report.get("api_key_masked"),
        "total_score": report.get("total_score"),
        "verdict": report.get("verdict"),
        "risk_level": _risk_level_from_report(report),
        "summary": report.get("summary") or "",
        "run_error": report.get("run_error") or job.error,
        "self_reported_identity": report.get("self_reported_identity"),
        "detected_brands": list(
            report.get("detected_non_anthropic_brands") or []
        ),
        "performance": report.get("performance") or {},
        "report": report,
        "probes": [
            {
                "id": None,
                "name": r.get("name"),
                "display_name": r.get("display_name"),
                "status": r.get("status"),
                "score": r.get("score"),
                "weight": r.get("weight"),
                "duration_ms": r.get("duration_ms"),
                "error": r.get("error"),
                "details": r.get("details") or {},
            }
            for r in results
        ],
        # ISO 字符串：报告内的 timestamp 就是 datetime 的 ISO 序列化。
        "created_at": timestamp,
        "started_at": None,
        "finished_at": timestamp,
        "total_probes": total,
        "passed_probes": passed,
        "failed_probes": failed,
        "credential_id": job.credential_id,
        "credential_name": job.credential_name,
        # 磁盘回落的旧 job 没有深测结果。
        "deep_report": None,
        "deep_status": None,
    }


def _deep_summary(record: dict[str, Any]) -> dict[str, Any] | None:
    """把 deep_report 压缩成 agent 友好的摘要;没有深测返回 None。"""
    deep_report = record.get("deep_report")
    if not isinstance(deep_report, dict):
        return None
    checks_raw = deep_report.get("checks")
    checks: list[dict[str, Any]] = []
    if isinstance(checks_raw, list):
        for item in checks_raw:
            if not isinstance(item, dict):
                continue
            checks.append(
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "status": item.get("status"),
                    "value": item.get("value"),
                    "summary": item.get("summary"),
                }
            )
    return {"status": deep_report.get("status"), "checks": checks}


async def _load_report(report_id: str) -> dict[str, Any] | None:
    """先查库；库里没有（或库不可用）时回落到磁盘 JSON。"""
    record: dict[str, Any] | None = None
    try:
        record = await repository.get_detection(report_id)
    except SQLAlchemyError as exc:
        # 列表接口按契约 503；详情接口回落磁盘更有用，也避免 500。
        logger.warning("db read failed: %s", exc)

    if record is not None:
        return _normalize_record(record)

    job = await jobs.get(report_id)
    if job is None or not isinstance(job.report, dict):
        return None
    return _normalize_record(_record_from_job(job))


async def _exchanges_payload(
    report_id: str, probe: str | None
) -> dict[str, Any]:
    """检测交换快照的统一响应体（/app 与 /api/v1 共用）。

    报告不存在（库与磁盘回落都没有）→ 404；老报告没有采集 → 空列表。
    """
    record = await _load_report(report_id)
    if record is None:
        raise HTTPException(status_code=404, detail="report not found")
    try:
        items = await repository.list_exchanges(report_id, probe_name=probe)
    except SQLAlchemyError as exc:
        logger.warning("db read failed: %s", exc)
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"items": items, "total": len(items)}


# --------------------------------------------------------------- /app/api/reports


@router.get("/app/api/reports")
async def app_reports(
    protocol: str | None = Query(default=None),
    model: str | None = Query(default=None),
    verdict: str | None = Query(default=None),
    min_score: float | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    credential_id: str | None = Query(default=None),
) -> dict[str, Any]:
    return await _reports_page(
        protocol=protocol,
        model=model,
        verdict=verdict,
        min_score=min_score,
        limit=limit,
        offset=offset,
        credential_id=credential_id,
    )


@router.get("/app/api/reports/{report_id}")
async def app_report_detail(report_id: str) -> dict[str, Any]:
    record = await _load_report(report_id)
    if record is None:
        raise HTTPException(status_code=404, detail="report not found")
    return record


_DEEP_NEEDS_CREDENTIAL = "该记录的凭据已删除，无法做深度诊断，请重新检测。"


@router.post("/app/api/reports/{report_id}/deep", status_code=202)
async def app_report_deep(report_id: str) -> dict[str, str]:
    """对一条已完成的历史检测补跑深度诊断(13–25 项)。

    先回放原记录的探针结果与结论,再跑深测,结果补写回原记录。
    凭据仍在时才能解密 Key;凭据被删除则拒绝。
    """
    try:
        record = await repository.get_detection(report_id)
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="database unavailable")
    if record is None:
        raise HTTPException(status_code=404, detail="report not found")
    if record.get("status") != "done" or not record.get("probes"):
        raise HTTPException(status_code=409, detail="只能对已完成且有探针结果的检测做深度诊断")

    cred_id = record.get("credential_id")
    credential = await _load_credential_secret(cred_id) if cred_id else None
    if credential is None:
        raise HTTPException(status_code=409, detail=_DEEP_NEEDS_CREDENTIAL)
    try:
        plain_key = decrypt_api_key(credential["api_key_enc"])
    except CredentialDecryptError:
        raise HTTPException(status_code=503, detail="credential cannot be decrypted")

    # 回放原记录的事件;终态 job_finished 需声明本次深测已开启,前端才会继续等待 deep_finished。
    replay: list[tuple[str, dict[str, Any]]] = []
    for event in _db_replay_events(record, include_deep=False):
        payload = event["payload"]
        if event["type"] == "job_finished":
            payload = {**payload, "deep_enabled": True, "deep_status": "running"}
        replay.append((event["type"], payload))

    job_id = await jobs.submit_deep_only(
        report_id,
        credential["base_url"],
        plain_key,
        record["target_model"],
        record["protocol"],
        main_report=record.get("report"),
        replay_events=replay,
        credential_id=credential["id"],
        credential_name=credential["name"],
    )
    return {"job_id": job_id, "status": "queued", "source_report_id": report_id}


@router.get("/app/api/reports/{report_id}/exchanges")
async def app_report_exchanges(
    report_id: str,
    probe: str | None = Query(default=None),
) -> dict[str, Any]:
    """按探针查看该检测采集到的请求/响应交换（完整 body）。"""
    return await _exchanges_payload(report_id, probe)


# ------------------------------------------------------------------- /api/v1


@agent_router.get("/reports")
async def agent_reports(
    protocol: str | None = Query(default=None),
    model: str | None = Query(default=None),
    verdict: str | None = Query(default=None),
    min_score: float | None = Query(default=None, ge=0, le=100),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    return await _reports_page(
        protocol=protocol,
        model=model,
        verdict=verdict,
        min_score=min_score,
        limit=limit,
        offset=offset,
    )


@agent_router.get("/reports/{report_id}")
async def agent_report_detail(report_id: str) -> dict[str, Any]:
    record = await _load_report(report_id)
    if record is None:
        raise HTTPException(status_code=404, detail="report not found")
    return record


@agent_router.get("/reports/{report_id}/exchanges")
async def agent_report_exchanges(
    report_id: str,
    probe: str | None = Query(default=None),
) -> dict[str, Any]:
    """外部 agent 只读接口：与 /app/api/reports/{id}/exchanges 同形状。"""
    return await _exchanges_payload(report_id, probe)


@agent_router.get("/reports/{report_id}/analysis")
async def agent_report_analysis(report_id: str) -> dict[str, Any]:
    record = await _load_report(report_id)
    if record is None:
        raise HTTPException(status_code=404, detail="report not found")

    probes = record.get("probes") or []
    analysis_probes: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    failed_probes: list[Any] = []

    for probe in probes:
        if not isinstance(probe, dict):
            continue
        details = probe.get("details")
        if not isinstance(details, dict):
            details = {}
        severities: list[str] = []

        for issue in details.get("issues") or []:
            severity: str | None = None
            message = ""
            if isinstance(issue, dict):
                value = issue.get("severity")
                if isinstance(value, str) and value:
                    severity = value
                    severities.append(value)
                # message 字段名在历史报告里不统一，按优先级容错。
                for key in ("message", "detail"):
                    candidate = issue.get(key)
                    if isinstance(candidate, str) and candidate.strip():
                        message = candidate.strip()
                        break
                if not message:
                    message = str(issue)
            else:
                message = str(issue)
            issues.append(
                {"probe": probe.get("name"), "severity": severity, "message": message}
            )

        if probe.get("status") in ("fail", "error"):
            failed_probes.append(probe.get("name"))

        analysis_probes.append(
            {
                "name": probe.get("name"),
                "display_name": probe.get("display_name"),
                "status": probe.get("status"),
                "score": probe.get("score"),
                "weight": probe.get("weight"),
                "duration_ms": probe.get("duration_ms"),
                "error": probe.get("error"),
                "issue_severities": severities,
            }
        )

    return {
        "id": record.get("id"),
        "protocol": record.get("protocol"),
        "host": record.get("host") or _host_from_url(
            str(record.get("base_url") or "")
        ),
        "target_model": record.get("target_model"),
        "status": record.get("status"),
        "created_at": record.get("created_at"),
        "score": {
            "total": record.get("total_score"),
            "verdict": record.get("verdict"),
            "risk_level": record.get("risk_level"),
            "summary": record.get("summary"),
        },
        "identity": {
            "self_reported": record.get("self_reported_identity"),
            "detected_brands": record.get("detected_brands") or [],
        },
        "performance": record.get("performance") or {},
        "credential": {
            "id": record.get("credential_id"),
            "name": record.get("credential_name"),
        },
        "probes": analysis_probes,
        "issues": issues,
        "failed_probes": failed_probes,
        # 本次显式勾选/实际参与的探针名;旧报告没有该字段时为 []。
        "selected_probes": record.get("selected_probes") or [],
        "exchange_count": int(record.get("exchange_count") or 0),
        # 深度诊断摘要(13–25 项);未开启深测时为 null。
        "deep_summary": _deep_summary(record),
        "report_url": f"/app/reports/{record.get('id') or report_id}",
    }


# 把 agent 路由并进主 router，server.py 只需要 include 一次。
router.include_router(agent_router)


# ------------------------------------------------------------ /app 前端挂载


def _frontend_index() -> FileResponse:
    if not INDEX_FILE.is_file():
        raise HTTPException(status_code=503, detail=_FRONTEND_MISSING)
    return FileResponse(INDEX_FILE, media_type="text/html")


def _safe_app_path(relative: str) -> Path | None:
    """把 /app 下的相对路径解析成构建目录内的真实文件；越界返回 None。

    resolve() 会展开 ``..`` 和软链接，再 relative_to 校验，双重防止目录穿越；
    不存在或不是普通文件同样返回 None（交给 index.html 兜底）。
    """
    if not relative:
        return None
    base = APP_DIR.resolve()
    try:
        candidate = (base / relative).resolve()
    except (OSError, ValueError):
        return None
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def mount_frontend(app: Any) -> None:
    """挂载 React 构建产物到 /app。

    注册顺序很关键：本函数必须在 ``app.include_router(router)`` 之后调用，
    这样 ``/app/api/*`` 先匹配；``/app/assets`` 挂载又必须早于
    ``/app/{path:path}`` 兜底，否则静态资源会被兜底路由吞掉。
    """
    assets_dir = APP_DIR / "assets"
    if assets_dir.is_dir():
        app.mount(
            "/app/assets",
            StaticFiles(directory=str(assets_dir)),
            name="app-assets",
        )

    @app.get("/app", include_in_schema=False)
    async def app_index() -> FileResponse:
        return _frontend_index()

    @app.get("/app/{path:path}", include_in_schema=False)
    async def app_static_or_index(path: str) -> FileResponse:
        target = _safe_app_path(path)
        if target is not None:
            return FileResponse(target)
        # 未命中的深链（/app/reports/xxx）统一返回 index.html，交给前端路由。
        return _frontend_index()
