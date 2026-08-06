"""
Watchdog.

WHAT IT DOES NOT DO, AND WHY
----------------------------
It does not place orders. It does not flatten. It does not restart the engine.
It does not write operating_mode.

The tempting design is "heartbeat lost + position open -> auto-flatten". That
introduces split-brain: the engine may be alive while its status file is stuck
behind a blocked fsync or a wedged filesystem. Then two processes act on one
account, and a duplicate position is strictly worse than an unattended one.

Doing it safely needs a lease, a fencing token, single-writer ownership, and
re-verification against the broker on takeover. That is a distributed-systems
project. For one account and one symbol it is not worth the state space.

The single-writer property is a stronger safety guarantee than automatic
flattening, so we keep the former and give up the latter.

This is only safe because of SPEC invariant 19: position limits are sized so an
unflattened position can be held overnight. If size ever grows past that, the
watchdog design must be revisited in the same change. The two are coupled.

Restart is deliberately manual. An automatic restart would reconcile and resume
trading while the cause of the kill is still undiagnosed -- it hides an incident
behind an automated action.

Writing operating_mode from here would also be unreliable: a process wedged on
fsync or spinning in a loop will never read the file. Anything reachable by a
file write is usually not the process that needs stopping. SIGKILL is the only
fencing action that does not require the target's cooperation.
"""

from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


@dataclass
class WatchdogConfig:
    status_path: Path
    heartbeat_timeout_seconds: float = 30.0
    grace_seconds_before_kill: float = 30.0
    poll_seconds: float = 5.0


@dataclass
class Verdict:
    """What the watchdog decided this cycle, and why."""

    healthy: bool
    reason: str
    should_alert: bool = False
    should_kill: bool = False
    severity: str = "INFO"


class Watchdog:
    def __init__(
        self,
        config: WatchdogConfig,
        alert: Callable[[str, str], None],
        clock=None,
    ):
        self.config = config
        self.alert = alert
        self.clock = clock
        self._unhealthy_since: Optional[float] = None

    # -- pure decision function, so it can be tested without processes ----

    def evaluate(self, now_mono: float, status: Optional[dict]) -> Verdict:
        cfg = self.config

        if status is None:
            return self._degraded(now_mono, "status file missing or unreadable", "CRITICAL")

        hb = status.get("heartbeat_mono")
        if hb is None:
            return self._degraded(now_mono, "status file has no heartbeat", "CRITICAL")

        age = now_mono - float(hb)
        if age > cfg.heartbeat_timeout_seconds:
            return self._degraded(
                now_mono,
                f"heartbeat stale by {age:.0f}s "
                f"(position={status.get('net_position', '?')})",
                "CRITICAL",
            )

        if status.get("fatal_shutdown_requested"):
            return Verdict(
                False,
                f"engine requested fatal shutdown ({status.get('journal_failure', 'runtime fault')})",
                should_alert=True,
                should_kill=True,
                severity="CRITICAL",
            )

        # Healthy link but a state we should be told about.
        self._unhealthy_since = None
        mode = status.get("operating_mode")
        if mode == "HALTED":
            return Verdict(True, "engine HALTED", should_alert=True, severity="CRITICAL")
        if status.get("sync_state") != "SYNCED":
            return Verdict(
                True,
                f"engine not synced ({status.get('sync_state')})",
                should_alert=True,
                severity="WARN",
            )
        return Verdict(True, "ok")

    def _degraded(self, now_mono: float, reason: str, severity: str) -> Verdict:
        if self._unhealthy_since is None:
            self._unhealthy_since = now_mono
            return Verdict(False, reason, should_alert=True, severity=severity)
        elapsed = now_mono - self._unhealthy_since
        kill = elapsed >= self.config.grace_seconds_before_kill
        return Verdict(
            False,
            reason + (f"; unhealthy for {elapsed:.0f}s" if not kill else "; killing engine"),
            should_alert=True,
            should_kill=kill,
            severity=severity,
        )

    # -- IO -------------------------------------------------------------

    def read_status(self) -> Optional[dict]:
        try:
            return json.loads(self.config.status_path.read_text())
        except Exception:  # noqa: BLE001 -- any failure is "no status"
            return None

    @staticmethod
    def _pid_start_ticks(pid: int) -> Optional[int]:
        """
        Process creation identity, used as a PID-reuse guard.

        PIDs are recycled. On a busy host the engine can die, its PID be reused
        by something unrelated, and a watchdog that trusts the number alone will
        SIGKILL an innocent process. The start time makes the identity unique:
        same pid AND same start time, or we refuse to signal.

        Linux exposes field 22 in ``/proc/<pid>/stat``.  Windows has no /proc;
        there we use ``GetProcessTimes`` and return the 64-bit creation FILETIME.
        Both values are opaque identity tokens -- callers must only compare them.
        """
        if os.name == "nt":
            return Watchdog._windows_process_creation_time(pid)
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                data = fh.read()
            # comm may contain spaces/parens; everything after the last ')' is safe
            fields = data[data.rfind(b")") + 2:].split()
            return int(fields[19])          # starttime, field 22 overall
        except (OSError, IndexError, ValueError):
            return None

    @staticmethod
    def _windows_process_creation_time(pid: int) -> Optional[int]:
        """Return Windows process creation FILETIME without a third-party dependency."""
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
            if not handle:
                return None
            try:
                creation = wintypes.FILETIME()
                exit_time = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                if not kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return None
                return (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, ValueError):
            return None

    @staticmethod
    def _force_kill(pid: int) -> None:
        """Fence a process: SIGKILL on POSIX, TerminateProcess on Windows."""
        if os.name != "nt":
            os.kill(pid, signal.SIGKILL)
            return

        import ctypes
        from ctypes import wintypes

        process_terminate = 0x0001
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(process_terminate, False, int(pid))
        if not handle:
            raise ProcessLookupError(pid)
        try:
            if not kernel32.TerminateProcess(handle, 137):
                raise OSError(ctypes.get_last_error(), "TerminateProcess failed")
        finally:
            kernel32.CloseHandle(handle)

    def kill_engine(self, status: Optional[dict]) -> bool:
        """
        SIGTERM then SIGKILL. This is the ONLY action the watchdog takes on the
        engine, and it is fencing: it does not require the engine's cooperation.
        Restart is a human decision.
        """
        pid = (status or {}).get("pid")
        if not pid:
            self.alert("CRITICAL", "cannot kill engine: no pid in status file")
            return False

        recorded = (status or {}).get("pid_start_ticks")
        current = self._pid_start_ticks(int(pid))
        if recorded is None or current is None:
            self.alert(
                "CRITICAL",
                f"cannot verify process identity for pid {pid}; refusing to signal it. "
                f"Verify the engine and broker state manually.",
            )
            return False
        if int(recorded) != int(current):
            # The PID was recycled. Killing it would take out an unrelated
            # process while the real engine is already gone.
            self.alert(
                "CRITICAL",
                f"pid {pid} was recycled (start time differs); refusing to signal it. "
                f"The engine is already dead. Verify broker state manually.",
            )
            return False

        try:
            if os.name == "nt":
                # Windows has no targetable POSIX SIGTERM/SIGKILL pair.
                # os.kill(pid, 0) is also not a harmless existence probe there;
                # non-console signals map to TerminateProcess. Fence directly.
                self._force_kill(int(pid))
            else:
                os.kill(int(pid), signal.SIGTERM)
                time.sleep(5)
                if self._pid_start_ticks(int(pid)) is not None:
                    self._force_kill(int(pid))
        except ProcessLookupError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.alert("CRITICAL", f"failed to kill engine pid={pid}: {exc}")
            return False

        self.alert(
            "CRITICAL",
            f"engine pid={pid} killed by watchdog. NOT restarting automatically. "
            f"Position at last known status: {(status or {}).get('net_position', '?')}. "
            f"Human action required: verify broker state, then decide whether to "
            f"run emergency_flatten.",
        )
        return True

    def run_once(self, now_mono: float) -> Verdict:
        status = self.read_status()
        v = self.evaluate(now_mono, status)
        if v.should_alert:
            self.alert(v.severity, f"watchdog: {v.reason}")
        if v.should_kill:
            self.kill_engine(status)
        return v

    def run_forever(self) -> None:  # pragma: no cover
        while True:
            self.run_once(time.monotonic())
            time.sleep(self.config.poll_seconds)


def write_status(path: Path, payload: dict) -> None:
    """
    Atomic status publication for the engine side.

    Written every second. Partial reads by the watchdog would produce spurious
    kills, so we always write-then-rename.
    """
    payload = dict(payload)
    payload["pid"] = os.getpid()
    payload["pid_start_ticks"] = Watchdog._pid_start_ticks(os.getpid())
    payload["heartbeat_mono"] = time.monotonic()
    payload["heartbeat_wall"] = time.time()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    os.replace(tmp, path)
