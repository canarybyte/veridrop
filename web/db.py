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
  框架——当前只有两张表。
"""

from __future__ import annotations

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
)
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
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
