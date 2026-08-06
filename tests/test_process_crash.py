"""True subprocess force-kill tests for the seven Gate B1 crash windows."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from decimal import Decimal

import pytest

from ib_execution.auditor import JournalAuditor
from ib_execution.calendar import TradingCalendar
from ib_execution.clock import ManualClock
from ib_execution.controller import Controller, ExecutionPolicy
from ib_execution.journal import Journal
from ib_execution.models import EventType, OperatingMode, Quote, SyncState
from ib_execution.risk import RiskConfig, RiskEngine
from conftest import SESSION_START
from crash_worker import DurableBroker


SCENARIOS = [
    "before_wal",
    "after_wal_before_send",
    "after_send_before_ack",
    "partial_fill",
    "cancel_request",
    "stable_snapshot",
    "halt_cause",
]


def _wait_for(path, proc, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc.poll() is not None:
            raise AssertionError(f"worker exited before checkpoint: {proc.returncode}")
        time.sleep(0.02)
    raise AssertionError("worker did not reach crash checkpoint")


@pytest.mark.process_crash
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_force_kill_window_recovers_fail_closed(tmp_path, scenario):
    journal_path = tmp_path / "journal.db"
    truth_path = tmp_path / "broker.json"
    checkpoint_path = tmp_path / "checkpoint.json"
    worker = __import__("pathlib").Path(__file__).with_name("crash_worker.py")
    env = os.environ.copy()
    src = str(worker.parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join([src, str(worker.parent), env.get("PYTHONPATH", "")])
    proc = subprocess.Popen(
        [sys.executable, str(worker), scenario, str(journal_path), str(truth_path),
         str(checkpoint_path)],
        env=env,
    )
    try:
        _wait_for(checkpoint_path, proc)
        proc.kill()  # TerminateProcess on Windows; SIGKILL on POSIX via Popen.kill().
        proc.wait(timeout=10)
        assert proc.returncode != 0

        state_before = json.loads(truth_path.read_text(encoding="utf-8"))
        sends_before = int(state_before["place_calls"])

        clock = ManualClock(SESSION_START)
        journal = Journal(journal_path, clock=clock)
        broker = DurableBroker(truth_path, clock)
        ctl = Controller(
            journal,
            broker,
            RiskEngine(RiskConfig(strategy_whitelist=("manual_test",)), clock),
            clock,
            TradingCalendar(),
            ExecutionPolicy(),
        )
        journal.commit(EventType.PROCESS_STARTED, {})
        ctl.on_connected(2)
        ctl.on_quote(
            Quote("SPY", Decimal("599.98"), Decimal("600.02"), 100, 100, clock.now())
        )
        ok = ctl.reconcile()

        state_after = json.loads(truth_path.read_text(encoding="utf-8"))
        # Existing broker truth is never duplicated. A proven-absent/cancelled
        # order may produce at most one replacement for the retained latest target.
        allowed_new = 1 if scenario in {"after_wal_before_send", "cancel_request"} else 0
        assert int(state_after["place_calls"]) <= sends_before + allowed_new
        assert len(state_after["orders"]) <= 1

        if scenario == "halt_cause":
            assert ctl.operating_mode is OperatingMode.HALTED
            assert not ok or ctl.sync_state in {SyncState.SYNCED, SyncState.UNVERIFIED}
        else:
            assert ok, f"{scenario} did not converge to durable broker truth"

        findings = JournalAuditor(journal.replay()).audit()
        assert not findings, "\n".join(str(f) for f in findings)
        journal.close()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
