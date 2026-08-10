"""
Recorder storage, health and self-measurement.

Most of these exist because the corresponding measurement was wrong once and
produced a number that looked like evidence. The recorder's whole value is
that months later someone can trust the dataset, so the checks that matter
are the ones that stop it from reporting a healthy day it did not have.
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from ib_execution.processlock import ProcessLockUnavailable
from ib_execution.quote_recorder import (
    DEFAULT_GAP_THRESHOLDS,
    ClockSkew,
    QuoteRecorder,
    RawEventLog,
    RawTick,
    ReconnectBudget,
    ReconnectBudgetExhausted,
    SubscriptionLimiter,
    compute_cross_stream_diagnostics,
    compute_health,
    finalize_day,
    measure_clock_skew,
    parquet_schema,
)

SESSION = date(2026, 8, 5)
OPEN = datetime(2026, 8, 5, 13, 30, tzinfo=timezone.utc)    # 09:30 ET
CLOSE = datetime(2026, 8, 5, 20, 0, tzinfo=timezone.utc)    # 16:00 ET
OPEN_NS = int(OPEN.timestamp() * 1e9)


def _tick(i: int, ns: int, **over) -> RawTick:
    base = dict(
        event_id=i,
        recorder_run_id="run0000001",
        connection_epoch=1,
        contract_id=756733,
        event_type="BID_ASK",
        broker_timestamp="2026-08-05T14:00:00+00:00",
        local_wall_ns=ns,
        local_monotonic_ns=ns,
        market_data_type="LIVE",
        receive_sequence=i,
        bid=599.98,
        ask=600.02,
        bid_size=500.0,
        ask_size=500.0,
    )
    base.update(over)
    return RawTick(**base)


def _dense(log: RawEventLog, stream: str, step_seconds: float, **over) -> int:
    """Fill the whole session window with one stream at a fixed cadence."""
    span = (CLOSE - OPEN).total_seconds()
    n = int(span / step_seconds) + 1
    for i in range(n):
        ns = OPEN_NS + int(i * step_seconds * 1e9)
        log.append(_tick(i, ns, event_type=stream, **over), now_mono=float(i))
    return n


def _health(log, **kw):
    return compute_health(log, session_open=OPEN, session_close=CLOSE, **kw)


def _clean_log(tmp_path) -> RawEventLog:
    log = RawEventLog(tmp_path, session=SESSION)
    _dense(log, "BID_ASK", 1.0)
    _dense(log, "ALL_LAST", 5.0, last=600.0, last_size=100.0)
    _dense(log, "BAR_5S", 5.0, open=599.9, high=600.1, low=599.8, close=600.0,
           volume=1000.0, wap=599.99, trade_count=25)
    log.close()
    return log


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------


def test_segments_roll_and_rename_atomically(tmp_path):
    """Never hold one file open all session: a crash at 15:45 costs the whole day."""
    log = RawEventLog(tmp_path, session=SESSION, roll_seconds=10)
    for i in range(5):
        log.append(_tick(i, 1_000_000_000 * i), now_mono=float(i))
    for i in range(5, 10):
        log.append(_tick(i, 1_000_000_000 * i), now_mono=float(i * 10))
    log.close()

    assert len(log.segments()) >= 2, "expected the log to roll"
    assert not list(log.dir.glob(".partial-*")), "no partial files left behind"
    assert len(list(log.read_all())) == 10


def test_same_day_restart_keeps_earlier_segments_and_takes_a_new_run_id(tmp_path):
    """A same-day restart is normal and supported; it must not overwrite."""
    a = RawEventLog(tmp_path, session=SESSION)
    a.append(_tick(0, OPEN_NS), now_mono=0)
    a.close()
    b = RawEventLog(tmp_path, session=SESSION)
    b.append(_tick(1, OPEN_NS + 1_000_000_000), now_mono=0)
    b.close()
    assert len(a.segments()) == 2
    assert a.run_id != b.run_id


def test_a_second_live_recorder_cannot_steal_the_session_directory(tmp_path):
    """Without the lock, the newcomer renames the file the incumbent is writing.

    ``_recover_crashed_segments`` treats every ``.partial-`` file as debris
    from a dead process. That is only true while exactly one recorder owns the
    directory.
    """
    first = RawEventLog(tmp_path, session=SESSION)
    try:
        first.append(_tick(0, OPEN_NS), now_mono=0)
        with pytest.raises(ProcessLockUnavailable):
            RawEventLog(tmp_path, session=SESSION)
        assert list(first.dir.glob(".partial-*")), "incumbent's open segment survived"
    finally:
        first.close()
    # The successor is welcome once the incumbent is gone.
    RawEventLog(tmp_path, session=SESSION).close()


def test_row_identity_is_run_scoped_not_event_id(tmp_path):
    """event_id restarts at 1 every process; finalize_day folds runs together."""
    a = RawEventLog(tmp_path, session=SESSION, run_id="aaaaaaaaaa")
    a.append(_tick(1, OPEN_NS, recorder_run_id="aaaaaaaaaa"), now_mono=0)
    a.close()
    b = RawEventLog(tmp_path, session=SESSION, run_id="bbbbbbbbbb")
    b.append(_tick(1, OPEN_NS + 1_000_000_000, recorder_run_id="bbbbbbbbbb"), now_mono=0)
    b.close()

    rows = list(b.read_all())
    assert len(rows) == 2
    assert len({r["event_id"] for r in rows}) == 1, "event_id alone collides"
    identities = {(r["recorder_run_id"], r["receive_sequence"]) for r in rows}
    assert len(identities) == 2, "run-scoped identity separates them"


# --------------------------------------------------------------------------
# per-stream health
# --------------------------------------------------------------------------


def test_health_passes_on_a_clean_session(tmp_path):
    health = _health(_clean_log(tmp_path), clock_skew_samples=[0.1, 0.12, 0.08])
    assert health.ok(), health.problems()
    assert health.market_data_type == "LIVE"
    assert set(health.streams) == {"BID_ASK", "ALL_LAST", "BAR_5S"}


def test_a_dead_trade_stream_cannot_hide_behind_a_healthy_quote_stream(tmp_path):
    """The failure a pooled coverage/gap number is structurally blind to.

    Quotes arrive ~5x more often than prints here (and orders of magnitude
    more often in reality), so pooled statistics stay green while AllLast --
    the stream L2/L3 and every VWAP reconstruction depend on -- has been dead
    since mid-session.
    """
    log = RawEventLog(tmp_path, session=SESSION)
    _dense(log, "BID_ASK", 1.0)
    _dense(log, "BAR_5S", 5.0, volume=1000.0, trade_count=25,
           open=599.9, high=600.1, low=599.8, close=600.0, wap=599.99)
    # AllLast stops one hour in.
    for i in range(720):
        log.append(
            _tick(i, OPEN_NS + int(i * 5 * 1e9), event_type="ALL_LAST",
                  last=600.0, last_size=100.0),
            now_mono=float(i),
        )
    log.close()

    health = _health(log, clock_skew_samples=[0.0])
    assert not health.ok()
    assert health.streams["ALL_LAST"].rows > 0, "row count alone looks fine"
    assert health.streams["BID_ASK"].coverage_fraction > 0.99
    assert health.streams["ALL_LAST"].coverage_fraction < 0.25
    assert any("ALL_LAST" in p for p in health.problems())


def test_coverage_is_not_a_span(tmp_path):
    """A hole in the middle used to score ~100%: (last-first)/session is a span."""
    log = RawEventLog(tmp_path, session=SESSION)
    span = (CLOSE - OPEN).total_seconds()
    for i in range(60):                                   # first minute
        log.append(_tick(i, OPEN_NS + int(i * 1e9)), now_mono=float(i))
    for i in range(60):                                   # last minute
        ns = OPEN_NS + int((span - 60 + i) * 1e9)
        log.append(_tick(1000 + i, ns), now_mono=float(i))
    log.close()

    stream = _health(log).streams["BID_ASK"]
    assert stream.first_utc is not None and stream.last_utc is not None
    # Endpoints span the whole session, but almost none of it was observed.
    assert stream.coverage_fraction < 0.02
    assert stream.gaps_over_threshold >= 1


def test_late_start_and_early_finish_both_count_as_missing(tmp_path):
    log = RawEventLog(tmp_path, session=SESSION)
    offset = 1800.0                                        # 30 min late, 30 min early
    span = (CLOSE - OPEN).total_seconds()
    n = int((span - 2 * offset))
    for i in range(n):
        log.append(_tick(i, OPEN_NS + int((offset + i) * 1e9)), now_mono=float(i))
    log.close()

    stream = _health(log).streams["BID_ASK"]
    assert stream.missing_seconds == pytest.approx(2 * offset, abs=2.0)
    assert stream.coverage_fraction == pytest.approx(1 - (2 * offset) / span, abs=0.01)


def test_absent_required_stream_is_reported_per_stream(tmp_path):
    log = RawEventLog(tmp_path, session=SESSION)
    _dense(log, "BID_ASK", 1.0)
    log.close()
    health = _health(log, clock_skew_samples=[0.0])
    assert not health.ok()
    assert health.streams["ALL_LAST"].rows == 0
    assert any("ALL_LAST: no rows" in p for p in health.problems())


def test_health_detects_delayed_data(tmp_path):
    """Three months of delayed data voids every L2/L3 conclusion built on it."""
    log = RawEventLog(tmp_path, session=SESSION)
    _dense(log, "BID_ASK", 1.0, market_data_type="DELAYED")
    log.close()
    health = _health(log, clock_skew_samples=[0.0])
    assert not health.ok()
    assert any("LIVE" in p for p in health.problems())


def test_health_does_not_hide_a_delayed_interval_behind_a_final_live_tick(tmp_path):
    log = RawEventLog(tmp_path, session=SESSION)
    _dense(log, "BID_ASK", 1.0, market_data_type="DELAYED")
    log.append(_tick(99999, OPEN_NS, market_data_type="LIVE"), now_mono=0.0)
    log.close()
    assert _health(log).market_data_type.startswith("MIXED:")


def test_system_events_cannot_mask_a_market_data_gap(tmp_path):
    """A heartbeat is not a quote. SYSTEM rows must not fabricate availability."""
    log = RawEventLog(tmp_path, session=SESSION)
    log.append(_tick(0, OPEN_NS), now_mono=0.0)
    for i in range(100):                                   # heartbeats through the hole
        log.append(
            _tick(1 + i, OPEN_NS + int((i + 1) * 60 * 1e9), event_type="SYSTEM",
                  special_conditions="HEARTBEAT"),
            now_mono=float(i),
        )
    log.append(_tick(999, int(CLOSE.timestamp() * 1e9)), now_mono=1.0)
    log.close()

    stream = _health(log).streams["BID_ASK"]
    assert stream.rows == 2
    assert stream.max_gap_seconds > 3000


def test_health_surfaces_a_fatal_recorder_error(tmp_path):
    log = _clean_log(tmp_path)
    reopened = RawEventLog(tmp_path, session=SESSION)
    reopened.append(
        _tick(1, OPEN_NS, event_type="SYSTEM",
              special_conditions="RECORDER_ERROR:RecorderPrerequisiteError:10197"),
        now_mono=0.0,
    )
    reopened.close()
    health = _health(reopened, clock_skew_samples=[0.0])
    assert not health.ok()
    assert any("10197" in p for p in health.problems())
    assert log.session == reopened.session


def test_intentional_close_is_not_a_disconnect(tmp_path):
    log = RawEventLog(tmp_path, session=SESSION)
    log.append(
        _tick(0, OPEN_NS, event_type="SYSTEM",
              special_conditions="CONNECTION_CLOSED_INTENTIONAL"),
        now_mono=0.0,
    )
    log.close()
    assert _health(log).disconnects == 0


def test_health_records_every_run_id_that_contributed(tmp_path):
    a = RawEventLog(tmp_path, session=SESSION, run_id="aaaaaaaaaa")
    a.append(_tick(1, OPEN_NS, recorder_run_id="aaaaaaaaaa"), now_mono=0)
    a.close()
    b = RawEventLog(tmp_path, session=SESSION, run_id="bbbbbbbbbb")
    b.append(_tick(1, OPEN_NS, recorder_run_id="bbbbbbbbbb"), now_mono=0)
    b.close()
    assert _health(b).recorder_run_ids == ["aaaaaaaaaa", "bbbbbbbbbb"]


# --------------------------------------------------------------------------
# clock skew
# --------------------------------------------------------------------------


class _FakeIB:
    """A server clock with a known offset, a known round trip, and optional
    one-second quantization -- the two error sources that made the 2026-08-07
    +1.4s reading uninterpretable."""

    def __init__(self, true_skew: float = 0.0, rtt: float = 0.4, quantize: bool = False):
        self.true_skew = true_skew
        self.rtt = rtt
        self.quantize = quantize
        self.sleeps = []

    def reqCurrentTime(self):
        time.sleep(self.rtt / 2)                 # request leg
        server_now = time.time() - self.true_skew
        if self.quantize:
            server_now = int(server_now)
        reply = datetime.fromtimestamp(server_now, timezone.utc)
        time.sleep(self.rtt / 2)                 # reply leg
        return reply

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        time.sleep(min(seconds, 0.01))


def test_clock_skew_is_round_trip_compensated():
    """A naive now()-reqCurrentTime() folds the round trip into the estimate."""
    rtt, true_skew = 0.4, 0.0
    ib = _FakeIB(true_skew=true_skew, rtt=rtt)

    # What the preflight did: read the server clock, then read the local one.
    server = ib.reqCurrentTime()
    naive = time.time() - server.timestamp()
    compensated = ClockSkew.from_samples(measure_clock_skew(ib, samples=5, pause=0.0))

    assert compensated.median_seconds == pytest.approx(true_skew, abs=0.05)
    assert naive == pytest.approx(true_skew + rtt / 2, abs=0.1)
    assert abs(compensated.median_seconds) < abs(naive)


def test_a_quantized_server_clock_does_not_look_like_drift():
    """IB reports whole seconds, so one sample cannot distinguish the two.

    A single quantized reading can land anywhere in a one-second band. The
    median over many readings stays well inside the 2s health threshold, which
    is the whole reason a day is no longer judged on one probe.
    """
    ib = _FakeIB(true_skew=0.0, rtt=0.05, quantize=True)
    skew = ClockSkew.from_samples(measure_clock_skew(ib, samples=15, pause=0.0))
    assert skew.samples == 15
    assert abs(skew.median_seconds) < 2.0


def test_clock_requests_are_paced_before_every_real_request():
    ib = _FakeIB(rtt=0.0)
    measure_clock_skew(ib, samples=3)
    assert ib.sleeps == [1.1, 1.1, 1.1]


def test_one_unlucky_sample_cannot_decide_the_day(tmp_path):
    """The old code kept only the last probe, so 15:59 decided 09:30-16:00."""
    good = [0.10, 0.05, -0.02, 0.08, 0.11]
    health = _health(
        _clean_log(tmp_path), clock_skew_samples=good + [9.9]   # one wild reading
    )
    assert health.ok(), health.problems()
    assert health.clock_skew.max_abs_seconds == pytest.approx(9.9)
    assert health.clock_skew.samples == 6


def test_sustained_skew_still_fails(tmp_path):
    health = _health(_clean_log(tmp_path), clock_skew_samples=[3.1, 3.4, 3.2, 3.3])
    assert not health.ok()
    assert any("skew" in p for p in health.problems())


def test_unmeasured_skew_is_a_problem_not_a_pass(tmp_path):
    health = _health(_clean_log(tmp_path), clock_skew_samples=[])
    assert not health.ok()
    assert any("never measured" in p for p in health.problems())


# --------------------------------------------------------------------------
# cross-stream diagnostics
# --------------------------------------------------------------------------


def _bar_and_trades(bar_ts: datetime, volume: float, sizes: list[float], price=600.0):
    rows = [{
        "event_type": "BAR_5S", "broker_timestamp": bar_ts.isoformat(),
        "volume": volume, "trade_count": float(len(sizes)),
        "low": price - 1, "high": price + 1,
    }]
    for i, size in enumerate(sizes):
        rows.append({
            "event_type": "ALL_LAST",
            "broker_timestamp": (bar_ts + timedelta(seconds=i * 0.5)).isoformat(),
            "last_size": size, "last": price,
        })
    return rows


def test_cross_stream_reports_the_ratio_and_judges_nothing():
    """Day one measures the transform; it does not assume one.

    A validator hard-coding ``bar.volume * 100`` can be confidently wrong in
    both directions, so this reports a distribution and leaves calibrated
    False until a real session has been seen.
    """
    rows = []
    for i in range(20):
        ts = OPEN + timedelta(seconds=5 * i)
        rows += _bar_and_trades(ts, volume=10.0, sizes=[500.0, 500.0])   # ratio 1/100
    diag = compute_cross_stream_diagnostics(rows)
    assert diag.bars == 20
    assert diag.volume_ratio_median == pytest.approx(0.01)
    assert diag.count_ratio_median == pytest.approx(1.0)
    assert diag.price_containment_fraction == pytest.approx(1.0)
    assert diag.calibrated is False


def test_cross_stream_flags_bars_whose_trades_never_arrived():
    """The signature of a dropped AllLast stream while bars keep coming."""
    rows = []
    for i in range(10):
        ts = OPEN + timedelta(seconds=5 * i)
        rows += _bar_and_trades(ts, volume=10.0, sizes=[] if i >= 5 else [500.0])
    diag = compute_cross_stream_diagnostics(rows)
    assert diag.bars == 10
    assert diag.bars_with_trades == 5
    assert diag.bars_with_volume_but_no_ticks == 5


def test_cross_stream_is_empty_without_bars():
    assert compute_cross_stream_diagnostics([]).bars == 0


# --------------------------------------------------------------------------
# parquet
# --------------------------------------------------------------------------


def test_parquet_schema_is_declared_not_inferred():
    schema = parquet_schema()
    assert [f.name for f in schema] == [f.name for f in RawTick.__dataclass_fields__.values()]


def test_days_with_and_without_a_stream_still_concatenate(tmp_path):
    """The interaction that corrupts the archive's shape, not just one day.

    Inferring types per day gives a ``null``-typed column whenever a field is
    None for the whole day -- so the first day AllLast dies produces a Parquet
    file that will not concatenate with any other day, and multi-day
    concatenation is exactly what L2/L3 needs.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    def write(day: date, with_trades: bool) -> pa.Table:
        root = tmp_path / ("with" if with_trades else "without")
        log = RawEventLog(root, session=day)
        _dense(log, "BID_ASK", 60.0)
        if with_trades:
            _dense(log, "ALL_LAST", 60.0, last=600.0, last_size=100.0)
            _dense(log, "BAR_5S", 60.0, open=1.0, high=2.0, low=0.5, close=1.5,
                   volume=10.0, wap=1.2, trade_count=3)
        finalize_day(log, session_open=OPEN, session_close=CLOSE, clock_skew_samples=[0.0])
        return pq.read_table(log.dir / "events.parquet")

    quotes_only = write(SESSION, with_trades=False)
    complete = write(date(2026, 8, 6), with_trades=True)
    assert quotes_only.schema == complete.schema
    assert pa.concat_tables([quotes_only, complete]).num_rows == (
        quotes_only.num_rows + complete.num_rows
    )


def test_finalize_writes_parquet_health_and_a_verified_manifest(tmp_path):
    log = _clean_log(tmp_path)
    reopened = RawEventLog(tmp_path, session=SESSION)
    manifest = finalize_day(
        reopened, session_open=OPEN, session_close=CLOSE,
        clock_skew_samples=[0.1, 0.05, 0.12],
    )
    assert manifest["health_ok"] is True, manifest["problems"]
    assert manifest["parquet_rows_verified"] == manifest["rows"]
    assert (reopened.dir / "events.parquet").exists()
    assert (reopened.dir / "health.json").exists()
    assert all(len(value) == 64 for value in manifest["files"].values())
    assert log.session.isoformat() == manifest["session"]

    import pyarrow.parquet as pq

    assert pq.read_table(reopened.dir / "events.parquet").num_rows == manifest["rows"]


def test_finalize_folds_every_run_of_the_session_into_one_file(tmp_path):
    a = RawEventLog(tmp_path, session=SESSION, run_id="aaaaaaaaaa")
    a.append(_tick(1, OPEN_NS, recorder_run_id="aaaaaaaaaa"), now_mono=0)
    a.close()
    b = RawEventLog(tmp_path, session=SESSION, run_id="bbbbbbbbbb")
    b.append(_tick(1, OPEN_NS, recorder_run_id="bbbbbbbbbb"), now_mono=0)
    manifest = finalize_day(b, session_open=OPEN, session_close=CLOSE,
                            clock_skew_samples=[0.0])
    assert manifest["rows"] == 2
    assert manifest["recorder_run_ids"] == ["aaaaaaaaaa", "bbbbbbbbbb"]


# --------------------------------------------------------------------------
# reconnect budget and error classification
# --------------------------------------------------------------------------


def test_reconnect_budget_tolerates_blips_spread_across_a_session():
    """Eight ordinary blips must not retire the recorder at 10:30."""
    now = [0.0]
    budget = ReconnectBudget(short_window_seconds=900, short_limit=5,
                             session_limit=20, now=lambda: now[0])
    for _ in range(15):
        now[0] += 1800.0            # one blip every 30 minutes
        budget.record()
    assert budget.session_total == 15
    assert budget.recent == 1


def test_reconnect_budget_stops_a_crash_loop():
    now = [0.0]
    budget = ReconnectBudget(short_window_seconds=900, short_limit=5,
                             session_limit=20, now=lambda: now[0])
    with pytest.raises(ReconnectBudgetExhausted):
        for _ in range(6):
            now[0] += 1.0
            budget.record()


def test_reconnect_budget_has_a_session_backstop():
    now = [0.0]
    budget = ReconnectBudget(short_window_seconds=900, short_limit=5,
                             session_limit=20, now=lambda: now[0])
    with pytest.raises(ReconnectBudgetExhausted):
        for _ in range(21):
            now[0] += 1000.0
            budget.record()


def test_subscription_limiter_waits_when_the_bucket_is_empty():
    limiter = SubscriptionLimiter(rate_per_second=1000, burst=1)
    limiter.wait(lambda _: None)
    sleeps: list[float] = []
    limiter.wait(lambda delay: (sleeps.append(delay), time.sleep(delay)))
    assert sleeps and sleeps[0] > 0


@pytest.mark.parametrize("code", [354, 10089, 10189, 10197])
def test_entitlement_errors_are_not_retryable(code):
    assert QuoteRecorder._is_fatal_market_data_error(code, "localized message")


def test_competing_session_is_a_prerequisite_failure_not_a_reconnect():
    """10197 means a live and a paper login are contending for one subscription.

    Reconnecting produces the same error at a slower rate; only a human
    closing the other session fixes it.
    """
    assert QuoteRecorder._is_fatal_market_data_error(10197, "No market data during competing session")


def test_permission_specific_realtime_bar_error_is_not_retryable():
    assert QuoteRecorder._is_fatal_market_data_error(
        420, "No market data permissions for AMEX STK"
    )
    assert not QuoteRecorder._is_fatal_market_data_error(420, "generic pacing error")


def test_gap_thresholds_are_per_stream():
    """5-second bars arrive on a cadence; quotes and prints do not."""
    assert DEFAULT_GAP_THRESHOLDS["BID_ASK"] < DEFAULT_GAP_THRESHOLDS["BAR_5S"]
    assert DEFAULT_GAP_THRESHOLDS["BAR_5S"] < DEFAULT_GAP_THRESHOLDS["ALL_LAST"]


def test_ticker_wiring_attaches_exactly_one_handler_per_buffer():
    """ib_async returns one Ticker per contract today; that is not a contract.

    Two handlers on one buffer would double-record every tick, and the code
    must be correct whether one Ticker or two comes back.
    """
    class FakeEvent:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, fn):
            self.handlers.append(fn)
            return self

    class FakeTicker:
        def __init__(self):
            self.updateEvent = FakeEvent()

    pytest.importorskip("ib_async")
    rec = QuoteRecorder("unused", "SPY")
    shared = FakeTicker()
    rec._wire_ticker(shared)
    rec._wire_ticker(shared)              # same object returned twice
    assert len(shared.updateEvent.handlers) == 1

    other = FakeTicker()
    rec._wire_ticker(other)               # a distinct Ticker still gets wired
    assert len(other.updateEvent.handlers) == 1


def test_recorder_config_rejects_unknown_options():
    with pytest.raises(TypeError):
        QuoteRecorder("root", "SPY", nonexistent_option=1)


def test_stream_health_replace_keeps_dataclass_contract(tmp_path):
    health = _health(_clean_log(tmp_path)).streams["BID_ASK"]
    assert replace(health, rows=0).rows == 0
