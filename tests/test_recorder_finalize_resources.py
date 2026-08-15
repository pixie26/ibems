from __future__ import annotations

import errno
import gzip
import json
import sqlite3
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

import pytest

import ib_execution.quote_recorder as recorder
from ib_execution.processlock import ProcessLock, ProcessLockUnavailable
from ib_execution.quote_recorder import (
    RawEventLog,
    RawTick,
    _stream_health,
    compute_cross_stream_diagnostics,
    compute_health,
    finalize_day,
)

pytest.importorskip("pyarrow")

SESSION = date(2026, 8, 14)
OPEN = datetime(2026, 8, 14, 13, 30, tzinfo=timezone.utc)
CLOSE = OPEN + timedelta(hours=6, minutes=30)
OPEN_NS = int(OPEN.timestamp() * 1e9)


def _tick(i: int, *, run_id: str, event_type: str = "BID_ASK", seconds: float = 0) -> RawTick:
    broker = OPEN + timedelta(seconds=seconds)
    values = {
        "bid": 600.0,
        "ask": 600.01,
        "bid_size": 100.0,
        "ask_size": 200.0,
    }
    if event_type == "ALL_LAST":
        values = {"last": 600.0 + i / 100, "last_size": float(i + 1)}
    elif event_type == "BAR_5S":
        values = {
            "open": 599.9,
            "high": 601.0,
            "low": 599.0,
            "close": 600.0,
            "volume": 1000.0,
            "wap": 600.0,
            "trade_count": 2,
        }
    return RawTick(
        event_id=i,
        recorder_run_id=run_id,
        connection_epoch=1,
        contract_id=756733,
        event_type=event_type,
        broker_timestamp=broker.isoformat(),
        local_wall_ns=OPEN_NS + int(seconds * 1e9),
        local_monotonic_ns=1_000_000 + i,
        market_data_type="LIVE",
        receive_sequence=i,
        **values,
    )


def _log_with_rows(tmp_path, count: int = 10) -> RawEventLog:
    log = RawEventLog(tmp_path, session=SESSION, run_id="resource01")
    for i in range(count):
        log.append(_tick(i, run_id=log.run_id, seconds=float(i)), now_mono=float(i))
    log.close()
    return log


def test_finalize_consumes_the_raw_snapshot_exactly_once(tmp_path, monkeypatch):
    log = _log_with_rows(tmp_path)
    original = log.read_all
    calls = 0

    def one_shot(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("finalize attempted a second whole-session read")
        yield from original(*args, **kwargs)

    monkeypatch.setattr(log, "read_all", one_shot)
    manifest = finalize_day(log, session_open=OPEN, session_close=CLOSE)
    assert manifest["rows"] == 10
    assert calls == 1


def test_finalize_writes_fixed_size_row_groups_instead_of_one_whole_day(
    tmp_path, monkeypatch
):
    import pyarrow.parquet as pq

    monkeypatch.setattr(recorder, "FINALIZE_BATCH_ROWS", 3)
    log = _log_with_rows(tmp_path, count=10)
    finalize_day(log, session_open=OPEN, session_close=CLOSE)
    metadata = pq.ParquetFile(log.dir / "events.parquet").metadata
    assert metadata.num_rows == 10
    assert metadata.num_row_groups == 4
    assert [metadata.row_group(i).num_rows for i in range(4)] == [3, 3, 3, 1]


def test_disk_backed_health_matches_the_original_sorting_semantics(tmp_path):
    rows = [
        asdict(_tick(1, run_id="resource01", seconds=10)),
        asdict(_tick(2, run_id="resource01", seconds=0)),
        asdict(_tick(3, run_id="resource01", seconds=0)),
        asdict(_tick(4, run_id="resource01", event_type="ALL_LAST", seconds=4)),
        asdict(_tick(5, run_id="resource01", event_type="ALL_LAST", seconds=1)),
        asdict(_tick(6, run_id="resource01", event_type="BAR_5S", seconds=0)),
        asdict(_tick(7, run_id="resource01", event_type="BAR_5S", seconds=2)),
    ]
    invalid = asdict(_tick(8, run_id="resource01", event_type="BAR_5S", seconds=20))
    invalid["broker_timestamp"] = "not-a-time"
    rows.insert(2, invalid)

    class Rows:
        session = SESSION

        def read_all(self):
            yield from rows

    health = compute_health(Rows(), session_open=OPEN, session_close=CLOSE)
    for stream in ("BID_ASK", "ALL_LAST", "BAR_5S"):
        stamps = [row["local_wall_ns"] for row in rows if row["event_type"] == stream]
        expected = _stream_health(
            stamps,
            stream,
            OPEN_NS,
            int(CLOSE.timestamp() * 1e9),
            recorder.DEFAULT_GAP_THRESHOLDS[stream],
        )
        assert health.streams[stream] == expected
    assert health.cross_stream == compute_cross_stream_diagnostics(rows)


def test_finalize_lock_refuses_a_competing_publisher(tmp_path):
    log = _log_with_rows(tmp_path, count=1)
    with ProcessLock(log.dir / ".finalize.lock").acquire(note="incumbent-finalizer"):
        with pytest.raises(ProcessLockUnavailable):
            finalize_day(log, session_open=OPEN, session_close=CLOSE)
    assert not (log.dir / "manifest.json").exists()


def test_finalize_uses_one_frozen_segment_snapshot(tmp_path):
    log = _log_with_rows(tmp_path, count=1)
    added = False

    def add_late_segment(stage: str, rows: int, _segments: int) -> None:
        nonlocal added
        if added or stage != "READING_RAW" or rows != 0:
            return
        added = True
        late = log.dir / "segment-zzzzzz-late-99999.jsonl.gz"
        with gzip.open(late, "wt", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(_tick(99, run_id=log.run_id, seconds=99))) + "\n")

    manifest = recorder._finalize_day(
        log,
        session_open=OPEN,
        session_close=CLOSE,
        progress=add_late_segment,
    )
    assert added
    assert len(log.segments()) == 2
    assert manifest["rows"] == 1


def test_corrupt_candidate_never_replaces_a_previous_complete_dataset(tmp_path):
    first = _log_with_rows(tmp_path, count=2)
    finalize_day(first, session_open=OPEN, session_close=CLOSE)
    published = {
        name: (first.dir / name).read_bytes()
        for name in ("events.parquet", "health.json", "manifest.json")
    }

    successor = RawEventLog(tmp_path, session=SESSION, run_id="resource02")
    successor.append(
        _tick(3, run_id=successor.run_id, seconds=3), now_mono=3.0
    )
    successor.close()
    corrupted = False

    def truncate_candidate(stage: str, _rows: int, _segments: int) -> None:
        nonlocal corrupted
        if corrupted or stage != "VERIFYING_PARQUET":
            return
        candidate = next(successor.dir.glob(".finalize-*/events.parquet"))
        with candidate.open("r+b") as fh:
            fh.truncate(max(1, candidate.stat().st_size // 2))
        corrupted = True

    with pytest.raises(Exception):
        recorder._finalize_day(
            successor,
            session_open=OPEN,
            session_close=CLOSE,
            progress=truncate_candidate,
        )
    assert corrupted
    for name, expected in published.items():
        assert (successor.dir / name).read_bytes() == expected


def test_sqlite_staging_failure_publishes_nothing(tmp_path, monkeypatch):
    log = _log_with_rows(tmp_path, count=2)

    def fail(_self, _rows):
        raise sqlite3.OperationalError("finalize staging disk full")

    monkeypatch.setattr(recorder._HealthStaging, "add_batch", fail)
    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        finalize_day(log, session_open=OPEN, session_close=CLOSE)
    assert not (log.dir / "events.parquet").exists()
    assert not (log.dir / "health.json").exists()
    assert not (log.dir / "manifest.json").exists()
    assert not list(log.dir.glob(".finalize-*"))


def test_prepublish_disk_full_preserves_previous_complete_dataset(tmp_path, monkeypatch):
    first = _log_with_rows(tmp_path, count=2)
    finalize_day(first, session_open=OPEN, session_close=CLOSE)
    published = {
        name: (first.dir / name).read_bytes()
        for name in ("events.parquet", "health.json", "manifest.json")
    }

    successor = RawEventLog(tmp_path, session=SESSION, run_id="resource03")
    successor.append(
        _tick(3, run_id=successor.run_id, seconds=3), now_mono=3.0
    )
    successor.close()
    durable_write = recorder.durable_atomic_write

    def fail_health_candidate(path, payload):
        if path.name == "health.json":
            raise OSError(errno.ENOSPC, "finalize candidate disk full")
        return durable_write(path, payload)

    monkeypatch.setattr(recorder, "durable_atomic_write", fail_health_candidate)
    with pytest.raises(OSError, match="disk full"):
        finalize_day(successor, session_open=OPEN, session_close=CLOSE)
    for name, expected in published.items():
        assert (successor.dir / name).read_bytes() == expected
    assert not list(successor.dir.glob(".finalize-*"))


def test_health_stage_reports_repeated_real_progress(tmp_path):
    log = _log_with_rows(tmp_path, count=10)
    stages: list[str] = []

    recorder._finalize_day(
        log,
        session_open=OPEN,
        session_close=CLOSE,
        progress=lambda stage, _rows, _segments: stages.append(stage),
    )

    assert stages.count("COMPUTING_HEALTH") >= 4
