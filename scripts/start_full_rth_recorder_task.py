"""Launch the read-only Full-RTH Recorder outside the Codex AppX lifecycle.

The task is registered without triggers and started explicitly.  It never
restarts the Recorder and never changes broker or order authorization.
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
        "ibems read-only Full-RTH Recorder; no restart and no broker writes"
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
        "ExecutionTimeLimit": f"PT{plan['execution_time_limit_hours']}H",
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
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
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
        raise ValueError(
            "task name must match ibems-full-rth-[A-Za-z0-9_.-]{1,64}"
        )
    if not 1 <= args.client_id <= 2_147_483_647:
        raise ValueError("client id is outside the supported range")
    if not 1 <= args.port <= 65_535:
        raise ValueError("port is outside the supported range")
    if not 1 <= args.execution_hours <= 24:
        raise ValueError("execution hours must be between 1 and 24")

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

    python = Path(args.python_exe or repo / ".venv312" / "python.exe").resolve(
        strict=True
    )
    artifact_root = Path(args.artifact_root)
    if not artifact_root.is_absolute():
        artifact_root = repo / artifact_root
    artifact_root = artifact_root.resolve(strict=False)
    allowed_root = (repo / "artifacts" / "ib_preflight").resolve(strict=False)
    if not _within(artifact_root, allowed_root) or artifact_root == allowed_root:
        raise ValueError(f"artifact root must stay below {allowed_root}")

    raw_root = artifact_root / "raw"
    status_path = (
        Path(args.status_path)
        if args.status_path
        else artifact_root / "recorder-status.json"
    )
    if not status_path.is_absolute():
        status_path = repo / status_path
    status_path = status_path.resolve(strict=False)
    if not _within(status_path, artifact_root):
        raise ValueError("status path must stay under artifact root")

    stdout_path = artifact_root / "recorder-stdout.log"
    stderr_path = artifact_root / "recorder-stderr.log"
    recorder_arguments = [
        "-m",
        "ib_execution.quote_recorder",
        "--root",
        str(raw_root),
        "--port",
        str(args.port),
        "--client-id",
        str(args.client_id),
        "--status-path",
        str(status_path),
    ]
    command = subprocess.list2cmdline([str(python), *recorder_arguments])
    command += f" 1>>{subprocess.list2cmdline([str(stdout_path)])}"
    command += f" 2>>{subprocess.list2cmdline([str(stderr_path)])}"
    comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")

    return {
        "schema_version": 1,
        "launcher": "WINDOWS_TASK_SCHEDULER",
        "purpose": "read-only Full-RTH QuoteRecorder outside the Codex AppX lifecycle",
        "task_name": args.task_name,
        "principal": _windows_identity(),
        "run_level": "LIMITED",
        "working_directory": str(repo),
        "execute": comspec,
        "arguments": f'/D /S /C "{command}"',
        "python": str(python),
        "recorder_arguments": recorder_arguments,
        "artifact_root": str(artifact_root),
        "status_path": str(status_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "execution_time_limit_hours": args.execution_hours,
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
    parser.add_argument("--status-path")
    parser.add_argument("--execution-hours", default=8, type=int)
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
        print(
            f"refusing to launch: scheduled task already exists: {args.task_name}",
            file=sys.stderr,
        )
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
