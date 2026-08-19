"""Gate B1: storage/queue loss must fence the process before any broker write."""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from ib_execution.async_bridge import AsyncControllerBridge, BridgeUnavailable
from ib_execution.calendar import TradingCalendar
from ib_execution.clock import ManualClock
from ib_execution.controller import Controller, ExecutionPolicy
from ib_execution.fake_broker import FakeBroker, Faults
from ib_execution.journal import Journal, JournalUnavailable
from ib_execution.models import OperatingMode, Quote, SyncState
from ib_execution.risk import RiskConfig, RiskEngine
from conftest import SESSION_START, target


# Building the Controller commits RISK_CONFIG_LOADED, which is a real SQLite
# write plus fsync before any test body runs. A setup budget tighter than the
# production one therefore turns a slow disk into a fake fencing regression:
# on a shared Windows CI runner with realtime AV scanning a single fsync can
# exceed a hundred milliseconds, `_system()` raises JournalUnavailable, and the
# injected fault under test is never installed. Use the same durability budget
# production is held to (max_writer_lag_ms <= 5000). This parameter stays as
# the seam for a test that needs a different setup budget; the one test that
# genuinely measures timeout behaviour narrows `_write_timeout_seconds`
# directly after setup instead -- see
# test_fsync_timeout_fences_and_never_sends.
SETUP_WRITE_TIMEOUT_SECONDS = 5.0


def _system(tmp_path, *, write_timeout: float = SETUP_WRITE_TIMEOUT_SECONDS):
    clock = ManualClock(SESSION_START)
    journal = Journal(
        tmp_path / "journal.db",
        clock=clock,
        write_timeout_seconds=write_timeout,
        sqlite_timeout_seconds=write_timeout,
    )
    broker = FakeBroker(clock, Faults())
    alerts: list[tuple[str, str]] = []
    config = RiskConfig(strategy_whitelist=("manual_test",), max_position_shares=5)
    ctl = Controller(
        journal,
        broker,
        RiskEngine(config, clock),
        clock,
        TradingCalendar(),
        ExecutionPolicy(),
        alert=lambda level, message: alerts.append((level, message)),
    )
    ctl.on_connected(1)
    ctl.on_quote(Quote("SPY", 599.98, 600.02, 100, 100, clock.now()))
    assert ctl.reconcile()
    return clock, journal, broker, ctl, alerts


@pytest.mark.parametrize(
    "storage_error",
    [
        sqlite3.OperationalError("database is locked"),
        sqlite3.OperationalError("database or disk is full"),
        sqlite3.DatabaseError("database disk image is malformed"),
    ],
    ids=["sqlite-locked", "disk-full", "wal-corrupt"],
)
def test_storage_error_fences_before_broker_write(tmp_path, storage_error):
    clock, journal, broker, ctl, alerts = _system(tmp_path)
    original = journal._apply
    journal._apply = lambda conn, req: (_ for _ in ()).throw(storage_error)
    try:
        with pytest.raises(JournalUnavailable):
            ctl.submit_target(target(1, clock))
        assert broker.place_calls == []
        assert ctl.operating_mode is OperatingMode.HALTED
        assert ctl.sync_state is SyncState.UNVERIFIED
        assert ctl.fatal_shutdown_requested is True
        assert any(level == "CRITICAL" and "shutdown required" in msg for level, msg in alerts)
    finally:
        journal._apply = original
        journal.close()


def test_fsync_timeout_fences_and_never_sends(tmp_path):
    clock, journal, broker, ctl, alerts = _system(tmp_path)
    journal._write_timeout_seconds = 0.03
    original = journal._apply

    def stalled_apply(conn, req):
        time.sleep(0.15)
        return original(conn, req)

    journal._apply = stalled_apply
    try:
        with pytest.raises(JournalUnavailable, match="timed out"):
            ctl.submit_target(target(1, clock))
        assert broker.place_calls == []
        assert ctl.fatal_shutdown_requested
        assert alerts and alerts[-1][0] == "CRITICAL"
    finally:
        journal._apply = original
        journal.close()


def test_dead_writer_thread_is_detected_without_waiting(tmp_path):
    clock, journal, broker, ctl, _ = _system(tmp_path)
    journal._q.put(None)
    journal._thread.join(timeout=2)
    assert not journal._thread.is_alive()

    with pytest.raises(JournalUnavailable, match="writer thread"):
        ctl.submit_target(target(1, clock))
    assert broker.place_calls == []
    assert ctl.fatal_shutdown_requested
    journal.close()


def test_broken_event_loop_to_controller_queue_fences_process(tmp_path):
    async def scenario() -> None:
        clock, journal, broker, ctl, _ = _system(tmp_path)
        bridge = AsyncControllerBridge(ctl)
        await bridge.start()
        assert bridge._consumer is not None
        bridge._consumer.cancel()
        try:
            await bridge._consumer
        except asyncio.CancelledError:
            pass

        with pytest.raises(BridgeUnavailable):
            await bridge.submit_target(target(1, clock))
        assert broker.place_calls == []
        assert ctl.operating_mode is OperatingMode.HALTED
        assert ctl.fatal_shutdown_requested
        await bridge.close()
        journal.close()

    asyncio.run(scenario())
