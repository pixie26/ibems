"""Validate the bounded v3 finalizer against one immutable Full-RTH capture.

The source raw segments and original v3 artifacts are read-only. A worker
finalizes through the production code into an independent candidate directory;
the parent samples finalize resources and deletes the candidate only after a
successful acceptance.

The production v3 finalizer performs exactly one gzip/JSON decode pass. It then
performs one sequential compressed-byte SHA-256 scan so the manifest can attest
the immutable raw inputs. This validator treats the latter as hashing, not a
second semantic/materialization pass, and reports both facts explicitly.
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import gzip
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from copy import deepcopy
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from uuid import uuid4

from ib_execution import quote_recorder as quote_recorder_module
from ib_execution.quote_recorder import finalize_day, parquet_schema

DEFAULT_EXPECTED_ROWS = 2_645_388
DEFAULT_MAX_WORKING_SET = 1 * 1024**3
DEFAULT_MAX_PRIVATE_COMMIT = int(1.5 * 1024**3)
DEFAULT_MAX_TEMP = 2 * 1024**3
DEFAULT_MAX_FINALIZE_SECONDS = 30 * 60.0
DEFAULT_MAX_HANDLE_DELTA = (
    max(2, 2 * (os.cpu_count() or 1)) if os.name == "nt" else 0
)
FINALIZE_BATCH_ROWS = 50_000


class ImmutableReplayLog:
    """Duck-typed RawEventLog view that can only read immutable source files."""

    def __init__(
        self,
        *,
        source_dir: Path,
        candidate_dir: Path,
        session,
        original_write_accounting: dict[str, Any],
    ) -> None:
        self.source_dir = source_dir
        self.dir = candidate_dir
        self.session = session
        self._write_accounting = _base_write_accounting(original_write_accounting)
        self.read_calls = 0
        self.normalized_sha256 = hashlib.sha256()
        self.rows = 0
        self.by_stream: Counter[str] = Counter()

    def close(self) -> None:
        return None

    def write_stats(self) -> dict[str, Any]:
        return deepcopy(self._write_accounting)

    def segments(self) -> list[Path]:
        return sorted(
            list(self.source_dir.glob("segment-*.jsonl.gz"))
            + list(self.source_dir.glob("crashed-*.jsonl.gz"))
        )

    def read_all(
        self,
        segments: Sequence[Path] | None = None,
        *,
        integrity_report: list[dict[str, Any]] | None = None,
    ) -> Iterator[dict[str, Any]]:
        self.read_calls += 1
        if self.read_calls != 1:
            raise RuntimeError(
                f"production finalizer requested a second gzip/JSON pass ({self.read_calls})"
            )
        for segment in self.segments() if segments is None else segments:
            rows = 0
            decompressed = 0
            trailing_partial = 0
            complete = True
            error: str | None = None
            yield_rows = True
            try:
                with gzip.open(segment, "rb") as handle:
                    for line in handle:
                        decompressed += len(line)
                        if line.endswith(b"\n"):
                            rows += 1
                        else:
                            trailing_partial = len(line)
                        if not yield_rows:
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            yield_rows = False
                            continue
                        self.rows += 1
                        self.by_stream[str(row.get("event_type") or "UNKNOWN")] += 1
                        self.normalized_sha256.update(_canonical_row(row))
                        yield row
            except (EOFError, OSError, gzip.BadGzipFile) as exc:
                complete = False
                error = f"{type(exc).__name__}: {exc}"
            finally:
                if integrity_report is not None:
                    integrity_report.append(
                        {
                            "segment": segment.name,
                            "salvaged": segment.name.startswith("crashed-"),
                            "compressed_bytes": segment.stat().st_size,
                            "decompressed_bytes": decompressed,
                            "readable_rows": rows,
                            "trailing_partial_bytes": trailing_partial,
                            "gzip_stream_complete": complete,
                            "read_error": error,
                        }
                    )


def _base_write_accounting(accounting: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "accepted",
        "persisted",
        "dropped",
        "enqueued_count",
        "persisted_count",
        "dropped_count",
        "queue_capacity",
        "queue_high_water",
        "max_writer_lag_ms",
        "fsync_count",
        "fsync_latency_ms",
        "accepted_by_stream",
        "persisted_by_stream",
        "accepted_by_run_id",
        "persisted_by_run_id",
        "writer_error",
    )
    missing = [key for key in keys if key not in accounting]
    if missing:
        raise ValueError(f"original v3 write_accounting is missing {missing}")
    return {key: deepcopy(accounting[key]) for key in keys}


def _canonical_row(row: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            row,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=True,
        )
        + "\n"
    ).encode("utf-8")


def _parquet_semantics(path: Path) -> dict[str, Any]:
    import pyarrow.parquet as pq

    digest = hashlib.sha256()
    by_stream: Counter[str] = Counter()
    rows = 0
    parquet = pq.ParquetFile(path)
    try:
        schema_matches = parquet.schema_arrow == parquet_schema()
        for batch in parquet.iter_batches(batch_size=FINALIZE_BATCH_ROWS):
            for row in batch.to_pylist():
                rows += 1
                by_stream[str(row.get("event_type") or "UNKNOWN")] += 1
                digest.update(_canonical_row(row))
    finally:
        parquet.close()
    return {
        "rows": rows,
        "by_stream": dict(sorted(by_stream.items())),
        "normalized_sha256": digest.hexdigest(),
        "schema_matches_declared": schema_matches,
    }


def _source_metadata(paths: Iterable[Path]) -> dict[str, dict[str, int]]:
    return {
        path.name: {"size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns}
        for path in paths
    }


def _semantic_health(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(payload)
    normalized.pop("clock_skew", None)
    normalized.pop("file_hashes", None)
    return normalized


def _semantic_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(payload)
    files = normalized.pop("files", {}) or {}
    normalized["raw_files"] = {
        name: value
        for name, value in sorted(files.items())
        if name.startswith("segment-") or name.startswith("crashed-")
    }
    return normalized


def _clock_replay_samples(original_health: dict[str, Any]) -> list[float]:
    value = (original_health.get("clock_skew") or {}).get("median_seconds")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return []
    return [number] if math.isfinite(number) else []


def _handle_count() -> int | None:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessHandleCount.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetProcessHandleCount.restype = wintypes.BOOL
        count = wintypes.DWORD()
        if not kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count)):
            return None
        return int(count.value)
    try:
        return len(list(Path("/proc/self/fd").iterdir()))
    except OSError:
        return None


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


def _thread_count() -> int | None:
    if os.name != "nt":
        try:
            return len(list(Path("/proc/self/task").iterdir()))
        except OSError:
            return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        return None
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel32.Thread32First(snapshot, ctypes.byref(entry)):
            return None
        pid = os.getpid()
        count = 0
        while True:
            if int(entry.th32OwnerProcessID) == pid:
                count += 1
            if not kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                break
        return count
    finally:
        kernel32.CloseHandle(snapshot)


def _exclusive_read_probe(paths: Sequence[Path]) -> dict[str, Any]:
    """Prove that the live worker retains no file handle on replay files."""
    if os.name != "nt":
        return {"applicable": False, "passed": True, "checked": 0, "errors": {}}
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    invalid = wintypes.HANDLE(-1).value
    errors: dict[str, int] = {}
    for path in paths:
        handle = kernel32.CreateFileW(
            str(path.resolve(strict=True)),
            0x80000000,
            0,
            None,
            3,
            0x00000080,
            None,
        )
        if handle == invalid:
            errors[str(path)] = int(ctypes.get_last_error())
            continue
        kernel32.CloseHandle(handle)
    return {
        "applicable": True,
        "passed": not errors,
        "checked": len(paths),
        "errors": errors,
    }


def _write_stage(path: Path, phase: str) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"phase": phase, "updated": time.time()}), encoding="utf-8"
    )
    os.replace(temporary, path)


def _warm_finalize_runtime(candidate: Path) -> None:
    """Load lazy Arrow/codecs before taking the leak-check baseline.

    Windows counts DLL/runtime initialization handles as a cold-start delta.
    They live until this short-lived worker exits and are not per-finalize file
    leaks.  Exercise the same Parquet+ZSTD open/write/read/close path with one
    tiny disposable file so the measured delta starts from a stable runtime.
    The warm-up is included in the time and memory envelope and never reads a
    source segment.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = candidate / ".finalize-runtime-warmup.parquet"
    schema = pa.schema([("warmup", pa.int64())])
    writer = pq.ParquetWriter(path, schema, compression="zstd")
    table = pa.Table.from_pylist([{"warmup": 1}], schema=schema)
    try:
        writer.write_table(table)
    finally:
        writer.close()
    parquet = pq.ParquetFile(path)
    try:
        readback = parquet.read()
        if readback.num_rows != 1:
            raise RuntimeError("Parquet runtime warm-up readback mismatch")
    finally:
        parquet.close()
    del readback, parquet, table, writer
    path.unlink()
    gc.collect()


def _worker(args: argparse.Namespace) -> int:
    source_dir = args.raw_dir.resolve(strict=True)
    candidate = args.candidate_dir.resolve(strict=False)
    candidate.mkdir(parents=True, exist_ok=False)
    stage_path = candidate / "worker-stage.json"

    original_manifest = json.loads(args.original_manifest.read_text(encoding="utf-8"))
    original_health = json.loads(args.original_health.read_text(encoding="utf-8"))
    session = datetime.fromisoformat(str(original_manifest["session"])).date()
    source_segments = sorted(
        list(source_dir.glob("segment-*.jsonl.gz"))
        + list(source_dir.glob("crashed-*.jsonl.gz"))
    )
    if not source_segments:
        raise FileNotFoundError(f"no immutable raw segments under {source_dir}")
    source_before = _source_metadata(source_segments)
    source_resolved = {path.resolve(strict=True): path.name for path in source_segments}
    compressed_hash_scans: Counter[str] = Counter()
    original_sha256 = quote_recorder_module._sha256

    def counted_sha256(path: Path) -> str:
        resolved = Path(path).resolve(strict=True)
        if resolved in source_resolved:
            compressed_hash_scans[source_resolved[resolved]] += 1
        return original_sha256(path)

    replay = ImmutableReplayLog(
        source_dir=source_dir,
        candidate_dir=candidate,
        session=session,
        original_write_accounting=original_manifest["write_accounting"],
    )
    accounting = original_manifest["write_accounting"]
    cold_handles_before = _handle_count()
    cold_threads_before = _thread_count()
    _write_stage(stage_path, "FINALIZING")
    started = time.monotonic()
    _warm_finalize_runtime(candidate)
    handles_before = _handle_count()
    threads_before = _thread_count()
    quote_recorder_module._sha256 = counted_sha256
    try:
        candidate_manifest = finalize_day(
            replay,
            session_open=args.session_open,
            session_close=args.session_close,
            clock_skew_samples=_clock_replay_samples(original_health),
            handler_counts=accounting.get("handled_by_stream") or None,
            selected_counts=accounting.get("selected_by_stream") or None,
            filtered_counts=accounting.get("filtered_by_stream") or None,
            capture_policy=original_manifest.get("capture_policy"),
            liveness=original_manifest.get("liveness"),
        )
    finally:
        quote_recorder_module._sha256 = original_sha256
    finalize_seconds = time.monotonic() - started
    gc.collect()
    file_handle_probe = _exclusive_read_probe(
        [
            *source_segments,
            candidate / "events.parquet",
            candidate / "health.json",
            candidate / "manifest.json",
        ]
    )
    handles_after = _handle_count()
    threads_after = _thread_count()
    _write_stage(stage_path, "VERIFYING_EQUIVALENCE")

    candidate_health = json.loads((candidate / "health.json").read_text(encoding="utf-8"))
    candidate_parquet = _parquet_semantics(candidate / "events.parquet")
    original_parquet = (
        _parquet_semantics(args.original_parquet)
        if args.original_parquet is not None and args.original_parquet.exists()
        else None
    )
    source_after = _source_metadata(source_segments)

    raw_semantics = {
        "rows": replay.rows,
        "by_stream": dict(sorted(replay.by_stream.items())),
        "normalized_sha256": replay.normalized_sha256.hexdigest(),
    }
    expected_streams = {
        name: int((details or {}).get("rows", 0))
        for name, details in (original_health.get("streams") or {}).items()
    }
    raw_hashes_original = {
        name: value
        for name, value in (original_manifest.get("files") or {}).items()
        if name.startswith("segment-") or name.startswith("crashed-")
    }
    raw_hashes_candidate = {
        name: value
        for name, value in (candidate_manifest.get("files") or {}).items()
        if name.startswith("segment-") or name.startswith("crashed-")
    }

    checks = {
        "expected_rows": replay.rows == args.expected_rows,
        "candidate_rows": candidate_parquet["rows"] == replay.rows,
        "candidate_manifest_rows": candidate_manifest["rows"] == replay.rows,
        "candidate_parquet_rows_verified": candidate_manifest["parquet_rows_verified"] == replay.rows,
        "declared_schema": bool(candidate_parquet["schema_matches_declared"]),
        "stream_counts_raw_vs_candidate": raw_semantics["by_stream"] == candidate_parquet["by_stream"],
        "stream_counts_match_original_health": all(
            raw_semantics["by_stream"].get(name, 0) == rows
            for name, rows in expected_streams.items()
        ),
        "normalized_content_and_order": raw_semantics["normalized_sha256"]
        == candidate_parquet["normalized_sha256"],
        "v3_health_semantics": _semantic_health(original_health)
        == _semantic_health(candidate_health),
        "v3_manifest_semantics": _semantic_manifest(original_manifest)
        == _semantic_manifest(candidate_manifest),
        "raw_hashes_unchanged": raw_hashes_original == raw_hashes_candidate,
        "source_metadata_unchanged": source_before == source_after,
        "single_gzip_json_decode_pass": replay.read_calls == 1,
        "single_compressed_sha256_scan_per_segment": (
            set(compressed_hash_scans) == set(source_resolved.values())
            and all(count == 1 for count in compressed_hash_scans.values())
        ),
    }
    if original_parquet is not None:
        checks["original_parquet_semantics"] = (
            original_parquet["rows"] == candidate_parquet["rows"]
            and original_parquet["by_stream"] == candidate_parquet["by_stream"]
            and original_parquet["normalized_sha256"]
            == candidate_parquet["normalized_sha256"]
            and original_parquet["schema_matches_declared"]
        )

    report = {
        "schema_version": 1,
        "source_dir": str(source_dir),
        "candidate_dir": str(candidate),
        "source_segment_count": len(source_segments),
        "source_metadata_before": source_before,
        "source_metadata_after": source_after,
        "raw_semantics": raw_semantics,
        "candidate_parquet": candidate_parquet,
        "original_parquet": original_parquet,
        "finalize_seconds": finalize_seconds,
        "handles_before_runtime_warmup": cold_handles_before,
        "runtime_warmup_handle_delta": (
            None
            if cold_handles_before is None or handles_before is None
            else handles_before - cold_handles_before
        ),
        "threads_before_runtime_warmup": cold_threads_before,
        "threads_before_finalize": threads_before,
        "threads_after_finalize": threads_after,
        "thread_delta": (
            None
            if threads_before is None or threads_after is None
            else threads_after - threads_before
        ),
        "runtime_thread_growth": (
            None
            if cold_threads_before is None or threads_after is None
            else threads_after - cold_threads_before
        ),
        "runtime_thread_growth_limit": 2 * max(0, (os.cpu_count() or 1) - 1),
        "file_handle_exclusive_read_probe": file_handle_probe,
        "handles_before_finalize": handles_before,
        "handles_after_finalize": handles_after,
        "handle_delta": (
            None
            if handles_before is None or handles_after is None
            else handles_after - handles_before
        ),
        "raw_access": {
            "gzip_json_decode_passes": replay.read_calls,
            "compressed_sha256_scans_by_segment": dict(sorted(compressed_hash_scans.items())),
            "compressed_sha256_scan_passes": (
                1
                if compressed_hash_scans
                and all(count == 1 for count in compressed_hash_scans.values())
                else None
            ),
            "compressed_sha256_scan_count_is_measured": True,
            "note": (
                "The current v3 finalizer decodes/materializes raw exactly once, then "
                "performs one sequential compressed-byte SHA-256 scan for manifest attestation."
            ),
        },
        "clock_skew_replay": {
            "exact_sample_vector_available": False,
            "original_summary": original_health.get("clock_skew"),
            "replayed_with_original_median_for_verdict_equivalence": True,
        },
        "checks": checks,
        "semantic_passed": all(checks.values()),
    }
    args.worker_report.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=True) + "\n",
        encoding="utf-8",
    )
    _write_stage(stage_path, "DONE")
    return 0 if report["semantic_passed"] else 2


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


def _windows_memory(pid: int) -> tuple[int | None, int | None]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    query_limited = 0x1000
    handle = kernel32.OpenProcess(query_limited, False, int(pid))
    if not handle:
        return None, None
    try:
        counters = _ProcessMemoryCountersEx()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), ctypes.sizeof(counters)
        ):
            return None, None
        return int(counters.WorkingSetSize), int(counters.PrivateUsage)
    finally:
        kernel32.CloseHandle(handle)


def _linux_memory(pid: int) -> tuple[int | None, int | None]:
    rss = None
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        pass
    return rss, None


def _memory(pid: int) -> tuple[int | None, int | None]:
    return _windows_memory(pid) if os.name == "nt" else _linux_memory(pid)


def _finalize_temp_bytes(candidate: Path) -> int:
    total = 0
    try:
        for path in candidate.rglob("*"):
            if path.is_file() and any(part.startswith(".finalize-") for part in path.parts):
                total += path.stat().st_size
    except OSError:
        pass
    return total


def _resource_checks(
    *,
    worker: dict[str, Any],
    peak_working_set: int,
    peak_private_commit: int,
    peak_temp: int,
    observed_finalize_sample: bool,
    args: argparse.Namespace,
) -> dict[str, bool]:
    runtime_thread_growth = worker.get("runtime_thread_growth")
    runtime_thread_limit = worker.get("runtime_thread_growth_limit")
    file_probe = worker.get("file_handle_exclusive_read_probe") or {}
    checks: dict[str, bool] = {
        "finalize_under_time_limit": float(worker["finalize_seconds"])
        <= args.max_finalize_seconds,
        "temporary_space_under_limit": peak_temp <= args.max_temp_bytes,
        "working_set_sample_observed": observed_finalize_sample and peak_working_set > 0,
        "working_set_under_limit": 0 < peak_working_set <= args.max_working_set_bytes,
        "handle_count_sample_observed": worker.get("handle_delta") is not None,
        "handle_delta_under_limit": (
            worker.get("handle_delta") is not None
            and int(worker["handle_delta"]) <= args.max_handle_delta
        ),
        "runtime_thread_count_sample_observed": (
            runtime_thread_growth is not None and runtime_thread_limit is not None
        ),
        "runtime_thread_growth_bounded": (
            runtime_thread_growth is not None
            and runtime_thread_limit is not None
            and 0 <= int(runtime_thread_growth) <= int(runtime_thread_limit)
        ),
    }
    if os.name == "nt":
        checks["exclusive_file_handle_probe_passed"] = (
            file_probe.get("applicable") is True and file_probe.get("passed") is True
        )
        checks["private_commit_sample_observed"] = peak_private_commit > 0
        checks["private_commit_under_limit"] = (
            0 < peak_private_commit <= args.max_private_commit_bytes
        )
    return checks


def _run_parent(args: argparse.Namespace) -> int:
    raw_dir = args.raw_dir.resolve(strict=True)
    work_root = args.work_root.resolve(strict=False)
    work_root.mkdir(parents=True, exist_ok=True)
    if work_root == raw_dir or raw_dir in work_root.parents:
        raise ValueError("work-root must be independent of the immutable raw directory")

    candidate = work_root / f"candidate-{uuid4().hex[:12]}"
    worker_report = work_root / f"{candidate.name}-worker.json"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--raw-dir",
        str(raw_dir),
        "--original-manifest",
        str(args.original_manifest.resolve(strict=True)),
        "--original-health",
        str(args.original_health.resolve(strict=True)),
        "--session-open",
        args.session_open.isoformat(),
        "--session-close",
        args.session_close.isoformat(),
        "--expected-rows",
        str(args.expected_rows),
        "--candidate-dir",
        str(candidate),
        "--worker-report",
        str(worker_report),
    ]
    if args.original_parquet is not None:
        command.extend(["--original-parquet", str(args.original_parquet.resolve(strict=True))])

    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    started = time.monotonic()
    peak_working_set = 0
    peak_private_commit = 0
    peak_temp = 0
    observed_finalize_sample = False
    timeout_seconds = args.max_finalize_seconds + 600.0
    while process.poll() is None:
        if time.monotonic() - started > timeout_seconds:
            process.kill()
            process.wait()
            if not args.retain_failed_candidate:
                shutil.rmtree(candidate, ignore_errors=True)
            raise TimeoutError(f"replay worker exceeded hard timeout {timeout_seconds:.0f}s")
        try:
            phase = json.loads((candidate / "worker-stage.json").read_text())["phase"]
        except (OSError, KeyError, json.JSONDecodeError):
            phase = None
        if phase == "FINALIZING":
            working_set, private_commit = _memory(process.pid)
            if working_set is not None:
                peak_working_set = max(peak_working_set, working_set)
            if private_commit is not None:
                peak_private_commit = max(peak_private_commit, private_commit)
            peak_temp = max(peak_temp, _finalize_temp_bytes(candidate))
            observed_finalize_sample = True
        time.sleep(args.poll_seconds)

    stdout, stderr = process.communicate()
    if not worker_report.exists():
        if not args.retain_failed_candidate:
            shutil.rmtree(candidate, ignore_errors=True)
        raise RuntimeError(
            f"replay worker did not produce a report; rc={process.returncode}; "
            f"stdout={stdout[-2000:]} stderr={stderr[-6000:]}"
        )
    worker = json.loads(worker_report.read_text(encoding="utf-8"))

    resource_checks = _resource_checks(
        worker=worker,
        peak_working_set=peak_working_set,
        peak_private_commit=peak_private_commit,
        peak_temp=peak_temp,
        observed_finalize_sample=observed_finalize_sample,
        args=args,
    )

    report = {
        "schema_version": 1,
        "generated_at_utc": datetime.now().astimezone().isoformat(),
        "worker_returncode": process.returncode,
        "semantic": worker,
        "resources": {
            "peak_working_set_bytes": peak_working_set,
            "peak_private_commit_bytes": peak_private_commit if os.name == "nt" else None,
            "peak_finalize_temp_bytes": peak_temp,
            "max_working_set_bytes": args.max_working_set_bytes,
            "max_private_commit_bytes": args.max_private_commit_bytes,
            "max_temp_bytes": args.max_temp_bytes,
            "max_finalize_seconds": args.max_finalize_seconds,
            "max_handle_delta": args.max_handle_delta,
            "checks": resource_checks,
        },
    }
    passed = bool(
        process.returncode == 0
        and worker.get("semantic_passed")
        and all(resource_checks.values())
    )

    cleanup_error = None
    cleanup_requested = passed or not args.retain_failed_candidate
    if cleanup_requested:
        try:
            shutil.rmtree(candidate)
        except OSError as exc:
            cleanup_error = f"{type(exc).__name__}: {exc}"
            passed = False
    report["candidate_cleanup"] = {
        "cleanup_requested": cleanup_requested,
        "deleted": cleanup_error is None and not candidate.exists(),
        "error": cleanup_error,
        "candidate_retained_for_failed_diagnosis": (
            not passed and args.retain_failed_candidate and candidate.exists()
        ),
        "failure_evidence_is_embedded_in_report": not passed,
    }
    report["passed"] = passed
    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=True) + "\n",
        encoding="utf-8",
    )
    worker_report.unlink(missing_ok=True)
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=True))
    return 0 if passed else 2


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include UTC offset")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", required=True, type=Path)
    parser.add_argument("--original-manifest", required=True, type=Path)
    parser.add_argument("--original-health", required=True, type=Path)
    parser.add_argument("--original-parquet", type=Path)
    parser.add_argument("--session-open", required=True, type=_aware)
    parser.add_argument("--session-close", required=True, type=_aware)
    parser.add_argument("--expected-rows", type=int, default=DEFAULT_EXPECTED_ROWS)
    parser.add_argument("--work-root", type=Path, default=Path("artifacts/full_rth_finalize_replay"))
    parser.add_argument(
        "--report-out",
        type=Path,
        default=Path("artifacts/full_rth_finalize_replay/finalize-replay-report.json"),
    )
    parser.add_argument("--max-working-set-bytes", type=int, default=DEFAULT_MAX_WORKING_SET)
    parser.add_argument("--max-private-commit-bytes", type=int, default=DEFAULT_MAX_PRIVATE_COMMIT)
    parser.add_argument("--max-temp-bytes", type=int, default=DEFAULT_MAX_TEMP)
    parser.add_argument("--max-finalize-seconds", type=float, default=DEFAULT_MAX_FINALIZE_SECONDS)
    parser.add_argument(
        "--max-handle-delta", type=int, default=DEFAULT_MAX_HANDLE_DELTA
    )
    parser.add_argument("--poll-seconds", type=float, default=0.05)
    parser.add_argument(
        "--retain-failed-candidate",
        action="store_true",
        help="retain rebuildable failed candidate files instead of only the small JSON report",
    )

    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--candidate-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-report", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.session_close <= args.session_open:
        parser.error("session-close must be after session-open")
    if args.expected_rows <= 0:
        parser.error("expected-rows must be positive")
    if min(
        args.max_working_set_bytes,
        args.max_private_commit_bytes,
        args.max_temp_bytes,
    ) <= 0 or args.max_finalize_seconds <= 0 or args.poll_seconds <= 0:
        parser.error("resource limits and polling interval must be positive")
    if args.max_handle_delta < 0:
        parser.error("max-handle-delta must be nonnegative")
    if args.worker and (args.candidate_dir is None or args.worker_report is None):
        parser.error("worker mode requires --candidate-dir and --worker-report")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return _worker(args) if args.worker else _run_parent(args)
    except (FileNotFoundError, OSError, RuntimeError, ValueError, TimeoutError) as exc:
        print(f"finalize replay refused/failed: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
