"""
Restart and reconciliation.

The premise: a process can die at any instruction. What survives is the journal
and whatever the broker knows. Everything in memory is gone. These tests build
a fresh Controller over the same journal, exactly as a restart would.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from ib_execution.calendar import TradingCalendar
from ib_execution.controller import Controller, ExecutionPolicy
from ib_execution.models import (
    EventType,
    OperatingMode,
    OrderState,
    SyncState,
)
from ib_execution.risk import RiskEngine
from conftest import quote, target


def rebuild(journal, broker, risk_config, clock) -> Controller:
    """Simulate a process restart: new Controller, same journal, same broker."""
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
    return c


def test_restart_rebuilds_position_from_journal(ctl, journal, broker, risk_config, clock):
    ctl.submit_target(target(3, clock))
    broker.pump()
    assert broker.position("SPY") == 3

    c2 = rebuild(journal, broker, risk_config, clock)
    assert c2.sync_state is SyncState.UNVERIFIED, "must not trust memory-free boot"

    c2.on_quote(quote(clock.now()))
    assert c2.reconcile() is True
    assert c2.leg("manual_test", "SPY").position == 3


def test_restart_does_not_send_before_reconcile(ctl, journal, broker, risk_config, clock):
    """Invariant 10."""
    c2 = rebuild(journal, broker, risk_config, clock)
    c2.on_quote(quote(clock.now()))
    n = len(broker.place_calls)

    c2.submit_target(target(3, clock))

    assert len(broker.place_calls) == n, "no send permitted before reconciliation"


def test_crash_after_send_is_resolved_not_resent(ctl, journal, broker, risk_config, clock, faults):
    """
    Crash between placeOrder and the callback.

    The order exists at the broker. A restart must find it, not send another.
    """
    faults.no_fill = True
    ctl.submit_target(target(3, clock))
    broker.pump()
    assert len(broker.place_calls) == 1

    c2 = rebuild(journal, broker, risk_config, clock)
    c2.on_quote(quote(clock.now()))
    assert c2.reconcile() is True

    # Same target arrives again after restart -- must not duplicate.
    c2.submit_target(target(3, clock))
    broker.pump()
    assert len(broker.place_calls) == 1, "restart must not re-send an existing order"


def test_explained_overnight_residual_is_flatten_only_not_halt(
    ctl, journal, broker, risk_config, clock
):
    """
    Invariant 15.

    A failed EOD flatten leaves a LEGAL position. If a restart treats it as an
    unknown position, every morning after an incident starts with a false HALT.
    But it does not get to be forgotten either: we come up FLATTEN_ONLY.
    """
    ctl.submit_target(target(3, clock))
    broker.pump()

    journal.commit(
        EventType.EOD_FLATTEN_FAILED,
        {"symbol": "SPY", "residual_quantity": 3, "failure_reason": "no liquidity"},
        symbol="SPY",
    )

    c2 = rebuild(journal, broker, risk_config, clock)
    c2.on_quote(quote(clock.now()))

    assert c2.operating_mode is OperatingMode.FLATTEN_ONLY
    assert c2.reconcile() is True, "residual is explained, not unknown"
    assert c2.leg("manual_test", "SPY").position == 3


def test_flatten_only_refuses_to_open(ctl, broker, clock):
    """Invariant 8: FLATTEN_ONLY permits target=0 and nothing else."""
    ctl.submit_target(target(3, clock))
    broker.pump()
    ctl.set_mode(OperatingMode.FLATTEN_ONLY, "test")

    n = len(broker.place_calls)
    ctl.submit_target(target(5, clock))
    broker.pump()
    assert len(broker.place_calls) == n

    ctl.submit_target(target(0, clock))
    broker.pump()
    assert broker.position("SPY") == 0, "closing must still be permitted"


def test_stop_new_permits_closing_but_not_opening(ctl, broker, clock):
    """
    The asymmetry a flat state enum gets wrong.

    STOP_NEW must not prevent us reducing risk.
    """
    ctl.submit_target(target(3, clock))
    broker.pump()
    ctl.set_mode(OperatingMode.STOP_NEW, "test")

    n = len(broker.place_calls)
    ctl.submit_target(target(5, clock))
    broker.pump()
    assert len(broker.place_calls) == n, "opening blocked"

    ctl.submit_target(target(0, clock))
    broker.pump()
    assert broker.position("SPY") == 0, "closing allowed"


def test_halt_blocks_everything(ctl, broker, clock):
    ctl.halt("test")
    n = len(broker.place_calls)
    ctl.submit_target(target(3, clock))
    ctl.submit_target(target(0, clock))
    broker.pump()
    assert len(broker.place_calls) == n


def test_stale_quote_blocks_reprice(ctl, broker, clock, faults):
    """
    Discovered by the reprice test: if the feed goes quiet, we stop sending.

    Worth freezing as its own case. A repricing loop running on a frozen quote
    is how a system walks itself into the book at a price nobody chose.
    """
    faults.no_fill = True
    ctl.submit_target(target(3, clock, ttl_seconds=100_000))
    broker.pump()
    n = len(broker.place_calls)

    # Time passes; no new quotes arrive.
    for _ in range(3):
        clock.advance(ctl.policy.order_timeout_seconds + 1)
        ctl.tick()
        broker.pump()

    assert len(broker.place_calls) == n, "must not reprice against a stale quote"
    misses = [
        e for e in ctl.journal.events_of(EventType.DECISION_MISSED)
        if e.payload["reason"] == "RISK_BLOCKED"
    ]
    assert misses
