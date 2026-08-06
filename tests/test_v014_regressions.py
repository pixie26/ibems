"""Review regressions added while auditing uploaded v0.1.4."""

from __future__ import annotations

from ib_execution.models import OrderState, SyncState
from conftest import target


def test_unstable_snapshot_cannot_terminalize_old_order_or_enable_replacement(
    ctl, broker, clock
):
    """
    A broker snapshot taken while position/order callbacks are backlogged is not
    a reconciliation barrier. Treating it as authoritative can mark an old
    order absent, send a replacement, then receive the old fill and breach the
    position limit.
    """
    assert ctl.submit_target(target(-4, clock))
    assert broker.pending_labels(), "the fake broker must have callbacks in flight"

    assert ctl.reconcile() is False
    assert ctl.sync_state is SyncState.UNVERIFIED
    assert ctl.leg("manual_test", "SPY").order_state is OrderState.PENDING_ACK

    before = len(broker.place_calls)
    ctl.submit_target(target(-2, clock))
    assert len(broker.place_calls) == before, "replacement sent from an unstable snapshot"

    broker.pump()
    assert broker.position("SPY") == -4
    assert abs(ctl.leg("manual_test", "SPY").position) <= 5

    assert ctl.reconcile() is True
    broker.pump()
    assert abs(broker.position("SPY")) <= 5


def test_restarts_preserve_original_halt_cause_without_nesting(
    ctl, journal, broker, risk_config, clock
):
    from ib_execution.calendar import TradingCalendar
    from ib_execution.controller import Controller, ExecutionPolicy
    from ib_execution.models import EventType, OperatingMode
    from ib_execution.risk import RiskEngine
    from conftest import quote

    ctl.halt("root cause")
    for epoch in range(2, 6):
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
        c.on_connected(epoch)
        c.on_quote(quote(clock.now()))
        c.reconcile()
        assert c.operating_mode is OperatingMode.HALTED

    halts = [
        event
        for event in journal.replay()
        if event.event_type is EventType.OPERATING_MODE_CHANGED
        and event.payload.get("to") == "HALTED"
    ]
    assert len(halts) == 1
    assert halts[0].payload["why"] == "root cause"
