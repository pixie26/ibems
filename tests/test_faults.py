"""
Gate B1 acceptance: every scenario here is a failure IB paper cannot reproduce.

The bar is not "recovers gracefully". The bar is:
  - no duplicate order, under any interleaving
  - no silent state loss
  - when the state is unknown, the system stops
"""

from __future__ import annotations

import pytest

from ib_execution.models import (
    EventType,
    LinkState,
    OperatingMode,
    OrderState,
    SyncState,
)
from conftest import quote, target


# --------------------------------------------------------------------------
# submission uncertainty -- the dangerous one
# --------------------------------------------------------------------------


def test_send_uncertain_does_not_retry(ctl, broker, clock, faults):
    """
    The order MAY exist at the broker. We must not send a second one.

    at-most-once beats at-least-once: a missed trade costs an opportunity,
    a duplicate costs a position nobody sized for.
    """
    faults.send_raises_uncertain = True
    ctl.submit_target(target(3, clock))

    leg = ctl.leg("manual_test", "SPY")
    assert leg.order_state is OrderState.SUBMISSION_UNCERTAIN
    assert ctl.sync_state is SyncState.UNVERIFIED, "coupling rule C1"
    assert len(broker.place_calls) == 1

    # A further target must NOT produce a second order while state is unknown.
    ctl.submit_target(target(5, clock))
    assert len(broker.place_calls) == 1


def test_uncertain_resolved_by_reconciliation(ctl, broker, clock, faults):
    faults.send_raises_uncertain = True
    ctl.submit_target(target(3, clock))
    assert ctl.sync_state is SyncState.UNVERIFIED

    faults.send_raises_uncertain = False
    ok = ctl.reconcile()

    assert ok, "broker snapshot should explain the order"
    assert ctl.sync_state is SyncState.SYNCED
    leg = ctl.leg("manual_test", "SPY")
    assert leg.order_state is OrderState.WORKING, "the order did exist"
    assert len(broker.place_calls) == 1, "still exactly one order"


def test_clean_rejection_returns_to_idle(ctl, broker, clock, faults):
    """A definite refusal is safe: no order exists, so no reconcile needed."""
    faults.send_raises_rejected = True
    ctl.submit_target(target(3, clock))

    leg = ctl.leg("manual_test", "SPY")
    assert leg.order_state is OrderState.IDLE
    assert ctl.sync_state is SyncState.SYNCED


# --------------------------------------------------------------------------
# callback pathologies
# --------------------------------------------------------------------------


def test_duplicate_callbacks_are_idempotent(ctl, broker, clock, faults):
    faults.duplicate_callbacks = True
    ctl.submit_target(target(3, clock))
    broker.pump()

    assert ctl.leg("manual_test", "SPY").position == 3, "double-counted fill"
    execs = ctl.journal.events_of(EventType.EXECUTION_RECEIVED)
    assert len(execs) == 1, "execId booked twice"


def test_reordered_callbacks_do_not_corrupt_state(ctl, broker, clock, faults):
    faults.reorder_callbacks = True
    ctl.submit_target(target(3, clock))
    broker.pump()

    leg = ctl.leg("manual_test", "SPY")
    assert leg.position == 3
    assert not ctl.violations


def test_execution_correction_reverses_never_mutates(ctl, broker, clock, faults):
    faults.emit_execution_correction = True
    ctl.submit_target(target(3, clock))
    broker.pump()

    events = ctl.journal.events_of(EventType.EXECUTION_RECEIVED)
    reversals = [e for e in events if e.payload.get("is_reversal")]
    corrections = ctl.journal.events_of(EventType.EXECUTION_CORRECTED)

    assert reversals, "correction must produce a reversal event"
    assert corrections, "correction must produce a corrected event"
    # net position after reverse+rebook is unchanged
    assert ctl.leg("manual_test", "SPY").position == 3


def test_late_fee_is_a_legal_intermediate_state(ctl, broker, clock, faults):
    faults.late_fee = True
    ctl.submit_target(target(3, clock))
    # deliver only the execution, not the fee
    broker.pump(limit=3)

    assert ctl.status()["fees_pending"] >= 0
    assert ctl.operating_mode is OperatingMode.NORMAL, "missing fee is not an error"


# --------------------------------------------------------------------------
# cancel races
# --------------------------------------------------------------------------


def test_fill_before_cancel_does_not_double_trade(ctl, broker, clock, faults):
    """
    The order fills in the window between our cancel and its acknowledgement.

    We must not pre-compute the reversal quantity when sending the cancel.
    """
    faults.no_fill = True
    ctl.submit_target(target(3, clock))
    broker.pump()
    assert ctl.leg("manual_test", "SPY").order_state is OrderState.WORKING

    faults.fill_before_cancel = True
    faults.no_fill = False
    ctl.submit_target(target(-3, clock))
    broker.pump()

    leg = ctl.leg("manual_test", "SPY")
    assert leg.order_state is OrderState.TERMINAL_UNRECONCILED
    assert ctl.sync_state is SyncState.UNVERIFIED, "must not trust state after this"
    # crucially: no blind SELL 6 was issued
    sells = [c for c in broker.place_calls if c.side.value == "SELL"]
    assert sells == [], "must reconcile before computing the reversal"


def test_cancel_timeout_halts_rather_than_guesses(ctl, broker, clock, faults):
    faults.no_fill = True
    ctl.submit_target(target(3, clock))
    broker.pump()

    faults.cancel_silent = True
    ctl.submit_target(target(0, clock))
    broker.pump()
    assert ctl.leg("manual_test", "SPY").order_state is OrderState.PENDING_CANCEL

    clock.advance(ctl.policy.cancel_timeout_seconds + 1)
    ctl.tick()

    leg = ctl.leg("manual_test", "SPY")
    assert leg.order_state is OrderState.TERMINAL_UNRECONCILED
    assert ctl.sync_state is SyncState.UNVERIFIED
    assert len(broker.cancel_calls) == 1, "must not blindly re-cancel"


def test_no_second_order_while_cancel_outstanding(ctl, broker, clock, faults):
    """Invariant 5."""
    faults.no_fill = True
    ctl.submit_target(target(3, clock))
    broker.pump()
    n = len(broker.place_calls)

    faults.cancel_silent = True
    ctl.submit_target(target(-3, clock))
    ctl.submit_target(target(5, clock))
    ctl.submit_target(target(-5, clock))

    assert len(broker.place_calls) == n, "no order may be sent during PENDING_CANCEL"


# --------------------------------------------------------------------------
# connectivity
# --------------------------------------------------------------------------


def test_reconnect_forces_unverified(ctl, broker, clock):
    """
    IB 1101: the socket is back, the subscriptions are not.

    A reconnect is never sufficient to resume trading.
    """
    broker.disconnect("gateway restart")
    assert ctl.link_state is LinkState.DISCONNECTED

    broker.reconnect(market_data_lost=True)
    assert ctl.sync_state is SyncState.UNVERIFIED
    assert ctl.link_state is LinkState.DEGRADED, "market data lost"

    ctl.submit_target(target(3, clock))
    assert broker.place_calls == [], "must not trade before reconciling"


def test_no_write_while_disconnected(ctl, broker, clock):
    ctl.on_disconnected("test")
    assert ctl.submit_target(target(3, clock)) is False
    assert broker.place_calls == []


def test_stale_snapshot_is_detected(ctl, broker, clock, faults):
    """Broker snapshot missing the newest fill must not silently pass."""
    ctl.submit_target(target(3, clock))
    broker.pump()

    faults.stale_snapshot = True
    ok = ctl.reconcile()

    assert ok is False
    assert ctl.operating_mode is OperatingMode.HALTED


# --------------------------------------------------------------------------
# external interference
# --------------------------------------------------------------------------


def test_external_order_halts(ctl, broker, clock, faults):
    """A manually placed TWS order: never adopt, never ignore."""
    faults.external_order = True
    ok = ctl.reconcile()

    assert ok is False
    assert ctl.operating_mode is OperatingMode.HALTED
    assert ctl.journal.events_of(EventType.EXTERNAL_ORDER_DETECTED)


def test_unknown_position_halts(ctl, broker, clock):
    broker.force_position("SPY", 77)
    ok = ctl.reconcile()

    assert ok is False
    assert ctl.operating_mode is OperatingMode.HALTED


# --------------------------------------------------------------------------
# reprice ladder must be bounded
# --------------------------------------------------------------------------


def test_reprice_ladder_is_bounded(ctl, broker, clock, faults):
    """
    Unbounded cancel-then-new is chasing, and this strategy's P&L lives in
    exactly the fast markets where chasing is most expensive.
    """
    faults.no_fill = True
    # TTL deliberately long: we want the LADDER to stop this, not expiry.
    # (In production expiry usually fires first, which is also correct.)
    ctl.submit_target(target(3, clock, ttl_seconds=100_000))
    broker.pump()

    for _ in range(ctl.policy.max_attempts + 3):
        clock.advance(ctl.policy.order_timeout_seconds + 1)
        ctl.on_quote(quote(clock.now()))  # live feed keeps ticking
        ctl.tick()
        broker.pump()

    assert len(broker.place_calls) <= ctl.policy.max_attempts, (
        f"sent {len(broker.place_calls)} orders, ladder capped at "
        f"{ctl.policy.max_attempts}"
    )
    misses = [
        e for e in ctl.journal.events_of(EventType.DECISION_MISSED)
        if e.payload["reason"] == "REPRICE_EXHAUSTED"
    ]
    assert misses, "exhausted ladder must be recorded as a miss, not retried forever"
