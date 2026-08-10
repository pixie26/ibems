"""
The preflight's measurement, tested without a Gateway.

The 2026-08-07 preflight produced a number (``AllLast=0``) that was treated as
evidence about IB and was actually an artifact of how it counted. The counting
is now separable from the connection, so it can be tested.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("ib_async")

_SPEC = importlib.util.spec_from_file_location(
    "_preflight", Path(__file__).resolve().parents[1] / "scripts" / "run_ib_readonly_preflight.py"
)
assert _SPEC and _SPEC.loader
preflight = importlib.util.module_from_spec(_SPEC)
sys.modules["_preflight"] = preflight
_SPEC.loader.exec_module(preflight)


def test_counter_accumulates_across_buffer_clears():
    """The defect this replaces: a buffer that is emptied between updates.

    Each ``record`` stands for one tick seen in a handler. Nothing is read back
    out of a Ticker afterwards, so clearing cannot lose a count.
    """
    counter = preflight.StreamCounter("all_last")
    for i in range(120):
        counter.record(now_mono=float(i) * 0.5)
    report = counter.report(window_seconds=60.0)
    assert report["count"] == 120
    assert report["per_second"] == pytest.approx(2.0)


def test_counter_reports_inter_arrival_so_a_low_count_is_interpretable():
    """"Three ticks" means something different at 1s spacing than at 40s."""
    counter = preflight.StreamCounter("all_last")
    for offset in (0.0, 1.0, 41.0):
        counter.record(now_mono=offset)
    report = counter.report(window_seconds=90.0)
    assert report["count"] == 3
    assert report["max_inter_arrival_seconds"] == pytest.approx(40.0)
    assert report["first_offset_seconds"] == pytest.approx(0.0)
    assert report["last_offset_seconds"] == pytest.approx(41.0)


def test_a_silent_stream_reports_no_inter_arrival_rather_than_zero():
    report = preflight.StreamCounter("all_last").report(window_seconds=90.0)
    assert report["count"] == 0
    assert report["max_inter_arrival_seconds"] is None
    assert report["median_inter_arrival_seconds"] is None


def test_competing_session_error_is_fatal():
    """10197 is not a reconnect; a human has to close the other session."""
    assert 10197 in preflight.ENTITLEMENT_CODES
    assert preflight._fatal_entitlement_error(
        {"code": 10197, "message": "No market data during competing session"}
    )


@pytest.mark.parametrize("code", [354, 10089, 10189, 10197])
def test_entitlement_codes_are_fatal(code):
    assert preflight._fatal_entitlement_error({"code": code, "message": "x"})


def test_pacing_errors_are_not_treated_as_entitlement_failures():
    assert not preflight._fatal_entitlement_error({"code": 420, "message": "pacing violation"})
    assert preflight._fatal_entitlement_error(
        {"code": 420, "message": "No market data permissions for ARCA STK"}
    )


def test_market_checks_fail_closed_after_fatal_error_even_with_live_ticks():
    checks = preflight._market_checks(
        stable_snapshot=True,
        market_type=1,
        sample_counts={"bid_ask": 19885, "all_last": 1509, "bars_5s": 13},
        clock_ok=True,
        entitlement_blocked=True,
        sample_window=68.078,
        required_sample_seconds=120.0,
    )
    assert checks["no_fatal_entitlement_error"] is False
    assert checks["sample_window_complete"] is False
    assert all(checks.values()) is False


def test_market_checks_accept_complete_unblocked_window():
    checks = preflight._market_checks(
        stable_snapshot=True,
        market_type=1,
        sample_counts={"bid_ask": 1, "all_last": 1, "bars_5s": 1},
        clock_ok=True,
        entitlement_blocked=False,
        sample_window=120.001,
        required_sample_seconds=120.0,
    )
    assert all(checks.values()) is True


def test_clock_skew_is_round_trip_compensated():
    """Same correction as the recorder: the midpoint removes the round trip."""
    import time
    from datetime import datetime, timezone

    class FakeIB:
        def __init__(self, rtt: float):
            self.rtt = rtt
            self.sleeps = []

        def reqCurrentTime(self):
            time.sleep(self.rtt / 2)
            reply = datetime.fromtimestamp(time.time(), timezone.utc)
            time.sleep(self.rtt / 2)
            return reply

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            time.sleep(min(seconds, 0.01))

    ib = FakeIB(rtt=0.2)
    server = ib.reqCurrentTime()
    naive = time.time() - server.timestamp()
    samples = preflight.measure_clock_skew(ib, samples=5)

    import statistics

    assert len(samples) == 5
    assert ib.sleeps == [preflight.CLOCK_REQUEST_MIN_INTERVAL_SECONDS] * 5
    assert abs(statistics.median(samples)) < abs(naive)
    assert abs(statistics.median(samples)) < 0.05


def test_identifier_coverage_does_not_expose_identifiers():
    rows = [
        {"order_id": 17, "perm_id": 9001, "client_id": 933, "order_ref": "owned"},
        {"order_id": 0, "perm_id": 9002, "client_id": 0, "order_ref": ""},
    ]
    assert preflight._identifier_coverage(rows) == {
        "order_id": 1,
        "perm_id": 2,
        "client_id": 1,
        "order_ref": 1,
    }


def test_bounded_read_marks_missing_completion_unknown():
    def times_out():
        raise TimeoutError()

    rows, status = preflight._bounded_read("completed_orders", times_out)
    assert rows is None
    assert status["completed"] is False
    assert status["exception_type"] == "TimeoutError"


def test_session_label_is_evidence_only():
    """The label is recorded and routing remains an explicit argument."""
    parser_source = Path(preflight.__file__).read_text(encoding="utf-8")
    assert 'choices=("UNSPECIFIED", "OVERNIGHT", "RTH")' in parser_source
    assert '"session_label": args.session_label' in parser_source


@pytest.mark.parametrize(
    ("label", "exchange"),
    [("OVERNIGHT", "OVERNIGHT"), ("RTH", "SMART"), ("UNSPECIFIED", "SMART")],
)
def test_session_label_accepts_only_matching_explicit_route(label, exchange):
    preflight._validate_session_exchange(label, exchange)


@pytest.mark.parametrize(
    ("label", "exchange"),
    [("OVERNIGHT", "SMART"), ("RTH", "OVERNIGHT")],
)
def test_session_label_rejects_wrong_route(label, exchange):
    with pytest.raises(ValueError):
        preflight._validate_session_exchange(label, exchange)
