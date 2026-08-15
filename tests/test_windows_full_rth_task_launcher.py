from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "start_full_rth_recorder_task.py"
HOST = ROOT / "scripts" / "run_full_rth_recorder_task.py"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("full_rth_task_launcher", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load launcher: {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launcher_declares_direct_fail_closed_hosting_contract():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "WINDOWS_TASK_SCHEDULER" in source
    assert '"InteractiveToken"' in source
    assert '"LeastPrivilege"' in source
    assert '"DisallowStartIfOnBatteries": "false"' in source
    assert '"StopIfGoingOnBatteries": "false"' in source
    assert '"auto_restart": False' in source
    assert '"order_authorization": "NONE"' in source
    assert '"trading_adapter": "NOT_IMPLEMENTED"' in source
    assert "Start-Process" not in source
    assert "COMSPEC" not in source
    assert "TASK_SCHEDULER_DIRECT_PYTHON_SAME_PROCESS_RECORDER" in source


def test_task_xml_has_no_restart_and_delegates_deadline_to_owned_python():
    launcher = _load_launcher()
    plan = {
        "principal": "HOST\\user",
        "execute": sys.executable,
        "arguments": f'"{HOST}" --probe',
        "working_directory": str(ROOT),
    }
    root = ET.fromstring(launcher._task_xml(plan))
    ns = {"t": launcher.TASK_NS}

    assert root.findtext(".//t:LogonType", namespaces=ns) == "InteractiveToken"
    assert root.findtext(".//t:RunLevel", namespaces=ns) == "LeastPrivilege"
    assert root.findtext(".//t:ExecutionTimeLimit", namespaces=ns) == "PT0S"
    assert root.findtext(".//t:MultipleInstancesPolicy", namespaces=ns) == "IgnoreNew"
    assert root.find(".//t:RestartOnFailure", namespaces=ns) is None
    assert root.findtext(".//t:Command", namespaces=ns) == sys.executable


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Task Scheduler launcher")
def test_validate_only_builds_direct_readonly_python_task_without_registering_it():
    artifact_root = ROOT / "artifacts" / "ib_preflight" / "launcher-validation-only"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--task-name",
            "ibems-full-rth-validation-only",
            "--artifact-root",
            str(artifact_root),
            "--client-id",
            "1999",
            "--python-exe",
            sys.executable,
            "--validate-only",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    plan = json.loads(completed.stdout)
    assert plan["launcher"] == "WINDOWS_TASK_SCHEDULER"
    assert plan["validation_only"] is True
    assert plan["run_level"] == "LIMITED"
    assert plan["auto_restart"] is False
    assert plan["operating_mode"] == "READ_ONLY"
    assert plan["order_authorization"] == "NONE"
    assert plan["execute"] == str(Path(sys.executable).resolve())
    assert plan["task_host_script"] == str(HOST.resolve())
    assert plan["process_ownership"] == "TASK_SCHEDULER_DIRECT_PYTHON_SAME_PROCESS_RECORDER"
    assert plan["scheduler_execution_time_limit"] == "PT0S"
    assert plan["dynamic_deadline_rule"] == "RTH_CLOSE_PLUS_3H_FINALIZE_PLUS_30M_SAFETY"
    assert plan["artifact_root"] == str(artifact_root.resolve())
    expected_status = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.rstrip().splitlines()
    assert plan["git_worktree_changes"] == expected_status
    assert not artifact_root.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Task Scheduler launcher")
def test_validate_only_rejects_artifacts_outside_the_audit_root(tmp_path):
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--task-name",
            "ibems-full-rth-invalid-root",
            "--artifact-root",
            str(tmp_path),
            "--client-id",
            "1999",
            "--python-exe",
            sys.executable,
            "--validate-only",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode != 0
    assert "artifact root must stay below" in completed.stderr
