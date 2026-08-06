"""Serialized asynchronous boundary between IB callbacks and the Phase-0 core.

The controller and journal are deliberately synchronous so every state
transition is deterministic in tests. They must NOT run on ib_async's event-loop
thread: durable SQLite commits wait for fsync, and waiting in the loop delays IB
callbacks. This bridge preserves a single writer by dispatching every callback
and strategy command through one dedicated worker thread, in arrival order.

Gate B2 must register this bridge with IbAdapter, not Controller directly.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .models import Execution, Quote, TargetPosition


class BridgeNotStarted(RuntimeError):
    pass


@dataclass
class _Item:
    method: str
    args: tuple[Any, ...]
    result: Optional[asyncio.Future] = None


class AsyncControllerBridge:
    """One FIFO queue + one worker thread = one serialized state-machine writer."""

    def __init__(self, controller, *, max_queue: int = 10_000):
        self.controller = controller
        self.max_queue = max_queue
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue[_Item]] = None
        self._consumer: Optional[asyncio.Task] = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="controller")
        self._closed = False
        self._overflowed = False

    async def start(self) -> None:
        if self._consumer is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=self.max_queue)
        self._consumer = asyncio.create_task(self._consume(), name="controller-bridge")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._consumer is not None:
            self._consumer.cancel()
            try:
                await self._consumer
            except asyncio.CancelledError:
                pass
        self._executor.shutdown(wait=True, cancel_futures=True)

    async def submit_target(self, target: TargetPosition) -> bool:
        return bool(await self._request("submit_target", target))

    async def reconcile(self) -> bool:
        return bool(await self._request("reconcile"))

    async def tick(self) -> None:
        await self._request("tick")

    async def _request(self, method: str, *args: Any) -> Any:
        queue = self._require_queue()
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        await queue.put(_Item(method, args, future))
        return await future

    def _post(self, method: str, *args: Any) -> None:
        """Thread-safe callback ingress. Never calls Controller on the IB loop."""
        if self._loop is None or self._queue is None or self._closed:
            raise BridgeNotStarted("AsyncControllerBridge.start() must run before callbacks")

        def put() -> None:
            assert self._queue is not None
            try:
                self._queue.put_nowait(_Item(method, args))
            except asyncio.QueueFull:
                # Losing a callback is unrecoverable. Do not merely raise in a
                # call_soon callback (which can be logged and ignored by the
                # event loop): fence future activity by HALTING on the same
                # single controller executor. Gate B3 must inject this path.
                if not self._overflowed:
                    self._overflowed = True
                    fut = self._executor.submit(
                        self.controller.halt,
                        "controller bridge queue overflow; callback state may be lost",
                    )
                    fut.add_done_callback(lambda f: f.exception())

        self._loop.call_soon_threadsafe(put)

    async def _consume(self) -> None:
        assert self._queue is not None
        loop = asyncio.get_running_loop()
        while True:
            item = await self._queue.get()
            try:
                fn: Callable[..., Any] = getattr(self.controller, item.method)
                value = await loop.run_in_executor(self._executor, fn, *item.args)
                if item.result is not None and not item.result.done():
                    item.result.set_result(value)
            except BaseException as exc:  # propagate command failures; callbacks fail closed inside controller
                if item.result is not None and not item.result.done():
                    item.result.set_exception(exc)
            finally:
                self._queue.task_done()

    def _require_queue(self) -> asyncio.Queue[_Item]:
        if self._queue is None or self._closed:
            raise BridgeNotStarted("bridge is not running")
        return self._queue

    # BrokerCallbacks protocol. These are intentionally tiny and non-blocking.
    def on_ack(self, order_ref: str, broker_order_id: int, perm_id: int | None) -> None:
        self._post("on_ack", order_ref, broker_order_id, perm_id)

    def on_working(self, order_ref: str, perm_id: int | None) -> None:
        self._post("on_working", order_ref, perm_id)

    def on_execution(self, execution: Execution) -> None:
        self._post("on_execution", execution)

    def on_fee(self, exec_id: str, commission: float, currency: str) -> None:
        self._post("on_fee", exec_id, commission, currency)

    def on_cancelled(self, order_ref: str) -> None:
        self._post("on_cancelled", order_ref)

    def on_cancel_rejected(self, order_ref: str, reason: str) -> None:
        self._post("on_cancel_rejected", order_ref, reason)

    def on_rejected(self, order_ref: str, reason: str) -> None:
        self._post("on_rejected", order_ref, reason)

    def on_disconnected(self, reason: str) -> None:
        self._post("on_disconnected", reason)

    def on_connected(self, connection_epoch: int) -> None:
        self._post("on_connected", connection_epoch)

    def on_market_data_lost(self) -> None:
        self._post("on_market_data_lost")

    def on_market_data_restored(self) -> None:
        self._post("on_market_data_restored")

    def on_quote(self, quote: Quote) -> None:
        self._post("on_quote", quote)
