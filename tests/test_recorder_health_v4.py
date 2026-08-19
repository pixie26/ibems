from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import ib_execution.recorder_health_v4 as recorder_health_v4
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


def _raw_row(row: dict[str, object], event_id: int) -> dict[str, object]:
    wall_ns = int(row["local_wall_ns"])
    payload: dict[str, object] = {
        "event_id": event_id,
        "recorder_run_id": "test-run",
        "connection_epoch": 1,
        "contract_id": 756733 if row["event_type"] != "SYSTEM" else 0,
        "event_type": row["event_type"],
        "broker_timestamp": datetime.fromtimestamp(wall_ns / 1e9, timezone.utc).isoformat(),
        "local_wall_ns": wall_ns,
        "local_monotonic_ns": event_id,
        "market_data_type": row.get("market_data_type", "UNKNOWN"),
        "receive_sequence": event_id,
        "bid": None,
        "ask": None,
        "bid_size": None,
        "ask_size": None,
        "last": None,
        "last_size": None,
        "exchange": None,
        "special_conditions": row.get("special_conditions"),
        "open": None,
        "high": None,
        "low": None,
        "close": None,
        "volume": None,
        "wap": None,
        "trade_count": None,
    }
    return payload


def _write_raw_segment(path: Path, rows: list[dict[str, object]]) -> str:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for event_id, row in enumerate(rows, 1):
            handle.write(json.dumps(_raw_row(row, event_id)) + "\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    secdef = next(
        item
        for item in health["advisories"]
        if item["stream"] == "SECURITY_DEFINITION_FARM"
    )
    assert secdef["classification"] == "AUXILIARY_FARM_DEGRADED"
    assert secdef["duration_seconds"] == pytest.approx(2.8)
    assert datetime.fromisoformat(secdef["start_utc"]) == BASE + timedelta(seconds=10)
    assert datetime.fromisoformat(secdef["end_utc"]) == BASE + timedelta(seconds=12.8)


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


def test_realtime_farm_only_marks_its_actual_overlap_with_a_bar_gap() -> None:
    start, end = _session()
    rows = [
        _market("BAR_5S", 5),
        _market("BAR_5S", 10),
        _status(2103, 20.0, "real-time farm broken"),
        _status(2104, 22.8, "real-time farm OK"),
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
    outages = [
        item for item in health["problems"] if item["classification"] == "FEED_OUTAGE"
    ]
    suspected = [
        item for item in health["problems"] if item["classification"] == "GAP_SUSPECTED"
    ]

    assert len(outages) == 1
    assert outages[0]["duration_seconds"] == pytest.approx(2.8)
    assert datetime.fromisoformat(outages[0]["start_utc"]) == BASE + timedelta(seconds=20)
    assert datetime.fromisoformat(outages[0]["end_utc"]) == BASE + timedelta(seconds=22.8)
    assert sum(item["duration_seconds"] for item in suspected) == pytest.approx(27.2)


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


def test_repeated_1100_keeps_the_first_outage_boundary() -> None:
    start, end = _session()
    rows = [
        *_bars(),
        _market("BID_ASK", 0),
        _market("BID_ASK", 60),
        _market("ALL_LAST", 0),
        _market("ALL_LAST", 60),
        _status(1100, 20, "connectivity lost"),
        _status(1100, 21, "connectivity still lost"),
        _status(1102, 22, "connectivity restored; data maintained"),
    ]

    health = analyze_rows_v4(rows, session_open=start, session_close=end)
    outage = next(item for item in health["problems"] if item["classification"] == "FEED_OUTAGE")

    assert outage["duration_seconds"] == pytest.approx(2.0)
    assert datetime.fromisoformat(outage["start_utc"]) == BASE + timedelta(seconds=20)


def test_one_realtime_farm_recovery_does_not_clear_another_farm() -> None:
    start, end = _session()
    rows = [
        _market("BAR_5S", 5),
        _market("BAR_5S", 10),
        _status(2103, 12, "Market data farm connection is broken:usfarm"),
        _status(2103, 14, "Market data farm connection is broken:eufarm"),
        _status(2104, 20, "Market data farm connection is OK:usfarm"),
        _status(2104, 30, "Market data farm connection is OK:eufarm"),
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
    outages = [
        item for item in health["problems"] if item["classification"] == "FEED_OUTAGE"
    ]

    assert sum(item["duration_seconds"] for item in outages) == pytest.approx(18.0)
    assert max(datetime.fromisoformat(item["end_utc"]) for item in outages) == (
        BASE + timedelta(seconds=30)
    )


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
    segment = raw_dir / "segment-test.jsonl.gz"
    segment_hash = _write_raw_segment(segment, rows)
    v3_health = tmp_path / "health.json"
    v3_manifest = tmp_path / "manifest.json"
    v3_health.write_text('{"ok": false}\n', encoding="utf-8")
    v3_manifest.write_text(
        json.dumps({"schema_version": 3, "files": {segment.name: segment_hash}}) + "\n",
        encoding="utf-8",
    )
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
    health = json.loads(health_path.read_text(encoding="utf-8"))
    assert health["input"]["original_manifest_inventory_match"] is True
    assert health["input"]["verification"] == {
        "compressed_sha256_scan_passes": 2,
        "pre_and_post_sha256_match": True,
        "segment_inventory_stable": True,
        "semantic_decode_passes": 1,
    }
    with pytest.raises(FileExistsError):
        write_reanalysis_v4(
            raw_dir,
            output,
            session_open=start,
            session_close=end,
            original_health=v3_health,
            original_manifest=v3_manifest,
        )


def test_v4_raw_reanalysis_rejects_unknown_event_type(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    rows = [
        *_bars(),
        _market("BID_ASK", 0),
        _market("BID_ASK", 60),
        _market("ALL_LAST", 0),
        _market("ALL_LAST", 60),
        _market("UNDECLARED_STREAM", 30),
    ]
    segment = raw_dir / "segment-test.jsonl.gz"
    segment_hash = _write_raw_segment(segment, rows)
    v3_health = tmp_path / "health.json"
    v3_manifest = tmp_path / "manifest.json"
    v3_health.write_text('{"ok": false}\n', encoding="utf-8")
    v3_manifest.write_text(
        json.dumps({"schema_version": 3, "files": {segment.name: segment_hash}}) + "\n",
        encoding="utf-8",
    )

    health_path, _ = write_reanalysis_v4(
        raw_dir,
        tmp_path / "v4",
        session_open=BASE,
        session_close=BASE + timedelta(seconds=60),
        original_health=v3_health,
        original_manifest=v3_manifest,
    )
    health = json.loads(health_path.read_text(encoding="utf-8"))

    assert health["health_ok"] is False
    raw_problem = next(item for item in health["problems"] if item["stream"] == "RAW")
    assert "RawRowValidationError" in raw_problem["evidence"][0]
    assert "unknown event_type" in raw_problem["evidence"][0]


def test_v4_reanalysis_refuses_manifest_raw_hash_mismatch(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    rows = [*_bars(), _market("BID_ASK", 0), _market("ALL_LAST", 0)]
    segment = raw_dir / "segment-test.jsonl.gz"
    _write_raw_segment(segment, rows)
    v3_health = tmp_path / "health.json"
    v3_manifest = tmp_path / "manifest.json"
    v3_health.write_text('{"ok": false}\n', encoding="utf-8")
    v3_manifest.write_text(
        json.dumps({"schema_version": 3, "files": {segment.name: "0" * 64}}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not match original v3 manifest"):
        write_reanalysis_v4(
            raw_dir,
            tmp_path / "v4",
            session_open=BASE,
            session_close=BASE + timedelta(seconds=60),
            original_health=v3_health,
            original_manifest=v3_manifest,
        )


def test_v4_reanalysis_refuses_raw_change_between_hash_and_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    rows = [
        *_bars(),
        _market("BID_ASK", 0),
        _market("BID_ASK", 60),
        _market("ALL_LAST", 0),
        _market("ALL_LAST", 60),
    ]
    segment = raw_dir / "segment-test.jsonl.gz"
    segment_hash = _write_raw_segment(segment, rows)
    replacement = tmp_path / "replacement.jsonl.gz"
    _write_raw_segment(replacement, [*rows, _status(2157, 20, "mutated")])
    replacement_bytes = replacement.read_bytes()
    v3_health = tmp_path / "health.json"
    v3_manifest = tmp_path / "manifest.json"
    v3_health.write_text('{"ok": false}\n', encoding="utf-8")
    v3_manifest.write_text(
        json.dumps({"schema_version": 3, "files": {segment.name: segment_hash}}) + "\n",
        encoding="utf-8",
    )
    original_capture = recorder_health_v4._capture_raw_state
    capture_count = 0

    def mutate_after_initial_capture(path: Path):
        nonlocal capture_count
        captured = original_capture(path)
        capture_count += 1
        if capture_count == 1:
            segment.write_bytes(replacement_bytes)
        return captured

    monkeypatch.setattr(recorder_health_v4, "_capture_raw_state", mutate_after_initial_capture)

    with pytest.raises(ValueError, match="changed between attestation and semantic analysis"):
        write_reanalysis_v4(
            raw_dir,
            tmp_path / "v4",
            session_open=BASE,
            session_close=BASE + timedelta(seconds=60),
            original_health=v3_health,
            original_manifest=v3_manifest,
        )
    assert not (tmp_path / "v4" / "health-v4.json").exists()
