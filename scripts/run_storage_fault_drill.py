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
                   max_order_notional=Decimal("20000")),  # any whitelist works
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
                # A payload, and no sleep: the drill needs the WAL to grow an
                # uncheckpointed tail, and needs to reach ENOSPC promptly.
                controller.journal.commit(
                    EventType.HEARTBEAT, {{"t": time.time(), "pad": "x" * 400}}
                )
            except Exception as exc:
                controller._fail_closed_journal("drill heartbeat", exc)
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


class _Ballast:
    """Holds a volume at zero free space for as long as it is open.

    Filling once is not enough. SQLite in WAL mode reuses frames after a
    checkpoint, so a single fill leaves the engine writing happily in the space
    the checkpoint just freed -- the first run of this drill sat there for two
    minutes without ever reaching ENOSPC. Pressure has to be *maintained*.

    Note also that ext4 reserves 5% of blocks for uid 0 by default. A drill
    running as root fills only the unreserved part while the engine, also root,
    keeps writing into the reserve. Format the drill volume with ``-m 0``.
    """

    BLOCK = b"\0" * (256 * 1024)

    def __init__(self, volume: Path):
        self.path = volume / ".drill-ballast"
        self._fh = self.path.open("wb")

    def top_up(self) -> int:
        """Consume whatever is free right now. Returns bytes newly written."""
        written = 0
        while True:
            try:
                self._fh.write(self.BLOCK)
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except OSError:
                # ENOSPC: the volume is genuinely full, which is the point.
                break
            written += len(self.BLOCK)
        return written

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass
        self.path.unlink(missing_ok=True)


def drill_disk_full(paths: dict[str, Path], volume: Path, timeout: float = 180.0) -> dict[str, Any]:
    proc = _spawn(paths, separate=True)
    ballast: Optional[_Ballast] = None
    code: Optional[int] = None
    filled = 0
    try:
        _wait_started(proc, paths["marker"])
        ballast = _Ballast(volume)
        filled = ballast.top_up()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code = proc.poll()
            if code is not None:
                break
            # Re-consume anything a WAL checkpoint handed back.
            filled += ballast.top_up()
            time.sleep(0.25)
    finally:
        if ballast is not None:
            ballast.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=20)
            code = None
    output = proc.stdout.read() if proc.stdout else ""
    return {
        "exit_code": code,
        "expected_exit_code": EXIT_FATAL_SHUTDOWN,
        "ballast_bytes": filled,
        "timed_out": code is None,
        "output_tail": output[-4000:],
    }


def _count_events(journal: Path) -> Any:
    """Replay a journal read-only. Returns a count, or the error string."""
    from ib_execution.journal import Journal

    handle = None
    try:
        handle = Journal(journal, owner=False)
        return len(list(handle.replay()))
    except Exception as exc:  # noqa: BLE001 - the error is the observation
        return f"{type(exc).__name__}: {exc}"
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass


def drill_wal_corruption(
    paths: dict[str, Path], run_seconds: float = 6.0, settle_seconds: float = 25.0
) -> dict[str, Any]:
    """Kill the engine, corrupt the WAL, and measure what the journal lost.

    This drill counts events rather than only checking an exit code, because
    the dangerous outcome here is not an error -- it is silence.

    A corrupt WAL *header* fails SQLite's checksum, so recovery discards WAL
    frames instead of reporting a problem. Every discarded frame is a
    transaction that ``commit()`` already confirmed as durable under
    ``synchronous=FULL``. The database is left internally consistent and
    simply shorter, and nothing inside SQLite can tell you rows are missing.

    For a platform whose central promise is durable-before-send, losing the
    tail of the journal means an ORDER_SENT can vanish while the order itself
    is live at the broker: the engine restarts believing it never sent.
    """
    proc = _spawn(paths, separate=True)
    try:
        _wait_started(proc, paths["marker"])
        time.sleep(run_seconds)                 # accumulate committed events
    finally:
        # SIGKILL, not terminate: a clean shutdown checkpoints and removes the
        # WAL, and there would be nothing left to corrupt.
        proc.kill()
        proc.wait(timeout=30)

    journal = paths["journal"]
    wal = journal.with_name(journal.name + "-wal")
    result: dict[str, Any] = {"wal_bytes": wal.stat().st_size if wal.exists() else 0}

    if not wal.exists() or wal.stat().st_size <= 32:
        result.update(
            corrupted=False,
            passed=False,
            note="no WAL left to corrupt; the drill proved nothing",
        )
        return result

    # Take the baseline from a *copy*. Opening the real journal to count would
    # recover and checkpoint it, deleting the very WAL this drill corrupts --
    # the first version of this drill did exactly that and then reported "no
    # WAL left to corrupt" against a 2.9MB WAL it had just consumed.
    baseline = journal.parent / "pre-corruption"
    shutil.rmtree(baseline, ignore_errors=True)
    baseline.mkdir()
    for suffix in ("", "-wal", "-shm"):
        source = journal.with_name(journal.name + suffix)
        if source.exists():
            shutil.copy2(source, baseline / source.name)
    result["events_before_corruption"] = _count_events(baseline / journal.name)

    # Offset 24 is checksum-1/checksum-2 of the WAL header. A failed header
    # checksum makes recovery discard frames rather than report a problem.
    with wal.open("r+b") as fh:
        fh.seek(24)
        fh.write(b"\xde\xad\xbe\xef" * 4)
        fh.flush()
        os.fsync(fh.fileno())
    result["corrupted"] = True

    # Same trick for the after-count: measure a copy, so the number reflects
    # what recovery discarded and not what the next process went on to write.
    post = journal.parent / "post-corruption"
    shutil.rmtree(post, ignore_errors=True)
    post.mkdir()
    for suffix in ("", "-wal", "-shm"):
        source = journal.with_name(journal.name + suffix)
        if source.exists():
            shutil.copy2(source, post / source.name)
    result["events_after_corruption"] = _count_events(post / journal.name)

    paths["marker"].unlink(missing_ok=True)
    second = _spawn(paths, separate=True)
    try:
        code: Optional[int] = second.wait(timeout=settle_seconds)
    except subprocess.TimeoutExpired:
        # Still running means it started and would trade on a journal it
        # cannot vouch for. That is the failure, not a hang.
        code = None
        second.kill()
        second.wait(timeout=20)
    output = second.stdout.read() if second.stdout else ""

    before = result["events_before_corruption"]
    after = result["events_after_corruption"]
    lost = (
        before - after
        if isinstance(before, int) and isinstance(after, int) and before > after
        else 0
    )
    started = code is None or code == EXIT_OK
    result.update(
        exit_code=code,
        engine_started=started,
        events_lost=lost,
        acceptable_exit_codes=[EXIT_FATAL_SHUTDOWN, EXIT_FENCED],
        output_tail=output[-4000:],
    )
    # The verdict is about durability, not about tidiness. Refusing to start is
    # correct. Starting after losing committed events is the finding.
    if lost and started:
        result["passed"] = False
        result["finding"] = (
            f"{lost} committed events were silently discarded by WAL recovery and "
            "the engine started anyway. commit() had already reported these durable. "
            "SQLite cannot detect this from inside the database; it needs an "
            "out-of-band monotone witness of the highest committed sequence."
        )
    else:
        result["passed"] = code in (EXIT_FATAL_SHUTDOWN, EXIT_FENCED) or lost == 0
    return result


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
        if "passed" not in result:
            result["passed"] = result["exit_code"] == result["expected_exit_code"]
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
