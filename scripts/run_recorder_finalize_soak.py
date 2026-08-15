"""Cross-platform, isolated peak-memory slope test for full Recorder finalization."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from ib_execution.quote_recorder import RawEventLog, RawTick, finalize_day, parquet_schema

SESSION = date(2026, 8, 14)
OPEN = datetime(2026, 8, 14, 13, 30, tzinfo=timezone.utc)
CLOSE = OPEN + timedelta(hours=6, minutes=30)
SESSION_NS = int((CLOSE - OPEN).total_seconds() * 1e9)


def _tick(index: int, events: int, run_id: str) -> RawTick:
    fraction = index / max(events - 1, 1)
    wall_ns = int(OPEN.timestamp() * 1e9) + int(SESSION_NS * fraction)
    broker = OPEN + timedelta(seconds=(CLOSE - OPEN).total_seconds() * fraction)
    event_type = "BAR_5S" if index % 50 == 0 else "ALL_LAST" if index % 6 == 0 else "BID_ASK"
    values: dict[str, object]
    if event_type == "BAR_5S":
        values = {
            "open": 599.9,
            "high": 600.2,
            "low": 599.8,
            "close": 600.0,
            "volume": 1000.0,
            "wap": 600.0,
            "trade_count": 10,
        }
    elif event_type == "ALL_LAST":
        values = {"last": 600.0, "last_size": 100.0}
    else:
        values = {
            "bid": 599.99,
            "ask": 600.01,
            "bid_size": 500.0,
            "ask_size": 700.0,
        }
    return RawTick(
        event_id=index + 1,
        recorder_run_id=run_id,
        connection_epoch=1,
        contract_id=756733,
        event_type=event_type,
        broker_timestamp=broker.isoformat(),
        local_wall_ns=wall_ns,
        local_monotonic_ns=index + 1,
        market_data_type="LIVE",
        receive_sequence=index + 1,
        **values,
    )


def _write_stage(path: Path, phase: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"phase": phase, "updated": time.time()}), encoding="utf-8")
    os.replace(tmp, path)


def _worker(root: Path, events: int, target_rate: float, report_path: Path) -> int:
    root.mkdir(parents=True, exist_ok=False)
    stage_path = root / "worker-stage.json"
    _write_stage(stage_path, "CAPTURING")
    log = RawEventLog(root, session=SESSION, batch_records=512)
    started = time.monotonic()
    for index in range(events):
        log.append(_tick(index, events, log.run_id), now_mono=time.monotonic())
        if index and index % 1_000 == 0:
            due = started + (index + 1) / target_rate
            if due > time.monotonic():
                time.sleep(due - time.monotonic())
    log.close()
    capture_seconds = time.monotonic() - started

    _write_stage(stage_path, "FINALIZING")
    finalized_at = time.monotonic()
    manifest = finalize_day(
        log,
        session_open=OPEN,
        session_close=CLOSE,
        clock_skew_samples=[0.0],
    )
    finalize_seconds = time.monotonic() - finalized_at
    _write_stage(stage_path, "DONE")

    import pyarrow.parquet as pq

    metadata = pq.ParquetFile(log.dir / "events.parquet").metadata
    report = {
        "schema_version": 1,
        "manifest_schema_version": manifest["schema_version"],
        "events": events,
        "capture_seconds": capture_seconds,
        "finalize_seconds": finalize_seconds,
        "rows": manifest["rows"],
        "parquet_rows_verified": manifest["parquet_rows_verified"],
        "row_groups": metadata.num_row_groups,
        "schema_matches_declared": metadata.schema.to_arrow_schema() == parquet_schema(),
        "health_ok": manifest["health_ok"],
        "file_hashes": manifest["files"],
        "write_accounting": manifest["write_accounting"],
        "run_id": log.run_id,
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return 0


class _ProcessMemoryCountersEx(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def _windows_private_bytes(pid: int) -> int | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessMemoryCountersEx),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    query_limited = 0x1000
    handle = kernel32.OpenProcess(query_limited, False, int(pid))
    if not handle:
        return None
    try:
        counters = _ProcessMemoryCountersEx()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), ctypes.sizeof(counters)
        ):
            return None
        return int(counters.PrivateUsage)
    finally:
        kernel32.CloseHandle(handle)


def _linux_rss_bytes(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _memory_bytes(pid: int) -> tuple[str, int | None]:
    if os.name == "nt":
        return "private_bytes", _windows_private_bytes(pid)
    return "rss_bytes", _linux_rss_bytes(pid)


def _finalize_temp_bytes(root: Path) -> int:
    total = 0
    try:
        for path in root.rglob("*"):
            if path.is_file() and any(part.startswith(".finalize-") for part in path.parts):
                total += path.stat().st_size
    except OSError:
        pass
    return total


def _run_case(
    root: Path,
    events: int,
    target_rate: float,
    poll_seconds: float,
    timeout_seconds: float,
) -> dict[str, object]:
    case_root = root / f"case-{events}-{uuid4().hex[:10]}"
    worker_report = case_root.parent / f"{case_root.name}-worker.json"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker-root",
        str(case_root),
        "--worker-events",
        str(events),
        "--target-rate",
        str(target_rate),
        "--worker-report",
        str(worker_report),
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    started = time.monotonic()
    peak_memory = 0
    peak_temp = 0
    metric = "unknown"
    while process.poll() is None:
        if time.monotonic() - started > timeout_seconds:
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
            raise TimeoutError(f"finalize soak worker exceeded {timeout_seconds}s")
        stage_path = case_root / "worker-stage.json"
        try:
            stage = json.loads(stage_path.read_text()).get("phase")
        except (OSError, json.JSONDecodeError):
            stage = None
        if stage == "FINALIZING":
            metric, current = _memory_bytes(process.pid)
            if current is not None:
                peak_memory = max(peak_memory, current)
            peak_temp = max(peak_temp, _finalize_temp_bytes(case_root))
        time.sleep(poll_seconds)
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        raise RuntimeError(
            f"finalize soak worker failed rc={process.returncode}: "
            f"stdout={stdout[-2000:]} stderr={stderr[-4000:]}"
        )
    worker = json.loads(worker_report.read_text())
    worker.update(
        {
            "memory_metric": metric,
            "peak_finalize_memory_bytes": peak_memory,
            "peak_finalize_temp_bytes": peak_temp,
            "case_root": str(case_root),
        }
    )
    return worker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="artifacts/recorder_finalize_soak")
    parser.add_argument("--small-events", type=int, default=250_000)
    parser.add_argument("--large-events", type=int, default=1_000_000)
    parser.add_argument("--max-peak-ratio", type=float, default=1.5)
    parser.add_argument("--target-rate", type=float, default=10_000.0)
    parser.add_argument("--poll-seconds", type=float, default=0.05)
    parser.add_argument("--case-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--worker-root")
    parser.add_argument("--worker-events", type=int)
    parser.add_argument("--worker-report")
    args = parser.parse_args(argv)

    if args.worker_root:
        if args.worker_events is None or args.worker_report is None:
            parser.error("worker mode requires --worker-events and --worker-report")
        return _worker(
            Path(args.worker_root),
            args.worker_events,
            args.target_rate,
            Path(args.worker_report),
        )
    if not 0 < args.small_events < args.large_events:
        parser.error("event counts must satisfy 0 < small-events < large-events")
    if args.max_peak_ratio <= 1 or args.target_rate <= 0 or args.poll_seconds <= 0:
        parser.error("ratio, target-rate and poll-seconds must be positive")

    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    small = _run_case(
        root,
        args.small_events,
        args.target_rate,
        args.poll_seconds,
        args.case_timeout_seconds,
    )
    large = _run_case(
        root,
        args.large_events,
        args.target_rate,
        args.poll_seconds,
        args.case_timeout_seconds,
    )
    if not small["peak_finalize_memory_bytes"]:
        raise RuntimeError("memory sampler observed no finalization sample")
    peak_ratio = float(large["peak_finalize_memory_bytes"]) / float(
        small["peak_finalize_memory_bytes"]
    )
    passed = bool(
        peak_ratio <= args.max_peak_ratio
        and small["rows"] == args.small_events
        and large["rows"] == args.large_events
        and small["parquet_rows_verified"] == args.small_events
        and large["parquet_rows_verified"] == args.large_events
        and small["schema_matches_declared"]
        and large["schema_matches_declared"]
        and small["manifest_schema_version"] == 3
        and large["manifest_schema_version"] == 3
    )
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "small": small,
        "large": large,
        "event_multiplier": args.large_events / args.small_events,
        "peak_memory_ratio": peak_ratio,
        "max_peak_memory_ratio": args.max_peak_ratio,
        "passed": passed,
    }
    report_path = root / "finalize-soak-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
