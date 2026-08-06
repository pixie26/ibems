"""
Regressions added in v0.1.2.

Each test here is pinned to a specific failure that a passing test suite missed.
The comment above each one records what went wrong and why the earlier tests did
not see it -- that context is the reason the test exists, and without it someone
will eventually "simplify" the test back into uselessness.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ib_execution.auditor import JournalAuditor, Finding
from ib_execution.calendar import TradingCalendar
from ib_execution.controller import Controller, ExecutionPolicy
from ib_execution.models import (
    EventType,
    LinkState,
    OperatingMode,
    OrderState,
    Quote,
    SyncState,
    TargetPosition,
)
from ib_execution.risk import RiskConfig, RiskEngine
from conftest import quote, target


def _rebuild(journal, broker, risk_config, clock) -> Controller:
    c = Controller(
        journal=journal,
        broker=broker,
        risk=RiskEngine(risk_config, clock),
        clock=clock,
        calendar=TradingCalendar(),
        policy=ExecutionPolicy(),
    )
    journal.commit(EventType.PROCESS_STARTED, {})
    c.restore_from_journal()
    c.on_connected(99)
    c.on_quote(quote(clock.now()))
    c.reconcile()
    return c


# --------------------------------------------------------------------------
# The duplicate-order bug that survived a 64-test suite
# --------------------------------------------------------------------------


def test_restart_then_DIFFERENT_target_does_not_duplicate(
    ctl, journal, broker, risk_config, clock, faults
):
    """
    v0.1.0 produced TWO simultaneously live orders here.

    Why the old suite missed it: the restart regression test re-sent the SAME
    quantity, so delta computed to zero and no second order was attempted. The
    bug only appears when the post-restart target DIFFERS -- which is the normal
    case in production, not the edge case.

    Root cause: reconciliation restored working_signed but not live_intent or
    order_state, so order_state stayed IDLE while an order was working at the
    broker. IDLE is not in BLOCKS_NEW_ORDER, so invariant 3's enforcement was
    silently disarmed.
    """
    faults.no_fill = True
    ctl.submit_target(target(3, clock))
    broker.pump()
    assert len(broker.place_calls) == 1

    c2 = _rebuild(journal, broker, risk_config, clock)

    leg = c2.leg("manual_test", "SPY")
    assert leg.order_state is OrderState.WORKING, "working order must be rehydrated"
    assert leg.live_intent is not None, "live intent must be rehydrated"

    c2.submit_target(target(-2, clock))
    live = [
        o for o in broker.snapshot().open_orders
        if o.status in ("PreSubmitted", "Submitted")
    ]
    assert len(live) <= 1, f"{len(live)} orders live simultaneously — invariant 3"
    assert broker.cancel_calls, "must cancel the working order, not race it"


def test_auditor_catches_the_duplicate_if_it_ever_returns(tmp_path, clock):
    """
    Pin the detector, not just the fix.

    The auditor was never wrong about this bug — the generated sequences simply
    never contained a restart. If someone reintroduces the defect, this proves
    the detector still fires.
    """
    from ib_execution.journal import Journal

    j = Journal(tmp_path / "forged.db", clock=clock)
    try:
        j.commit(EventType.PROCESS_STARTED, {})
        j.commit(EventType.RECONCILIATION_COMPLETED, {})
        j.commit(EventType.SYNC_STATE_CHANGED, {"from": "SYNCING", "to": "SYNCED"})
        for n in ("one", "two"):
            j.commit(
                EventType.ORDER_INTENT_COMMITTED,
                {"target_quantity": 3, "risk_config_hash": "x",
                 "valid_until": (clock.now() + timedelta(minutes=5)).isoformat()},
                strategy_id="manual_test", symbol="SPY",
                intent_id=n, order_ref=f"manual_test|d|{n}",
            )
            j.commit(
                EventType.SEND_ATTEMPT_STARTED, {"attempt": 0},
                strategy_id="manual_test", symbol="SPY",
                intent_id=n, order_ref=f"manual_test|d|{n}",
            )
        findings = JournalAuditor(j.replay()).audit()
        assert any(f.invariant == 3 for f in findings)
    finally:
        j.close()


# --------------------------------------------------------------------------
# EOD residual must be explained at the time, not inferred tomorrow
# --------------------------------------------------------------------------


def test_eod_residual_is_recorded_even_when_disconnected(ctl, broker, clock, journal):
    """
    v0.1.1 left an unflattened position with NO durable explanation.

    The failure is quiet in exactly the wrong way: the position is held
    overnight, tomorrow's reconciliation sees a position the journal cannot
    explain, and the system false-HALTs on a residual we actually understood.
    Invariant 15 only means something if the explanation is written at the time.

    A silent overnight position is as dangerous as an unknown one, and unlike an
    unknown one it is entirely preventable.
    """
    ctl.submit_target(target(3, clock))
    broker.pump()
    assert ctl.leg("manual_test", "SPY").position == 3

    broker.disconnect("gateway died before the close")
    clock.set(datetime(2026, 8, 5, 19, 50, tzinfo=timezone.utc))
    for _ in range(20):
        ctl.tick()
        clock.advance(60)

    failed = journal.events_of(EventType.EOD_FLATTEN_FAILED)
    assert failed, "no durable record of the unflattened position"
    assert failed[0].payload["residual_quantity"] == 3
    assert "link" in failed[0].payload["failure_reason"]
    assert ctl.operating_mode is OperatingMode.FLATTEN_ONLY


def test_eod_residual_recorded_once_per_session(ctl, broker, clock, journal):
    ctl.submit_target(target(3, clock))
    broker.pump()
    broker.disconnect("x")
    clock.set(datetime(2026, 8, 5, 19, 50, tzinfo=timezone.utc))
    for _ in range(40):
        ctl.tick()
        clock.advance(30)
    assert len(journal.events_of(EventType.EOD_FLATTEN_FAILED)) == 1


def test_eod_residual_recorded_even_when_halted(ctl, broker, clock, journal):
    """
    A HALTED system holding a position at the close still owes an explanation.

    HALT means "stop trading", not "stop keeping records". This is the case the
    original early-return guard silently skipped.
    """
    ctl.submit_target(target(3, clock))
    broker.pump()
    ctl.halt("injected for test")
    clock.set(datetime(2026, 8, 5, 19, 50, tzinfo=timezone.utc))
    for _ in range(20):
        ctl.tick()
        clock.advance(60)
    assert journal.events_of(EventType.EOD_FLATTEN_FAILED)


# --------------------------------------------------------------------------
# Auditor coverage added in v0.1.2
# --------------------------------------------------------------------------


def test_auditor_now_proves_the_runaway_breaker(tmp_path, clock):
    """
    Invariant 16 had no offline proof at all until now.

    The disaster case for an automated trader is not one bad order, it is a
    loop emitting ten thousand. That control deserves the same offline evidence
    as everything else.
    """
    from ib_execution.journal import Journal

    j = Journal(tmp_path / "runaway.db", clock=clock)
    try:
        for i in range(12):
            j.commit(
                EventType.SEND_ATTEMPT_STARTED, {"attempt": 0},
                strategy_id="manual_test", symbol="SPY",
                intent_id=f"i{i}", order_ref=f"manual_test|d{i}|x",
            )
        findings = JournalAuditor(j.replay(), max_orders_per_minute=4).audit()
        assert any(f.invariant == 16 for f in findings), "runaway not detected"
    finally:
        j.close()


def test_auditor_reports_honest_coverage(ctl, journal, broker, clock):
    """
    Coverage claims must be checkable and may not quietly omit structural rows.
    """
    ctl.submit_target(target(3, clock))
    broker.pump()
    s = JournalAuditor(journal.replay()).summary()
    assert set(s["audited_invariants"]) == set(range(1, 23))
    assert s["not_fully_audited"] == []


def test_clean_session_still_audits_clean(ctl, journal, broker, clock):
    journal.commit(EventType.PROCESS_STARTED, {})
    ctl.reconcile()
    ctl.submit_target(target(3, clock))
    broker.pump()
    ctl.submit_target(target(0, clock))
    broker.pump()
    findings = JournalAuditor(journal.replay()).audit()
    assert not findings, "\n".join(str(f) for f in findings)


# --------------------------------------------------------------------------
# Watchdog PID identity
# --------------------------------------------------------------------------


def test_watchdog_refuses_to_kill_a_recycled_pid(tmp_path):
    """
    PIDs are recycled. On a busy host the engine dies, its PID is reused by
    something unrelated, and a watchdog that trusts the number alone SIGKILLs an
    innocent process while the real engine is already gone.

    Same pid AND same start time, or we refuse to signal.
    """
    import os
    from ib_execution.watchdog import Watchdog, WatchdogConfig

    alerts: list[tuple[str, str]] = []
    w = Watchdog(
        WatchdogConfig(status_path=tmp_path / "s.json"),
        alert=lambda lvl, msg: alerts.append((lvl, msg)),
    )
    real = Watchdog._pid_start_ticks(os.getpid())
    assert real is not None, "platform process-creation identity unavailable"

    stale = {"pid": os.getpid(), "pid_start_ticks": real + 999_999}
    assert w.kill_engine(stale) is False
    assert any("recycled" in m for _, m in alerts)


def test_status_publishes_pid_identity(tmp_path):
    import json
    from ib_execution.watchdog import write_status

    p = tmp_path / "status.json"
    write_status(p, {"operating_mode": "NORMAL"})
    d = json.loads(p.read_text())
    assert d["pid"] and d["pid_start_ticks"] is not None
