"""Run non-destructive Windows NTFS publication and ownership fault drills.

Disk-full and kernel-level flush stalls intentionally are not synthesized on a
shared volume. Those require a dedicated marked scratch volume and remain in
the destructive production-equivalent runbook.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ib_execution.durable_io import durable_atomic_write
from ib_execution.processlock import ProcessLock, ProcessLockUnavailable


def _filesystem_name(path: Path) -> str:
    root = Path(path.anchor)
    volume_name = ctypes.create_unicode_buffer(261)
    filesystem = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()
    max_component = wintypes.DWORD()
    flags = wintypes.DWORD()
    fn = ctypes.WinDLL("kernel32", use_last_error=True).GetVolumeInformationW
    fn.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    )
    fn.restype = wintypes.BOOL
    if not fn(
        str(root),
        volume_name,
        len(volume_name),
        ctypes.byref(serial),
        ctypes.byref(max_component),
        ctypes.byref(flags),
        filesystem,
        len(filesystem),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return filesystem.value


def _holder(lock_path: Path, ready: Path) -> int:
    with ProcessLock(lock_path).acquire(note="windows-ntfs-safe-drill-holder"):
        ready.write_text("ready", encoding="utf-8")
        while True:
            time.sleep(1)


def _publisher(target: Path, ready: Path) -> int:
    seq = 0
    while True:
        payload = json.dumps({"seq": seq, "payload": "x" * 8192}).encode("utf-8")
        durable_atomic_write(target, payload)
        if seq == 0:
            ready.write_text("ready", encoding="utf-8")
        seq += 1


def _wait(path: Path, process: subprocess.Popen, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if not path.exists():
        raise RuntimeError(f"child did not become ready; exit={process.poll()}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_text_with_bounded_permission_retry(
    path: Path,
    *,
    timeout: float = 2.0,
    retry_seconds: float = 0.01,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, dict[str, int]]:
    """Reopen a killed publisher's target across a bounded sharing collision.

    Windows can report a transient sharing/access denial after the publisher
    process has exited while NTFS or a filter driver releases the final path.
    Retry only that platform boundary. Corrupt content, missing files, and a
    denial lasting through the deadline remain hard drill failures.
    """

    started = monotonic()
    deadline = started + timeout
    permission_denied_retries = 0
    while True:
        try:
            text = path.read_text(encoding="utf-8")
        except PermissionError:
            permission_denied_retries += 1
            now = monotonic()
            if now >= deadline:
                raise
            sleep(min(retry_seconds, deadline - now))
            continue
        elapsed_ms = max(0, round((monotonic() - started) * 1000))
        return text, {
            "permission_denied_retries": permission_denied_retries,
            "release_wait_ms": elapsed_ms,
        }


def _acquire_lock_after_known_holder_exit(
    path: Path,
    *,
    note: str,
    timeout: float = 2.0,
    retry_seconds: float = 0.01,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    lock_factory: Callable[[Path], ProcessLock] = ProcessLock,
) -> tuple[ProcessLock, dict[str, int]]:
    """Acquire across bounded Windows lock release after ``Popen.wait()``.

    The caller must first prove that the old holder exited.  Retrying belongs
    at that measured drill boundary: ``ProcessLock.acquire()`` remains a
    non-blocking fail-loudly primitive and never queues behind a live writer.
    """

    started = monotonic()
    deadline = started + timeout
    lock_refused_retries = 0
    retry_wait_seconds = 0.0
    while True:
        candidate = lock_factory(path)
        try:
            candidate.acquire(note=note)
        except ProcessLockUnavailable:
            lock_refused_retries += 1
            now = monotonic()
            if now >= deadline:
                raise
            delay = min(retry_seconds, deadline - now)
            sleep(delay)
            retry_wait_seconds += delay
            continue
        elapsed_ms = max(0, round((monotonic() - started) * 1000))
        return candidate, {
            "lock_refused_retries": lock_refused_retries,
            "retry_wait_ms": max(0, round(retry_wait_seconds * 1000)),
            "acquire_elapsed_ms": elapsed_ms,
        }


def _run(root: Path) -> int:
    if os.name != "nt":
        raise RuntimeError("this drill must run on Windows")
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    filesystem = _filesystem_name(root)
    if filesystem.upper() != "NTFS":
        raise RuntimeError(f"drill root must be on NTFS, observed {filesystem!r}")
    results: dict[str, str] = {}

    target = root / "durable-publication.json"
    for seq in range(100):
        durable_atomic_write(target, json.dumps({"seq": seq, "payload": "x" * 4096}).encode())
    assert json.loads(target.read_text(encoding="utf-8"))["seq"] == 99
    results["ntfs_durable_replace_readback"] = "PASS"

    lock = root / "process.lock"
    holder_ready = root / "holder.ready"
    holder = subprocess.Popen(
        [sys.executable, __file__, "--holder", str(lock), str(holder_ready)]
    )
    try:
        _wait(holder_ready, holder)
        try:
            ProcessLock(lock).acquire(note="should-be-refused")
        except ProcessLockUnavailable:
            results["two_process_single_owner"] = "PASS"
        else:
            raise AssertionError("second process unexpectedly acquired the lock")
        holder.kill()
        holder.wait(timeout=5)
        successor, lock_release = _acquire_lock_after_known_holder_exit(
            lock, note="successor-after-force-kill"
        )
        with successor:
            pass
        results["force_kill_releases_kernel_lock"] = "PASS"
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)

    publish_target = root / "force-killed-publication.json"
    publisher_ready = root / "publisher.ready"
    publisher = subprocess.Popen(
        [sys.executable, __file__, "--publisher", str(publish_target), str(publisher_ready)]
    )
    try:
        _wait(publisher_ready, publisher)
        time.sleep(0.05)
        publisher.kill()
        publisher.wait(timeout=5)
    finally:
        if publisher.poll() is None:
            publisher.kill()
            publisher.wait(timeout=5)
    published_text, publication_release = _read_text_with_bounded_permission_retry(
        publish_target
    )
    loaded = json.loads(published_text)
    assert isinstance(loaded["seq"], int) and len(loaded["payload"]) == 8192
    results["publication_force_kill_leaves_complete_generation"] = "PASS"

    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platform": "windows",
        "filesystem": filesystem,
        "root": str(root),
        "results": results,
        "artifact_sha256": {
            target.name: _sha256(target),
            publish_target.name: _sha256(publish_target),
        },
        "observations": {
            "process_lock_release": lock_release,
            "publication_path_release": publication_release,
        },
        "passed": all(value == "PASS" for value in results.values()),
        "explicitly_not_covered": ["ntfs_disk_full", "ntfs_flush_stall"],
    }
    report_path = root / "manifest.json"
    durable_atomic_write(report_path, json.dumps(report, indent=2, sort_keys=True).encode())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--holder":
        return _holder(Path(args[1]), Path(args[2]))
    if args and args[0] == "--publisher":
        return _publisher(Path(args[1]), Path(args[2]))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root")
    ns = ap.parse_args(args)
    if not ns.root:
        ap.error("--root is required")
    return _run(Path(ns.root))


if __name__ == "__main__":
    raise SystemExit(main())
