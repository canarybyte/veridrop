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

import secrets
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import delete, func, select

from .db import Credential, Detection, ProbeResult, get_session


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


async def save_detection(
    detection_id: str,
    *,
    protocol: str,
    base_url: str,
    target_model: str,
    mode: str,
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
        "mode": mode,
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
        "created_at": created_at,
        "started_at": started_at,
        "finished_at": finished_at,
    }

    async with get_session() as session:
        async with session.begin():
            await session.execute(
                delete(ProbeResult).where(ProbeResult.detection_id == detection_id)
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
    statement = (
        select(
            Detection.id,
            Detection.protocol,
            Detection.host,
            Detection.base_url,
            Detection.target_model,
            Detection.mode,
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

        payload: dict[str, Any] = {
            "id": detection.id,
            "protocol": detection.protocol,
            "base_url": detection.base_url,
            "host": detection.host,
            "api_key_masked": detection.api_key_masked,
            "target_model": detection.target_model,
            "mode": detection.mode,
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
            "created_at": _iso(detection.created_at),
            "started_at": _iso(detection.started_at),
            "finished_at": _iso(detection.finished_at),
            "probes": [_probe_to_dict(p) for p in probes],
        }

    return payload
