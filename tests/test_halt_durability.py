"""
Invariant 22: a restart must not launder a HALT.

Found by running v0.1.3's OWN auditor against v0.1.3's own soak output. The
auditor folded operating_mode across PROCESS_STARTED boundaries and reported
"broker send in HALTED mode". The natural reading was auditor false positive.

It was not. The auditor was right and the controller was wrong: a HALT is a
durable statement about an unexplained condition, and every version up to and
including v0.1.3 cleared it on restart.

The dangerous path is the likely one: watchdog kills the engine, someone
restarts it, and trading resumes with the root cause undiagnosed. The RUNBOOK
said not to do that. ADR-004 said restart is manual for this reason. Nothing
enforced either — documentation is not a control.

The fix makes the controller match the auditor, rather than weakening the
auditor to match the controller. That direction matters: a detector that gets
relaxed to fit current behaviour stops being a detector.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from ib_execution.ack_halt import find_unacknowledged_halt, main as ack_main
from ib_execution.journal import HaltAcknowledgementConflict, Journal
from ib_execution.auditor import JournalAuditor
from ib_execution.calendar import TradingCalendar
from ib_execution.controller import Controller, ExecutionPolicy
from ib_execution.models import EventType, OperatingMode
from ib_execution.risk import RiskEngine
from conftest import quote, target


def _restart(journal, broker, risk_config, clock) -> Controller:
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
    c.on_connected(2)
    c.on_quote(quote(clock.now()))
    c.reconcile()
    return c


def test_restart_does_not_clear_a_halt(ctl, journal, broker, risk_config, clock):
    ctl.halt("unexplained position mismatch")

    c2 = _restart(journal, broker, risk_config, clock)

    assert c2.operating_mode is OperatingMode.HALTED
    n = len(broker.place_calls)
    c2.submit_target(target(3, clock))
    broker.pump()
    assert len(broker.place_calls) == n, "restart laundered the HALT"


def test_halt_outranks_a_residual_on_boot(ctl, journal, broker, risk_config, clock):
    """
    A HALTED system must not be quietly downgraded to FLATTEN_ONLY.

    FLATTEN_ONLY still permits closing orders. If a residual downgraded a HALT,
    a system that stopped because it could not explain its own position would
    resume sending orders against that position.
    """
    ctl.submit_target(target(3, clock))
    broker.pump()
    journal.commit(
        EventType.EOD_FLATTEN_FAILED,
        {"symbol": "SPY", "residual_quantity": 3, "failure_reason": "test"},
        symbol="SPY",
    )
    ctl.halt("cannot explain broker state")

    c2 = _restart(journal, broker, risk_config, clock)
    assert c2.operating_mode is OperatingMode.HALTED


def test_acknowledgement_clears_it_and_survives_restart(
    ctl, journal, broker, risk_config, clock
):
    ctl.halt("external order detected")
    c2 = _restart(journal, broker, risk_config, clock)
    assert c2.operating_mode is OperatingMode.HALTED

    c2.acknowledge_halt("olivia", "manual TWS order, cancelled, account verified flat")
    assert c2.operating_mode is OperatingMode.HALTED, (
        "acknowledgement must not resume a live controller; restart and reconcile first"
    )

    c3 = _restart(journal, broker, risk_config, clock)
    assert c3.operating_mode is OperatingMode.NORMAL, "acknowledgement must persist"


def test_acknowledgement_requires_attribution(ctl, clock):
    """
    Named operator and written resolution, both journalled.

    "Who cleared this and what did they find" must be answerable months later,
    when the same symptom recurs and someone is deciding whether it is the same
    cause.
    """
    ctl.halt("something")
    with pytest.raises(ValueError):
        ctl.acknowledge_halt("", "found it")
    with pytest.raises(ValueError):
        ctl.acknowledge_halt("olivia", "")


def test_a_second_halt_needs_a_second_acknowledgement(
    ctl, journal, broker, risk_config, clock
):
    ctl.halt("first")
    ctl.acknowledge_halt("olivia", "resolved")
    resumed = _restart(journal, broker, risk_config, clock)
    assert resumed.operating_mode is OperatingMode.NORMAL
    resumed.halt("second, different cause")

    c2 = _restart(journal, broker, risk_config, clock)
    assert c2.operating_mode is OperatingMode.HALTED, "stale acknowledgement reused"


def test_ack_tool_refuses_without_attribution(ctl, journal, broker, clock, capsys):
    ctl.halt("needs a human")
    rc = ack_main(["--journal", journal.path])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Refusing to clear" in out
    assert "If you cannot explain the cause, do not clear it." in out


def test_ack_tool_shows_the_leading_events(ctl, journal, broker, clock, capsys):
    """The operator should read the cause before being offered the button."""
    ctl.halt("unexplained position mismatch: {'SPY': {'expected': 0, 'actual': 77}}")
    rc = ack_main(["--journal", journal.path, "--show"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "UNACKNOWLEDGED HALT" in out
    assert "Leading events:" in out


def test_ack_tool_is_a_noop_when_nothing_is_halted(ctl, journal, capsys):
    rc = ack_main(["--journal", journal.path])
    assert rc == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_find_unacknowledged_halt_tracks_the_latest(ctl, journal, clock):
    assert find_unacknowledged_halt(journal) is None
    ctl.halt("one")
    assert find_unacknowledged_halt(journal) is not None
    ctl.acknowledge_halt("olivia", "done")
    assert find_unacknowledged_halt(journal) is None


def test_auditor_invariant_7_is_now_consistent_with_the_controller(
    ctl, journal, broker, risk_config, clock
):
    """
    The check that found this must still pass on correct behaviour.

    Before the fix, the auditor's cross-restart mode fold produced findings on
    real runs. Now the controller genuinely keeps the mode, so a clean session
    that halts and never resumes audits clean.
    """
    ctl.submit_target(target(3, clock))
    broker.pump()
    ctl.halt("injected")
    c2 = _restart(journal, broker, risk_config, clock)
    c2.submit_target(target(-3, clock))
    broker.pump()

    findings = [f for f in JournalAuditor(journal.replay()).audit() if f.invariant == 7]
    assert not findings, "\n".join(str(f) for f in findings)


def test_restart_does_not_append_synthetic_halts(
    ctl, journal, broker, risk_config, clock
):
    ctl.halt("root cause")
    original = find_unacknowledged_halt(journal)
    assert original is not None

    for _ in range(3):
        restarted = _restart(journal, broker, risk_config, clock)
        assert restarted.operating_mode is OperatingMode.HALTED

    halt_events = [
        e for e in journal.replay()
        if e.event_type is EventType.OPERATING_MODE_CHANGED
        and e.payload.get("to") == OperatingMode.HALTED.value
    ]
    assert len(halt_events) == 1
    assert halt_events[0].seq == original["seq"]
    assert halt_events[0].payload.get("why") == "root cause"


def test_stale_ack_cannot_clear_a_newer_halt(tmp_path, clock):
    path = tmp_path / "race.db"
    j1 = Journal(path, clock=clock)
    j2 = Journal(path, clock=clock)
    try:
        first = j1.commit(
            EventType.OPERATING_MODE_CHANGED,
            {"from": "NORMAL", "to": "HALTED", "why": "first"},
        )
        j1.acknowledge_halt(first, "operator-a", "resolved first")
        second = j1.commit(
            EventType.OPERATING_MODE_CHANGED,
            {"from": "NORMAL", "to": "HALTED", "why": "second"},
        )

        with pytest.raises(HaltAcknowledgementConflict):
            j2.acknowledge_halt(first, "operator-b", "stale screen")

        halt = find_unacknowledged_halt(j1)
        assert halt is not None and halt["seq"] == second
    finally:
        j2.close()
        j1.close()


def test_ack_event_must_reference_exact_active_halt(ctl, journal):
    ctl.halt("exact")
    halt = find_unacknowledged_halt(journal)
    assert halt is not None
    journal.commit(
        EventType.HALT_ACKNOWLEDGED,
        {
            "operator": "forged",
            "resolution": "wrong halt",
            "acknowledged_halt_seq": halt["seq"] - 1,
        },
    )
    assert find_unacknowledged_halt(journal) is not None
    findings = JournalAuditor(journal.replay()).audit()
    assert not any(f.invariant == 22 and "left HALTED" in f.detail for f in findings)


def test_new_cause_while_already_halted_advances_ack_token(ctl, journal):
    ctl.halt("first unexplained fact")
    first = find_unacknowledged_halt(journal)
    assert first is not None

    ctl.halt("second unexplained fact")
    second = find_unacknowledged_halt(journal)
    assert second is not None
    assert second["seq"] > first["seq"]
    assert second["started_seq"] == first["started_seq"]
    assert second["why"] == "second unexplained fact"

    with pytest.raises(HaltAcknowledgementConflict):
        journal.acknowledge_halt(first["seq"], "stale-operator", "only saw first cause")

    journal.acknowledge_halt(second["seq"], "current-operator", "resolved both causes")
    assert find_unacknowledged_halt(journal) is None
