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
VERIFIER = ROOT / "scripts" / "verify_windows_full_rth_task_lifecycle.py"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("full_rth_task_launcher", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load launcher: {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_verifier():
    spec = importlib.util.spec_from_file_location("full_rth_task_verifier", VERIFIER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load verifier: {VERIFIER}")
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


def test_task_xml_has_no_restart_and_keeps_an_independent_bounded_backstop():
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
    assert root.findtext(".//t:ExecutionTimeLimit", namespaces=ns) == "PT24H"
    assert root.findtext(".//t:MultipleInstancesPolicy", namespaces=ns) == "IgnoreNew"
    assert root.find(".//t:RestartOnFailure", namespaces=ns) is None
    assert root.findtext(".//t:Command", namespaces=ns) == sys.executable


def test_lifecycle_probe_cleanup_ends_task_before_delete_and_waits_for_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = _load_verifier()
    artifact = tmp_path / "probe"
    artifact.mkdir()
    (artifact / "task-runtime-status.json").write_text(
        json.dumps({"task_action_pid": 101, "recorder_pid": 101}), encoding="utf-8"
    )
    calls: list[tuple[str, int | str]] = []
    monkeypatch.setattr(
        verifier,
        "_process",
        lambda pid: {"ProcessId": pid, "CommandLine": str(HOST)},
    )
    monkeypatch.setattr(
        verifier,
        "_descendant_processes",
        lambda pid: [{"ProcessId": 202, "ParentProcessId": pid}],
    )
    monkeypatch.setattr(verifier, "_end_task", lambda name: calls.append(("end", name)))
    monkeypatch.setattr(verifier, "_delete_task", lambda name: calls.append(("delete", name)))
    monkeypatch.setattr(
        verifier,
        "_wait_process_gone",
        lambda pid, timeout_seconds: calls.append(("wait", pid)),
    )

    verifier._cleanup_task("ibems-full-rth-test", artifact, timeout_seconds=1.0)

    assert calls[0] == ("end", "ibems-full-rth-test")
    assert set(calls[1:3]) == {("wait", 101), ("wait", 202)}
    assert calls[-1] == ("delete", "ibems-full-rth-test")


def test_lifecycle_allows_only_one_direct_system_console_host() -> None:
    verifier = _load_verifier()
    console = {
        "ProcessId": 202,
        "ParentProcessId": 101,
        "ExecutablePath": r"C:\Windows\System32\conhost.exe",
        "CommandLine": r"\??\C:\Windows\System32\conhost.exe 0x4",
    }
    application_child = {
        "ProcessId": 303,
        "ParentProcessId": 101,
        "ExecutablePath": r"C:\Windows\System32\cmd.exe",
        "CommandLine": r"C:\Windows\System32\cmd.exe /c echo unsafe",
    }
    grandchild_console = {**console, "ProcessId": 404, "ParentProcessId": 303}

    expected, unexpected = verifier._classify_descendants(
        101, [console, application_child, grandchild_console]
    )

    assert expected == [console]
    assert unexpected == [application_child, grandchild_console]


def test_lifecycle_failure_is_preserved_as_small_unique_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = _load_verifier()

    def fail(_args):
        raise RuntimeError("causal failure")

    monkeypatch.setattr(verifier, "verify", fail)

    assert (
        verifier.main(
            [
                "--artifact-parent",
                str(tmp_path),
                "--task-prefix",
                "ibems-full-rth-test",
            ]
        )
        == 2
    )
    reports = list(tmp_path.glob("lifecycle-probe-failure-*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["passed"] is False
    assert payload["error_type"] == "RuntimeError"
    assert payload["error"] == "causal failure"
    assert reports[0].stat().st_size < 4096


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
    assert plan["scheduler_execution_time_limit"] == "PT24H"
    assert plan["scheduler_backstop_role"] == (
        "INDEPENDENT_PRE_WATCHDOG_AND_PROCESS_HANG_BOUND"
    )
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
