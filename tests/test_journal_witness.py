"""
Gate B1.6: the journal still holds the evidence that authorised a broker write.

Measured on a real ext4 volume before this existed: 27 of 4,406 committed
events discarded by WAL recovery, no error, engine started normally. Every
test here reconstructs that loss deterministically by truncating the events
table -- the same observable end state as a WAL rollback, without needing a
crash to reproduce it.

``test_a_halt_can_be_lost_while_the_witness_is_satisfied`` is the adversarial
drill that decides how far witness coverage has to reach. It is written to
fail loudly if broker-write-only coverage turns out to be insufficient.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal

import pytest

from ib_execution.calendar import TradingCalendar
from ib_execution.clock import ManualClock
from ib_execution.controller import Controller, ExecutionPolicy
from ib_execution.execution_host import (
    EXIT_STARTUP,
    EXIT_WITNESS,
    ExecutionHost,
    HostConfig,
    HostStartupRefused,
)
from ib_execution.failure_domain import FailureDomainError
from ib_execution.fake_broker import FakeBroker, Faults
from ib_execution.fatal_fence import FatalFence, FenceStillRaised
from ib_execution.journal import Journal
from ib_execution.journal_witness import (
    JournalWitness,
    WitnessViolation,
    WitnessWriteFailed,
    event_digest,
)
from ib_execution.models import EventType, LinkState, OperatingMode, Quote, SyncState
from ib_execution.risk import RiskConfig, RiskEngine

SESSION_START = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)


def _rollback_to(journal_path, keep_up_to_seq: int) -> None:
    """Reproduce what WAL recovery leaves behind: a shorter, valid database.

    Deleting rows is not how the loss happens, but it is exactly what the loss
    looks like afterwards -- the database is internally consistent, passes
    integrity_check, and simply has fewer events than were committed.
    """
    import sqlite3

    conn = sqlite3.connect(journal_path)
    try:
        conn.execute("DELETE FROM events WHERE seq > ?", (keep_up_to_seq,))
        conn.commit()
    finally:
        conn.close()


def _journal(tmp_path, name="journal.db") -> Journal:
    return Journal(tmp_path / name, clock=ManualClock(SESSION_START))


# --------------------------------------------------------------------------
# the record itself
# --------------------------------------------------------------------------


def test_a_witness_pins_event_identity_not_just_a_sequence_number(tmp_path):
    journal = _journal(tmp_path)
    witness = JournalWitness(tmp_path / "witness.json")
    try:
        seq = journal.commit(
            EventType.SEND_ATTEMPT_STARTED, {"attempt": 1},
            intent_id="i-1", order_ref="ref-1",
        )
        record = witness.record(journal, seq)
        assert record.seq == seq
        assert record.journal_id == journal.journal_id
        assert record.event_type == EventType.SEND_ATTEMPT_STARTED.value
        assert record.intent_id == "i-1" and record.order_ref == "ref-1"
        assert len(record.digest) == 64
    finally:
        journal.close()


def test_verification_passes_while_the_evidence_is_intact(tmp_path):
    journal = _journal(tmp_path)
    witness = JournalWitness(tmp_path / "witness.json")
    try:
        seq = journal.commit(EventType.SEND_ATTEMPT_STARTED, {}, order_ref="r")
        witness.record(journal, seq)
        for _ in range(20):
            journal.commit(EventType.HEARTBEAT, {})
        assert witness.verify(journal) is not None
    finally:
        journal.close()


def test_no_witness_means_nothing_was_ever_sent(tmp_path):
    journal = _journal(tmp_path)
    try:
        assert JournalWitness(tmp_path / "absent.json").verify(journal) is None
    finally:
        journal.close()


# --------------------------------------------------------------------------
# the four ways verification must fail
# --------------------------------------------------------------------------


def test_a_lost_tail_is_detected(tmp_path):
    """The measured failure: committed events gone, database still valid."""
    path = tmp_path / "journal.db"
    journal = Journal(path, clock=ManualClock(SESSION_START))
    witness = JournalWitness(tmp_path / "witness.json")
    try:
        for _ in range(5):
            journal.commit(EventType.HEARTBEAT, {})
        seq = journal.commit(EventType.SEND_ATTEMPT_STARTED, {}, order_ref="r")
        witness.record(journal, seq)
    finally:
        journal.close()

    _rollback_to(path, seq - 1)

    reopened = Journal(path, clock=ManualClock(SESSION_START))
    try:
        with pytest.raises(WitnessViolation, match="missing"):
            witness.verify(reopened)
    finally:
        reopened.close()


def test_a_hole_at_the_witnessed_sequence_is_detected(tmp_path):
    """Later events survive, the authorising one does not."""
    path = tmp_path / "journal.db"
    journal = Journal(path, clock=ManualClock(SESSION_START))
    witness = JournalWitness(tmp_path / "witness.json")
    try:
        seq = journal.commit(EventType.SEND_ATTEMPT_STARTED, {}, order_ref="r")
        witness.record(journal, seq)
        for _ in range(5):
            journal.commit(EventType.HEARTBEAT, {})
    finally:
        journal.close()

    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM events WHERE seq = ?", (seq,))
    conn.commit()
    conn.close()

    reopened = Journal(path, clock=ManualClock(SESSION_START))
    try:
        with pytest.raises(WitnessViolation, match="no longer in the journal"):
            witness.verify(reopened)
    finally:
        reopened.close()


def test_a_changed_event_is_detected(tmp_path):
    """The sequence is present but no longer says what authorised the send."""
    path = tmp_path / "journal.db"
    journal = Journal(path, clock=ManualClock(SESSION_START))
    witness = JournalWitness(tmp_path / "witness.json")
    try:
        seq = journal.commit(EventType.SEND_ATTEMPT_STARTED, {"quantity": 1}, order_ref="r")
        witness.record(journal, seq)
    finally:
        journal.close()

    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("UPDATE events SET payload = ? WHERE seq = ?", ('{"quantity": 999}', seq))
    conn.commit()
    conn.close()

    reopened = Journal(path, clock=ManualClock(SESSION_START))
    try:
        with pytest.raises(WitnessViolation, match="differs from the event"):
            witness.verify(reopened)
    finally:
        reopened.close()


def test_a_different_journal_is_detected(tmp_path):
    """A restored backup or a swapped file, which a sequence number cannot catch."""
    first = _journal(tmp_path, "a.db")
    witness = JournalWitness(tmp_path / "witness.json")
    try:
        seq = first.commit(EventType.SEND_ATTEMPT_STARTED, {}, order_ref="r")
        witness.record(first, seq)
    finally:
        first.close()

    other = _journal(tmp_path, "b.db")
    try:
        for _ in range(50):
            other.commit(EventType.HEARTBEAT, {})   # longer, but not the same journal
        with pytest.raises(WitnessViolation, match="belongs to journal"):
            witness.verify(other)
    finally:
        other.close()


def test_an_unreadable_witness_is_not_treated_as_absent(tmp_path):
    """Otherwise corrupting the witness would be the way to bypass the check."""
    journal = _journal(tmp_path)
    witness = JournalWitness(tmp_path / "witness.json")
    witness.path.write_text("{ not json", encoding="utf-8")
    try:
        with pytest.raises(WitnessViolation, match="could not be read"):
            witness.verify(journal)
    finally:
        journal.close()


def test_witnessing_a_sequence_that_is_not_there_fails_loudly(tmp_path):
    journal = _journal(tmp_path)
    witness = JournalWitness(tmp_path / "witness.json")
    try:
        with pytest.raises(WitnessWriteFailed, match="not in the journal"):
            witness.record(journal, 99999)
    finally:
        journal.close()


def test_the_digest_ignores_timestamps_but_not_content(tmp_path):
    journal = _journal(tmp_path)
    try:
        seq = journal.commit(EventType.SEND_ATTEMPT_STARTED, {"quantity": 1}, order_ref="r")
        event = journal.event_at(seq)
        assert event is not None
        from dataclasses import replace

        assert event_digest(replace(event, ts_mono_ns=event.ts_mono_ns + 1)) == event_digest(event)
        assert event_digest(replace(event, payload={"quantity": 2})) != event_digest(event)
        assert event_digest(replace(event, order_ref="other")) != event_digest(event)
    finally:
        journal.close()


# --------------------------------------------------------------------------
# the controller refuses to send without a witness
# --------------------------------------------------------------------------


def _controller(tmp_path, witness, fence=None):
    clock = ManualClock(SESSION_START)
    journal = Journal(tmp_path / "journal.db", clock=clock)
    risk = RiskEngine(
        RiskConfig(
            symbol_whitelist=("SPY",), strategy_whitelist=("manual_test",),
            max_position_shares=5, max_order_shares=10,
            max_order_notional=Decimal("20000"),
        ),
        clock,
    )
    ctl = Controller(
        journal=journal, broker=FakeBroker(clock, Faults()), risk=risk, clock=clock,
        calendar=TradingCalendar(), policy=ExecutionPolicy(),
        alert=lambda level, msg: None,
        fence=fence or FatalFence(tmp_path / "fence.json", tmp_path / "journal.db",
                                  require_separate_domain=False),
        witness=witness,
    )
    ctl.on_connected(1)
    ctl.on_quote(Quote("SPY", Decimal("599.98"), Decimal("600.02"), 500, 500, clock.now()))
    ctl.reconcile()
    assert ctl.link_state is LinkState.CONNECTED and ctl.sync_state is SyncState.SYNCED
    return journal, ctl, clock


def _target(clock, qty=1):
    from datetime import timedelta

    from ib_execution.models import TargetPosition

    return TargetPosition(
        strategy_id="manual_test", symbol="SPY", target_quantity=qty,
        decision_id=f"d-{qty}-{clock.now().timestamp()}",
        valid_until=clock.now() + timedelta(seconds=60),
    )


def test_a_send_records_a_witness_before_the_order_reaches_the_broker(tmp_path):
    witness = JournalWitness(tmp_path / "witness.json")
    journal, ctl, clock = _controller(tmp_path, witness)
    try:
        assert ctl.submit_target(_target(clock))
        record = witness.read()
        assert record is not None
        assert record.event_type == EventType.SEND_ATTEMPT_STARTED.value
        # And the pinned event is the one that authorised this order.
        event = journal.event_at(record.seq)
        assert event is not None and event.order_ref == record.order_ref
    finally:
        journal.close()


def test_a_witness_that_cannot_be_written_stops_the_send(tmp_path):
    """The one moment where doing nothing is unambiguously safe: nothing sent yet."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    witness = JournalWitness(blocker / "witness.json")
    journal, ctl, clock = _controller(tmp_path, witness)
    try:
        placed_before = len(ctl.broker.place_calls)
        assert ctl.submit_target(_target(clock)) is False
        assert len(ctl.broker.place_calls) == placed_before, "sent without a witness"
        assert ctl.fatal_shutdown_requested
        assert ctl.operating_mode is OperatingMode.HALTED
    finally:
        journal.close()


def test_a_cancel_is_witnessed_too(tmp_path):
    """Cancels are broker writes and carry the same durability requirement."""
    witness = JournalWitness(tmp_path / "witness.json")
    journal, ctl, clock = _controller(tmp_path, witness)
    try:
        assert ctl.submit_target(_target(clock, qty=1))
        first = witness.read()
        assert first is not None
        assert first.event_type == EventType.SEND_ATTEMPT_STARTED.value

        leg = ctl.leg("manual_test", "SPY")
        ref = leg.live_intent.order_ref
        ctl.on_ack(ref, broker_order_id=1, perm_id=1)
        ctl.on_working(ref, perm_id=1)
        assert ctl._request_cancel(leg, "witness test")
        assert ctl.broker.cancel_calls == [ref]

        latest = witness.read()
        assert latest is not None
        assert latest.seq > first.seq
        assert latest.event_type == EventType.CANCEL_REQUESTED.value
    finally:
        journal.close()


# --------------------------------------------------------------------------
# the host refuses to start
# --------------------------------------------------------------------------


def _host(tmp_path) -> ExecutionHost:
    clock = ManualClock(SESSION_START)
    risk = RiskEngine(
        RiskConfig(
            symbol_whitelist=("SPY",), strategy_whitelist=("manual_test",),
            max_position_shares=5, max_order_shares=10,
            max_order_notional=Decimal("20000"),
        ),
        clock,
    )
    return ExecutionHost(
        HostConfig(
            journal_path=tmp_path / "journal.db",
            fence_path=tmp_path / "fatal-fence.json",
            status_path=tmp_path / "status.json",
            witness_path=tmp_path / "journal-witness.json",
            require_separate_fence_domain=False,
        ),
        broker_factory=lambda: FakeBroker(clock, Faults()),
        risk=risk, clock=clock, alert=lambda level, msg: None,
        sleeper=lambda _s: None,
    )


def _bring_online(ctl, clock):
    ctl.on_connected(1)
    ctl.on_quote(Quote("SPY", Decimal("599.98"), Decimal("600.02"), 500, 500, clock.now()))
    ctl.reconcile()
    assert ctl.sync_state is SyncState.SYNCED


def test_the_host_refuses_to_start_after_a_lost_send_event(tmp_path):
    """End to end: an order may be live at IB with no local record of it."""
    host = _host(tmp_path)
    ctl = host.start()
    try:
        _bring_online(ctl, host.clock)
        assert ctl.submit_target(_target(host.clock))
        seq = host.witness.read().seq
    finally:
        host.close()

    _rollback_to(tmp_path / "journal.db", seq - 1)

    successor = _host(tmp_path)
    with pytest.raises(HostStartupRefused) as excinfo:
        successor.start()
    assert excinfo.value.code == EXIT_WITNESS
    assert successor.controller is None, "no controller, therefore no broker session"
    assert host.fence.read() is not None, "a witness violation raises the durable fence"


def test_the_host_starts_normally_when_the_evidence_is_intact(tmp_path):
    host = _host(tmp_path)
    ctl = host.start()
    try:
        _bring_online(ctl, host.clock)
        assert ctl.submit_target(_target(host.clock))
    finally:
        host.close()

    successor = _host(tmp_path)
    try:
        assert successor.start() is not None
    finally:
        successor.close()


# --------------------------------------------------------------------------
# the adversarial drill that decides witness coverage
# --------------------------------------------------------------------------


def test_a_lost_halt_is_detected(tmp_path):
    """The adversarial drill that decided how far witness coverage must reach.

    Written first with SAFETY_CRITICAL_TYPES empty, on the theory that pinning
    the last broker write covered everything dangerous. It did not. HALT and
    HALT_CAUSE_ADDED are not broker writes, so a rollback could drop a HALT
    while leaving max_seq above the witnessed send: witness satisfied, HALT
    gone, restart back to NORMAL, and invariant 22 broken by storage rather
    than by logic.

    The witness now covers those events too, so the same rollback is caught.
    ``test_a_bare_high_water_number_would_have_missed_this`` keeps the original
    finding visible.
    """
    path = tmp_path / "journal.db"
    witness = JournalWitness(tmp_path / "witness.json")
    journal, ctl, clock = _controller(tmp_path, witness)
    try:
        assert ctl.submit_target(_target(clock))
        witnessed_send = witness.read().seq

        for _ in range(5):
            journal.commit(EventType.HEARTBEAT, {})
        ctl.halt("unexplained position mismatch")

        pinned = witness.read()
        assert pinned is not None
        assert pinned.seq > witnessed_send, "the HALT, not the send, is now pinned"
        assert pinned.event_type == EventType.OPERATING_MODE_CHANGED.value
    finally:
        journal.close()

    _rollback_to(path, witnessed_send + 2)      # drops the HALT, keeps the send

    reopened = Journal(path, clock=ManualClock(SESSION_START))
    try:
        with pytest.raises(WitnessViolation, match="missing"):
            witness.verify(reopened)
    finally:
        reopened.close()


def test_a_further_halt_cause_advances_the_witness(tmp_path):
    """HALT_CAUSE_ADDED is the second unexplained fact, and equally losable."""
    witness = JournalWitness(tmp_path / "witness.json")
    journal, ctl, clock = _controller(tmp_path, witness)
    try:
        ctl.halt("first cause")
        first = witness.read()
        ctl.halt("second cause")
        second = witness.read()
        assert second.seq > first.seq
        assert second.event_type == EventType.HALT_CAUSE_ADDED.value
    finally:
        journal.close()


def test_a_bare_high_water_number_would_have_missed_this(tmp_path):
    """Why the witness binds event identity rather than just a sequence.

    Reproduces the original finding against the rejected design: a witness that
    only remembered "the journal had at least N rows" is satisfied by the very
    rollback that removes the HALT, because the send it pinned is still there.
    """
    path = tmp_path / "journal.db"
    witness = JournalWitness(tmp_path / "witness.json")
    journal, ctl, clock = _controller(tmp_path, witness)
    try:
        assert ctl.submit_target(_target(clock))
        send_seq = witness.read().seq
        for _ in range(5):
            journal.commit(EventType.HEARTBEAT, {})
        ctl.halt("unexplained position mismatch")
    finally:
        journal.close()

    _rollback_to(path, send_seq + 2)

    reopened = Journal(path, clock=ManualClock(SESSION_START))
    try:
        # The rejected design: high-water mark pinned to the last broker write.
        assert reopened.max_seq() >= send_seq, "a bare high-water check passes"
        halts = [
            ev for ev in reopened.replay()
            if ev.event_type is EventType.OPERATING_MODE_CHANGED
            and ev.payload.get("to") == OperatingMode.HALTED.value
        ]
        assert halts == [], "yet the HALT is gone"
    finally:
        reopened.close()


def _halt_with_unwritable_witness(tmp_path):
    """A HALT whose witness update cannot be persisted. Returns (ctl, journal, alerts)."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    alerts: list[tuple[str, str]] = []

    clock = ManualClock(SESSION_START)
    journal = Journal(tmp_path / "journal.db", clock=clock)
    risk = RiskEngine(
        RiskConfig(
            symbol_whitelist=("SPY",), strategy_whitelist=("manual_test",),
            max_position_shares=5, max_order_shares=10,
            max_order_notional=Decimal("20000"),
        ),
        clock,
    )
    ctl = Controller(
        journal=journal, broker=FakeBroker(clock, Faults()), risk=risk, clock=clock,
        calendar=TradingCalendar(), policy=ExecutionPolicy(),
        alert=lambda level, msg: alerts.append((level, msg)),
        fence=FatalFence(tmp_path / "fatal-fence.json", tmp_path / "journal.db",
                         require_separate_domain=False),
        witness=JournalWitness(blocker / "witness.json"),
    )
    ctl.halt("unexplained position mismatch")
    return ctl, journal, alerts


def test_halt_survives_a_witness_that_cannot_be_written(tmp_path):
    """Refusing to HALT would be absurd -- it already happened and is durable."""
    ctl, journal, alerts = _halt_with_unwritable_witness(tmp_path)
    try:
        assert ctl.operating_mode is OperatingMode.HALTED
        assert any("could not witness" in msg for _, msg in alerts)
    finally:
        journal.close()


def test_a_halt_whose_witness_failed_raises_the_durable_fence(tmp_path):
    """Alerting alone leaves invariant 22 reachable again.

        HALT at seq 120 commits, witness update fails
        the witness still points at the last send, seq 100
        crash, WAL rollback to seq 110
        110 >= 100, so startup verification passes -- and the HALT is gone

    The fence is what stops the *next* process. It lives on a different volume
    from the journal, so a witness-specific failure usually leaves it writable.
    """
    ctl, journal, _alerts = _halt_with_unwritable_witness(tmp_path)
    try:
        assert ctl.fence is not None
        record = ctl.fence.read()
        assert record is not None
        assert "witness update failed" in record.reason
    finally:
        journal.close()


def test_a_restart_is_refused_even_if_the_halt_tail_then_disappears(tmp_path):
    """End to end: the scenario the fence exists to stop.

    The witness could not be updated for the HALT, and a later rollback removes
    the HALT while leaving the journal longer than the stale witness. Nothing
    in the journal or the witness can object -- only the fence can.
    """
    ctl, journal, _alerts = _halt_with_unwritable_witness(tmp_path)
    path = Path(journal.path)
    try:
        halt_seq = journal.max_seq()
    finally:
        journal.close()

    _rollback_to(path, halt_seq - 1)          # the HALT is gone

    reopened = Journal(path, clock=ManualClock(SESSION_START))
    try:
        halts = [
            ev for ev in reopened.replay()
            if ev.event_type is EventType.OPERATING_MODE_CHANGED
            and ev.payload.get("to") == OperatingMode.HALTED.value
        ]
        assert halts == [], "the HALT is no longer in the journal"
    finally:
        reopened.close()

    fence = FatalFence(tmp_path / "fatal-fence.json", path, require_separate_domain=False)
    with pytest.raises(FenceStillRaised):
        fence.require_clear()


# --------------------------------------------------------------------------
# the witness must be able to outlive the journal's storage
# --------------------------------------------------------------------------


def test_a_witness_sharing_the_journal_volume_is_refused(tmp_path):
    """The CLI help said so and nothing checked it. Documentation is not a control."""
    witness = JournalWitness(
        tmp_path / "journal-witness.json",
        journal_path=tmp_path / "journal.db",
        require_separate_domain=True,
    )
    with pytest.raises(FailureDomainError):
        witness.verify_domain()


def test_the_host_refuses_a_witness_on_the_journal_volume(tmp_path):
    """An operator can pass --witness; the gate has to be in the host, not the docs."""
    clock = ManualClock(SESSION_START)
    risk = RiskEngine(
        RiskConfig(
            symbol_whitelist=("SPY",), strategy_whitelist=("manual_test",),
            max_position_shares=5, max_order_shares=10,
            max_order_notional=Decimal("20000"),
        ),
        clock,
    )
    host = ExecutionHost(
        HostConfig(
            journal_path=tmp_path / "journal.db",
            fence_path=tmp_path / "fatal-fence.json",
            status_path=tmp_path / "status.json",
            witness_path=tmp_path / "journal-witness.json",
            require_separate_fence_domain=True,
        ),
        broker_factory=lambda: FakeBroker(clock, Faults()),
        risk=risk, clock=clock, alert=lambda level, msg: None,
        sleeper=lambda _s: None,
    )
    with pytest.raises(HostStartupRefused) as excinfo:
        host.start()
    assert excinfo.value.code == EXIT_STARTUP
    assert "journal witness" in str(excinfo.value) or "fatal fence" in str(excinfo.value)


def test_the_witness_file_is_json_an_operator_can_read(tmp_path):
    journal = _journal(tmp_path)
    witness = JournalWitness(tmp_path / "witness.json")
    try:
        seq = journal.commit(EventType.SEND_ATTEMPT_STARTED, {}, order_ref="ref-9")
        witness.record(journal, seq)
        payload = json.loads(witness.path.read_text(encoding="utf-8"))
        assert payload["order_ref"] == "ref-9"
        assert payload["schema_version"] == 1
        assert payload["written_utc"]
    finally:
        journal.close()
