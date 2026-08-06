import asyncio
import time

from ib_execution.async_bridge import AsyncControllerBridge


class SlowController:
    def __init__(self):
        self.calls = []

    def slow(self, value):
        time.sleep(0.08)
        self.calls.append(value)
        return value

    def on_connected(self, epoch):
        return self.slow(epoch)


async def _exercise():
    c = SlowController()
    b = AsyncControllerBridge(c)
    await b.start()
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        for _ in range(8):
            await asyncio.sleep(0.015)
            ticks += 1

    # Callback enters synchronously but its slow work must not block the loop.
    b.on_connected(1)
    b.on_connected(2)
    await heartbeat()
    await b._queue.join()  # type: ignore[union-attr]
    await b.close()
    return c.calls, ticks


def test_bridge_keeps_event_loop_responsive_and_serializes_callbacks():
    calls, ticks = asyncio.run(_exercise())
    assert calls == [1, 2]
    assert ticks == 8


class OverflowController(SlowController):
    def __init__(self):
        super().__init__()
        self.halts = []

    def on_connected(self, epoch):
        time.sleep(0.15)
        self.calls.append(epoch)

    def halt(self, why):
        self.halts.append(why)


async def _overflow():
    c = OverflowController()
    b = AsyncControllerBridge(c, max_queue=1)
    await b.start()
    # First item occupies worker, second fills queue, later items overflow.
    b.on_connected(1)
    await asyncio.sleep(0.01)
    b.on_connected(2)
    b.on_connected(3)
    b.on_connected(4)
    await asyncio.sleep(0.5)
    await b.close()
    return c


def test_bridge_queue_overflow_fails_closed():
    c = asyncio.run(_overflow())
    assert c.halts
    assert "overflow" in c.halts[0]
