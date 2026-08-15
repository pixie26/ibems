"""Verify the Full-RTH Task Scheduler lifecycle on real Windows without IB.

This is an operational probe, not a unit test. It registers three direct-Python
scheduled tasks through ``start_full_rth_recorder_task.py``:

* PASS: host exits 0 and Scheduler reports LastTaskResult == 0.
* FAIL: host exits 2 and Scheduler reports LastTaskResult == 2.
* HOLD: launcher exits, Task-owned Python remains alive independently, then
  ``schtasks /End`` stops it and the recorded PID disappears with no orphan.

The probe also checks that task_action_pid == recorder_pid, that the PID command
line is the expected host script, and that runtime-status/stdout/stderr files
belong to the same artifact directory. No IB connection is attempted and no
order path is enabled.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "start_full_rth_recorder_task.py"
HOST = ROOT / "scripts" / "run_full_rth_recorder_task.py"
CREATE_NO_WINDOW = 0x08000000


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _powershell_json(script: str) -> Any:
    completed = _run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            f"$ErrorActionPreference='Stop'; {script} | ConvertTo-Json -Compress",
        ]
    )
    text = completed.stdout.strip()
    return None if not text else json.loads(text)


def _task_info(task_name: str) -> dict[str, Any]:
    escaped = task_name.replace("'", "''")
    value = _powershell_json(
        f"Get-ScheduledTaskInfo -TaskName '{escaped}' | "
        "Select-Object LastRunTime,LastTaskResult,NextRunTime,NumberOfMissedRuns"
    )
    if not isinstance(value, dict):
        raise RuntimeError(f"unexpected scheduled-task info for {task_name}: {value!r}")
    return value


def _process(pid: int) -> dict[str, Any] | None:
    value = _powershell_json(
        f"Get-CimInstance Win32_Process -Filter \"ProcessId = {int(pid)}\" | "
        "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine"
    )
    return value if isinstance(value, dict) else None


def _descendant_processes(pid: int) -> list[dict[str, Any]]:
    value = _powershell_json(
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine"
    )
    if value is None:
        return []
    processes = [value] if isinstance(value, dict) else value
    if not isinstance(processes, list) or not all(isinstance(item, dict) for item in processes):
        raise RuntimeError(f"unexpected Win32_Process inventory: {value!r}")
    by_parent: dict[int, list[dict[str, Any]]] = {}
    for item in processes:
        try:
            parent = int(item.get("ParentProcessId"))
        except (TypeError, ValueError):
            continue
        by_parent.setdefault(parent, []).append(item)
    descendants: list[dict[str, Any]] = []
    pending = [int(pid)]
    seen = {int(pid)}
    while pending:
        parent = pending.pop()
        for child in by_parent.get(parent, []):
            child_pid = int(child["ProcessId"])
            if child_pid in seen:
                continue
            seen.add(child_pid)
            pending.append(child_pid)
            descendants.append(child)
    return descendants


def _wait_for_status(
    path: Path,
    phases: set[str],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            candidate = None
        if isinstance(candidate, dict):
            last = candidate
            if str(candidate.get("phase")) in phases:
                return candidate
        time.sleep(0.1)
    raise TimeoutError(f"timed out waiting for {sorted(phases)} in {path}; last={last}")


def _wait_for_task_result(
    task_name: str,
    expected: int,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last = None
    while time.monotonic() < deadline:
        last = _task_info(task_name)
        if int(last.get("LastTaskResult", -1)) == expected:
            return last
        time.sleep(0.2)
    raise TimeoutError(
        f"Scheduler did not report LastTaskResult={expected} for {task_name}; last={last}"
    )


def _wait_process_gone(pid: int, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _process(pid) is None:
            return
        time.sleep(0.1)
    raise TimeoutError(f"PID {pid} is still alive after Scheduler stop")


def _delete_task(task_name: str) -> None:
    _run(["schtasks.exe", "/Delete", "/TN", task_name, "/F"], check=False)


def _end_task(task_name: str) -> None:
    _run(["schtasks.exe", "/End", "/TN", task_name], check=False)


def _cleanup_task(task_name: str, artifact: Path, *, timeout_seconds: float) -> None:
    runtime = artifact / "task-runtime-status.json"
    owned_pids: set[int] = set()
    try:
        state = json.loads(runtime.read_text(encoding="utf-8"))
        pid = int(state["task_action_pid"])
        process = _process(pid)
        if process is not None and HOST.name in str(process.get("CommandLine") or ""):
            owned_pids.add(pid)
        owned_pids.update(int(item["ProcessId"]) for item in _descendant_processes(pid))
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        pass

    _end_task(task_name)
    failures: list[str] = []
    for pid in sorted(owned_pids, reverse=True):
        try:
            _wait_process_gone(pid, timeout_seconds=timeout_seconds)
        except TimeoutError as exc:
            failures.append(str(exc))
    _delete_task(task_name)
    if failures:
        raise RuntimeError(
            f"probe cleanup left Task-owned process alive for {task_name}: {failures}"
        )


def _launch_probe(
    *,
    python: Path,
    task_name: str,
    artifact_root: Path,
    mode: str,
    client_id: int,
    hold_seconds: float = 3600.0,
) -> dict[str, Any]:
    completed = _run(
        [
            str(python),
            str(LAUNCHER),
            "--task-name",
            task_name,
            "--artifact-root",
            str(artifact_root),
            "--client-id",
            str(client_id),
            "--python-exe",
            str(python),
            "--lifecycle-probe",
            mode,
            "--probe-hold-seconds",
            str(hold_seconds),
        ],
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"launcher failed for {mode}: rc={completed.returncode}; "
            f"stdout={completed.stdout[-2000:]}; stderr={completed.stderr[-4000:]}"
        )
    plan = json.loads(completed.stdout)
    if plan.get("lifecycle_probe") != mode:
        raise RuntimeError(f"launcher did not bind lifecycle probe {mode}: {plan}")
    return plan


def _status_paths(plan: dict[str, Any]) -> tuple[Path, Path, Path]:
    runtime = Path(str(plan["runtime_status_path"])).resolve(strict=False)
    stdout = Path(str(plan["stdout_path"])).resolve(strict=False)
    stderr = Path(str(plan["stderr_path"])).resolve(strict=False)
    artifact = Path(str(plan["artifact_root"])).resolve(strict=False)
    for path in (runtime, stdout, stderr):
        try:
            path.relative_to(artifact)
        except ValueError as exc:
            raise RuntimeError(f"probe path escaped artifact root: {path}") from exc
    return runtime, stdout, stderr


def _assert_owned_process(state: dict[str, Any]) -> dict[str, Any]:
    task_pid = int(state["task_action_pid"])
    recorder_pid = int(state["recorder_pid"])
    if task_pid != recorder_pid:
        raise RuntimeError(f"task PID {task_pid} != recorder PID {recorder_pid}")
    process = _process(task_pid)
    if process is None:
        raise RuntimeError(f"recorded Task-owned PID {task_pid} is not alive")
    command_line = str(process.get("CommandLine") or "")
    if HOST.name not in command_line or "--lifecycle-probe" not in command_line:
        raise RuntimeError(f"unexpected Task-owned command line: {command_line}")
    return process


def verify(args: argparse.Namespace) -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("real Windows Task Scheduler is required")
    python = Path(args.python_exe or sys.executable).resolve(strict=True)
    artifact_parent = Path(args.artifact_parent)
    if not artifact_parent.is_absolute():
        artifact_parent = ROOT / artifact_parent
    artifact_parent = artifact_parent.resolve(strict=False)
    allowed = (ROOT / "artifacts" / "ib_preflight").resolve(strict=False)
    try:
        artifact_parent.relative_to(allowed)
    except ValueError as exc:
        raise ValueError(f"artifact parent must stay below {allowed}") from exc
    if artifact_parent == allowed:
        raise ValueError(f"artifact parent must stay below {allowed}")
    artifact_parent.mkdir(parents=True, exist_ok=False)

    suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    results: dict[str, Any] = {}
    task_names: list[str] = []
    task_artifacts: dict[str, Path] = {}
    try:
        for offset, mode in enumerate(("pass", "fail"), 1):
            task_name = f"{args.task_prefix}-{mode}-{suffix}"
            task_names.append(task_name)
            artifact = artifact_parent / mode
            task_artifacts[task_name] = artifact
            plan = _launch_probe(
                python=python,
                task_name=task_name,
                artifact_root=artifact,
                mode=mode,
                client_id=args.client_id + offset,
            )
            runtime, stdout, stderr = _status_paths(plan)
            expected = 0 if mode == "pass" else 2
            state = _wait_for_status(runtime, {"FINALIZED"}, timeout_seconds=args.timeout_seconds)
            info = _wait_for_task_result(
                task_name, expected, timeout_seconds=args.timeout_seconds
            )
            if int(state["task_action_pid"]) != int(state["recorder_pid"]):
                raise RuntimeError(f"{mode}: task and recorder PID differ")
            if not stdout.exists() or not stderr.exists():
                raise RuntimeError(f"{mode}: expected stdout/stderr audit files are missing")
            results[mode] = {
                "plan": plan,
                "runtime_status": state,
                "scheduler": info,
                "expected_exit_code": expected,
                "stdout_bytes": stdout.stat().st_size,
                "stderr_bytes": stderr.stat().st_size,
            }

        task_name = f"{args.task_prefix}-hold-{suffix}"
        task_names.append(task_name)
        artifact = artifact_parent / "hold"
        task_artifacts[task_name] = artifact
        plan = _launch_probe(
            python=python,
            task_name=task_name,
            artifact_root=artifact,
            mode="hold",
            client_id=args.client_id + 3,
            hold_seconds=max(60.0, args.timeout_seconds * 4.0),
        )
        # _launch_probe waits until the launcher itself exits. If the Task-owned
        # PID is still alive now, the task is demonstrably independent of the
        # launcher/Codex-like parent process.
        runtime, stdout, stderr = _status_paths(plan)
        state = _wait_for_status(
            runtime, {"PROBE_HOLDING"}, timeout_seconds=args.timeout_seconds
        )
        process_before = _assert_owned_process(state)
        pid = int(state["task_action_pid"])
        descendants_before = _descendant_processes(pid)
        if descendants_before:
            raise RuntimeError(
                f"Task-owned Recorder host unexpectedly created child processes: {descendants_before}"
            )
        ended = _run(["schtasks.exe", "/End", "/TN", task_name], check=False)
        if ended.returncode != 0:
            raise RuntimeError(
                f"schtasks /End failed: rc={ended.returncode}; stderr={ended.stderr}"
            )
        _wait_process_gone(pid, timeout_seconds=args.timeout_seconds)
        if not stdout.exists() or not stderr.exists():
            raise RuntimeError("hold: expected stdout/stderr audit files are missing")
        results["hold"] = {
            "plan": plan,
            "runtime_status_before_stop": state,
            "process_before_stop": process_before,
            "descendant_processes_before_stop": descendants_before,
            "pid_gone_after_scheduler_stop": True,
            "no_descendant_processes": True,
            "launcher_already_exited_while_task_alive": True,
            "stdout_bytes": stdout.stat().st_size,
            "stderr_bytes": stderr.stat().st_size,
        }

        return {
            "schema_version": 1,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "python": str(python),
            "no_ib": True,
            "read_only": True,
            "task_scheduler_direct_python": True,
            "results": results,
            "passed": True,
        }
    finally:
        cleanup_failures: list[str] = []
        for task_name in reversed(task_names):
            try:
                _cleanup_task(
                    task_name,
                    task_artifacts[task_name],
                    timeout_seconds=args.timeout_seconds,
                )
            except (OSError, RuntimeError, subprocess.SubprocessError, TimeoutError) as exc:
                cleanup_failures.append(f"{task_name}: {type(exc).__name__}: {exc}")
        if cleanup_failures:
            raise RuntimeError(f"Windows lifecycle probe cleanup failed: {cleanup_failures}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact-parent",
        default="artifacts/ib_preflight/full-rth-lifecycle-probe",
    )
    parser.add_argument("--task-prefix", default="ibems-full-rth-lifecycle")
    parser.add_argument("--client-id", type=int, default=9100)
    parser.add_argument("--python-exe")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0:
        parser.error("timeout must be positive")
    if not args.task_prefix.startswith("ibems-full-rth-"):
        parser.error("task prefix must start with ibems-full-rth-")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report_path: Path | None = None
    try:
        report = verify(args)
        artifact_parent = Path(args.artifact_parent)
        if not artifact_parent.is_absolute():
            artifact_parent = ROOT / artifact_parent
        report_path = artifact_parent.resolve(strict=False) / "lifecycle-probe-report.json"
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except (KeyError, OSError, RuntimeError, subprocess.SubprocessError, ValueError, TimeoutError) as exc:
        print(f"lifecycle probe failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
