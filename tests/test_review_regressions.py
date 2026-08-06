"""Safety regressions found during the 2026-08-06 independent review."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from ib_execution.calendar import TradingCalendar
from ib_execution.controller import Controller, ExecutionPolicy
from ib_execution.models import (
    BrokerOrder,
    EventType,
    Execution,
    LinkState,
    OperatingMode,
    OrderState,
    Side,
    SyncState,
)
from ib_execution.risk import RiskEngine
from conftest import quote, target


def rebuild(journal, broker, risk_config, clock) -> Controller:
    c = Controller(
        journal=journal,
        broker=broker,
        risk=RiskEngine(risk_config, clock),
        clock=clock,
        calendar=TradingCalendar(),
        policy=ExecutionPolicy(),
    )
    c.restore_from_journal()
    c.on_connected(99)
    c.on_quote(quote(clock.now()))
    return c


def test_restart_rehydrates_working_order_before_new_target(
    ctl, journal, broker, risk_config, clock, faults
):
    """A restart must not turn one live BUY into BUY+SELL working simultaneously."""
    faults.no_fill = True
    ctl.submit_target(target(3, clock, ttl_seconds=3600))
    broker.pump()
    assert len(broker.place_calls) == 1

    c2 = rebuild(journal, broker, risk_config, clock)
    assert c2.reconcile() is True
    leg = c2.leg("manual_test", "SPY")
    assert leg.order_state is OrderState.WORKING
    assert leg.working_signed == 3
    assert leg.live_intent is not None

    c2.submit_target(target(0, clock, ttl_seconds=3600))
    assert len(broker.place_calls) == 1, "must cancel the existing BUY, not place SELL beside it"
    assert len(broker.cancel_calls) == 1


def test_reprice_timer_never_cancels_while_disconnected(ctl, broker, clock, faults):
    faults.no_fill = True
    ctl.submit_target(target(3, clock, ttl_seconds=3600))
    broker.pump()
    assert ctl.leg("manual_test", "SPY").order_state is OrderState.WORKING

    broker.disconnect("injected")
    clock.advance(ctl.policy.order_timeout_seconds + 1)
    ctl.tick()

    assert broker.cancel_calls == []
    assert ctl.link_state is LinkState.DISCONNECTED
    assert ctl.sync_state is SyncState.UNVERIFIED


def test_1101_requires_explicit_restore_and_reconcile(ctl, broker, clock):
    ctl.on_market_data_lost()
    assert ctl.link_state is LinkState.DEGRADED
    assert ctl.sync_state is SyncState.UNVERIFIED

    ctl.on_quote(quote(clock.now()))
    assert ctl.link_state is LinkState.DEGRADED, "one quote is not subscription recovery"
    assert ctl.sync_state is SyncState.UNVERIFIED
    assert ctl.submit_target(target(1, clock)) is False

    ctl.on_market_data_restored()
    assert ctl.link_state is LinkState.CONNECTED
    assert ctl.sync_state is SyncState.UNVERIFIED
    assert ctl.reconcile() is True
    assert ctl.sync_state is SyncState.SYNCED


def test_forged_whitelisted_order_ref_is_external(ctl, broker):
    """A matching strategy prefix is not ownership."""
    broker._orders["manual_test|forged|deadbeef"] = BrokerOrder(  # noqa: SLF001
        order_ref="manual_test|forged|deadbeef",
        perm_id=777001,
        broker_order_id=7001,
        symbol="SPY",
        side=Side.BUY,
        total_quantity=1,
        filled_quantity=0,
        status="Submitted",
    )
    assert ctl.reconcile() is False
    assert ctl.operating_mode is OperatingMode.HALTED


def test_external_completed_execution_is_not_silently_adopted(ctl, broker, clock):
    ex = Execution(
        exec_id="EXT00001.01",
        order_ref="manual_test|forged|deadbeef",
        perm_id=777002,
        symbol="SPY",
        side=Side.BUY,
        quantity=1,
        price=Decimal("600"),
        ts=clock.now(),
    )
    broker._executions.append(ex)  # noqa: SLF001
    broker.force_position("SPY", 1)

    assert ctl.reconcile() is False
    assert ctl.operating_mode is OperatingMode.HALTED


def test_risk_runaway_counters_survive_restart(
    ctl, journal, broker, risk_config, clock, faults
):
    faults.no_fill = True
    ctl.submit_target(target(1, clock, ttl_seconds=3600))
    broker.pump()
    assert ctl.risk.snapshot()["orders"] == 1

    c2 = rebuild(journal, broker, risk_config, clock)
    assert c2.risk.snapshot()["orders"] == 1


def test_live_external_execution_callback_halts_without_booking(ctl, clock):
    ex = Execution(
        exec_id="EXTLIVE01.01",
        order_ref="manual_test|forged|live",
        perm_id=888001,
        symbol="SPY",
        side=Side.BUY,
        quantity=1,
        price=Decimal("600"),
        ts=clock.now(),
    )
    ctl.on_execution(ex)
    assert ctl.operating_mode is OperatingMode.HALTED
    assert not [
        e for e in ctl.journal.events_of(__import__("ib_execution.models", fromlist=["EventType"]).EventType.EXECUTION_RECEIVED)
        if e.exec_id == ex.exec_id
    ]


def test_clean_broker_rejections_count_toward_runaway_cap(ctl, broker, clock, faults):
    faults.send_raises_rejected = True
    for i in range(ctl.risk.config.max_orders_per_minute + 3):
        ctl.submit_target(target(1 if i % 2 == 0 else -1, clock, decision_id=f"rej-{i}"))
    assert ctl.risk.snapshot()["orders"] == ctl.risk.config.max_orders_per_minute
    assert len(broker.place_calls) == ctl.risk.config.max_orders_per_minute


def test_ack_timeout_forces_unverified_and_blocks_resend(ctl, broker, clock, faults):
    """No ack by the deadline is uncertainty, never permission to retry."""
    faults.delay_ack = True
    faults.no_fill = True
    assert ctl.submit_target(target(3, clock, ttl_seconds=3600)) is True
    leg = ctl.leg("manual_test", "SPY")
    assert leg.order_state is OrderState.PENDING_ACK
    assert len(broker.place_calls) == 1

    clock.advance(ctl.policy.ack_timeout_seconds + 1)
    ctl.tick()

    assert leg.order_state is OrderState.SUBMISSION_UNCERTAIN
    assert ctl.sync_state is SyncState.UNVERIFIED
    ctl.submit_target(target(4, clock, ttl_seconds=3600))
    assert len(broker.place_calls) == 1


def test_target_expiry_cancels_working_before_reprice(ctl, broker, clock, faults):
    """A still-working order must be cancelled as soon as its target expires."""
    faults.no_fill = True
    ttl = 5
    assert ttl < ctl.policy.order_timeout_seconds
    ctl.submit_target(target(3, clock, ttl_seconds=ttl))
    broker.pump()
    assert ctl.leg("manual_test", "SPY").order_state is OrderState.WORKING

    clock.advance(ttl + 1)
    ctl.tick()

    assert len(broker.cancel_calls) == 1
    assert ctl.leg("manual_test", "SPY").order_state is OrderState.PENDING_CANCEL


def test_reconciliation_terminalizes_broker_proven_absent_intent(
    ctl, broker, clock, faults
):
    """An uncertain send proven absent must not stay open forever in the journal."""
    faults.send_raises_uncertain = True
    ctl.submit_target(target(3, clock, ttl_seconds=3600))
    leg = ctl.leg("manual_test", "SPY")
    order_ref = leg.live_intent.order_ref
    assert leg.order_state is OrderState.SUBMISSION_UNCERTAIN

    # Model a broker snapshot that proves the order neither remains open nor filled.
    broker._orders.pop(order_ref)  # noqa: SLF001
    faults.send_raises_uncertain = False
    assert ctl.reconcile() is True

    from ib_execution.models import EventType

    absent = ctl.journal.events_of(EventType.ORDER_ABSENT_CONFIRMED)
    assert any(e.order_ref == order_ref for e in absent)
    assert order_ref not in ctl._journal_open_intents_by_ref()  # noqa: SLF001
    # Once the broker has proved the uncertain submission absent, the still-valid
    # desired target may safely create a fresh intent. It must not resurrect or
    # duplicate the old broker identity.
    assert leg.order_state is OrderState.PENDING_ACK
    assert leg.live_intent is not None
    assert leg.live_intent.order_ref != order_ref


def test_reconciliation_persists_recovered_broker_identity(
    ctl, journal, broker, risk_config, clock, faults
):
    """A restart-recovered working order must durably acquire broker identity."""
    faults.no_fill = True
    faults.delay_ack = True
    ctl.submit_target(target(3, clock, ttl_seconds=3600))
    original_ref = broker.place_calls[-1].order_ref
    assert not [
        e for e in journal.events_of(EventType.BROKER_ACK_RECEIVED)
        if e.order_ref == original_ref
    ]

    c2 = rebuild(journal, broker, risk_config, clock)
    assert c2.reconcile() is True

    recovered = [
        e
        for e in journal.events_of(EventType.BROKER_ACK_RECEIVED)
        if e.order_ref == original_ref and e.payload.get("source") == "reconcile"
    ]
    assert recovered
    intent = c2._journal_intents_by_ref()[original_ref]  # noqa: SLF001
    assert intent.perm_id is not None
    assert intent.broker_order_id is not None
