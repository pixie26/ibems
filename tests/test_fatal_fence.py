"""
Gate B1.3b: fail-closed survives the restart it forces.

The gap these close: ``_fail_closed_journal`` cannot journal (the journal is
what failed), so the HALT existed only in memory. The process exited non-zero,
storage was repaired, the next process replayed a journal containing no HALT,
and came back NORMAL. Invariant 22 held the whole time and was never reached --
it can only protect a HALT that reached the disk.

The test that matters is ``test_a_repaired_journal_still_refuses_to_trade``:
restarting against completely healthy storage must still refuse.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ib_execution.calendar import TradingCalendar
from ib_execution.clock import ManualClock
from ib_execution.controller import Controller, ExecutionPolicy
from ib_execution.execution_host import (
    EXIT_CALENDAR,
    EXIT_FATAL_SHUTDOWN,
    EXIT_FENCED,
    EXIT_NOT_OWNER,
    EXIT_OK,
    EXIT_STARTUP,
    ExecutionHost,
    HostConfig,
    HostStartupRefused,
)
from ib_execution.fake_broker import FakeBroker, Faults
from ib_execution.fatal_fence import (
    STATE_ACKNOWLEDGED,
    STATE_RAISED,
    FatalFence,
    FenceDomainError,
    FenceStillRaised,
)
from ib_execution.journal import Journal
from ib_execution.risk import RiskConfig, RiskEngine

SESSION_START = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)


def _fence(tmp_path, name="fatal-fence.json") -> FatalFence:
    return FatalFence(
        tmp_path / name, tmp_path / "journal.db", require_separate_domain=False
    )


def _risk(clock) -> RiskEngine:
    return RiskEngine(
        RiskConfig(
            symbol_whitelist=("SPY",),
            strategy_whitelist=("manual_test",),
            max_position_shares=5,
            max_order_shares=10,
            max_order_notional=Decimal("20000"),
        ),
        clock,
    )


def _host(tmp_path, *, clock=None, calendar=None, allow_shared=True) -> ExecutionHost:
    clock = clock or ManualClock(SESSION_START)
    return ExecutionHost(
        HostConfig(
            journal_path=tmp_path / "journal.db",
            fence_path=tmp_path / "fatal-fence.json",
            status_path=tmp_path / "status.json",
            require_separate_fence_domain=not allow_shared,
        ),
        broker_factory=lambda: FakeBroker(clock, Faults()),
        risk=_risk(clock),
        clock=clock,
        calendar=calendar or TradingCalendar(),
        alert=lambda level, msg: None,
        sleeper=lambda _seconds: None,
    )


# --------------------------------------------------------------------------
# the fence record itself
# --------------------------------------------------------------------------


def test_a_fence_is_durable_and_readable(tmp_path):
    fence = _fence(tmp_path)
    assert fence.read() is None
    fence.raise_fence("journal writer died: disk full")
    record = fence.read()
    assert record is not None
    assert record.state == STATE_RAISED
    assert "disk full" in record.reason
    assert record.pid > 0


def test_the_first_cause_is_kept_not_the_last(tmp_path):
    """Later failures on the way down are consequences of the first one."""
    fence = _fence(tmp_path)
    fence.raise_fence("original cause")
    fence.raise_fence("consequential failure")
    assert fence.read().reason == "original cause"


def test_an_unparseable_fence_is_still_a_fence(tmp_path):
    fence = _fence(tmp_path)
    fence.path.write_text("{ this is not json", encoding="utf-8")
    record = fence.read()
    assert record is not None and record.state == STATE_RAISED
    with pytest.raises(FenceStillRaised):
        fence.require_clear()


def test_acknowledgement_does_not_retire_the_fence(tmp_path):
    """Otherwise clicking "yes" is itself the route back to trading.

    The gap between raising and retiring is where somebody has to look at real
    broker state; collapsing the two phases removes exactly that.
    """
    fence = _fence(tmp_path)
    fence.raise_fence("journal writer died")
    fence.acknowledge("olivia", "disk was full; freed 40GB; account inspected in TWS")

    record = fence.read()
    assert record is not None and record.state == STATE_ACKNOWLEDGED
    assert record.acknowledged_by == "olivia"
    with pytest.raises(FenceStillRaised):
        fence.require_clear()          # still fenced


def test_retirement_requires_both_acknowledgement_and_reconciliation(tmp_path):
    fence = _fence(tmp_path)
    fence.raise_fence("journal writer died")

    with pytest.raises(FenceStillRaised):
        fence.retire(reconciled=True)          # not acknowledged yet

    fence.acknowledge("olivia", "cause understood")
    with pytest.raises(FenceStillRaised):
        fence.retire(reconciled=False)         # acknowledged but unreconciled

    fence.retire(reconciled=True)
    assert fence.read() is None
    fence.require_clear()


def test_acknowledgement_requires_attribution(tmp_path):
    fence = _fence(tmp_path)
    fence.raise_fence("journal writer died")
    with pytest.raises(ValueError):
        fence.acknowledge("", "resolution")
    with pytest.raises(ValueError):
        fence.acknowledge("olivia", "")


def test_a_fence_sharing_the_journal_volume_is_refused(tmp_path):
    """Enforced, not documented: a full journal volume is why we are here."""
    same_volume = FatalFence(
        tmp_path / "fence.json", tmp_path / "journal.db", require_separate_domain=True
    )
    with pytest.raises(FenceDomainError):
        same_volume.verify_domain()


# --------------------------------------------------------------------------
# controller integration
# --------------------------------------------------------------------------


def _controller(tmp_path, fence, clock):
    journal = Journal(tmp_path / "journal.db", clock=clock)
    return journal, Controller(
        journal=journal,
        broker=FakeBroker(clock, Faults()),
        risk=_risk(clock),
        clock=clock,
        calendar=TradingCalendar(),
        policy=ExecutionPolicy(),
        alert=lambda level, msg: None,
        fence=fence,
    )


def test_a_journal_failure_raises_the_durable_fence(tmp_path):
    clock = ManualClock(SESSION_START)
    fence = _fence(tmp_path)
    journal, ctl = _controller(tmp_path, fence, clock)
    try:
        ctl._fail_closed_runtime("journal unavailable; writer thread died")
        assert ctl.fatal_shutdown_requested
        record = fence.read()
        assert record is not None and "writer thread died" in record.reason
    finally:
        journal.close()


def test_a_fence_that_cannot_be_written_is_reported_as_a_failure(tmp_path):
    """Never dressed up as a durable fence.

    The process still exits non-zero with a CRITICAL alert, which is where
    this stood before the fence existed -- no worse, and honest about it.
    """
    clock = ManualClock(SESSION_START)
    # A regular file where a directory must be: unwritable regardless of the
    # euid running the tests, which a chmod is not when that euid is root.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    unwritable = FatalFence(
        blocker / "fence.json", tmp_path / "journal.db", require_separate_domain=False
    )

    alerts: list[tuple[str, str]] = []
    journal = Journal(tmp_path / "journal.db", clock=clock)
    ctl = Controller(
        journal=journal, broker=FakeBroker(clock, Faults()), risk=_risk(clock),
        clock=clock, calendar=TradingCalendar(), policy=ExecutionPolicy(),
        alert=lambda level, msg: alerts.append((level, msg)), fence=unwritable,
    )
    try:
        ctl._fail_closed_runtime("journal unavailable")
        assert ctl.fatal_shutdown_requested, "still fenced in memory"
        assert ctl.fence_write_failed
        assert any("DURABLE FENCE COULD NOT BE WRITTEN" in msg for _, msg in alerts)
    finally:
        journal.close()


# --------------------------------------------------------------------------
# the host: startup gates and exit codes
# --------------------------------------------------------------------------


def test_host_starts_and_exits_zero_when_asked_to_stop(tmp_path):
    host = _host(tmp_path)
    host.start()
    try:
        host.request_stop()
        assert host.run(ticks=5) == EXIT_OK
    finally:
        host.close()


def test_host_exits_non_zero_on_fatal_shutdown(tmp_path):
    """Gate B1.3a: fatal_shutdown_requested finally has a consumer."""
    host = _host(tmp_path)
    controller = host.start()
    try:
        controller._fail_closed_runtime("journal unavailable; writer thread died")
        assert host.run(ticks=5) == EXIT_FATAL_SHUTDOWN
    finally:
        host.close()


def test_a_repaired_journal_still_refuses_to_trade(tmp_path):
    """Gate B1.3b, end to end. The whole point of the fence.

    Storage is completely healthy on the second start -- the journal replays
    cleanly and contains no HALT, because none could be written. Before the
    fence, this returned NORMAL and traded.
    """
    first = _host(tmp_path)
    controller = first.start()
    controller._fail_closed_runtime("journal unavailable; disk full")
    assert first.run(ticks=3) == EXIT_FATAL_SHUTDOWN
    first.close()

    # Storage is repaired: the journal opens, replays and reports no HALT.
    healthy = Journal(tmp_path / "journal.db", clock=ManualClock(SESSION_START))
    try:
        assert list(healthy.replay()), "journal is readable"
    finally:
        healthy.close()

    second = _host(tmp_path)
    with pytest.raises(HostStartupRefused) as excinfo:
        second.start()
    assert excinfo.value.code == EXIT_FENCED
    assert second.controller is None, "no controller, therefore no broker session"


def test_a_fenced_host_does_not_construct_a_broker(tmp_path):
    """A process that will not be allowed to trade must not open a session."""
    _fence(tmp_path).raise_fence("previous fatal shutdown")
    built: list[str] = []
    host = _host(tmp_path)
    host.broker_factory = lambda: built.append("broker") or FakeBroker(  # type: ignore[func-returns-value]
        ManualClock(SESSION_START), Faults()
    )
    with pytest.raises(HostStartupRefused):
        host.start()
    assert built == []


def test_the_fence_survives_acknowledgement_and_only_reconciliation_clears_it(tmp_path):
    _fence(tmp_path).raise_fence("previous fatal shutdown")

    host = _host(tmp_path)
    with pytest.raises(HostStartupRefused):
        host.start()

    host.fence.acknowledge("olivia", "disk full; freed space; positions checked in TWS")
    with pytest.raises(HostStartupRefused):
        host.start()                       # acknowledgement alone is not enough

    host.retire_fence_after_reconciliation(reconciled=True)
    controller = host.start()
    try:
        assert controller is not None
    finally:
        host.close()


def test_host_refuses_when_another_host_owns_the_journal(tmp_path):
    incumbent = _host(tmp_path)
    incumbent.start()
    try:
        with pytest.raises(HostStartupRefused) as excinfo:
            _host(tmp_path).start()
        assert excinfo.value.code == EXIT_NOT_OWNER
    finally:
        incumbent.close()


def test_host_refuses_a_date_outside_calendar_coverage(tmp_path):
    """2027-01-01 is a Friday and every 2027 holiday used to be a full session."""
    clock = ManualClock(datetime(2027, 1, 1, 15, 0, tzinfo=timezone.utc))
    with pytest.raises(HostStartupRefused) as excinfo:
        _host(tmp_path, clock=clock).start()
    assert excinfo.value.code == EXIT_CALENDAR


def test_host_refuses_a_fence_sharing_the_journal_volume(tmp_path):
    with pytest.raises(HostStartupRefused) as excinfo:
        _host(tmp_path, allow_shared=False).start()
    assert excinfo.value.code == EXIT_STARTUP


def test_host_writes_a_status_file_the_watchdog_can_read(tmp_path):
    import json

    host = _host(tmp_path)
    host.start()
    try:
        host.run_once()
        status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
        assert status["operating_mode"] == "NORMAL"
        assert status["fatal_shutdown_requested"] is False
        assert "pid" in status
    finally:
        host.close()


def test_exit_codes_are_distinct():
    """A supervisor branches on these; a collision would silently merge cases."""
    codes = [
        EXIT_OK, EXIT_FATAL_SHUTDOWN, EXIT_NOT_OWNER,
        EXIT_FENCED, EXIT_CALENDAR, EXIT_STARTUP,
    ]
    assert len(set(codes)) == len(codes)
