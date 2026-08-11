from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ib_execution.quote_recorder import (
    DEFAULT_GAP_THRESHOLDS,
    RawEventLog,
    RawTick,
    RecorderWriteFailed,
    compute_health,
)


SESSION = date(2026, 8, 11)


def _tick(i: int, event_type: str = "BID_ASK", ns: int = 1) -> RawTick:
    values = {
        "bid": 600.0,
        "ask": 600.01,
        "last": 600.0,
        "last_size": 1.0,
        "open": 600.0,
        "high": 600.0,
        "low": 600.0,
        "close": 600.0,
        "volume": 1.0,
        "wap": 600.0,
        "trade_count": 1,
    }
    if event_type == "BID_ASK":
        values.update(last=None, last_size=None, open=None, high=None, low=None,
                      close=None, volume=None, wap=None, trade_count=None)
    elif event_type == "ALL_LAST":
        values.update(bid=None, ask=None, open=None, high=None, low=None,
                      close=None, volume=None, wap=None, trade_count=None)
    else:
        values.update(bid=None, ask=None, last=None, last_size=None)
    return RawTick(
        event_id=i,
        recorder_run_id="resilience",
        connection_epoch=1,
        contract_id=756733,
        event_type=event_type,
        broker_timestamp="2026-08-11T13:30:00+00:00",
        local_wall_ns=ns,
        local_monotonic_ns=ns,
        market_data_type="LIVE",
        receive_sequence=i,
        **values,
    )


def test_close_timeout_is_fatal_and_does_not_release_a_live_writer_lock(tmp_path, monkeypatch):
    log = RawEventLog(
        tmp_path,
        session=SESSION,
        batch_records=1,
        close_timeout_seconds=0.05,
    )
    entered = threading.Event()
    release = threading.Event()
    original = log._write_batch

    def blocked(batch):
        entered.set()
        release.wait(2)
        original(batch)

    monkeypatch.setattr(log, "_write_batch", blocked)
    log.append(_tick(1), now_mono=1.0)
    assert entered.wait(1)
    with pytest.raises(RecorderWriteFailed, match="did not stop"):
        log.close()
    assert log._lock is not None and log._lock.held

    release.set()
    log._thread.join(2)
    with pytest.raises(RecorderWriteFailed):
        log.close()
    successor = RawEventLog(tmp_path, session=SESSION)
    successor.close()


def test_force_killed_writer_recovers_its_fsynced_gzip_prefix(tmp_path):
    ready = tmp_path / "ready.txt"
    helper = Path(__file__).parent / "helpers" / "recorder_crash_holder.py"
    env = os.environ.copy()
    source = str(Path(__file__).parents[1] / "src")
    env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.Popen(
        [sys.executable, str(helper), str(tmp_path), str(ready), SESSION.isoformat()],
        env=env,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "crash holder did not publish readiness"
        process.kill()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    recovered = RawEventLog(tmp_path, session=SESSION)
    rows = list(recovered.read_all())
    recovered.close()
    assert len(rows) == 1
    assert rows[0]["event_type"] == "BID_ASK"
    assert list((tmp_path / SESSION.isoformat()).glob("crashed-*.jsonl.gz"))


class _MemoryRows:
    def __init__(self):
        self.rows: list[dict] = []
        self.session = SESSION

    def read_all(self):
        yield from self.rows


def test_sixty_second_virtual_session_exercises_all_gap_boundaries():
    start = datetime(2026, 8, 11, 13, 30, tzinfo=timezone.utc)
    end = start + timedelta(seconds=60)
    log = _MemoryRows()
    event_id = 0
    for stream in ("BID_ASK", "ALL_LAST", "BAR_5S"):
        step = DEFAULT_GAP_THRESHOLDS[stream]
        for offset in range(0, 61, int(step)):
            event_id += 1
            tick = _tick(
                event_id,
                stream,
                int((start.timestamp() + offset) * 1e9),
            )
            log.rows.append(asdict(tick))
    health = compute_health(
        log,
        session_open=start,
        session_close=end,
        clock_skew_samples=[0.0],
    )
    assert health.ok(), health.problems()


def test_a_salvaged_segment_stops_the_day_reporting_itself_as_clean(tmp_path):
    """Prefix salvage recovers real rows -- and hides how many it did not.

    A ``crashed-`` segment ends wherever the kernel happened to stop the
    writer, so no count taken from it is complete. The recovery is still
    worth doing, but before this the only trace was a filename inside
    ``file_hashes``: ``health_ok`` stayed true and ``problems`` said nothing,
    so a truncated day was indistinguishable from a whole one in the
    manifest a backtest would read months later.
    """
    ready = tmp_path / "ready.txt"
    helper = Path(__file__).parent / "helpers" / "recorder_crash_holder.py"
    env = os.environ.copy()
    source = str(Path(__file__).parents[1] / "src")
    env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.Popen(
        [sys.executable, str(helper), str(tmp_path), str(ready), SESSION.isoformat()],
        env=env,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "crash holder did not publish readiness"
        process.kill()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    recovered = RawEventLog(tmp_path, session=SESSION)
    try:
        health = compute_health(
            recovered,
            session_open=datetime(2026, 8, 11, 13, 30, tzinfo=timezone.utc),
            session_close=datetime(2026, 8, 11, 20, 0, tzinfo=timezone.utc),
            clock_skew_samples=[0.0],
        )
    finally:
        recovered.close()

    assert health.salvaged_segments, "the crashed segment must be named, not just hashed"
    assert all(name.startswith("crashed-") for name in health.salvaged_segments)
    assert health.ok() is False
    assert any("capture truncated" in problem for problem in health.problems())
    assert health.as_dict()["salvaged_segments"] == sorted(health.salvaged_segments)


def test_a_clean_session_reports_no_salvage(tmp_path):
    """The mirror: the disclosure must not fire on an ordinary session."""
    log = RawEventLog(tmp_path, session=SESSION)
    log.append(_tick(1), now_mono=1.0)
    log.close()

    reopened = RawEventLog(tmp_path, session=SESSION)
    try:
        health = compute_health(
            reopened,
            session_open=datetime(2026, 8, 11, 13, 30, tzinfo=timezone.utc),
            session_close=datetime(2026, 8, 11, 20, 0, tzinfo=timezone.utc),
            clock_skew_samples=[0.0],
        )
    finally:
        reopened.close()

    assert health.salvaged_segments == []
    assert not any("capture truncated" in problem for problem in health.problems())
