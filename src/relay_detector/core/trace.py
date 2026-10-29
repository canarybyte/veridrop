"""探针级请求/响应采集(trace)。

用 contextvars 保存两样东西:

- ``_probe``:当前正在执行的探针名。Runner 为每个 active detector 用
  ``probe_scope(name)`` 包住执行体;``asyncio.gather`` 创建的子任务会复制父
  上下文,所以并发探针之间天然隔离,不会串味。
- ``_collector``:采集回调。由 Web 作业在整次检测期间挂上,拿到一条交换就
  追加到自己列表里。

``record_exchange`` 走「尽力而为」策略:没有回调直接返回,回调抛异常一律吞掉
——采集绝不能影响探测本身。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any


_probe: ContextVar[str | None] = ContextVar("veridrop_probe", default=None)
_collector: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "veridrop_trace_collector", default=None
)


@contextmanager
def probe_scope(name: str) -> Iterator[None]:
    """把当前探针名设为 ``name``,退出时还原(同一 task 内生效)。"""
    token = _probe.set(name)
    try:
        yield
    finally:
        _probe.reset(token)


def set_collector(cb: Callable[[dict[str, Any]], None]) -> Token:
    """挂上采集回调,返回用于还原的 token。"""
    return _collector.set(cb)


def reset_collector(token: Token) -> None:
    """还原采集回调(通常放在 try/finally 里)。token 失效时静默忽略。"""
    try:
        _collector.reset(token)
    except (ValueError, RuntimeError):
        # 跨上下文 reset 会抛错;此时直接摘掉回调,保证后续不再采集。
        _collector.set(None)


def current_probe() -> str | None:
    """当前探针名(没有 scope 时为 None);主要供测试与调试使用。"""
    return _probe.get()


def record_exchange(
    *,
    request: Any = None,
    response: Any = None,
    latency_ms: int | None = None,
    status_code: int | None = None,
    error: Any = None,
    stream: bool = False,
    events: list[Any] | None = None,
) -> None:
    """记录一次请求/响应交换。

    request/response 直接存引用,不做拷贝或截断:调用方(Web 作业)负责序列化
    与容量上限。任何异常都被吞掉,采集失败不影响探测结果。
    """
    collector = _collector.get()
    if collector is None:
        return
    try:
        collector(
            {
                "probe": _probe.get() or "unknown",
                "ts": time.time(),
                "request": request,
                "response": response,
                "latency_ms": latency_ms,
                "status_code": status_code,
                "stream": stream,
                "events": events,
                "error": error,
            }
        )
    except Exception:  # noqa: BLE001 — 采集绝不能影响探测
        pass
