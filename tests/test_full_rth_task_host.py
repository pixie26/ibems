from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "scripts" / "run_full_rth_recorder_task.py"


def _load_host():
    spec = importlib.util.spec_from_file_location("full_rth_task_host", HOST)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load host: {HOST}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _WatchdogProbe:
    def __init__(self) -> None:
        self.deadline = None

    def arm(self, deadline) -> None:
        self.deadline = deadline


def test_task_host_contains_no_child_process_lifecycle() -> None:
    source = HOST.read_text(encoding="utf-8")

    assert "subprocess.Popen" not in source
    assert "subprocess.run" not in source
    assert "TaskOwnedQuoteRecorder" in source
    assert "recorder.run()" in source


def test_runtime_status_records_one_pid_for_task_action_and_recorder(tmp_path: Path) -> None:
    host = _load_host()
    path = tmp_path / "runtime.json"

    host.RuntimeStatus(path)
    state = json.loads(path.read_text(encoding="utf-8"))

    assert state["task_action_pid"] == os.getpid()
    assert state["recorder_pid"] == os.getpid()
    assert state["phase"] == "WAITING_FOR_SESSION"


def test_deadline_is_actual_rth_close_plus_three_hours_thirty(tmp_path: Path) -> None:
    host = _load_host()
    status = host.RuntimeStatus(tmp_path / "runtime.json")
    watchdog = _WatchdogProbe()
    recorder = host.TaskOwnedQuoteRecorder(
        tmp_path / "raw",
        runtime_status=status,
        watchdog=watchdog,
    )
    start = datetime(2026, 8, 14, 13, 30, tzinfo=timezone.utc)
    end = start + timedelta(hours=6, minutes=30)
    details = SimpleNamespace(
        liquidSessions=lambda: [SimpleNamespace(start=start, end=end)]
    )

    session = recorder._session(details, start - timedelta(minutes=5))
    state = json.loads((tmp_path / "runtime.json").read_text(encoding="utf-8"))

    assert session.end == end
    assert watchdog.deadline == end + timedelta(hours=3, minutes=30)
    assert state["deadline_rule"] == "RTH_CLOSE_PLUS_3H_FINALIZE_PLUS_30M_SAFETY"
    assert datetime.fromisoformat(state["deadline_utc"]) == watchdog.deadline


def test_finalize_progress_exposes_the_requested_lifecycle_names(tmp_path: Path) -> None:
    host = _load_host()
    status_path = tmp_path / "runtime.json"
    status = host.RuntimeStatus(status_path)
    recorder = host.TaskOwnedQuoteRecorder(
        tmp_path / "raw",
        runtime_status=status,
        watchdog=_WatchdogProbe(),
    )

    expected = {
        "READING_RAW": "WRITING_PARQUET",
        "VERIFYING_PARQUET": "VERIFYING_PARQUET",
        "COMPUTING_HEALTH": "COMPUTING_HEALTH",
        "HASHING": "PUBLISHING_MANIFEST",
        "PUBLISHING": "PUBLISHING_MANIFEST",
    }
    for source_stage, visible_stage in expected.items():
        recorder._finalize_progress(source_stage, 123, 7)
        state = json.loads(status_path.read_text(encoding="utf-8"))
        assert state["phase"] == visible_stage
        assert state["rows_processed"] == 123
        assert state["segments_total"] == 7


def test_exit_codes_are_explicit_and_stable() -> None:
    host = _load_host()

    assert host.EXIT_HEALTH_PASS == 0
    assert host.EXIT_HEALTH_FAIL == 2
    assert host.EXIT_RUNTIME_ERROR != 0
    assert host.EXIT_DEADLINE != 0
    assert host.EXIT_RUNTIME_ERROR != host.EXIT_HEALTH_FAIL
    assert host.EXIT_DEADLINE != host.EXIT_HEALTH_FAIL
