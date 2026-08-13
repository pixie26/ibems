from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "start_full_rth_recorder_task.py"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("full_rth_task_launcher", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load launcher: {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launcher_declares_independent_fail_closed_hosting_contract():
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


def test_task_xml_has_no_restart_and_has_bounded_limited_execution():
    launcher = _load_launcher()
    plan = {
        "principal": "HOST\\user",
        "execution_time_limit_hours": 8,
        "execute": r"C:\Windows\System32\cmd.exe",
        "arguments": "/D /S /C echo probe",
        "working_directory": str(ROOT),
    }
    root = ET.fromstring(launcher._task_xml(plan))
    ns = {"t": launcher.TASK_NS}

    assert root.findtext(".//t:LogonType", namespaces=ns) == "InteractiveToken"
    assert root.findtext(".//t:RunLevel", namespaces=ns) == "LeastPrivilege"
    assert root.findtext(".//t:ExecutionTimeLimit", namespaces=ns) == "PT8H"
    assert root.findtext(".//t:MultipleInstancesPolicy", namespaces=ns) == "IgnoreNew"
    assert root.find(".//t:RestartOnFailure", namespaces=ns) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Task Scheduler launcher")
def test_validate_only_builds_readonly_task_without_registering_it():
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
    assert plan["recorder_arguments"][:2] == ["-m", "ib_execution.quote_recorder"]
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
