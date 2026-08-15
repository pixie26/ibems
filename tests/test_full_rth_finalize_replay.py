from __future__ import annotations

import importlib.util
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ib_execution.quote_recorder import RawEventLog, RawTick, finalize_day

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate_full_rth_finalize_replay.py"
SESSION = date(2026, 8, 14)
OPEN = datetime(2026, 8, 14, 13, 30, tzinfo=timezone.utc)
CLOSE = OPEN + timedelta(seconds=30)


def _load_replay():
    spec = importlib.util.spec_from_file_location("full_rth_finalize_replay", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load replay validator: {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tick(index: int, stream: str, seconds: float, run_id: str) -> RawTick:
    broker = OPEN + timedelta(seconds=seconds)
    wall_ns = int(broker.timestamp() * 1e9)
    values = {}
    if stream == "BID_ASK":
        values = {"bid": 599.99, "ask": 600.01, "bid_size": 10.0, "ask_size": 12.0}
    elif stream == "ALL_LAST":
        values = {"last": 600.0, "last_size": 1.0}
    elif stream == "BAR_5S":
        values = {
            "open": 600.0,
            "high": 600.1,
            "low": 599.9,
            "close": 600.0,
            "volume": 10.0,
            "wap": 600.0,
            "trade_count": 1,
        }
    return RawTick(
        event_id=index,
        recorder_run_id=run_id,
        connection_epoch=1,
        contract_id=756733,
        event_type=stream,
        broker_timestamp=broker.isoformat(),
        local_wall_ns=wall_ns,
        local_monotonic_ns=index,
        market_data_type="LIVE",
        receive_sequence=index,
        **values,
    )


def test_replay_uses_production_finalize_without_touching_source_raw(tmp_path: Path) -> None:
    replay_module = _load_replay()
    source_root = tmp_path / "source"
    log = RawEventLog(source_root, session=SESSION, batch_records=16)
    index = 0
    for seconds in (0, 5, 10, 15, 20, 25, 30):
        index += 1
        log.append(_tick(index, "BID_ASK", seconds, log.run_id), now_mono=float(index))
    for seconds in (0, 30):
        index += 1
        log.append(_tick(index, "ALL_LAST", seconds, log.run_id), now_mono=float(index))
    for seconds in (5, 10, 15, 20, 25):
        index += 1
        log.append(_tick(index, "BAR_5S", seconds, log.run_id), now_mono=float(index))

    original_manifest = finalize_day(
        log,
        session_open=OPEN,
        session_close=CLOSE,
        clock_skew_samples=[0.0],
    )
    source_dir = log.dir
    source_segments = sorted(source_dir.glob("segment-*.jsonl.gz"))
    before = {path.name: (path.stat().st_size, path.stat().st_mtime_ns) for path in source_segments}

    candidate = tmp_path / "candidate"
    report_path = tmp_path / "worker-report.json"
    args = SimpleNamespace(
        raw_dir=source_dir,
        candidate_dir=candidate,
        original_manifest=source_dir / "manifest.json",
        original_health=source_dir / "health.json",
        original_parquet=source_dir / "events.parquet",
        session_open=OPEN,
        session_close=CLOSE,
        expected_rows=original_manifest["rows"],
        worker_report=report_path,
    )

    assert replay_module._worker(args) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    after = {path.name: (path.stat().st_size, path.stat().st_mtime_ns) for path in source_segments}

    assert report["semantic_passed"] is True
    assert all(report["checks"].values())
    assert report["raw_access"]["gzip_json_decode_passes"] == 1
    assert report["raw_access"]["compressed_sha256_scan_passes"] == 1
    assert report["raw_access"]["compressed_sha256_scan_count_is_measured"] is True
    assert report["checks"]["single_compressed_sha256_scan_per_segment"] is True
    assert report["clock_skew_replay"]["exact_sample_vector_available"] is False
    assert isinstance(report["runtime_warmup_handle_delta"], int)
    assert report["handle_delta"] == 0
    assert report["runtime_thread_growth"] <= report["runtime_thread_growth_limit"]
    if os.name == "nt":
        assert report["file_handle_exclusive_read_probe"] == {
            "applicable": True,
            "passed": True,
            "checked": len(source_segments) + 3,
            "errors": {},
        }
    assert before == after
    assert (candidate / "events.parquet").exists()
    assert (candidate / "health.json").exists()
    assert (candidate / "manifest.json").exists()


def test_replay_resource_acceptance_fails_on_handle_leak() -> None:
    replay_module = _load_replay()
    args = SimpleNamespace(
        max_finalize_seconds=1800.0,
        max_temp_bytes=2 * 1024**3,
        max_working_set_bytes=1024**3,
        max_private_commit_bytes=int(1.5 * 1024**3),
        max_handle_delta=0,
    )

    checks = replay_module._resource_checks(
        worker={"finalize_seconds": 10.0, "handle_delta": 1},
        peak_working_set=128 * 1024**2,
        peak_private_commit=256 * 1024**2,
        peak_temp=64 * 1024**2,
        observed_finalize_sample=True,
        args=args,
    )

    assert checks["handle_count_sample_observed"] is True
    assert checks["handle_delta_under_limit"] is False


@pytest.mark.skipif(os.name != "nt", reason="Windows process handle API only")
def test_windows_handle_count_is_observable() -> None:
    replay_module = _load_replay()

    count = replay_module._handle_count()

    assert isinstance(count, int)
    assert count > 0
