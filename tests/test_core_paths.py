"""Gate B1: basic target-position arithmetic and lifecycle."""

from __future__ import annotations

from ib_execution.models import EventType, OrderState, Side
from conftest import quote, target


def test_flat_to_long(ctl, broker, clock):
    ctl.submit_target(target(3, clock))
    broker.pump()
    assert broker.position("SPY") == 3
    assert ctl.leg("manual_test", "SPY").position == 3
    assert ctl.leg("manual_test", "SPY").order_state is OrderState.IDLE


def test_long_to_flat(ctl, broker, clock):
    ctl.submit_target(target(3, clock))
    broker.pump()
    ctl.submit_target(target(0, clock))
    broker.pump()
    assert broker.position("SPY") == 0
    assert len(broker.place_calls) == 2
    assert broker.place_calls[1].side is Side.SELL
    assert broker.place_calls[1].quantity == 3


def test_flip_sends_double_quantity(ctl, broker, clock):
    """+3 -> -3 must be SELL 6, never SELL 3. The classic reversal bug."""
    ctl.submit_target(target(3, clock))
    broker.pump()
    assert broker.position("SPY") == 3

    ctl.submit_target(target(-3, clock))
    broker.pump()

    assert broker.place_calls[-1].side is Side.SELL
    assert broker.place_calls[-1].quantity == 6
    assert broker.position("SPY") == -3


def test_repeated_identical_target_does_not_trade(ctl, broker, clock):
    ctl.submit_target(target(3, clock))
    broker.pump()
    n = len(broker.place_calls)

    ctl.submit_target(target(3, clock))  # new decision_id, same quantity
    broker.pump()

    assert len(broker.place_calls) == n, "identical target must not re-trade"


def test_duplicate_decision_id_rejected(ctl, broker, clock):
    """Invariant 1, enforced by a database PRIMARY KEY."""
    t = target(3, clock, decision_id="fixed-id")
    assert ctl.submit_target(t) is True
    broker.pump()
    n = len(broker.place_calls)

    t2 = target(5, clock, decision_id="fixed-id")
    assert ctl.submit_target(t2) is False
    broker.pump()

    assert len(broker.place_calls) == n
    rejects = [
        e for e in ctl.journal.events_of(EventType.TARGET_REJECTED)
        if e.payload.get("reason") == "duplicate_decision_id"
    ]
    assert len(rejects) == 1


def test_expired_target_never_sent(ctl, broker, clock):
    """Invariant 11. A 10:00 signal is not a 10:20 trade."""
    t = target(3, clock, ttl_seconds=30)
    clock.advance(45)
    assert ctl.submit_target(t) is False
    broker.pump()
    assert broker.place_calls == []

    misses = ctl.journal.events_of(EventType.DECISION_MISSED)
    assert any(m.payload["reason"] == "EXPIRED" for m in misses)


def test_partial_fill_tracks_remaining(ctl, broker, clock, faults):
    faults.partial_fill_qty = 2
    ctl.submit_target(target(5, clock))
    broker.pump()

    leg = ctl.leg("manual_test", "SPY")
    assert broker.position("SPY") == 2
    assert leg.position == 2
    assert leg.working_signed == 3, "3 shares still working"
    assert leg.order_state is OrderState.WORKING


def test_partial_fill_then_new_target_recomputes(ctl, broker, clock, faults):
    faults.partial_fill_qty = 2
    ctl.submit_target(target(5, clock))
    broker.pump()
    assert ctl.leg("manual_test", "SPY").position == 2

    faults.partial_fill_qty = None
    ctl.submit_target(target(0, clock))
    broker.pump()

    # must cancel the working remainder first, then flatten the 2 actually held
    assert broker.cancel_calls, "working order must be cancelled before reversing"
    assert broker.position("SPY") == 0


def test_decision_miss_is_recorded_when_not_synced(ctl, broker, clock):
    """A transient outage is deferred; it becomes a miss only at expiry."""
    ctl.on_disconnected("test")
    ctl.submit_target(target(3, clock))
    assert ctl.journal.events_of(EventType.TARGET_DEFERRED)
    assert not ctl.journal.events_of(EventType.DECISION_MISSED)
    assert broker.place_calls == []

    clock.advance(61)
    ctl.tick()
    misses = ctl.journal.events_of(EventType.DECISION_MISSED)
    assert any(m.payload["reason"] == "EXPIRED" for m in misses)
