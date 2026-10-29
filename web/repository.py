"""检测结果的读写仓储 —— 只对上层暴露普通 dict。

设计约定:
- 所有函数都是 async,内部用 `db.get_session()` 拿会话,调用方不感知 Session。
- 绝不把 ORM 对象传出去:async 会话关闭后再访问 ORM 属性会踩 lazy-load 报错,
  而且上层的 API 层只想要能直接 json 化的结构。
- 时间统一序列化成带 UTC 偏移的 ISO 8601 字符串。SQLite 的 DateTime 列不保留
  tzinfo,读回来是 naive 的;在这里补齐时区,让上层拿到的时间戳含义明确,同时
  保证 dict 可以直接丢进 JSONResponse。
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import delete, func, select

from .db import (
    Credential,
    Detection,
    ProbeExchange,
    ProbeResult,
    get_session,
)


def _host_from_url(base_url: str) -> str:
    """从 base_url 推导域名;解析不出来就落空字符串(列非空)。"""
    try:
        hostname = urlparse(base_url).hostname
    except ValueError:
        # 畸形 URL(比如非法端口)会让 urlparse 在访问 hostname 时抛错。
        hostname = None
    return hostname or ""


def _iso(value: datetime | None) -> str | None:
    """datetime → ISO 8601 字符串;naive 时间按 UTC 补齐。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_from_ts(ts: Any) -> datetime:
    """epoch 秒 → 带时区的 datetime;缺失或非法时退回当前时间。"""
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    return _now()


def _detection_filters(
    protocol: str | None,
    model: str | None,
    verdict: str | None,
    min_score: float | None,
    credential_id: str | None = None,
) -> list[Any]:
    """列表/计数共用的过滤条件,保证两边永远对得上。"""
    conditions: list[Any] = []
    if protocol:
        conditions.append(Detection.protocol == protocol)
    if model:
        conditions.append(Detection.target_model == model)
    if verdict:
        conditions.append(Detection.verdict == verdict)
    if min_score is not None:
        conditions.append(Detection.total_score >= min_score)
    if credential_id:
        conditions.append(Detection.credential_id == credential_id)
    return conditions


def _probe_to_dict(probe: ProbeResult) -> dict[str, Any]:
    return {
        "id": probe.id,
        "name": probe.name,
        "display_name": probe.display_name,
        "status": probe.status,
        "score": probe.score,
        "weight": probe.weight,
        "duration_ms": probe.duration_ms,
        "error": probe.error,
        "details": probe.details,
    }


def _json_safe(value: Any) -> Any:
    """把任意对象降级成可 JSON 序列化的结构。

    探针采集到的是 httpx/json 原生对象,理论上都能序列化;但防御性兜底
    成本极低,且能保证「一条坏数据」绝不会让整次入库失败(那会连带丢掉
    报告与探针结果)。
    """
    if value is None:
        return None
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:  # noqa: BLE001
        try:
            return str(value)
        except Exception:  # noqa: BLE001
            return None


def _exchange_to_dict(exchange: ProbeExchange) -> dict[str, Any]:
    return {
        "id": exchange.id,
        "probe_name": exchange.probe_name,
        "seq": exchange.seq,
        "request": exchange.request,
        "response": exchange.response,
        "events": exchange.events,
        "error": exchange.error,
        "stream": exchange.stream,
        "latency_ms": exchange.latency_ms,
        "status_code": exchange.status_code,
        "created_at": _iso(exchange.created_at),
    }


def _credential_to_dict(cred: Credential) -> dict[str, Any]:
    """凭据的公开视图:绝不含 api_key_enc,更不含明文。"""
    return {
        "id": cred.id,
        "name": cred.name,
        "base_url": cred.base_url,
        "host": cred.host,
        "api_key_masked": cred.api_key_masked,
        "created_at": _iso(cred.created_at),
        "updated_at": _iso(cred.updated_at),
        "last_used_at": _iso(cred.last_used_at),
    }


def _credential_secret_dict(cred: Credential) -> dict[str, Any]:
    """内部视图:附带 Fernet 密文,仅供调用方解密出请求用的 key。"""
    return {**_credential_to_dict(cred), "api_key_enc": cred.api_key_enc}


async def create_credential(
    *,
    name: str,
    base_url: str,
    api_key_enc: str,
    api_key_masked: str,
) -> dict[str, Any]:
    """新建凭据。名称冲突由唯一约束抛 IntegrityError,路由层转 409。"""
    now = _now()
    cred = Credential(
        # 12 字符 URL-safe id,足够避免碰撞又短。
        id=secrets.token_urlsafe(9),
        name=name,
        base_url=base_url,
        host=_host_from_url(base_url),
        api_key_enc=api_key_enc,
        api_key_masked=api_key_masked,
        created_at=now,
        updated_at=now,
        last_used_at=None,
    )

    async with get_session() as session:
        async with session.begin():
            session.add(cred)
        # 在会话内组装返回值,避免脱离 ORM 对象后访问属性。
        result = _credential_to_dict(cred)

    return result


async def list_credentials() -> list[dict[str, Any]]:
    """按最近更新倒序列出全部凭据(公开视图)。"""
    statement = select(Credential).order_by(Credential.updated_at.desc())

    async with get_session() as session:
        rows = (await session.execute(statement)).scalars().all()
        return [_credential_to_dict(cred) for cred in rows]


async def get_credential(credential_id: str) -> dict[str, Any] | None:
    """公开视图的单个凭据;不存在返回 None。"""
    async with get_session() as session:
        cred = await session.get(Credential, credential_id)
        if cred is None:
            return None
        return _credential_to_dict(cred)


async def get_credential_secret(credential_id: str) -> dict[str, Any] | None:
    """内部视图(含 api_key_enc);只给需要解密的地方用。"""
    async with get_session() as session:
        cred = await session.get(Credential, credential_id)
        if cred is None:
            return None
        return _credential_secret_dict(cred)


async def update_credential(
    credential_id: str,
    *,
    name: str | None = None,
    base_url: str | None = None,
    api_key_enc: str | None = None,
    api_key_masked: str | None = None,
) -> dict[str, Any] | None:
    """只更新传入的字段并刷新 updated_at;不存在返回 None。"""
    async with get_session() as session:
        async with session.begin():
            cred = await session.get(Credential, credential_id)
            if cred is None:
                return None

            if name is not None:
                cred.name = name
            if base_url is not None:
                cred.base_url = base_url
                cred.host = _host_from_url(base_url)
            if api_key_enc is not None:
                cred.api_key_enc = api_key_enc
            if api_key_masked is not None:
                cred.api_key_masked = api_key_masked
            cred.updated_at = _now()

            return _credential_to_dict(cred)


async def delete_credential(credential_id: str) -> bool:
    """删除凭据;返回是否真的删掉了一条。"""
    async with get_session() as session:
        async with session.begin():
            result = await session.execute(
                delete(Credential).where(Credential.id == credential_id)
            )
            return bool(result.rowcount)


async def touch_credential(credential_id: str) -> None:
    """记录最近一次使用时间;凭据不存在时静默返回。"""
    async with get_session() as session:
        async with session.begin():
            cred = await session.get(Credential, credential_id)
            if cred is None:
                return None
            cred.last_used_at = _now()
    return None


async def update_deep_report(
    detection_id: str,
    deep_report: dict[str, Any],
    deep_status: str,
) -> bool:
    """给已存在的检测补写深度诊断结果;记录不存在返回 False。"""
    async with get_session() as session:
        async with session.begin():
            detection = await session.get(Detection, detection_id)
            if detection is None:
                return False
            detection.deep_report = _json_safe(deep_report)
            detection.deep_status = deep_status
    return True


async def save_detection(
    detection_id: str,
    *,
    protocol: str,
    base_url: str,
    target_model: str,
    api_key_masked: str,
    status: str,
    created_at: datetime,
    started_at: datetime | None,
    finished_at: datetime | None,
    total_score: float | None = None,
    verdict: str | None = None,
    risk_level: str | None = None,
    summary: str = "",
    run_error: str | None = None,
    self_reported_identity: str | None = None,
    detected_brands: list[str] | None = None,
    performance: dict[str, Any] | None = None,
    report: dict[str, Any] | None = None,
    probes: list[dict[str, Any]] | None = None,
    total_probes: int = 0,
    passed_probes: int = 0,
    failed_probes: int = 0,
    credential_id: str | None = None,
    credential_name: str | None = None,
    exchanges: list[dict[str, Any]] | None = None,
    deep_report: dict[str, Any] | None = None,
    deep_status: str | None = None,
) -> None:
    """写入/覆盖一条检测记录及其探针结果(幂等)。

    典型调用顺序是「先写一行 status=running 的占位记录,跑完再写一次终态」,
    所以这里走「先删旧探针 → 存在则更新全部字段,不存在则插入 → 批量插探针」,
    整个流程包在一个事务里,不会出现只有半条记录的中间态。
    """
    values: dict[str, Any] = {
        "protocol": protocol,
        "base_url": base_url,
        "host": _host_from_url(base_url),
        "api_key_masked": api_key_masked,
        "target_model": target_model,
        # 检测模式已废弃:列保留以兼容旧库(NOT NULL),新记录统一写空串。
        "mode": "",
        "status": status,
        "total_score": total_score,
        "verdict": verdict,
        "risk_level": risk_level,
        "summary": summary or "",
        "run_error": run_error,
        "self_reported_identity": self_reported_identity,
        # JSON 列不能是 None:模型上声明了非空,这里统一兜底成空容器。
        "detected_brands": list(detected_brands or []),
        "performance": dict(performance or {}),
        "report": report,
        "total_probes": total_probes,
        "passed_probes": passed_probes,
        "failed_probes": failed_probes,
        "credential_id": credential_id,
        "credential_name": credential_name,
        "deep_report": deep_report,
        "deep_status": deep_status,
        "created_at": created_at,
        "started_at": started_at,
        "finished_at": finished_at,
    }

    async with get_session() as session:
        async with session.begin():
            await session.execute(
                delete(ProbeResult).where(ProbeResult.detection_id == detection_id)
            )
            # 旧交换快照随新一次写入整体替换:占位记录(status=running)也会走
            # 这里,先清空可避免残留上一次运行的 body。
            await session.execute(
                delete(ProbeExchange).where(
                    ProbeExchange.detection_id == detection_id
                )
            )

            existing = await session.get(Detection, detection_id)
            if existing is None:
                session.add(Detection(id=detection_id, **values))
            else:
                for key, value in values.items():
                    setattr(existing, key, value)

            if probes:
                session.add_all(
                    [
                        ProbeResult(
                            detection_id=detection_id,
                            name=str(p.get("name") or ""),
                            display_name=str(p.get("display_name") or ""),
                            status=str(p.get("status") or ""),
                            score=float(p.get("score") or 0.0),
                            weight=(
                                1.0
                                if p.get("weight") is None
                                else float(p["weight"])
                            ),
                            duration_ms=p.get("duration_ms"),
                            error=p.get("error"),
                            details=p.get("details") or {},
                        )
                        for p in probes
                    ]
                )

            if exchanges:
                session.add_all(
                    [
                        ProbeExchange(
                            detection_id=detection_id,
                            probe_name=str(e.get("probe") or "unknown")[:64],
                            seq=index,
                            request=_json_safe(e.get("request")),
                            response=_json_safe(e.get("response")),
                            events=_json_safe(e.get("events")),
                            error=_json_safe(e.get("error")),
                            stream=bool(e.get("stream")),
                            latency_ms=e.get("latency_ms"),
                            status_code=e.get("status_code"),
                            created_at=_utc_from_ts(e.get("ts")),
                        )
                        for index, e in enumerate(exchanges, start=1)
                    ]
                )


async def list_detections(
    protocol: str | None = None,
    model: str | None = None,
    verdict: str | None = None,
    min_score: float | None = None,
    limit: int = 20,
    offset: int = 0,
    credential_id: str | None = None,
) -> list[dict[str, Any]]:
    """按创建时间倒序列出检测摘要(不含 report / performance 这类大字段)。"""
    # 相关子查询拿 exchange 数量:列表页只需要一个计数,不读 body,避免 N+1。
    exchange_count = (
        select(func.count(ProbeExchange.id))
        .where(ProbeExchange.detection_id == Detection.id)
        .correlate(Detection)
        .scalar_subquery()
        .label("exchange_count")
    )
    statement = (
        select(
            Detection.id,
            Detection.protocol,
            Detection.host,
            Detection.base_url,
            Detection.target_model,
            Detection.status,
            Detection.total_score,
            Detection.verdict,
            Detection.risk_level,
            Detection.summary,
            Detection.created_at,
            Detection.finished_at,
            Detection.total_probes,
            Detection.passed_probes,
            Detection.failed_probes,
            Detection.credential_id,
            Detection.credential_name,
            Detection.deep_status,
            exchange_count,
        )
        .where(
            *_detection_filters(
                protocol, model, verdict, min_score, credential_id
            )
        )
        .order_by(Detection.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    async with get_session() as session:
        rows = (await session.execute(statement)).mappings().all()

    return [
        {
            **dict(row),
            "created_at": _iso(row["created_at"]),
            "finished_at": _iso(row["finished_at"]),
            "exchange_count": int(row["exchange_count"] or 0),
        }
        for row in rows
    ]


async def count_detections(
    protocol: str | None = None,
    model: str | None = None,
    verdict: str | None = None,
    min_score: float | None = None,
    credential_id: str | None = None,
) -> int:
    """统计满足同样过滤条件的检测条数(供列表分页用)。"""
    statement = select(func.count()).select_from(Detection).where(
        *_detection_filters(protocol, model, verdict, min_score, credential_id)
    )

    async with get_session() as session:
        total = (await session.execute(statement)).scalar_one()

    return int(total)


async def get_detection(detection_id: str) -> dict[str, Any] | None:
    """取一条检测的全部字段,并附上逐探针结果;不存在返回 None。"""
    async with get_session() as session:
        detection = await session.get(Detection, detection_id)
        if detection is None:
            return None

        probes = (
            (
                await session.execute(
                    select(ProbeResult)
                    .where(ProbeResult.detection_id == detection_id)
                    .order_by(ProbeResult.id)
                )
            )
            .scalars()
            .all()
        )

        exchange_total = (
            await session.execute(
                select(func.count())
                .select_from(ProbeExchange)
                .where(ProbeExchange.detection_id == detection_id)
            )
        ).scalar_one()

        payload: dict[str, Any] = {
            "id": detection.id,
            "protocol": detection.protocol,
            "base_url": detection.base_url,
            "host": detection.host,
            "api_key_masked": detection.api_key_masked,
            "target_model": detection.target_model,
            "status": detection.status,
            "total_score": detection.total_score,
            "verdict": detection.verdict,
            "risk_level": detection.risk_level,
            "summary": detection.summary,
            "run_error": detection.run_error,
            "self_reported_identity": detection.self_reported_identity,
            "detected_brands": detection.detected_brands,
            # performance / report 原样返回:里面的嵌套结构由渲染层自己解释。
            "performance": detection.performance,
            "report": detection.report,
            "total_probes": detection.total_probes,
            "passed_probes": detection.passed_probes,
            "failed_probes": detection.failed_probes,
            "credential_id": detection.credential_id,
            "credential_name": detection.credential_name,
            # 深测结果是大 JSON,只在详情接口返回;列表接口只带 deep_status。
            "deep_report": detection.deep_report,
            "deep_status": detection.deep_status,
            "created_at": _iso(detection.created_at),
            "started_at": _iso(detection.started_at),
            "finished_at": _iso(detection.finished_at),
            "exchange_count": int(exchange_total or 0),
            "probes": [_probe_to_dict(p) for p in probes],
        }

    return payload


async def list_exchanges(
    detection_id: str,
    probe_name: str | None = None,
) -> list[dict[str, Any]]:
    """列出某次检测采集到的请求/响应交换(按 seq, id 升序)。

    body 可能很大,所以只在专用接口里按需调用;``probe_name`` 用于按探针过滤。
    """
    statement = select(ProbeExchange).where(
        ProbeExchange.detection_id == detection_id
    )
    if probe_name:
        statement = statement.where(ProbeExchange.probe_name == probe_name)
    statement = statement.order_by(ProbeExchange.seq, ProbeExchange.id)

    async with get_session() as session:
        rows = (await session.execute(statement)).scalars().all()

    return [_exchange_to_dict(row) for row in rows]


async def count_exchanges(
    detection_id: str,
    probe_name: str | None = None,
) -> int:
    """统计某次检测采集到的交换条数(可按探针过滤)。"""
    statement = (
        select(func.count())
        .select_from(ProbeExchange)
        .where(ProbeExchange.detection_id == detection_id)
    )
    if probe_name:
        statement = statement.where(ProbeExchange.probe_name == probe_name)

    async with get_session() as session:
        total = (await session.execute(statement)).scalar_one()

    return int(total)
