"""
Property-based stress tests for the invariants currently implemented.

SPEC 20 remains a gate until docs/INVARIANT_COVERAGE.md is complete. These
random sequences are one layer of evidence, not proof that all 22 invariants
already have property/runtime/auditor coverage.

(c) matters most and is the one usually skipped. A green test suite proves the
code was right on inputs somebody thought of. Only the auditor proves that the
system which actually ran obeyed the spec on the day it ran.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

pytest.importorskip("hypothesis", reason="install the dev extra to run property tests")
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ib_execution.ack_halt import find_unacknowledged_halt
from ib_execution.auditor import JournalAuditor
from ib_execution.calendar import TradingCalendar
from ib_execution.clock import ManualClock
from ib_execution.controller import Controller, ExecutionPolicy
from ib_execution.fake_broker import FakeBroker, Faults
from ib_execution.journal import Journal
from ib_execution.models import (
    EventType,
    OperatingMode,
    Quote,
    TargetPosition,
)
from ib_execution.risk import RiskConfig, RiskEngine
from conftest import SESSION_START



pytestmark = pytest.mark.property

GATE_CAMPAIGN = settings.get_current_profile_name() == "gate"
PROPERTY_MAX_ACTIONS = 90 if GATE_CAMPAIGN else 40

# Weighted deliberately. Uniform sampling spends most of its time disconnected
# and barely exercises the order lifecycle -- correct behaviour, useless as a
# stress test. Disconnects stay in, but rare, the way they are in production.
ACTIONS = st.sampled_from(
    [
        "target", "target", "target", "target",
        "pump", "pump", "pump", "pump",
        "quote", "quote",
        "tick", "tick",
        "advance",
        "reconcile",
        "disconnect",
        "reconnect",
        "restart",
        "restart",
        "halt",
        "ack_halt",
    ]
)

# "restart" was ABSENT from this list in v0.1.0, and that omission is the
# entire reason a duplicate-order bug survived a 64-test suite: after a
# restart the leg table is empty, so reconciliation had nothing to update,
# order_state stayed IDLE while an order was live at the broker, and the
# next differing target sent a second one. The auditor caught it instantly
# once the sequences contained restarts.
#
# The lesson generalises: the auditor was never wrong, the INPUTS were too
# narrow. Any process-lifecycle event that can happen in production must be
# in this list.


def _build(tmpdir, faults: Faults):
    clock = ManualClock(SESSION_START)
    journal = Journal(tmpdir / "j.db", clock=clock)
    broker = FakeBroker(clock, faults)
    cfg = RiskConfig(strategy_whitelist=("manual_test",), max_position_shares=5)
    ctl = Controller(
        journal=journal,
        broker=broker,
        risk=RiskEngine(cfg, clock),
        clock=clock,
        calendar=TradingCalendar(),
        policy=ExecutionPolicy(),
    )
    journal.commit(EventType.PROCESS_STARTED, {})
    ctl.on_connected(1)
    ctl.on_quote(_q(clock))
    ctl.reconcile()
    return clock, journal, broker, ctl


def _q(clock) -> Quote:
    return Quote("SPY", Decimal("599.98"), Decimal("600.02"), 500, 500, clock.now())


@settings(
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    actions=st.lists(ACTIONS, min_size=5, max_size=PROPERTY_MAX_ACTIONS),
    quantities=st.lists(st.integers(min_value=-5, max_value=5), min_size=1, max_size=10),
    fault_flags=st.lists(st.booleans(), min_size=5, max_size=5),
)
def test_no_sequence_violates_invariants(tmp_path_factory, actions, quantities, fault_flags):
    """
    Random interleavings of targets, timers, callbacks and disconnects.

    The claim is not that the system trades well under chaos. It is that it
    never duplicates an order, never books a fill twice, and never continues
    on a state it cannot justify.
    """
    tmpdir = tmp_path_factory.mktemp("prop")
    faults = Faults(
        duplicate_callbacks=fault_flags[0],
        reorder_callbacks=fault_flags[1],
        partial_fill_qty=2 if fault_flags[2] else None,
        cancel_rejects=fault_flags[3],
        no_fill=fault_flags[4],
    )
    clock, journal, broker, ctl = _build(tmpdir, faults)

    qi = 0
    di = 0
    try:
        for a in actions:
            if a == "target":
                di += 1
                qty = quantities[qi % len(quantities)]
                qi += 1
                ctl.submit_target(
                    TargetPosition(
                        strategy_id="manual_test",
                        symbol="SPY",
                        target_quantity=qty,
                        decision_id=f"p{di:04d}",
                        valid_until=clock.now() + timedelta(seconds=120),
                    )
                )
            elif a == "tick":
                ctl.tick()
            elif a == "pump":
                broker.pump()
            elif a == "advance":
                clock.advance(7)
            elif a == "quote":
                ctl.on_quote(_q(clock))
            elif a == "disconnect":
                broker.disconnect("prop")
            elif a == "reconnect":
                broker.reconnect()
            elif a == "reconcile":
                if broker.is_connected():
                    ctl.reconcile()
            elif a == "halt":
                # Invariant 22: once halted, no generated sequence -- including
                # a restart -- may get the system trading again without an
                # explicit acknowledgement.
                ctl.halt("injected by property test")
            elif a == "ack_halt":
                active = find_unacknowledged_halt(journal)
                if active is not None:
                    journal.acknowledge_halt(
                        active["seq"], "property-test", "generated acknowledgement"
                    )
                # Deliberately do not change the live controller's mode. A
                # later generated restart + stable reconciliation is required.
            elif a == "restart":
                # Rebuild the controller over the same journal and broker.
                # Nothing in memory survives; only the journal and whatever
                # the broker knows.
                ctl = Controller(
                    journal=journal,
                    broker=broker,
                    risk=RiskEngine(
                        RiskConfig(strategy_whitelist=("manual_test",),
                                   max_position_shares=5),
                        clock,
                    ),
                    clock=clock,
                    calendar=TradingCalendar(),
                    policy=ExecutionPolicy(),
                )
                journal.commit(EventType.PROCESS_STARTED, {})
                ctl.restore_from_journal()
                if broker.is_connected():
                    ctl.on_connected(99)
                    ctl.on_quote(_q(clock))
                    ctl.reconcile()

        # (b) no runtime assertion tripped
        assert not ctl.violations, f"runtime invariant violations: {ctl.violations}"

        # (c) the log itself must be clean
        findings = JournalAuditor(journal.replay()).audit()
        assert not findings, "auditor findings:\n" + "\n".join(str(f) for f in findings)

        # position limit never exceeded, whatever happened
        for leg in ctl.legs.values():
            assert abs(leg.position) <= ctl.risk.config.max_position_shares, (
                f"position {leg.position} exceeded hard limit "
                f"{ctl.risk.config.max_position_shares}"
            )
    finally:
        journal.close()


@settings(
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(quantities=st.lists(st.integers(min_value=-5, max_value=5), min_size=2, max_size=8))
def test_never_two_orders_in_flight(tmp_path_factory, quantities):
    """Invariant 3, under rapid target churn."""
    tmpdir = tmp_path_factory.mktemp("prop2")
    clock, journal, broker, ctl = _build(tmpdir, Faults(no_fill=True))
    try:
        for i, q in enumerate(quantities):
            ctl.submit_target(
                TargetPosition(
                    strategy_id="manual_test",
                    symbol="SPY",
                    target_quantity=q,
                    decision_id=f"c{i:04d}",
                    valid_until=clock.now() + timedelta(seconds=300),
                )
            )
            live = [
                o for o in broker.snapshot().open_orders if o.status in ("PreSubmitted", "Submitted")
            ]
            assert len(live) <= 1, f"{len(live)} orders live simultaneously"
        findings = JournalAuditor(journal.replay()).audit()
        assert not findings, "\n".join(str(f) for f in findings)
    finally:
        journal.close()


def test_auditor_catches_a_planted_violation(tmp_path):
    """
    The auditor has to be able to fail, or it proves nothing.

    We forge a log with a send that has no committed intent and confirm it is
    reported.
    """
    clock = ManualClock(SESSION_START)
    j = Journal(tmp_path / "forged.db", clock=clock)
    try:
        j.commit(EventType.PROCESS_STARTED, {})
        j.commit(EventType.RECONCILIATION_COMPLETED, {})
        j.commit(EventType.SYNC_STATE_CHANGED, {"from": "SYNCING", "to": "SYNCED"})
        # send with no ORDER_INTENT_COMMITTED before it -> invariant 2
        j.commit(
            EventType.SEND_ATTEMPT_STARTED, {"attempt": 0},
            intent_id="ghost", order_ref="manual_test|d1|ghost",
        )
        findings = JournalAuditor(j.replay()).audit()
        assert any(f.invariant == 2 for f in findings), (
            "auditor failed to catch a send with no durable intent"
        )
    finally:
        j.close()


def test_auditor_catches_duplicate_execution(tmp_path):
    clock = ManualClock(SESSION_START)
    j = Journal(tmp_path / "forged2.db", clock=clock)
    try:
        for _ in range(2):
            j.commit(
                EventType.EXECUTION_RECEIVED,
                {"symbol": "SPY", "signed_quantity": 3, "exec_id": "E1.01"},
                exec_id="E1.01",
            )
        findings = JournalAuditor(j.replay()).audit()
        assert any(f.invariant == 12 for f in findings)
    finally:
        j.close()


def test_clean_run_audits_clean(ctl, journal, broker, clock):
    from conftest import target

    journal.commit(EventType.PROCESS_STARTED, {})
    ctl.reconcile()
    ctl.submit_target(target(3, clock))
    broker.pump()
    ctl.submit_target(target(0, clock))
    broker.pump()

    findings = JournalAuditor(journal.replay()).audit()
    assert not findings, "\n".join(str(f) for f in findings)
