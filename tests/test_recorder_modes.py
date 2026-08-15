from __future__ import annotations

import time
from dataclasses import asdict

import pytest

from ib_execution.market_liveness import LivenessAction
from ib_execution.quote_recorder import (
    DataMode,
    QuoteRecorder,
    RawEventLog,
    RawTick,
    RecorderConfig,
)


def _tick(event_id: int, event_type: str, monotonic_ns: int, **values):
    defaults = dict(
        event_id=event_id,
        recorder_run_id="mode-test",
        connection_epoch=1,
        contract_id=756733,
        event_type=event_type,
        broker_timestamp="2026-08-10T13:30:00+00:00",
        local_wall_ns=event_id,
        local_monotonic_ns=monotonic_ns,
        market_data_type="LIVE",
        receive_sequence=event_id,
    )
    defaults.update(values)
    return RawTick(**defaults)


def test_research_full_persists_every_bidask(tmp_path):
    recorder = QuoteRecorder(tmp_path, mode=DataMode.RESEARCH_FULL)
    recorder.log = RawEventLog(tmp_path, run_id=recorder.run_id)
    try:
        for index in range(3):
            recorder._record_market_event(
                "BID_ASK",
                "2026-08-10T13:30:00+00:00",
                contract_id=756733,
                bid=600.0 + index * 0.01,
                ask=600.02 + index * 0.01,
                bid_size=10.0,
                ask_size=12.0,
            )
        recorder.log.flush()
        rows = list(recorder.log.read_all())
    finally:
        recorder.log.close()

    assert [row["event_type"] for row in rows] == ["BID_ASK"] * 3
    assert recorder.capture_manifest()["selected_events"]["BID_ASK"] == 3
    assert recorder.capture_manifest()["filtered_events"].get("BID_ASK", 0) == 0


def test_evidence_sampled_can_filter_bidask_but_never_bars_or_trades(tmp_path):
    recorder = QuoteRecorder(
        tmp_path,
        mode=DataMode.EVIDENCE_SAMPLED,
        bidask_sample_interval_seconds=60.0,
        bidask_on_price_change=False,
    )
    recorder.log = RawEventLog(tmp_path, run_id=recorder.run_id)
    try:
        recorder._record_market_event(
            "BID_ASK",
            "2026-08-10T13:30:00+00:00",
            contract_id=756733,
            bid=600.0,
            ask=600.02,
            bid_size=10.0,
            ask_size=12.0,
        )
        recorder._record_market_event(
            "BID_ASK",
            "2026-08-10T13:30:00.100000+00:00",
            contract_id=756733,
            bid=600.0,
            ask=600.02,
            bid_size=10.0,
            ask_size=12.0,
        )
        recorder._record_market_event(
            "ALL_LAST",
            "2026-08-10T13:30:00.100000+00:00",
            contract_id=756733,
            last=600.01,
            last_size=100.0,
        )
        recorder._record_market_event(
            "BAR_5S",
            "2026-08-10T13:30:05+00:00",
            contract_id=756733,
            open=600.0,
            high=600.1,
            low=599.9,
            close=600.05,
            volume=1000.0,
            wap=600.02,
            trade_count=20,
        )
        recorder.log.flush()
        rows = list(recorder.log.read_all())
    finally:
        recorder.log.close()

    assert [row["event_type"] for row in rows] == ["BID_ASK", "ALL_LAST", "BAR_5S"]
    manifest = recorder.capture_manifest()
    assert manifest["selected_events"]["BID_ASK"] == 1
    assert manifest["filtered_events"]["BID_ASK"] == 1
    assert manifest["selected_events"]["ALL_LAST"] == 1
    assert manifest["selected_events"]["BAR_5S"] == 1


def test_bar_staleness_is_the_only_actionable_stream_gap(tmp_path):
    """Bars are time-driven, so their absence is decidable where a quote gap is not."""
    recorder = QuoteRecorder(tmp_path, mode=DataMode.RESEARCH_FULL)
    recorder.liveness.subscription_started(100.0)
    for stream in ("BID_ASK", "ALL_LAST"):
        recorder.liveness.note_event(stream, 119.0)

    state = recorder.liveness.assess(now_mono=120.0)

    assert state.action is LivenessAction.RECOVER_SUBSCRIPTION
    assert state.heartbeat_lost is True
    assert state.heartbeat_age == 20.0


def test_quote_staleness_is_reported_but_never_acts(tmp_path):
    """Event-driven silence becomes a 30s advisory but never a recovery trigger."""
    recorder = QuoteRecorder(tmp_path, mode=DataMode.RESEARCH_FULL)
    recorder.liveness.subscription_started(100.0)
    recorder.liveness.note_event("ALL_LAST", 110.0)
    recorder.liveness.note_event("BAR_5S", 129.0)

    assert recorder.stream_staleness(now_mono=131.0) == {"BID_ASK": 31.0}
    assert "BAR_5S" not in recorder.stream_staleness(now_mono=131.0)
    assert recorder.liveness.assess(now_mono=131.0).action is LivenessAction.CONTINUE


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
    try:
        base = time.monotonic()
        recorder.capture_policy._last_bidask_persist_mono = base
        recorder._record_market_event(
            "BID_ASK",
            "2026-08-10T13:30:00+00:00",
            contract_id=756733,
            bid=600.0,
            ask=600.02,
            bid_size=10.0,
            ask_size=12.0,
        )
        recorder.mark_decision(now_mono=base + 0.1)
        recorder.log.flush()
        rows = list(recorder.log.read_all())
    finally:
        recorder.log.close()

    assert len(rows) == 1
    assert rows[0]["event_type"] == "BID_ASK"
    assert recorder.capture_manifest()["filtered_events"].get("BID_ASK", 0) == 0


def test_config_default_is_research_full():
    cfg = RecorderConfig(root=".")

    assert cfg.mode == DataMode.RESEARCH_FULL
    assert cfg.bidask_sample_interval_seconds == pytest.approx(1.0)
