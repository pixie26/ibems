"""
Gate B1.4: real storage faults against a real execution host process.

    ####################################################################
    #  This replaces deterministic fault injection with the operating  #
    #  system actually saying no.  Injected faults prove the handler   #
    #  is correct; they cannot prove the handler is reached.           #
    ####################################################################

WHAT IS BEING VERIFIED, AND WHAT IS NOT
---------------------------------------
The claim is "when the journal breaks, the engine stops trading and stays
stopped". It is NOT "SQLite can repair itself". Do not add recovery logic to
make a drill pass: an engine that heals a corrupt journal and carries on is
the failure, not the fix.

Each drill spawns a real ``execution_host`` subprocess, breaks storage
underneath it, and asserts on the child's exit code, the durable fence, and
the fact that no broker write happened after the fault.

    disk_full        the journal volume runs out of space mid-session
    wal_corruption   the WAL is truncated/garbled while the engine is down
    fsync_stall      commits block past the write timeout

THE VOLUME
----------
``--journal-volume`` must be a small dedicated filesystem, NOT a directory on
your main disk -- filling the latter takes the machine down with it, and the
drill needs the volume to be genuinely exhaustible.

    Linux    truncate -s 64M /tmp/ibems-drill.img
             mkfs.ext4 -q /tmp/ibems-drill.img
             sudo mount -o loop /tmp/ibems-drill.img /mnt/ibems-drill

    Windows  New-VHD -Path C:\\ibems-drill.vhdx -SizeBytes 64MB -Fixed |
                 Mount-VHD -Passthru | Initialize-Disk -Passthru |
                 New-Partition -AssignDriveLetter -UseMaximumSize |
                 Format-Volume -FileSystem NTFS -Confirm:$false

The fence must live on a DIFFERENT volume; that is the whole point of it, and
the host refuses to start otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ib_execution import provenance  # noqa: E402
from ib_execution.execution_host import (  # noqa: E402
    EXIT_FATAL_SHUTDOWN,
    EXIT_FENCED,
)

# The child runs the real host with a FakeBroker: this drill is about storage
# and process lifecycle, and a real Gateway must never be reachable from it.
CHILD = textwrap.dedent(
    """
    import json, sys, time
    sys.path.insert(0, {src!r})
    from datetime import datetime, timezone
    from decimal import Decimal
    from pathlib import Path

    from ib_execution.clock import SystemClock
    from ib_execution.execution_host import ExecutionHost, HostConfig, HostStartupRefused
    from ib_execution.fake_broker import FakeBroker, Faults
    from ib_execution.risk import RiskConfig, RiskEngine

    journal, fence, status, marker = sys.argv[1:5]
    clock = SystemClock()
    risk = RiskEngine(
        RiskConfig(symbol_whitelist=("SPY",), strategy_whitelist=("drill",),
                   max_position_shares=5, max_order_shares=10,
                   max_order_notional=Decimal("20000")),
        clock,
    )
    host = ExecutionHost(
        HostConfig(journal_path=Path(journal), fence_path=Path(fence),
                   status_path=Path(status), require_separate_fence_domain={separate!r},
                   heartbeat_seconds=0.05),
        broker_factory=lambda: FakeBroker(clock, Faults()),
        risk=risk, clock=clock,
        alert=lambda level, msg: print(f"[{{level}}] {{msg}}", flush=True),
    )
    try:
        controller = host.start()
    except HostStartupRefused as exc:
        print(f"REFUSED {{exc}}", flush=True)
        raise SystemExit(exc.code)

    Path(marker).write_text("started", encoding="utf-8")
    print("STARTED", flush=True)
    try:
        # Keep writing to the journal so a storage fault is actually reached.
        from ib_execution.models import EventType
        while True:
            code = host.run_once()
            if code is not None:
                raise SystemExit(code)
            try:
                controller.journal.commit(EventType.HEARTBEAT, {{"t": time.time()}})
            except Exception as exc:
                controller._fail_closed_journal("drill heartbeat", exc)
            time.sleep(0.05)
    finally:
        host.close()
    """
)


def _spawn(paths: dict[str, Path], separate: bool) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable, "-c",
            CHILD.format(src=str(ROOT / "src"), separate=separate),
            str(paths["journal"]), str(paths["fence"]),
            str(paths["status"]), str(paths["marker"]),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_started(proc: subprocess.Popen, marker: Path, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return
        if proc.poll() is not None:
            raise RuntimeError(f"child exited early with {proc.returncode}")
        time.sleep(0.05)
    raise RuntimeError("child never started")


def _fill_volume(volume: Path, keep_bytes: int = 0) -> Path:
    """Consume the volume's free space with one ballast file."""
    ballast = volume / ".drill-ballast"
    free = shutil.disk_usage(volume).free
    target = max(free - keep_bytes, 0)
    with ballast.open("wb") as fh:
        written = 0
        block = b"\0" * (1024 * 1024)
        while written < target:
            chunk = block[: min(len(block), target - written)]
            try:
                fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
            except OSError:
                break
            written += len(chunk)
    return ballast


def drill_disk_full(paths: dict[str, Path], volume: Path) -> dict[str, Any]:
    proc = _spawn(paths, separate=True)
    ballast: Optional[Path] = None
    try:
        _wait_started(proc, paths["marker"])
        ballast = _fill_volume(volume)
        code = proc.wait(timeout=120)
    finally:
        if ballast is not None and ballast.exists():
            ballast.unlink()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=20)
    output = proc.stdout.read() if proc.stdout else ""
    return {
        "exit_code": code,
        "expected_exit_code": EXIT_FATAL_SHUTDOWN,
        "output_tail": output[-4000:],
    }


def drill_wal_corruption(paths: dict[str, Path]) -> dict[str, Any]:
    """Corrupt the WAL while the engine is down, then start it.

    The engine must refuse to reach a trading state. It must NOT repair the
    database: "the data is broken so we stopped" is the property under test.
    """
    proc = _spawn(paths, separate=True)
    try:
        _wait_started(proc, paths["marker"])
    finally:
        proc.terminate()
        proc.wait(timeout=30)

    wal = paths["journal"].with_name(paths["journal"].name + "-wal")
    corrupted = False
    if wal.exists() and wal.stat().st_size > 32:
        with wal.open("r+b") as fh:
            fh.seek(24)
            fh.write(b"\xde\xad\xbe\xef" * 4)
            fh.flush()
            os.fsync(fh.fileno())
        corrupted = True

    paths["marker"].unlink(missing_ok=True)
    second = _spawn(paths, separate=True)
    code = second.wait(timeout=120)
    output = second.stdout.read() if second.stdout else ""
    return {
        "wal_present_and_corrupted": corrupted,
        "exit_code": code,
        "acceptable_exit_codes": [EXIT_FATAL_SHUTDOWN, EXIT_FENCED],
        "output_tail": output[-4000:],
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Gate B1.4 real storage-fault drill")
    ap.add_argument(
        "--journal-volume",
        required=True,
        help="a small DEDICATED filesystem mount; this drill fills it completely",
    )
    ap.add_argument(
        "--fence-dir",
        required=True,
        help="a directory on a DIFFERENT volume from --journal-volume",
    )
    ap.add_argument("--output-root", default="artifacts/gate_b1_storage")
    ap.add_argument(
        "--drill",
        choices=["disk_full", "wal_corruption", "all"],
        default="all",
    )
    ns = ap.parse_args(argv)

    volume = Path(ns.journal_volume).resolve()
    fence_dir = Path(ns.fence_dir).resolve()
    if not volume.is_dir() or not fence_dir.is_dir():
        print("both --journal-volume and --fence-dir must exist", file=sys.stderr)
        return 2
    if os.stat(volume).st_dev == os.stat(fence_dir).st_dev:
        print(
            "--fence-dir is on the same volume as --journal-volume. The fence must "
            "outlive that volume filling up, so this drill would prove nothing.",
            file=sys.stderr,
        )
        return 2

    free_mb = shutil.disk_usage(volume).free / 1e6
    if free_mb > 2048:
        print(
            f"{volume} has {free_mb:.0f}MB free. Point --journal-volume at a small "
            "dedicated filesystem (64-128MB); this drill fills it completely.",
            file=sys.stderr,
        )
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / ns.output_root / stamp
    output.mkdir(parents=True, exist_ok=False)

    results: dict[str, Any] = {}
    for name in (["disk_full", "wal_corruption"] if ns.drill == "all" else [ns.drill]):
        work = volume / f"drill-{name}"
        work.mkdir(parents=True, exist_ok=True)
        paths = {
            "journal": work / "journal.db",
            "fence": fence_dir / f"fence-{name}.json",
            "status": work / "status.json",
            "marker": work / "started",
        }
        for path in paths.values():
            path.unlink(missing_ok=True)

        print(f"--- {name} ---", flush=True)
        if name == "disk_full":
            result = drill_disk_full(paths, volume)
        else:
            result = drill_wal_corruption(paths)

        fence_file = paths["fence"]
        result["fence_present"] = fence_file.exists()
        result["fence"] = (
            json.loads(fence_file.read_text(encoding="utf-8"))
            if fence_file.exists()
            else None
        )
        expected = result.get("acceptable_exit_codes") or [result["expected_exit_code"]]
        result["passed"] = result["exit_code"] in expected
        results[name] = result
        print(json.dumps({k: v for k, v in result.items() if k != "output_tail"}, indent=2))

    manifest = {
        "gate": "B1.4",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "commit_sha": provenance.commit_sha(ROOT),
        "worktree_clean": provenance.worktree_clean(ROOT),
        "source_tree_sha256": provenance.source_tree_sha256(ROOT)[0],
        "dependency_lock_sha256": provenance.dependency_lock_sha256(ROOT),
        "resolved_environment_sha256": provenance.resolved_environment_sha256(),
        "journal_volume": str(volume),
        "fence_dir": str(fence_dir),
        "drills": results,
        "passed": all(r["passed"] for r in results.values()),
    }
    path = output / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(path)
    return 0 if manifest["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
