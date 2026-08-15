"""Same-process Windows Task Scheduler host for the read-only Full-RTH Recorder.

Task Scheduler owns this Python process directly. QuoteRecorder runs in that
same process, with no intermediate shell or child Recorder lifecycle. A
watchdog enforces the actual session deadline even if the main thread is stuck
inside a blocking storage call.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import threading
import traceback
from typing import Any

from ib_execution.durable_io import durable_atomic_write
from ib_execution.quote_recorder import QuoteRecorder
from ib_execution.recorder_health_v4 import write_reanalysis_v4

SESSION_RESOLUTION_GRACE = timedelta(minutes=30)
FINALIZE_GRACE = timedelta(hours=3)
DEADLINE_SAFETY = timedelta(minutes=30)
EXIT_HEALTH_PASS = 0
EXIT_HEALTH_FAIL = 2
EXIT_RUNTIME_ERROR = 4
EXIT_DEADLINE = 5


class _FsyncLog:
    """Small audit log writer: every flush is made durable before returning."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            written = self._handle.write(text)
            self._handle.flush()
            os.fsync(self._handle.fileno())
            return written

    def flush(self) -> None:
        with self._lock:
            self._handle.flush()
            os.fsync(self._handle.fileno())

    def close(self) -> None:
        with self._lock:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()


class RuntimeStatus:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._state: dict[str, Any] = {
            "schema_version": 1,
            "task_action_pid": os.getpid(),
            "recorder_pid": os.getpid(),
            "phase": "WAITING_FOR_SESSION",
        }
        self._lock = threading.Lock()
        self.write()

    def update(self, phase: str, **values: Any) -> None:
        with self._lock:
            self._state.update(values)
            self._state["phase"] = phase
            self._state["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
            payload = (json.dumps(self._state, indent=2, sort_keys=True) + "\n").encode("utf-8")
            durable_atomic_write(self.path, payload)

    def write(self) -> None:
        self.update(str(self._state["phase"]))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)


class DeadlineWatchdog:
    """Hard process deadline independent of main-thread responsiveness.

    ``KeyboardInterrupt`` is insufficient here: a main thread stuck inside a
    kernel fsync cannot service it. The watchdog therefore terminates the one
    Task-owned Python process directly with ``EXIT_DEADLINE``. It intentionally
    performs no disk I/O at expiry, because the very failure being bounded may
    be a wedged storage stack. The last durable runtime-status phase plus the
    Scheduler exit code are the evidence in that case.
    """

    def __init__(self, status: RuntimeStatus) -> None:
        self.status = status
        self._deadline: datetime | None = None
        self._changed = threading.Event()
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, name="full-rth-deadline", daemon=True)
        self._thread.start()

    def arm(self, deadline: datetime) -> None:
        if deadline.tzinfo is None:
            raise ValueError("deadline must be timezone-aware")
        self._deadline = deadline.astimezone(timezone.utc)
        self._changed.set()

    def close(self) -> None:
        self._closed.set()
        self._changed.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._closed.is_set():
            deadline = self._deadline
            if deadline is None:
                self._changed.wait(1.0)
                self._changed.clear()
                continue
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                os._exit(EXIT_DEADLINE)
            self._changed.wait(min(remaining, 1.0))
            self._changed.clear()


class TaskOwnedQuoteRecorder(QuoteRecorder):
    """QuoteRecorder with task lifecycle publication, still the same process."""

    def __init__(
        self,
        *args: Any,
        runtime_status: RuntimeStatus,
        watchdog: DeadlineWatchdog,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.runtime_status = runtime_status
        self.watchdog = watchdog
        self.task_session = None

    def _session(self, details: Any, now: datetime):
        session = QuoteRecorder._session(details, now)
        self.task_session = session
        deadline = session.end + FINALIZE_GRACE + DEADLINE_SAFETY
        self.watchdog.arm(deadline)
        self.runtime_status.update(
            "WAITING_FOR_SESSION",
            session_open=session.start.isoformat(),
            session_close=session.end.isoformat(),
            deadline_utc=deadline.astimezone(timezone.utc).isoformat(),
            deadline_rule="RTH_CLOSE_PLUS_3H_FINALIZE_PLUS_30M_SAFETY",
        )
        return session

    def _pulse(self, **state: Any) -> None:
        super()._pulse(**state)
        phase = str(state.get("phase") or "")
        if phase == "WAITING_FOR_SESSION":
            self.runtime_status.update("WAITING_FOR_SESSION")
        elif phase == "CAPTURING":
            self.runtime_status.update("CAPTURING")

    def _finalize(self, session: Any) -> dict[str, Any]:
        self.runtime_status.update("DRAINING")
        return super()._finalize(session)

    def _finalize_progress(self, stage: str, rows: int, segments: int) -> None:
        super()._finalize_progress(stage, rows, segments)
        mapped = {
            "READING_RAW": "WRITING_PARQUET",
            "VERIFYING_PARQUET": "VERIFYING_PARQUET",
            "COMPUTING_HEALTH": "COMPUTING_HEALTH",
            "HASHING": "PUBLISHING_MANIFEST",
            "PUBLISHING": "PUBLISHING_MANIFEST",
        }.get(stage)
        if mapped is not None:
            self.runtime_status.update(
                mapped,
                finalize_stage=stage,
                rows_processed=rows,
                segments_total=segments,
            )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id", type=int, required=True)
    parser.add_argument("--recorder-status", required=True, type=Path)
    parser.add_argument("--runtime-status", required=True, type=Path)
    parser.add_argument("--stdout", required=True, type=Path)
    parser.add_argument("--stderr", required=True, type=Path)
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    stdout = _FsyncLog(args.stdout)
    stderr = _FsyncLog(args.stderr)
    status = RuntimeStatus(args.runtime_status)
    watchdog = DeadlineWatchdog(status)
    startup_deadline = datetime.now(timezone.utc) + SESSION_RESOLUTION_GRACE
    watchdog.arm(startup_deadline)
    status.update(
        "WAITING_FOR_SESSION",
        startup_deadline_utc=startup_deadline.isoformat(),
        startup_deadline_rule="SESSION_DETAILS_MUST_RESOLVE_WITHIN_30_MINUTES",
    )
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            recorder = TaskOwnedQuoteRecorder(
                args.root,
                args.symbol,
                host=args.host,
                port=args.port,
                client_id=args.client_id,
                status_path=args.recorder_status,
                runtime_status=status,
                watchdog=watchdog,
            )
            try:
                manifest_v3 = recorder.run()
                session = recorder.task_session
                if session is None:
                    raise RuntimeError("Recorder completed without resolving an RTH session")
                session_dir = args.root / session.start.date().isoformat()
                health_v4_path, amendment_path = write_reanalysis_v4(
                    session_dir,
                    session_dir,
                    session_open=session.start,
                    session_close=session.end,
                    original_health=session_dir / "health.json",
                    original_manifest=session_dir / "manifest.json",
                )
                health_v4 = json.loads(health_v4_path.read_text(encoding="utf-8"))
                health_ok = bool(health_v4.get("health_ok"))
                status.update(
                    "FINALIZED",
                    health_ok=health_ok,
                    health_v3_ok=bool(manifest_v3.get("health_ok")),
                    health_v4=str(health_v4_path),
                    manifest_amendment_v4=str(amendment_path),
                )
                print(json.dumps({"v3": manifest_v3, "v4": health_v4}, indent=2, sort_keys=True))
                return EXIT_HEALTH_PASS if health_ok else EXIT_HEALTH_FAIL
            except BaseException as exc:
                status.update("FAILED", failure=f"{type(exc).__name__}: {exc}")
                traceback.print_exc(file=sys.stderr)
                return EXIT_RUNTIME_ERROR
    finally:
        watchdog.close()
        stdout.close()
        stderr.close()


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
