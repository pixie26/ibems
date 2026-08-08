"""
Watchdog and emergency-flatten scaffolding.

The watchdog tests are mostly about what it REFUSES to do.
Recorder storage and health live in tests/test_recorder.py.
"""

from __future__ import annotations

import io
import json

import pytest

from ib_execution.emergency_flatten import FlattenPlan, build_plan_from_snapshot, confirm
from ib_execution.models import BrokerOrder, BrokerSnapshot, Side
from ib_execution.watchdog import Watchdog, WatchdogConfig, write_status


# --------------------------------------------------------------------------
# watchdog
# --------------------------------------------------------------------------


@pytest.fixture
def wd(tmp_path):
    alerts: list[tuple[str, str]] = []
    w = Watchdog(
        WatchdogConfig(
            status_path=tmp_path / "status.json",
            heartbeat_timeout_seconds=30,
            grace_seconds_before_kill=30,
        ),
        alert=lambda lvl, msg: alerts.append((lvl, msg)),
    )
    w.alerts = alerts  # type: ignore[attr-defined]
    return w


def test_healthy_heartbeat_is_quiet(wd):
    v = wd.evaluate(1000.0, {"heartbeat_mono": 995.0, "operating_mode": "NORMAL",
                             "sync_state": "SYNCED"})
    assert v.healthy and not v.should_alert and not v.should_kill


def test_stale_heartbeat_alerts_before_it_kills(wd):
    """First a warning; the kill only after the grace period."""
    v1 = wd.evaluate(1000.0, {"heartbeat_mono": 900.0})
    assert v1.should_alert and not v1.should_kill

    v2 = wd.evaluate(1010.0, {"heartbeat_mono": 900.0})
    assert not v2.should_kill, "grace period not elapsed"

    v3 = wd.evaluate(1040.0, {"heartbeat_mono": 900.0})
    assert v3.should_kill


def test_recovery_resets_the_grace_clock(wd):
    wd.evaluate(1000.0, {"heartbeat_mono": 900.0})
    wd.evaluate(1010.0, {"heartbeat_mono": 1009.0, "sync_state": "SYNCED",
                         "operating_mode": "NORMAL"})
    v = wd.evaluate(1020.0, {"heartbeat_mono": 900.0})
    assert not v.should_kill, "a recovery must reset the unhealthy timer"


def test_watchdog_never_proposes_trading():
    """
    The whole design rests on this. The Verdict type has no order field, no
    flatten field, no mode field. It can alert and it can kill. That is all.
    """
    fields = set(Verdict_fields())
    assert fields == {"healthy", "reason", "should_alert", "should_kill", "severity"}
    assert not any("flatten" in f or "order" in f or "mode" in f for f in fields)


def Verdict_fields():
    from dataclasses import fields as dc_fields
    from ib_execution.watchdog import Verdict
    return [f.name for f in dc_fields(Verdict)]


def test_missing_status_file_is_critical(wd):
    v = wd.evaluate(1000.0, None)
    assert v.should_alert and v.severity == "CRITICAL"


def test_halted_engine_is_reported_but_not_killed(wd):
    v = wd.evaluate(1000.0, {"heartbeat_mono": 999.0, "operating_mode": "HALTED",
                             "sync_state": "SYNCED"})
    assert v.healthy, "a HALTED engine is alive and behaving correctly"
    assert v.should_alert and not v.should_kill


def test_fatal_shutdown_request_is_fenced_immediately(wd):
    status = {
        "heartbeat_mono": 999.0,
        "operating_mode": "HALTED",
        "sync_state": "UNVERIFIED",
        "fatal_shutdown_requested": True,
        "journal_failure": "database is full",
    }
    verdict = wd.evaluate(1000.0, status)
    assert not verdict.healthy
    assert verdict.should_alert and verdict.should_kill


def test_status_write_is_atomic(tmp_path):
    p = tmp_path / "status.json"
    for i in range(20):
        write_status(p, {"operating_mode": "NORMAL", "n": i})
        loaded = json.loads(p.read_text())   # never a partial read
        assert loaded["n"] == i
        assert "pid" in loaded and "heartbeat_mono" in loaded



# --------------------------------------------------------------------------
# emergency flatten scaffolding
# --------------------------------------------------------------------------


def test_plan_is_built_from_broker_truth():
    snap = BrokerSnapshot(
        positions={"SPY": -4},
        open_orders=[
            BrokerOrder("manual_test|d1|abc", 1, 1, "SPY", Side.BUY, 2, 0, "Submitted")
        ],
        executions=[],
        server_time=None,  # type: ignore[arg-type]
        is_stable=True,
    )
    plan = build_plan_from_snapshot(snap, "SPY", "DU123", is_live=False)

    assert plan.broker_position == -4
    assert plan.closing_side is Side.BUY
    assert plan.closing_quantity == 4
    assert plan.open_order_refs == ["manual_test|d1|abc"]


def test_live_confirmation_requires_a_different_token():
    """'y' is not enough for a live account."""
    live = FlattenPlan("U123", "SPY", 3, is_live=True)
    paper = FlattenPlan("DU123", "SPY", 3, is_live=False)

    assert confirm(live, io.StringIO("FLATTEN\n"), io.StringIO()) is False
    assert confirm(live, io.StringIO("FLATTEN LIVE\n"), io.StringIO()) is True
    assert confirm(paper, io.StringIO("FLATTEN\n"), io.StringIO()) is True
    assert confirm(paper, io.StringIO("y\n"), io.StringIO()) is False


def test_flat_account_needs_no_closing_order():
    plan = FlattenPlan("DU123", "SPY", 0)
    assert plan.closing_side is None
    assert "already flat" in plan.describe()
