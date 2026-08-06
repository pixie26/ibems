"""Regressions found while reviewing v0.1.2.

These are lifecycle failures, not cosmetic edge cases.  Each test pins a state
transition that can otherwise create duplicate orders, stale positions or a
false cost-model miss.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ib_execution.models import (
    EventType,
    Execution,
    LinkState,
    OperatingMode,
    OrderState,
    Side,
    SyncState,
)
from ib_execution.watchdog import Watchdog, WatchdogConfig
from conftest import target


def test_target_change_resets_reprice_ladder(ctl, broker, faults, clock):
    faults.no_fill = True
    ctl.submit_target(target(3, clock, ttl_seconds=600))
    broker.pump()

    # First timeout legitimately consumes one reprice rung.
    clock.advance(ctl.policy.order_timeout_seconds + 1)
    # Repricing requires a fresh quote; otherwise the correct outcome is a
    # quote-stale risk rejection rather than another order.
    from conftest import quote
    ctl.on_quote(quote(clock.now()))
    ctl.tick()
    broker.pump()
    assert broker.place_calls[-1].attempt == 1

    # A new target is a new execution decision, not another rung of the old one.
    ctl.submit_target(target(-2, clock, ttl_seconds=600))
    broker.pump()
    assert broker.place_calls[-1].target_quantity == -2
    assert broker.place_calls[-1].attempt == 0


def test_eod_flatten_cancels_working_then_converges_to_zero(
    ctl, broker, faults, clock
):
    ctl.submit_target(target(3, clock, ttl_seconds=3600))
    broker.pump()
    assert ctl.leg("manual_test", "SPY").position == 3

    # Leave a second order working when the EOD window opens.
    faults.no_fill = True
    ctl.submit_target(target(5, clock, ttl_seconds=3600))
    broker.pump()
    assert ctl.leg("manual_test", "SPY").order_state is OrderState.WORKING

    clock.set(datetime(2026, 8, 5, 19, 41, tzinfo=timezone.utc))  # 15:41 ET
    ctl.tick()
    broker.pump()

    last = broker.place_calls[-1]
    assert last.target_quantity == 0
    assert last.side is Side.SELL
    assert last.quantity == 3
    assert last.attempt == 0
    live = [o for o in broker.snapshot().open_orders if o.status in ("PreSubmitted", "Submitted")]
    assert len(live) <= 1


def test_cancel_rejection_does_not_make_our_working_order_external(
    ctl, broker, faults, clock, journal
):
    faults.no_fill = True
    ctl.submit_target(target(3, clock, ttl_seconds=600))
    broker.pump()
    faults.cancel_rejects = True

    ctl.submit_target(target(-2, clock, ttl_seconds=600))
    broker.pump()
    # The order is still at the broker. Reconciliation restores it as WORKING,
    # but a rejected cancel is never retried automatically: the engine HALTs
    # for human inspection instead of entering a cancel-reject loop.
    assert ctl.leg("manual_test", "SPY").order_state is OrderState.WORKING
    assert ctl.operating_mode is OperatingMode.HALTED
    assert len(broker.cancel_calls) == 1
    assert journal.events_of(EventType.CANCEL_REJECTED)
    assert not journal.events_of(EventType.EXTERNAL_ORDER_DETECTED)


def test_late_execution_after_cancel_updates_position_and_forces_reconcile(
    ctl, broker, faults, clock
):
    faults.no_fill = True
    ctl.submit_target(target(3, clock, ttl_seconds=600))
    broker.pump()
    leg = ctl.leg("manual_test", "SPY")
    intent = leg.live_intent
    assert intent is not None and intent.perm_id is not None

    ctl.submit_target(target(0, clock, ttl_seconds=600))
    broker.pump()
    assert leg.order_state is OrderState.IDLE
    assert leg.position == 0

    late = Execution(
        exec_id="LATE0001.01",
        order_ref=intent.order_ref,
        perm_id=intent.perm_id,
        symbol="SPY",
        side=Side.BUY,
        quantity=1,
        price=Decimal("600"),
        ts=clock.now(),
    )
    ctl.on_execution(late)

    assert leg.position == 1
    assert leg.order_state is OrderState.TERMINAL_UNRECONCILED
    assert ctl.sync_state is SyncState.UNVERIFIED


def test_order_callback_cannot_repair_account_sync_after_1101(
    ctl, broker, faults, clock
):
    faults.no_fill = True
    ctl.submit_target(target(3, clock, ttl_seconds=600))
    leg = ctl.leg("manual_test", "SPY")
    ref = leg.live_intent.order_ref

    ctl.on_market_data_lost()
    assert ctl.link_state is LinkState.DEGRADED
    assert ctl.sync_state is SyncState.UNVERIFIED

    broker.deliver_ack_now(ref)
    assert leg.order_state is OrderState.WORKING
    assert ctl.sync_state is SyncState.UNVERIFIED


def test_successful_eod_flatten_closes_lifecycle(ctl, broker, clock, journal):
    ctl.submit_target(target(3, clock, ttl_seconds=3600))
    broker.pump()
    clock.set(datetime(2026, 8, 5, 19, 41, tzinfo=timezone.utc))
    ctl.tick()
    broker.pump()

    leg = ctl.leg("manual_test", "SPY")
    assert leg.position == 0
    assert leg.flatten_reason is None
    assert journal.events_of(EventType.EOD_FLATTEN_COMPLETED)


def test_recording_eod_residual_never_downgrades_halted(
    ctl, broker, clock, journal
):
    ctl.submit_target(target(3, clock, ttl_seconds=3600))
    broker.pump()
    ctl.halt("injected")
    clock.set(datetime(2026, 8, 5, 19, 58, tzinfo=timezone.utc))
    ctl.tick()

    assert journal.events_of(EventType.EOD_FLATTEN_FAILED)
    assert ctl.operating_mode is OperatingMode.HALTED


def test_watchdog_refuses_kill_without_process_identity(tmp_path):
    alerts: list[tuple[str, str]] = []
    w = Watchdog(
        WatchdogConfig(status_path=tmp_path / "status.json"),
        alert=lambda level, msg: alerts.append((level, msg)),
    )
    assert w.kill_engine({"pid": 999_999_999}) is False
    assert any("cannot verify process identity" in msg for _, msg in alerts)


def test_temporary_disconnect_is_deferred_not_immediately_counted_as_miss(
    ctl, broker, clock, journal
):
    broker.disconnect("test")
    t = target(3, clock, ttl_seconds=30)
    assert ctl.submit_target(t) is False
    assert journal.events_of(EventType.TARGET_DEFERRED)
    assert not journal.events_of(EventType.DECISION_MISSED)

    clock.advance(31)
    ctl.tick()
    misses = journal.events_of(EventType.DECISION_MISSED)
    assert len(misses) == 1
    assert misses[0].payload["reason"] == "EXPIRED"


def test_latest_target_converges_after_reconnect_and_reconcile(
    ctl, broker, faults, clock
):
    faults.no_fill = True
    ctl.submit_target(target(3, clock, ttl_seconds=600))
    broker.pump()
    first_ref = ctl.leg("manual_test", "SPY").live_intent.order_ref

    broker.disconnect("test")
    ctl.submit_target(target(-2, clock, ttl_seconds=600))
    assert ctl.leg("manual_test", "SPY").desired_target.target_quantity == -2

    broker.reconnect()
    ctl.on_market_data_restored()
    from conftest import quote
    ctl.on_quote(quote(clock.now()))
    assert ctl.reconcile() is True
    # Reconciliation restores the broker-open +3 order, then notices the latest
    # desired target differs and requests cancellation before replacement.
    assert ctl.leg("manual_test", "SPY").order_state is OrderState.PENDING_CANCEL
    assert broker.cancel_calls[-1] == first_ref
    broker.pump()
    assert broker.place_calls[-1].target_quantity == -2
    assert broker.place_calls[-1].order_ref != first_ref


def test_auditor_checks_daily_share_and_notional_caps(journal):
    from ib_execution.auditor import JournalAuditor

    journal.commit(
        EventType.SEND_ATTEMPT_STARTED,
        {"attempt": 0, "quantity": 6, "price": "100"},
        intent_id="i1",
        order_ref="r1",
    )
    findings = JournalAuditor(
        journal.replay(),
        max_orders_per_day=50,
        max_orders_per_minute=10,
        max_daily_shares=5,
        max_daily_notional=Decimal("500"),
    ).audit()
    i16 = [f for f in findings if f.invariant == 16]
    assert any("submitted shares" in f.detail for f in i16)
    assert any("submitted notional" in f.detail for f in i16)


def test_auditor_treats_flat_position_with_working_order_as_residual(journal):
    from ib_execution.auditor import JournalAuditor

    journal.commit(
        EventType.EOD_FLATTEN_FAILED,
        {"symbol": "SPY", "residual_quantity": 0, "working_signed": 3},
        strategy_id="manual_test",
        symbol="SPY",
    )
    journal.commit(EventType.PROCESS_STARTED, {})
    journal.commit(
        EventType.ORDER_INTENT_COMMITTED,
        {
            "target_quantity": 1,
            "risk_config_hash": "x",
        },
        strategy_id="manual_test",
        symbol="SPY",
        intent_id="i-after-residual",
        order_ref="r-after-residual",
    )
    findings = JournalAuditor(journal.replay()).audit()
    assert any(f.invariant == 15 for f in findings)


def test_clean_cancel_reconciles_before_replacement(ctl, broker, faults, clock, journal):
    faults.no_fill = True
    ctl.submit_target(target(3, clock, ttl_seconds=600))
    broker.pump()
    before = len(journal.events_of(EventType.RECONCILIATION_STARTED))

    ctl.submit_target(target(-2, clock, ttl_seconds=600))
    broker.pump()

    after = len(journal.events_of(EventType.RECONCILIATION_STARTED))
    assert after == before + 1
    assert broker.place_calls[-1].target_quantity == -2
    assert ctl.sync_state is SyncState.SYNCED


def test_auditor_fee_rule_does_not_match_verify_substring(journal):
    from ib_execution.auditor import JournalAuditor

    journal.commit(
        EventType.OPERATING_MODE_CHANGED,
        {"from": "NORMAL", "to": "HALTED", "why": "verify broker state in TWS"},
    )
    findings = JournalAuditor(journal.replay()).audit()
    assert not [f for f in findings if f.invariant == 13]
