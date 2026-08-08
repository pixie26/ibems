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
    wal_corruption   the WAL is damaged while the engine is down, and B1.6's
                     witness has to notice the committed events it discarded
    fsync_stall      commits block past the write timeout, at the block layer
                     via dm-delay -- SQLite runs unmodified and cannot tell it
                     is being tested. Run as a positive/negative pair, so the
                     drill distinguishes "fails closed past its timeout" from
                     "dies whenever storage is slow". The failing delay is
                     injected only AFTER a healthy host has fully started. A
                     FUSE fallback exists for hosts without device-mapper but
                     is usually inconclusive; see _fuse_supports_sqlite_wal.

    On a host with device-mapper this is the whole command:

        sudo python scripts/run_storage_fault_drill.py \
            --journal-volume /mnt/ibems-drill --fence-dir /var/opt/ibems-fence \
            --drill fsync_stall

THE VOLUME
----------
``--journal-volume`` must be a small dedicated filesystem, NOT a directory on
your main disk -- filling the latter takes the machine down with it, and the
drill needs the volume to be genuinely exhaustible. The fsync-stall drill
creates its disposable dm-delay image inside this dedicated filesystem; it
never takes over a caller-supplied block device.

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

from ib_execution import dm_delay, provenance  # noqa: E402
from ib_execution.execution_host import (  # noqa: E402
    EXIT_FATAL_SHUTDOWN,
    EXIT_FENCED,
    EXIT_OK,
    EXIT_WITNESS,
)

# The child runs the real host with a FakeBroker: this drill is about storage
# and process lifecycle, and a real Gateway must never be reachable from it.
#
# For fsync_stall the child starts healthy and waits for a trigger on the fence
# volume. The parent either triggers while storage is healthy (positive
# control), or first reloads dm-delay past the write timeout and only then
# triggers a target. Broker calls are durably logged on the fence volume so
# "post_fault_broker_writes == 0" is an observation, not a hard-coded claim.
CHILD = textwrap.dedent(
    """
    import json, os, sys, time
    sys.path.insert(0, {src!r})
    from datetime import datetime, timezone
    from decimal import Decimal
    from pathlib import Path

    from datetime import timedelta

    from ib_execution.clock import SystemClock
    from ib_execution.execution_host import ExecutionHost, HostConfig, HostStartupRefused
    from ib_execution.fake_broker import FakeBroker, Faults
    from ib_execution.models import Quote, TargetPosition
    from ib_execution.risk import RiskConfig, RiskEngine

    journal, fence, status, marker, witness, broker_calls, trigger, probe_done = sys.argv[1:9]
    halt_after = float(sys.argv[9])
    mode = sys.argv[10]
    clock = SystemClock()

    class RecordingFakeBroker(FakeBroker):
        def _record_write(self, operation, order_ref):
            entry = dict(
                ts_monotonic_ns=time.monotonic_ns(),
                pid=os.getpid(),
                operation=operation,
                order_ref=order_ref,
            )
            path = Path(broker_calls)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\\n")
                fh.flush()
                os.fsync(fh.fileno())

        def place_order(self, intent):
            self._record_write("place_order", intent.order_ref)
            return super().place_order(intent)

        def cancel_order(self, order_ref):
            self._record_write("cancel_order", order_ref)
            return super().cancel_order(order_ref)

    risk = RiskEngine(
        RiskConfig(symbol_whitelist=("SPY",), strategy_whitelist=("drill",),
                   max_position_shares=5, max_order_shares=10,
                   max_order_notional=Decimal("20000")),
        clock,
    )
    broker = RecordingFakeBroker(clock, Faults())
    host = ExecutionHost(
        HostConfig(journal_path=Path(journal), fence_path=Path(fence),
                   status_path=Path(status), witness_path=Path(witness),
                   require_separate_fence_domain={separate!r},
                   heartbeat_seconds=0.05),
        broker_factory=lambda: broker,
        risk=risk, clock=clock,
        alert=lambda level, msg: print(f"[{{level}}] {{msg}}", flush=True),
    )
    try:
        controller = host.start()
    except HostStartupRefused as exc:
        print(f"REFUSED {{exc}}", flush=True)
        raise SystemExit(exc.code)

    controller.on_connected(1)
    now = clock.now()
    controller.on_quote(Quote("SPY", Decimal("599.98"), Decimal("600.02"), 500, 500, now))
    controller.reconcile()

    # The traffic drills need one witnessed broker write before damaging the
    # WAL. The fsync-stall child deliberately does NOT send here: it must prove
    # that a fully healthy, already-started host blocks the first broker write
    # attempted after the block-layer stall is injected.
    if mode != "fsync_trigger":
        sent = controller.submit_target(TargetPosition(
            strategy_id="drill", symbol="SPY", target_quantity=1,
            decision_id="drill-1", valid_until=now + timedelta(hours=8),
        ))
        print(f"SENT {{sent}} witness_seq={{host.witness.read().seq if host.witness.read() else None}}",
              flush=True)

    Path(marker).write_text("started", encoding="utf-8")
    print("STARTED", flush=True)
    halt_deadline = time.monotonic() + halt_after if halt_after > 0 else None
    trigger_handled = False
    try:
        from ib_execution.models import EventType
        while True:
            if mode == "fsync_trigger":
                if not trigger_handled and Path(trigger).exists():
                    trigger_handled = True
                    try:
                        probe_now = clock.now()
                        sent = controller.submit_target(TargetPosition(
                            strategy_id="drill", symbol="SPY", target_quantity=1,
                            decision_id="fsync-trigger",
                            valid_until=probe_now + timedelta(hours=1),
                        ))
                        Path(probe_done).write_text(
                            json.dumps(dict(
                                sent=bool(sent),
                                ts_monotonic_ns=time.monotonic_ns(),
                            )),
                            encoding="utf-8",
                        )
                        print(f"FSYNC_PROBE sent={{sent}}", flush=True)
                    except Exception as exc:
                        # submit_target has already fail-closed on a journal
                        # timeout. Keep the process alive long enough for the
                        # real host supervision tick to emit contractual exit 10.
                        print(f"FSYNC_PROBE_EXCEPTION {{type(exc).__name__}}: {{exc}}", flush=True)

                if trigger_handled:
                    code = host.run_once()
                    if code is not None:
                        raise SystemExit(code)
                time.sleep(0.02)
                continue

            # Traffic mode: keep writing so ENOSPC and WAL-tail behavior are
            # reached by the real journal rather than an injected exception.
            if halt_deadline is not None and time.monotonic() >= halt_deadline:
                controller.halt("drill: provoked mid-session halt")
                print(f"HALTED witness_seq={{host.witness.read().seq}}", flush=True)
                halt_deadline = None
            code = host.run_once()
            if code is not None:
                raise SystemExit(code)
            try:
                controller.journal.commit(
                    EventType.HEARTBEAT, {{"t": time.time(), "pad": "x" * 400}}
                )
            except Exception as exc:
                controller._fail_closed_journal("drill heartbeat", exc)
    finally:
        host.close()
    """
)


def _spawn(
    paths: dict[str, Path],
    separate: bool,
    halt_after: float = 0.0,
    mode: str = "traffic",
) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable, "-c",
            CHILD.format(src=str(ROOT / "src"), separate=separate),
            str(paths["journal"]), str(paths["fence"]),
            str(paths["status"]), str(paths["marker"]), str(paths["witness"]),
            str(paths["broker_calls"]), str(paths["trigger"]), str(paths["probe_done"]),
            str(halt_after), mode,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_for_path(
    proc: subprocess.Popen,
    path: Path,
    *,
    timeout: float,
    description: str,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc.poll() is not None:
            raise RuntimeError(
                f"child exited early with {proc.returncode} while waiting for {description}"
            )
        time.sleep(0.05)
    raise RuntimeError(f"timed out waiting for {description}")


def _wait_started(proc: subprocess.Popen, marker: Path, timeout: float = 30.0) -> None:
    _wait_for_path(proc, marker, timeout=timeout, description="STARTED marker")


def _clear_case_paths(paths: dict[str, Path]) -> None:
    """Remove only files owned by this drill case, including SQLite sidecars."""
    for key in (
        "journal", "status", "marker", "witness", "fence",
        "broker_calls", "trigger", "probe_done",
    ):
        path = paths[key]
        suffixes = ("", "-wal", "-shm", ".lock") if key == "journal" else ("",)
        for suffix in suffixes:
            Path(str(path) + suffix).unlink(missing_ok=True)


def _read_broker_calls(path: Path) -> list[dict[str, Any]]:
    """Read the child-side durable observation of broker writes."""
    if not path.exists():
        return []
    calls: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            calls.append(json.loads(line))
    return calls


def _stop_child(proc: subprocess.Popen, timeout: float = 30.0) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout)


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
    proc = _spawn(paths, separate=True, halt_after=run_seconds / 2)
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
    witness_file = paths["witness"]
    witnessed = None
    if witness_file.exists():
        from ib_execution.journal_witness import WitnessRecord

        witnessed = WitnessRecord.from_dict(
            json.loads(witness_file.read_text(encoding="utf-8"))
        )

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
    witnessed_seq = witnessed.seq if witnessed is not None else None
    crossed = (
        witnessed_seq is not None
        and isinstance(after, int)
        and after < witnessed_seq
    )
    result.update(
        exit_code=code,
        engine_started=started,
        events_lost=lost,
        witnessed_seq=witnessed_seq,
        rollback_crossed_the_witness=crossed,
        acceptable_exit_codes=[EXIT_WITNESS, EXIT_FATAL_SHUTDOWN, EXIT_FENCED],
        output_tail=output[-4000:],
    )
    # Losing events is expected -- that is the fault being injected. What is
    # being judged is whether the platform notices when the loss reaches
    # something that mattered.
    #
    # Above the witness: telemetry only. Starting is correct, and refusing
    # would make every damaged WAL an outage.
    # Across the witness: a send or a HALT is gone. Starting is the failure.
    if crossed:
        result["passed"] = code in (EXIT_WITNESS, EXIT_FATAL_SHUTDOWN, EXIT_FENCED)
        if not result["passed"]:
            result["finding"] = (
                f"{lost} committed events were discarded, including seq {witnessed_seq} "
                "which authorised a broker write or recorded a HALT, and the engine "
                "started anyway."
            )
    else:
        result["passed"] = True
        result["note"] = (
            f"{lost} committed events were discarded by WAL recovery, all of them "
            f"above the witnessed seq {witnessed_seq}. No send or HALT was lost, so "
            "starting is correct. The witness is what makes that distinguishable."
        )

    # Second phase: force the loss to reach the witnessed event.
    #
    # Where the natural rollback lands is a matter of when the WAL last
    # checkpointed, so it usually takes only the tail. The property that has to
    # hold is the other branch -- when the loss *does* reach a send or a HALT,
    # the real host must refuse -- and that is arranged rather than waited for.
    # Still the real volume, the real host process and the real witness file.
    if witnessed_seq is not None:
        import sqlite3

        conn = sqlite3.connect(journal)
        try:
            conn.execute("DELETE FROM events WHERE seq >= ?", (witnessed_seq,))
            conn.commit()
        finally:
            conn.close()
        paths["marker"].unlink(missing_ok=True)
        forced = _spawn(paths, separate=True)
        try:
            forced_code: Optional[int] = forced.wait(timeout=120)
        except subprocess.TimeoutExpired:
            forced_code = None
            forced.kill()
            forced.wait(timeout=20)
        forced_out = forced.stdout.read() if forced.stdout else ""
        result["forced_crossing"] = {
            "removed_from_seq": witnessed_seq,
            "exit_code": forced_code,
            "expected_exit_code": EXIT_WITNESS,
            "passed": forced_code == EXIT_WITNESS,
            "output_tail": forced_out[-2000:],
        }
        result["passed"] = result["passed"] and result["forced_crossing"]["passed"]
    return result


def _fuse_supports_sqlite_wal(mount: Path, timeout: float) -> tuple[bool, str]:
    """Probe, rather than assume, whether SQLite's WAL works on a FUSE mount.

    A FUSE passthrough cannot back the ``-shm`` file WAL mode mmaps, and SQLite
    dies with SIGBUS rather than an error. Measured: default WAL SIGBUSes,
    while non-WAL and ``locking_mode=EXCLUSIVE`` (which skips the shm file)
    both succeed. EXCLUSIVE is not an option for the platform -- it locks out
    the read-only auditor -- so the honest outcome is inconclusive rather than
    a FAIL that reads as a product defect.
    """
    probe = textwrap.dedent(
        """
        import sqlite3, sys
        c = sqlite3.connect(sys.argv[1])
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL")
        c.execute("CREATE TABLE t(x)")
        c.execute("INSERT INTO t VALUES (1)")
        c.commit()
        print("OK")
        """
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe, str(mount / ".wal-probe.db")],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"the SQLite WAL probe did not finish within {timeout:.0f}s"
    if result.returncode == 0:
        return True, ""
    signal_name = f"signal {-result.returncode}" if result.returncode < 0 else str(result.returncode)
    return False, (
        f"SQLite WAL is unusable on this mount ({signal_name}). A FUSE passthrough "
        "cannot back the -shm file WAL mode mmaps. Use dm-delay at the block layer, "
        "which is transparent to SQLite, and do not claim B1.4 evidence from this run."
    )


def _run_fsync_healthy_control(
    paths: dict[str, Path],
    volume,
    *,
    healthy_delay_ms: int,
    write_timeout: float,
) -> dict[str, Any]:
    """Start healthy, trigger one broker write, and prove the engine stays alive."""
    _clear_case_paths(paths)
    volume.set_delay(healthy_delay_ms)
    proc = _spawn(paths, separate=True, mode="fsync_trigger")
    started_at = time.monotonic_ns()
    result: dict[str, Any] = {
        "delay_ms": healthy_delay_ms,
        "started_at_monotonic_ns": started_at,
        "expectation": "healthy trigger reaches broker; engine stays alive and unfenced",
    }
    try:
        _wait_started(proc, paths["marker"], timeout=max(30.0, write_timeout + 10.0))
        trigger_at = time.monotonic_ns()
        paths["trigger"].write_text(str(trigger_at), encoding="utf-8")
        _wait_for_path(
            proc,
            paths["probe_done"],
            timeout=write_timeout + 30.0,
            description="healthy fsync probe completion",
        )
        # Give the host another supervision turn after the broker call. A
        # healthy control that exits immediately afterwards is not healthy.
        time.sleep(max(0.25, healthy_delay_ms / 1000.0 * 2.0))
        calls = _read_broker_calls(paths["broker_calls"])
        probe = json.loads(paths["probe_done"].read_text(encoding="utf-8"))
        alive = proc.poll() is None
        fence_present = paths["fence"].exists()
        result.update(
            trigger_at_monotonic_ns=trigger_at,
            probe=probe,
            broker_writes=calls,
            broker_write_count=len(calls),
            still_running_after_probe=alive,
            fence_present=fence_present,
        )
        result["passed"] = bool(
            alive
            and probe.get("sent") is True
            and len(calls) == 1
            and calls[0].get("operation") == "place_order"
            and not fence_present
        )
    except Exception as exc:  # noqa: BLE001 - evidence must record harness failures
        result.update(passed=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        exit_before_cleanup = proc.poll()
        _stop_child(proc)
        result["exit_code_before_cleanup"] = exit_before_cleanup
        result["exit_code_after_cleanup"] = proc.returncode
        output = proc.stdout.read() if proc.stdout else ""
        result["output_tail"] = output[-3000:]
    return result


def _run_fsync_stalling_case(
    paths: dict[str, Path],
    volume,
    *,
    healthy_delay_ms: int,
    stalling_delay_ms: int,
    write_timeout: float,
) -> dict[str, Any]:
    """Start healthy, inject the stall live, then demand a broker write.

    This ordering is the claim B1.4 needs. Starting a process on an already
    stalled device only proves startup refusal; it does not prove that a live
    engine fails closed when storage degrades underneath it.
    """
    _clear_case_paths(paths)
    volume.set_delay(healthy_delay_ms)
    proc = _spawn(paths, separate=True, mode="fsync_trigger")
    started_at = time.monotonic_ns()
    result: dict[str, Any] = {
        "healthy_boot_delay_ms": healthy_delay_ms,
        "stalling_delay_ms": stalling_delay_ms,
        "started_at_monotonic_ns": started_at,
        "expected_exit_code": EXIT_FATAL_SHUTDOWN,
        "expectation": "live stall -> JournalUnavailable -> fence -> exit 10; no broker write",
    }
    try:
        _wait_started(proc, paths["marker"], timeout=max(30.0, write_timeout + 10.0))
        calls_before = _read_broker_calls(paths["broker_calls"])

        fault_requested_at = time.monotonic_ns()
        volume.set_delay(stalling_delay_ms)
        fault_active_at = time.monotonic_ns()

        # Only after dm-delay confirms the new table is active do we request a
        # target that would normally place an order. Its journal write must
        # time out before the broker boundary is reached.
        trigger_at = time.monotonic_ns()
        paths["trigger"].write_text(str(trigger_at), encoding="utf-8")

        try:
            code: Optional[int] = proc.wait(
                timeout=(stalling_delay_ms / 1000.0) + write_timeout + 180.0
            )
        except subprocess.TimeoutExpired:
            code = None
            _stop_child(proc, timeout=60.0)
        exit_observed_at = time.monotonic_ns()

        calls = _read_broker_calls(paths["broker_calls"])
        post_fault = [
            call for call in calls
            if int(call.get("ts_monotonic_ns", 0)) >= fault_active_at
        ]
        fence_present = paths["fence"].exists()
        fence = (
            json.loads(paths["fence"].read_text(encoding="utf-8"))
            if fence_present else None
        )
        result.update(
            fault_requested_at_monotonic_ns=fault_requested_at,
            fault_injected_at_monotonic_ns=fault_active_at,
            trigger_at_monotonic_ns=trigger_at,
            exit_observed_at_monotonic_ns=exit_observed_at,
            exit_code=code,
            broker_writes_before_fault=calls_before,
            broker_writes=calls,
            post_fault_broker_writes=len(post_fault),
            post_fault_broker_write_records=post_fault,
            probe_completed=paths["probe_done"].exists(),
            fence_present=fence_present,
            fence=fence,
        )
        result["passed"] = bool(
            code == EXIT_FATAL_SHUTDOWN
            and len(calls_before) == 0
            and len(post_fault) == 0
            and fence_present
            and fault_active_at <= trigger_at <= exit_observed_at
        )
    except Exception as exc:  # noqa: BLE001
        result.update(passed=False, error=f"{type(exc).__name__}: {exc}")
        _stop_child(proc, timeout=60.0)
    finally:
        if proc.poll() is None:
            _stop_child(proc, timeout=60.0)
        output = proc.stdout.read() if proc.stdout else ""
        result["output_tail"] = output[-4000:]
    return result


def drill_fsync_stall_dm_delay(
    paths: dict[str, Path],
    image: Path,
    mount: Path,
    healthy_delay_ms: int,
    stalling_delay_ms: int,
    write_timeout: float,
) -> dict[str, Any]:
    """Gate B1.4 fsync stall, at the block layer, as a positive/negative pair.

    dm-delay sits under the filesystem, so SQLite runs in its production
    configuration and cannot tell it is being tested. Patching ``os.fsync``
    would only re-test the patch.

    Both cases boot under the healthy delay. The negative case then changes the
    live, mounted device to a delay past the write timeout and only afterwards
    requests a target that would normally reach ``place_order``. This proves a
    runtime storage degradation, not merely failure to start on a bad disk.
    """
    result: dict[str, Any] = {
        "mechanism": "dm-delay",
        "journal_write_timeout_seconds": write_timeout,
        "healthy_delay_ms": healthy_delay_ms,
        "stalling_delay_ms": stalling_delay_ms,
        "expected_exit_code": EXIT_FATAL_SHUTDOWN,
    }
    available, why = dm_delay.availability()
    if not available:
        result.update(passed=False, inconclusive=True, note=f"dm-delay unusable: {why}")
        return result

    volume = dm_delay.DelayedVolume(
        image=image, mount_point=mount, size_mb=128, delay_ms=healthy_delay_ms
    )
    try:
        volume.create()
        result["device"] = volume.device
        paths["journal"] = mount / "journal.db"
        paths["status"] = mount / "status.json"
        paths["marker"] = mount / "started"

        healthy = _run_fsync_healthy_control(
            paths,
            volume,
            healthy_delay_ms=healthy_delay_ms,
            write_timeout=write_timeout,
        )
        result["healthy"] = healthy

        stalling = _run_fsync_stalling_case(
            paths,
            volume,
            healthy_delay_ms=healthy_delay_ms,
            stalling_delay_ms=stalling_delay_ms,
            write_timeout=write_timeout,
        )
        result["stalling"] = stalling
        result["fence_present"] = bool(stalling.get("fence_present"))
        result["fence"] = stalling.get("fence")
        result["post_fault_broker_writes"] = stalling.get("post_fault_broker_writes")
        result["passed"] = bool(healthy.get("passed") and stalling.get("passed"))
    except dm_delay.DmDelayUnsafe as exc:
        result.update(passed=False, inconclusive=True, note=f"refused: {exc}")
    except dm_delay.DmDelayUnavailable as exc:
        result.update(passed=False, inconclusive=True, note=str(exc))
    finally:
        volume.destroy()
    return result


def drill_fsync_stall_fuse(
    paths: dict[str, Path],
    mount: Path,
    backing: Path,
    delay_seconds: float,
    write_timeout: float,
) -> dict[str, Any]:
    """Fallback for hosts without device-mapper. Usually inconclusive; see the probe."""
    result: dict[str, Any] = {
        "mechanism": "fuse",
        "delay_seconds": delay_seconds,
        "journal_write_timeout_seconds": write_timeout,
        "mount": str(mount),
        "expected_exit_code": EXIT_FATAL_SHUTDOWN,
    }
    server = subprocess.Popen(
        [
            sys.executable, str(ROOT / "scripts" / "slow_fsync_fs.py"),
            "--backing", str(backing), "--mount", str(mount),
            "--delay", str(delay_seconds),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not os.path.ismount(mount):
            if server.poll() is not None:
                out = server.stdout.read() if server.stdout else ""
                result.update(passed=False, inconclusive=True,
                              note=f"slow-fsync FS failed to mount: {out[-600:]}")
                return result
            time.sleep(0.2)
        if not os.path.ismount(mount):
            result.update(passed=False, inconclusive=True,
                          note="slow-fsync FS never mounted")
            return result

        usable, why = _fuse_supports_sqlite_wal(mount, timeout=delay_seconds * 8 + 60)
        if not usable:
            result.update(passed=False, inconclusive=True, note=why)
            return result

        proc = _spawn(paths, separate=True)
        started = time.monotonic()
        try:
            code: Optional[int] = proc.wait(timeout=delay_seconds + write_timeout + 120)
        except subprocess.TimeoutExpired:
            code = None
            proc.kill()
            proc.wait(timeout=30)
        output = proc.stdout.read() if proc.stdout else ""
        result.update(
            exit_code=code,
            seconds_to_exit=round(time.monotonic() - started, 1),
            timed_out=code is None,
            output_tail=output[-4000:],
            passed=code == EXIT_FATAL_SHUTDOWN,
        )
    finally:
        subprocess.run(["fusermount", "-u", str(mount)], capture_output=True)
        server.terminate()
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()
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
        choices=["disk_full", "wal_corruption", "fsync_stall", "all"],
        default="all",
    )
    ap.add_argument(
        "--fsync-delay",
        type=float,
        default=45.0,
        help="seconds of block-layer delay for the failing case; must exceed the "
             "journal write timeout (30s)",
    )
    ap.add_argument(
        "--healthy-delay-ms",
        type=int,
        default=200,
        help="delay for the positive control: slow, but inside the write timeout, "
             "so the engine must NOT fail closed",
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

    all_drills = ["disk_full", "wal_corruption", "fsync_stall"]
    results: dict[str, Any] = {}
    for name in (all_drills if ns.drill == "all" else [ns.drill]):
        work = volume / f"drill-{name}"
        work.mkdir(parents=True, exist_ok=True)
        paths = {
            "journal": work / "journal.db",
            "fence": fence_dir / f"fence-{name}.json",
            # Per drill, like the fence: a witness from another drill refers to
            # another journal, and the host correctly refuses to start on it.
            "witness": fence_dir / f"witness-{name}.json",
            "status": work / "status.json",
            "marker": work / "started",
            # Test instrumentation lives with the fence, not the delayed journal
            # volume, so the observation itself survives the injected stall.
            "broker_calls": fence_dir / f"broker-calls-{name}.jsonl",
            "trigger": fence_dir / f"trigger-{name}",
            "probe_done": fence_dir / f"probe-done-{name}.json",
        }
        for path in paths.values():
            path.unlink(missing_ok=True)

        print(f"--- {name} ---", flush=True)
        if name == "disk_full":
            result = drill_disk_full(paths, volume)
        elif name == "wal_corruption":
            result = drill_wal_corruption(paths)
        else:
            # dm-delay is the supported mechanism: it is under the filesystem,
            # so SQLite runs unmodified. FUSE is only a fallback.
            available, _why = dm_delay.availability()
            if available:
                result = drill_fsync_stall_dm_delay(
                    paths,
                    image=volume / "fsync-stall.img",
                    mount=volume / "fsync-stall-mount",
                    healthy_delay_ms=ns.healthy_delay_ms,
                    stalling_delay_ms=int(ns.fsync_delay * 1000),
                    write_timeout=30.0,
                )
            else:
                mount = volume / "slow-mount"
                backing = volume / "slow-backing"
                mount.mkdir(parents=True, exist_ok=True)
                backing.mkdir(parents=True, exist_ok=True)
                paths["journal"] = mount / "journal.db"
                paths["status"] = mount / "status.json"
                paths["marker"] = mount / "started"
                result = drill_fsync_stall_fuse(
                    paths, mount, backing, ns.fsync_delay, write_timeout=30.0
                )

        witness_file = paths["witness"]
        result["witness"] = (
            json.loads(witness_file.read_text(encoding="utf-8"))
            if witness_file.exists()
            else None
        )
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
        # Inconclusive is not a pass. A drill that could not run says nothing
        # about the platform, and recording it as either outcome would be a
        # lie in one direction or the other.
        "inconclusive": sorted(k for k, r in results.items() if r.get("inconclusive")),
        "passed": all(
            r["passed"] for r in results.values() if not r.get("inconclusive")
        ) and not any(r.get("inconclusive") for r in results.values()),
    }
    path = output / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(path)
    return 0 if manifest["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
