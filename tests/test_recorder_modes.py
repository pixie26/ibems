from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from ib_execution.market_liveness import LivenessAction
from ib_execution.quote_recorder import (
    QuoteRecorder,
    RawEventLog,
    RawTick,
    RecorderWriteFailed,
    finalize_day,
)
from ib_execution.recorder_modes import CapturePolicy, DataMode


def _tick(i: int, event_type: str, run_id: str) -> RawTick:
    return RawTick(
        event_id=i,
        recorder_run_id=run_id,
        connection_epoch=1,
        contract_id=756733,
        event_type=event_type,
        broker_timestamp="2026-08-11T13:30:00+00:00",
        local_wall_ns=i,
        local_monotonic_ns=i,
        market_data_type="LIVE",
        receive_sequence=i,
        bid=600.0 if event_type == "BID_ASK" else None,
        ask=600.01 if event_type == "BID_ASK" else None,
        last=600.0 if event_type == "ALL_LAST" else None,
        last_size=1.0 if event_type == "ALL_LAST" else None,
        open=600.0 if event_type == "BAR_5S" else None,
        high=600.0 if event_type == "BAR_5S" else None,
        low=600.0 if event_type == "BAR_5S" else None,
        close=600.0 if event_type == "BAR_5S" else None,
        volume=1.0 if event_type == "BAR_5S" else None,
        wap=600.0 if event_type == "BAR_5S" else None,
        trade_count=1 if event_type == "BAR_5S" else None,
    )


def test_execution_minimal_never_starts_the_raw_recorder(tmp_path):
    policy = CapturePolicy(DataMode.EXECUTION_MINIMAL)
    assert not policy.should_persist("ALL_LAST", now_mono=1.0)
    assert policy.manifest()["bidask"] == "none"
    with pytest.raises(ValueError, match="does not start RawEventLog"):
        QuoteRecorder(tmp_path, mode=DataMode.EXECUTION_MINIMAL)


def test_research_full_keeps_every_market_event():
    policy = CapturePolicy(DataMode.RESEARCH_FULL)
    assert policy.should_persist("BID_ASK", now_mono=1.0, bid=1.0, ask=1.1)
    assert policy.should_persist("BID_ASK", now_mono=1.01, bid=1.0, ask=1.1)
    assert policy.should_persist("ALL_LAST", now_mono=1.01)
    assert policy.should_persist("BAR_5S", now_mono=1.01)


def test_evidence_sampled_rule_is_deterministic_and_declared():
    policy = CapturePolicy(
        DataMode.EVIDENCE_SAMPLED,
        bidask_sample_interval_seconds=1.0,
        decision_window_seconds=2.0,
    )
    assert policy.should_persist("BID_ASK", now_mono=10.0, bid=1.0, ask=1.1)
    assert not policy.should_persist("BID_ASK", now_mono=10.1, bid=1.0, ask=1.1)
    assert policy.should_persist("BID_ASK", now_mono=10.2, bid=1.01, ask=1.1)
    assert policy.should_persist("BID_ASK", now_mono=11.2, bid=1.01, ask=1.1)
    policy.open_decision_window(12.0)
    assert policy.should_persist("BID_ASK", now_mono=12.1, bid=1.01, ask=1.1)
    assert policy.should_persist("ALL_LAST", now_mono=12.1)
    assert policy.should_persist("BAR_5S", now_mono=12.1)
    manifest = policy.manifest()
    assert manifest["mode"] == "evidence_sampled"
    assert manifest["bidask"] == "interval_or_price_change_or_decision_window"


def test_manifest_carries_the_whole_callback_to_readback_chain(tmp_path):
    log = RawEventLog(tmp_path, queue_capacity=10, batch_records=2)
    for i, stream in enumerate(("BID_ASK", "ALL_LAST", "BAR_5S"), 1):
        log.append(_tick(i, stream, log.run_id), now_mono=float(i))
    start = datetime(2026, 8, 11, 13, 30, tzinfo=timezone.utc)
    manifest = finalize_day(
        log,
        session_open=start,
        session_close=start + timedelta(seconds=60),
        clock_skew_samples=[0.0],
        handler_counts={"BID_ASK": 2, "ALL_LAST": 1, "BAR_5S": 1},
        selected_counts={"BID_ASK": 1, "ALL_LAST": 1, "BAR_5S": 1},
        filtered_counts={"BID_ASK": 1},
        capture_policy=CapturePolicy(DataMode.EVIDENCE_SAMPLED).manifest(),
    )
    accounting = manifest["write_accounting"]
    assert accounting["handled_count"] == 4
    assert accounting["selected_count"] == 3
    assert accounting["enqueued_count"] == 3
    assert accounting["persisted_count"] == 3
    assert accounting["readback_count"] == 3
    assert accounting["dropped_count"] == 0
    assert accounting["fsync_count"] >= 1
    assert set(accounting["fsync_latency_ms"]) == {"max", "mean", "p95"}
    assert manifest["capture_policy"]["mode"] == "evidence_sampled"


def test_callback_accounting_mismatch_refuses_finalization(tmp_path):
    log = RawEventLog(tmp_path)
    log.append(_tick(1, "BID_ASK", log.run_id), now_mono=1.0)
    start = datetime(2026, 8, 11, 13, 30, tzinfo=timezone.utc)
    with pytest.raises(RecorderWriteFailed, match="callback accounting mismatch"):
        finalize_day(
            log,
            session_open=start,
            session_close=start + timedelta(seconds=60),
            handler_counts={"BID_ASK": 2},
            selected_counts={"BID_ASK": 1},
            filtered_counts={},
        )


def test_liveness_detects_a_live_socket_with_a_dead_bar_subscription(tmp_path):
    """The socket is up and quotes still flow, but the bar cadence stopped.

    Bars are time-driven, so their absence is decidable where a quote gap is
    not -- this is the one market-data observation allowed to stop the run.
    """
    recorder = QuoteRecorder(tmp_path, mode=DataMode.RESEARCH_FULL)
    recorder.liveness.subscription_started(100.0)
    for stream in ("BID_ASK", "ALL_LAST"):
        recorder.liveness.note_event(stream, 119.0)

    state = recorder.liveness.assess(now_mono=120.0)

    assert state.action is LivenessAction.RECOVER_SUBSCRIPTION
    assert state.heartbeat_lost is True
    assert state.heartbeat_age == 20.0


def test_quote_staleness_is_reported_but_never_acts(tmp_path):
    """The mirror image: bars healthy, quotes silent. Report only.

    A long BID_ASK gap on a live subscription is a fact about the tape.
    Acting on it is what made a quiet OVERNIGHT window produce the same
    failure text as a genuinely dead feed.
    """
    recorder = QuoteRecorder(tmp_path, mode=DataMode.RESEARCH_FULL)
    recorder.liveness.subscription_started(100.0)
    recorder.liveness.note_event("BAR_5S", 118.0)

    assert recorder.stream_staleness(now_mono=120.0) == {"BID_ASK": 20.0}
    assert "BAR_5S" not in recorder.stream_staleness(now_mono=120.0)
    assert recorder.liveness.assess(now_mono=120.0).action is LivenessAction.CONTINUE


def test_decision_window_promotes_the_complete_predecision_bidask_ring(tmp_path):
    recorder = QuoteRecorder(
        tmp_path,
        mode=DataMode.EVIDENCE_SAMPLED,
        bidask_sample_interval_seconds=60.0,
        bidask_on_price_change=False,
        decision_pre_window_seconds=30.0,
        decision_window_seconds=30.0,
    )
    recorder.log = RawEventLog(tmp_path, run_id=recorder.run_id)
    when = datetime.now(timezone.utc)
    values = {"contract_id": 756733, "bid": 600.0, "ask": 600.01}
    for _ in range(3):
        recorder._note_handled("BID_ASK")
        recorder._record_market_event("BID_ASK", when, **values)
    assert recorder.selected_events == {"BID_ASK": 1}
    assert recorder.filtered_events == {"BID_ASK": 2}

    recorder.mark_decision(now_mono=time.monotonic())
    recorder.log.close()
    rows = list(recorder.log.read_all())
    assert len(rows) == 3
    assert recorder.selected_events == {"BID_ASK": 3}
    assert recorder.filtered_events == {}


def test_the_halt_tick_is_not_requested_by_default(tmp_path):
    """A real Gateway answered error 321 and the whole subscription failed.

    docs/GATE_B2_CONTROLLED_DISCONNECT_20260812_ZH.md section 1: the probe
    never got a LIVE marketDataType callback and the run ended with three
    zero streams. Asking for an unsupported generic tick can cost the entire
    market-data request, so the default must be the set the Gateway serves.
    """
    recorder = QuoteRecorder(tmp_path, mode=DataMode.RESEARCH_FULL)

    assert recorder.config.market_data_generic_ticks == ""


def test_an_operator_can_still_opt_into_the_halt_tick(tmp_path):
    recorder = QuoteRecorder(
        tmp_path, mode=DataMode.RESEARCH_FULL, market_data_generic_ticks="49"
    )

    assert recorder.config.market_data_generic_ticks == "49"
