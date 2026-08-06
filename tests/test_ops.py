"""
Watchdog, recorder storage, and emergency-flatten scaffolding.

The watchdog tests are mostly about what it REFUSES to do.
"""

from __future__ import annotations

import io
import json
from datetime import date

import pytest

from ib_execution.emergency_flatten import FlattenPlan, build_plan_from_snapshot, confirm
from ib_execution.models import BrokerOrder, BrokerSnapshot, Side
from ib_execution.quote_recorder import RawEventLog, RawTick, compute_health
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


def test_status_write_is_atomic(tmp_path):
    p = tmp_path / "status.json"
    for i in range(20):
        write_status(p, {"operating_mode": "NORMAL", "n": i})
        loaded = json.loads(p.read_text())   # never a partial read
        assert loaded["n"] == i
        assert "pid" in loaded and "heartbeat_mono" in loaded


# --------------------------------------------------------------------------
# recorder storage
# --------------------------------------------------------------------------


def _tick(i: int, ns: int) -> RawTick:
    return RawTick(
        event_id=i,
        connection_epoch=1,
        contract_id=756733,
        event_type="BID_ASK",
        broker_timestamp="2026-08-05T14:00:00Z",
        local_wall_ns=ns,
        local_monotonic_ns=ns,
        market_data_type="LIVE",
        receive_sequence=i,
        bid=599.98,
        ask=600.02,
        bid_size=500,
        ask_size=500,
    )


def test_segments_roll_and_rename_atomically(tmp_path):
    """
    Never hold one file open all session: a crash at 15:45 costs the whole day.
    """
    log = RawEventLog(tmp_path, session=date(2026, 8, 5), roll_seconds=10)
    for i in range(5):
        log.append(_tick(i, 1_000_000_000 * i), now_mono=float(i))
    for i in range(5, 10):
        log.append(_tick(i, 1_000_000_000 * i), now_mono=float(i * 10))
    log.close()

    segs = log.segments()
    assert len(segs) >= 2, "expected the log to roll"
    assert not list(log.dir.glob(".partial-*")), "no partial files left behind"
    assert len(list(log.read_all())) == 10


def test_health_detects_delayed_data(tmp_path):
    """
    The classic silent failure: three months of delayed data, and every L2/L3
    conclusion built on it is void. A daily check makes it a one-day loss.
    """
    log = RawEventLog(tmp_path, session=date(2026, 8, 5))
    for i in range(10):
        t = _tick(i, 1_000_000_000 * i)
        log.append(RawTick(**{**t.__dict__, "market_data_type": "DELAYED"}), now_mono=float(i))
    log.close()

    h = compute_health(log, session_seconds=10.0)
    assert not h.ok()
    assert any("LIVE" in p for p in h.problems())


def test_health_detects_gaps(tmp_path):
    log = RawEventLog(tmp_path, session=date(2026, 8, 5))
    log.append(_tick(0, 0), now_mono=0.0)
    log.append(_tick(1, 600 * 1_000_000_000), now_mono=1.0)   # 10 minute hole
    log.close()

    h = compute_health(log, session_seconds=600.0)
    assert h.max_gap_seconds >= 600
    assert not h.ok()


def test_health_passes_on_a_clean_session(tmp_path):
    log = RawEventLog(tmp_path, session=date(2026, 8, 5))
    for i in range(100):
        log.append(_tick(i, i * 1_000_000_000), now_mono=float(i))
    log.close()

    h = compute_health(log, session_seconds=99.0)
    assert h.ok(), h.problems()


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


def test_health_does_not_hide_delayed_interval_with_final_live_tick(tmp_path):
    log = RawEventLog(tmp_path, session=date(2026, 8, 5))
    delayed = _tick(0, 0)
    log.append(RawTick(**{**delayed.__dict__, "market_data_type": "DELAYED"}), now_mono=0)
    log.append(_tick(1, 1_000_000_000), now_mono=1)
    log.close()
    h = compute_health(log, session_seconds=1.0)
    assert not h.ok()
    assert h.market_data_type.startswith("MIXED")


def test_system_events_cannot_mask_market_data_gap(tmp_path):
    log = RawEventLog(tmp_path, session=date(2026, 8, 5))
    log.append(_tick(0, 0), now_mono=0)
    for i in range(1, 10):
        t = _tick(i, i * 60 * 1_000_000_000)
        log.append(
            RawTick(**{**t.__dict__, "event_type": "SYSTEM", "special_conditions": "HEARTBEAT"}),
            now_mono=i,
        )
    log.append(_tick(10, 600 * 1_000_000_000), now_mono=10)
    log.close()
    h = compute_health(log, session_seconds=600.0)
    assert h.max_gap_seconds == 600.0
    assert not h.ok()


def test_same_day_recorder_restarts_use_unique_segment_names(tmp_path):
    d = date(2026, 8, 5)
    a = RawEventLog(tmp_path, session=d)
    a.append(_tick(0, 0), now_mono=0)
    a.close()
    b = RawEventLog(tmp_path, session=d)
    b.append(_tick(1, 1_000_000_000), now_mono=0)
    b.close()
    assert len(a.segments()) == 2
