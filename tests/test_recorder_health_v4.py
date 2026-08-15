from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ib_execution.recorder_health_v4 import analyze_rows_v4, write_reanalysis_v4

BASE = datetime(2026, 8, 14, 13, 30, tzinfo=timezone.utc)


def _ns(seconds: float) -> int:
    return int((BASE + timedelta(seconds=seconds)).timestamp() * 1e9)


def _market(stream: str, seconds: float) -> dict[str, object]:
    return {
        "event_type": stream,
        "local_wall_ns": _ns(seconds),
        "market_data_type": "LIVE",
    }


def _status(code: int, seconds: float, message: str = "status") -> dict[str, object]:
    return {
        "event_type": "SYSTEM",
        "local_wall_ns": _ns(seconds),
        "special_conditions": f"IB_ERROR:{code}:-1:{message}",
    }


def _session(seconds: float = 60.0) -> tuple[datetime, datetime]:
    return BASE, BASE + timedelta(seconds=seconds)


def _bars(seconds: float = 60.0) -> list[dict[str, object]]:
    return [_market("BAR_5S", value) for value in range(5, int(seconds), 5)]


def test_auxiliary_farm_churn_cannot_fail_a_day_with_live_bars() -> None:
    start, end = _session()
    rows = [
        *_bars(),
        _market("BID_ASK", 0),
        _market("BID_ASK", 10),
        _market("BID_ASK", 60),
        _market("ALL_LAST", 0),
        _market("ALL_LAST", 60),
        _status(2157, 10.0, "sec-def broken"),
        _status(2158, 12.8, "sec-def OK"),
    ]

    health = analyze_rows_v4(rows, session_open=start, session_close=end)

    assert health["health_ok"] is True
    assert health["problems"] == []
    assert [item["code"] for item in health["auxiliary_farm_statuses"]] == [2157, 2158]


def test_six_short_bidask_gaps_remain_a_metric_not_a_hard_problem() -> None:
    start, end = _session(36.0)
    rows = [
        *_bars(36.0),
        *[_market("BID_ASK", value) for value in (0, 6, 12, 18, 24, 30, 36)],
        _market("ALL_LAST", 0),
        _market("ALL_LAST", 36),
    ]

    health = analyze_rows_v4(rows, session_open=start, session_close=end)
    bidask = health["stream_metrics"]["BID_ASK"]

    assert health["health_ok"] is True
    assert bidask["legacy_v3_gaps_over_threshold"] == 6
    assert bidask["gaps_over_observation_threshold"] == 0
    assert all(item["stream"] != "BID_ASK" for item in health["problems"])


def test_event_driven_gap_over_30_seconds_is_advisory_only() -> None:
    start, end = _session()
    rows = [
        *_bars(),
        _market("BID_ASK", 0),
        _market("BID_ASK", 40),
        _market("BID_ASK", 60),
        _market("ALL_LAST", 0),
        _market("ALL_LAST", 60),
    ]

    health = analyze_rows_v4(rows, session_open=start, session_close=end)

    assert health["health_ok"] is True
    assert any(
        item["stream"] == "BID_ASK" and item["classification"] == "EVENT_DRIVEN_GAP"
        for item in health["advisories"]
    )


def test_unexplained_bar_gap_is_hard_gap_suspected() -> None:
    start, end = _session()
    rows = [
        _market("BAR_5S", 5),
        _market("BAR_5S", 10),
        _market("BAR_5S", 40),
        _market("BAR_5S", 45),
        _market("BAR_5S", 50),
        _market("BAR_5S", 55),
        _market("BID_ASK", 0),
        _market("BID_ASK", 60),
        _market("ALL_LAST", 0),
        _market("ALL_LAST", 60),
    ]

    health = analyze_rows_v4(rows, session_open=start, session_close=end)

    assert health["health_ok"] is False
    assert any(item["classification"] == "GAP_SUSPECTED" for item in health["problems"])


def test_realtime_farm_plus_bar_gap_is_hard_feed_outage() -> None:
    start, end = _session()
    rows = [
        _market("BAR_5S", 5),
        _market("BAR_5S", 10),
        _status(2103, 12, "real-time farm broken"),
        _market("BAR_5S", 40),
        _status(2104, 41, "real-time farm OK"),
        _market("BAR_5S", 45),
        _market("BAR_5S", 50),
        _market("BAR_5S", 55),
        _market("BID_ASK", 0),
        _market("BID_ASK", 60),
        _market("ALL_LAST", 0),
        _market("ALL_LAST", 60),
    ]

    health = analyze_rows_v4(rows, session_open=start, session_close=end)

    assert health["health_ok"] is False
    assert any(item["classification"] == "FEED_OUTAGE" for item in health["problems"])


def test_1100_is_direct_hard_outage_even_if_bars_continue() -> None:
    start, end = _session()
    rows = [
        *_bars(),
        _market("BID_ASK", 0),
        _market("BID_ASK", 60),
        _market("ALL_LAST", 0),
        _market("ALL_LAST", 60),
        _status(1100, 20, "connectivity lost"),
        _status(1102, 22, "connectivity restored; data maintained"),
    ]

    health = analyze_rows_v4(rows, session_open=start, session_close=end)

    assert health["health_ok"] is False
    outage = next(item for item in health["problems"] if item["classification"] == "FEED_OUTAGE")
    assert outage["duration_seconds"] == pytest.approx(2.0)


def test_v4_sidecars_are_create_only_and_reference_v3_hashes(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    rows = [
        *_bars(),
        _market("BID_ASK", 0),
        _market("BID_ASK", 60),
        _market("ALL_LAST", 0),
        _market("ALL_LAST", 60),
    ]
    with gzip.open(raw_dir / "segment-test.jsonl.gz", "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    v3_health = tmp_path / "health.json"
    v3_manifest = tmp_path / "manifest.json"
    v3_health.write_text('{"ok": false}\n', encoding="utf-8")
    v3_manifest.write_text('{"schema_version": 3}\n', encoding="utf-8")
    output = tmp_path / "v4"
    start, end = _session()

    health_path, amendment_path = write_reanalysis_v4(
        raw_dir,
        output,
        session_open=start,
        session_close=end,
        original_health=v3_health,
        original_manifest=v3_manifest,
    )
    amendment = json.loads(amendment_path.read_text(encoding="utf-8"))

    assert health_path.name == "health-v4.json"
    assert amendment["original_gate_unchanged"] is True
    assert amendment["originals"]["health_v3"]["sha256"]
    assert amendment["originals"]["manifest_v3"]["sha256"]
    with pytest.raises(FileExistsError):
        write_reanalysis_v4(
            raw_dir,
            output,
            session_open=start,
            session_close=end,
            original_health=v3_health,
            original_manifest=v3_manifest,
        )
