"""Register and start the read-only Full-RTH Recorder as a direct Python task.

Task Scheduler owns the actual Python process. There is no shell parent and no
child Python process. The task host computes its deadline from the actual IB
RTH session and enforces ``RTH close + 3h finalize + 30m safety``.

For acceptance outside RTH, ``--lifecycle-probe`` passes a no-IB probe mode to
the identical Task-owned host process.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import hashlib
import json
import locale
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from xml.etree import ElementTree as ET

TASK_NAME_RE = re.compile(r"^ibems-full-rth-[A-Za-z0-9_.-]{1,64}$")
TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
CREATE_NO_WINDOW = 0x08000000
SCHEDULER_BACKSTOP = "PT24H"


def _windows_identity() -> str:
    domain = os.environ.get("USERDOMAIN", "").strip()
    user = os.environ.get("USERNAME", "").strip() or getpass.getuser()
    return f"{domain}\\{user}" if domain else user


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _task_xml(plan: dict[str, object]) -> bytes:
    ET.register_namespace("", TASK_NS)
    task = ET.Element(f"{{{TASK_NS}}}Task", {"version": "1.4"})
    registration = ET.SubElement(task, f"{{{TASK_NS}}}RegistrationInfo")
    ET.SubElement(registration, f"{{{TASK_NS}}}Description").text = (
        "ibems read-only Full-RTH Recorder; direct Python ownership; no restart"
    )
    ET.SubElement(task, f"{{{TASK_NS}}}Triggers")

    principals = ET.SubElement(task, f"{{{TASK_NS}}}Principals")
    principal = ET.SubElement(principals, f"{{{TASK_NS}}}Principal", {"id": "Author"})
    ET.SubElement(principal, f"{{{TASK_NS}}}UserId").text = str(plan["principal"])
    ET.SubElement(principal, f"{{{TASK_NS}}}LogonType").text = "InteractiveToken"
    ET.SubElement(principal, f"{{{TASK_NS}}}RunLevel").text = "LeastPrivilege"

    settings = ET.SubElement(task, f"{{{TASK_NS}}}Settings")
    values = {
        "MultipleInstancesPolicy": "IgnoreNew",
        "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false",
        "AllowHardTerminate": "true",
        "StartWhenAvailable": "true",
        "RunOnlyIfNetworkAvailable": "false",
        "Enabled": "true",
        "Hidden": "false",
        # The directly-owned Python host enforces the precise IB session close
        # + 3h30m deadline. Scheduler retains a deliberately wider independent
        # backstop so interpreter/import/status-publication hangs that happen
        # before the in-process watchdog starts are still bounded.
        "ExecutionTimeLimit": SCHEDULER_BACKSTOP,
        "Priority": "7",
    }
    for name, value in values.items():
        ET.SubElement(settings, f"{{{TASK_NS}}}{name}").text = value

    actions = ET.SubElement(task, f"{{{TASK_NS}}}Actions", {"Context": "Author"})
    execute = ET.SubElement(actions, f"{{{TASK_NS}}}Exec")
    ET.SubElement(execute, f"{{{TASK_NS}}}Command").text = str(plan["execute"])
    ET.SubElement(execute, f"{{{TASK_NS}}}Arguments").text = str(plan["arguments"])
    ET.SubElement(execute, f"{{{TASK_NS}}}WorkingDirectory").text = str(
        plan["working_directory"]
    )
    return ET.tostring(task, encoding="utf-16", xml_declaration=True)


def _run_schtasks(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["schtasks.exe", *arguments],
        capture_output=True,
        text=True,
        encoding=locale.getpreferredencoding(False),
        errors="replace",
        check=check,
        creationflags=CREATE_NO_WINDOW,
    )


def _write_atomic_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)


def _git_output(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.rstrip()


def build_plan(args: argparse.Namespace) -> dict[str, object]:
    if not TASK_NAME_RE.fullmatch(args.task_name):
        raise ValueError("task name must match ibems-full-rth-[A-Za-z0-9_.-]{1,64}")
    if not 1 <= args.client_id <= 2_147_483_647:
        raise ValueError("client id is outside the supported range")
    if not 1 <= args.port <= 65_535:
        raise ValueError("port is outside the supported range")
    if args.probe_hold_seconds <= 0:
        raise ValueError("probe hold seconds must be positive")

    repo = Path(__file__).resolve().parents[1]
    state = json.loads((repo / "STATE.json").read_text(encoding="utf-8"))
    gate = state["gate_status"]
    required = {
        "gate_b2": "READ_ONLY_IN_PROGRESS",
        "order_authorization": "NONE",
        "trading_adapter": "NOT_IMPLEMENTED",
    }
    for key, expected in required.items():
        if gate.get(key) != expected:
            raise RuntimeError(f"STATE.json {key} must be {expected}")

    python = Path(args.python_exe or repo / ".venv312" / "python.exe").resolve(strict=True)
    host_script = (repo / "scripts" / "run_full_rth_recorder_task.py").resolve(strict=True)

    artifact_root = Path(args.artifact_root)
    if not artifact_root.is_absolute():
        artifact_root = repo / artifact_root
    artifact_root = artifact_root.resolve(strict=False)
    allowed_root = (repo / "artifacts" / "ib_preflight").resolve(strict=False)
    if not _within(artifact_root, allowed_root) or artifact_root == allowed_root:
        raise ValueError(f"artifact root must stay below {allowed_root}")

    raw_root = artifact_root / "raw"
    recorder_status = artifact_root / "recorder-status.json"
    runtime_status = artifact_root / "task-runtime-status.json"
    stdout_path = artifact_root / "recorder-stdout.log"
    stderr_path = artifact_root / "recorder-stderr.log"

    host_arguments = [
        str(host_script),
        "--root",
        str(raw_root),
        "--port",
        str(args.port),
        "--client-id",
        str(args.client_id),
        "--recorder-status",
        str(recorder_status),
        "--runtime-status",
        str(runtime_status),
        "--stdout",
        str(stdout_path),
        "--stderr",
        str(stderr_path),
    ]
    if args.lifecycle_probe is not None:
        host_arguments.extend(
            [
                "--lifecycle-probe",
                str(args.lifecycle_probe),
                "--probe-hold-seconds",
                str(float(args.probe_hold_seconds)),
            ]
        )

    return {
        "schema_version": 2,
        "launcher": "WINDOWS_TASK_SCHEDULER",
        "purpose": "read-only Full-RTH QuoteRecorder outside the Codex AppX lifecycle",
        "task_name": args.task_name,
        "principal": _windows_identity(),
        "run_level": "LIMITED",
        "working_directory": str(repo),
        "execute": str(python),
        "arguments": subprocess.list2cmdline(host_arguments),
        "python": str(python),
        "task_host_script": str(host_script),
        "task_host_arguments": host_arguments,
        "artifact_root": str(artifact_root),
        "raw_root": str(raw_root),
        "recorder_status_path": str(recorder_status),
        "runtime_status_path": str(runtime_status),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "process_ownership": "TASK_SCHEDULER_DIRECT_PYTHON_SAME_PROCESS_RECORDER",
        "scheduler_execution_time_limit": SCHEDULER_BACKSTOP,
        "scheduler_backstop_role": "INDEPENDENT_PRE_WATCHDOG_AND_PROCESS_HANG_BOUND",
        "dynamic_deadline_rule": "RTH_CLOSE_PLUS_3H_FINALIZE_PLUS_30M_SAFETY",
        "lifecycle_probe": args.lifecycle_probe,
        "allow_start_on_battery": True,
        "stop_on_battery": False,
        "multiple_instances": "IGNORE_NEW",
        "auto_restart": False,
        "operating_mode": "READ_ONLY",
        "order_authorization": "NONE",
        "trading_adapter": "NOT_IMPLEMENTED",
        "git_commit": _git_output(repo, "rev-parse", "HEAD"),
        "git_branch": _git_output(repo, "branch", "--show-current"),
        "git_worktree_changes": _git_output(repo, "status", "--porcelain").splitlines(),
        "source_tree_sha256": state["tree"]["source_tree_sha256"],
        "config_tree_sha256": state["tree"]["config_tree_sha256"],
        "dependency_lock_sha256": state["tree"]["dependency_lock_sha256"],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-name", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--client-id", required=True, type=int)
    parser.add_argument("--port", default=4002, type=int)
    parser.add_argument("--python-exe")
    parser.add_argument(
        "--lifecycle-probe",
        choices=("pass", "fail", "hold"),
        help="start the Task-owned host without connecting to IB",
    )
    parser.add_argument("--probe-hold-seconds", type=float, default=3600.0)
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan = build_plan(args)
    except (KeyError, OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        print(f"refusing to launch: {exc}", file=sys.stderr)
        return 2

    if args.validate_only:
        plan["validation_only"] = True
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    if os.name != "nt":
        print("refusing to launch: Windows Task Scheduler is required", file=sys.stderr)
        return 2

    query = _run_schtasks("/Query", "/TN", args.task_name, check=False)
    if query.returncode == 0:
        print(f"refusing to launch: scheduled task already exists: {args.task_name}", file=sys.stderr)
        return 3

    artifact_root = Path(str(plan["artifact_root"]))
    artifact_root.mkdir(parents=True, exist_ok=False)
    (artifact_root / "raw").mkdir()
    xml_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as handle:
            xml_path = Path(handle.name)
            task_xml = _task_xml(plan)
            plan["task_xml_sha256"] = hashlib.sha256(task_xml).hexdigest()
            handle.write(task_xml)
        _run_schtasks("/Create", "/TN", args.task_name, "/XML", str(xml_path))
        plan["registration_state"] = "REGISTERED"
        plan["registered_at_utc"] = datetime.now(timezone.utc).isoformat()
        _write_atomic_json(artifact_root / "task-launch.json", plan)
        _run_schtasks("/Run", "/TN", args.task_name)
        plan["registration_state"] = "START_REQUESTED"
        plan["started_at_utc"] = datetime.now(timezone.utc).isoformat()
        _write_atomic_json(artifact_root / "task-launch.json", plan)
    except (OSError, subprocess.CalledProcessError) as exc:
        plan["registration_state"] = "FAILED"
        plan["failure"] = str(exc)
        _write_atomic_json(artifact_root / "task-launch.json", plan)
        print(f"task launch failed: {exc}", file=sys.stderr)
        return 4
    finally:
        if xml_path is not None:
            xml_path.unlink(missing_ok=True)

    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
