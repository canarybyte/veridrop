"""数据层 —— 检测结果持久化 (SQLAlchemy 2.0 async)。

默认落本地 SQLite(`web_data/veridrop.db`),生产可通过 DATABASE_URL 指向
Postgres(asyncpg 驱动)。上层的 API 层只和 `repository.py` 打交道,这里只负责
引擎、会话和表结构。

关键决策:
- 用 SQLAlchemy 2.0 的 DeclarativeBase + Mapped + mapped_column 写法,类型即
  声明,避免 1.x 的 Column(...) 混用。
- JSON 列用通用 JSON 类型:SQLite 上落 TEXT,Postgres 上落 JSON,一套模型两处
  都能跑;报告/性能数据本来就是嵌套结构,拆表得不偿失。
- `detections.id` 直接复用 job_id(TEXT),这样 `/r/{job_id}` 和入库记录一一对应,
  不需要额外映射。
- 表创建走 `create_all`,幂等,进程启动时调一次 `init_db()` 即可,不引入迁移
  框架。`create_all` 不会给已存在的表补列,所以 `init_db()` 里额外跑一段
  `_ensure_columns()` 做最小限度的向后兼容迁移(只加列/加索引)。
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    inspect,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


logger = logging.getLogger("veridrop")

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./web_data/veridrop.db"

# 模块加载时读一次环境变量:引擎是进程级的,配置在启动时固定下来最简单。
DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


class Base(DeclarativeBase):
    """所有表模型的声明式基类。"""


class Detection(Base):
    """一次检测(job)的汇总记录,主键就是 job_id。"""

    __tablename__ = "detections"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    # host 是从 base_url 推导出来的域名,单独存一列是为了按站点聚合/筛选时
    # 不用在 SQL 里切字符串。索引已在列上声明。
    host: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    api_key_masked: Mapped[str] = mapped_column(String(64), nullable=False)
    target_model: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)

    total_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    verdict: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    risk_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # run_error 非空表示这次跑挂了(网络/鉴权/超时),与「检测不通过」是两回事。
    run_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    self_reported_identity: Mapped[str | None] = mapped_column(Text, nullable=True)

    detected_brands: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    performance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    total_probes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    passed_probes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_probes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 检测来自哪个凭据;凭据删除后历史报告仍需可读,所以不做外键、只留两个
    # 冗余字段(名称快照),删除凭据不会影响已出报告。
    credential_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    credential_name: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Credential(Base):
    """用户命名的中转站凭据(URL + API Key)。

    API Key 只存 Fernet 密文;明文永远不落库,前端也只能看到掩码。
    ``name`` 全局唯一,重名由数据库唯一约束兜底,路由层转 409。
    """

    __tablename__ = "credentials"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    api_key_enc: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_masked: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ProbeResult(Base):
    """单探针(单个 detector)的结果,挂在 detections.id 下。"""

    __tablename__ = "probe_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    detection_id: Mapped[str] = mapped_column(
        ForeignKey("detections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


def _sqlite_db_path(database_url: str) -> Path | None:
    """从 SQLite URL 里解析出数据库文件路径;非文件型 SQLite 返回 None。

    走 make_url 而不是自己切字符串,是因为 `sqlite+aiosqlite:///./web_data/x.db`
    的 database 是相对路径 `./web_data/x.db`,而 `sqlite+aiosqlite:///C:/...`
    是 Windows 绝对路径,两者手写解析很容易在盘符上翻车。
    """
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        return None
    database = url.database or ""
    # 内存库 / 空库名 / file: URI 都不需要在文件系统上建目录。
    if not database or database == ":memory:" or database.startswith("file:"):
        return None
    return Path(database)


# future=True 是 2.0 的默认行为,显式写出来表明代码只按 2.0 语义写。
engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    future=True,
    pool_pre_ping=True,
)

# expire_on_commit=False:提交后对象仍可读,避免在 async 上下文里触发隐式
# lazy load(SQLAlchemy 的 async 会话里那是硬错误)。
async_session: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    expire_on_commit=False,
)


def _ensure_columns(sync_conn: Any) -> None:
    """给老库补上 create_all 不会自动新增的列与索引。

    `create_all` 只建缺失的表,对已存在的表不补列;已部署实例升级后
    `detections` 会缺 credential_id / credential_name。这里用 inspector 做
    最小检测,缺什么补什么(SQLite 与 PG 都支持 ADD COLUMN)。任何一步失败
    只记 warning,不影响进程启动——老数据照样能读,只是没有凭据信息。
    """
    inspector = inspect(sync_conn)

    try:
        existing_columns = {c["name"] for c in inspector.get_columns("detections")}
    except Exception as exc:  # noqa: BLE001 — 表都没有时交给 create_all
        logger.warning("inspect detections failed: %s", exc)
        return

    additions = (
        ("credential_id", "VARCHAR(32)"),
        ("credential_name", "VARCHAR(128)"),
    )
    for name, ddl_type in additions:
        if name in existing_columns:
            continue
        try:
            sync_conn.execute(
                text(f"ALTER TABLE detections ADD COLUMN {name} {ddl_type}")
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("add column detections.%s failed: %s", name, exc)

    # 索引同样不会被补;已存在则跳过,失败忽略。
    try:
        existing_indexes = {
            idx["name"] for idx in inspector.get_indexes("detections")
        }
    except Exception:  # noqa: BLE001
        existing_indexes = set()
    for name, column in (
        ("ix_detections_credential_id", "credential_id"),
        ("ix_detections_credential_name", "credential_name"),
    ):
        if name in existing_indexes:
            continue
        try:
            sync_conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {name} "
                    f"ON detections ({column})"
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("create index %s failed: %s", name, exc)


async def init_db() -> None:
    """建表,可重复调用。

    SQLite 不会自动创建父目录,所以先按 URL 解析出文件路径并 mkdir;否则
    第一次启动会在 `unable to open database file` 上直接失败。
    """
    db_path = _sqlite_db_path(DATABASE_URL)
    if db_path is not None:
        db_path.parent.mkdir(parents=True, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_columns)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """一个请求/一次操作一个会话;异常回滚,结束时关闭。

    不在这里 commit —— 事务边界由调用方(repository 的单事务写入)决定。
    """
    session = async_session()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
