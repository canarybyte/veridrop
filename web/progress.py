"""进程内进度事件总线（供 SSE 推送使用）。

项目当前是单 worker 部署，事件生产者（Runner 的进度回调）与消费者（SSE
响应）都跑在同一个事件循环里，所以不需要跨进程广播。每个 job 维护：

- 最近 500 条事件的环形缓存（订阅 / 断线重连时回放）；
- 一组 asyncio.Queue 订阅者（新事件广播给每个队列，队列满时丢最旧一条，
  绝不阻塞发布方）。

注意：publish / subscribe 都是同步方法（Runner 的回调契约是同步函数，
不能 await），因此内部用 threading.Lock 保护字典而不是 asyncio.Lock；
临界区内只做内存操作和 put_nowait，不会阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Any

# 每个 job 缓存的最大事件条数（用于 SSE 回放）
_BUFFER_SIZE = 500
# 单个订阅队列的容量；满了丢最旧，防止慢客户端拖垮发布方
_QUEUE_SIZE = 256
# 关闭信号：订阅者读到 None 即认为该 job 已结束
_SENTINEL = None


class EventBus:
    """单进程内、按 job 分组的事件总线。"""

    def __init__(
        self,
        buffer_size: int = _BUFFER_SIZE,
        queue_size: int = _QUEUE_SIZE,
    ) -> None:
        self._buffer_size = buffer_size
        self._queue_size = queue_size
        self._buffers: dict[str, deque[dict[str, Any]]] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._seq: dict[str, int] = {}
        self._closed: set[str] = set()
        # 见模块 docstring：同步接口 + 单事件循环，用线程锁保护共享字典。
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 发布

    def publish(
        self,
        job_id: str,
        type: str,  # noqa: A002 — 按约定沿用 type 作为参数名
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """写入一条事件并广播给所有订阅队列，返回该事件。"""
        with self._lock:
            seq = self._seq.get(job_id, 0) + 1
            self._seq[job_id] = seq
            event: dict[str, Any] = {
                "seq": seq,
                "ts": time.time(),
                "type": type,
                "payload": payload,
            }
            buffer = self._buffers.get(job_id)
            if buffer is None:
                buffer = deque(maxlen=self._buffer_size)
                self._buffers[job_id] = buffer
            buffer.append(event)
            for queue in list(self._subscribers.get(job_id, ())):
                self._put(queue, event)
            return event

    # ------------------------------------------------------------------ 订阅

    def subscribe(
        self,
        job_id: str,
        last_event_id: int | None = None,
    ) -> "Subscription":
        """同步创建订阅。

        replay 为该 job 缓存中 last_event_id 之后的事件（None 表示全部缓存）；
        之后产生的新事件会进入 .queue。已关闭的 job 会直接收到关闭哨兵。
        """
        with self._lock:
            buffer = list(self._buffers.get(job_id, ()))
            replay = [
                event
                for event in buffer
                if last_event_id is None or event["seq"] > last_event_id
            ]
            queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
            if job_id in self._closed:
                # job 已结束：不登记订阅，只给一个哨兵让消费者读完回放后退出。
                self._put(queue, _SENTINEL)
            else:
                self._subscribers.setdefault(job_id, []).append(queue)
        return Subscription(self, job_id, queue, replay)

    def close(self, job_id: str) -> None:
        """标记 job 结束，并向所有订阅队列投递关闭哨兵。"""
        with self._lock:
            self._closed.add(job_id)
            for queue in list(self._subscribers.get(job_id, ())):
                self._put(queue, _SENTINEL)

    # -------------------------------------------------------------- 查询/测试

    def is_closed(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._closed

    def get_events(self, job_id: str) -> list[dict[str, Any]]:
        """返回该 job 缓存的事件副本。"""
        with self._lock:
            return list(self._buffers.get(job_id, ()))

    def reset(self, job_id: str) -> None:
        """清空指定 job 的全部状态（测试用）。"""
        with self._lock:
            self._buffers.pop(job_id, None)
            self._subscribers.pop(job_id, None)
            self._seq.pop(job_id, None)
            self._closed.discard(job_id)

    def reset_all(self) -> None:
        """清空全部 job 状态（测试用）。"""
        with self._lock:
            self._buffers.clear()
            self._subscribers.clear()
            self._seq.clear()
            self._closed.clear()

    # ---------------------------------------------------------------- 内部实现

    def _unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            queues = self._subscribers.get(job_id)
            if not queues:
                return
            try:
                queues.remove(queue)
            except ValueError:
                return
            if not queues:
                self._subscribers.pop(job_id, None)

    @staticmethod
    def _put(queue: asyncio.Queue, item: Any) -> None:
        """非阻塞写入：队列满时丢最旧一条，仍满则丢弃最新一条。"""
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                pass


class Subscription:
    """订阅句柄：.replay 为历史事件，.queue 为后续实时事件。"""

    def __init__(
        self,
        bus: EventBus,
        job_id: str,
        queue: asyncio.Queue,
        replay: list[dict[str, Any]],
    ) -> None:
        self.bus = bus
        self.job_id = job_id
        self.queue = queue
        self.replay = replay
        self._closed = False

    def close(self) -> None:
        """取消订阅（幂等）。"""
        if self._closed:
            return
        self._closed = True
        self.bus._unsubscribe(self.job_id, self.queue)


# 全局单例：jobs.py / server.py 直接 from web.progress import bus
bus = EventBus()


def reset_all() -> None:
    """清空全局总线状态（测试用）。"""
    bus.reset_all()
