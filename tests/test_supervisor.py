"""
Gate B1.3a: the production restart policy is part of the evidence.

Proving in a test that the child exits non-zero says nothing about production
if production is configured to restart it. The supervisor configuration is
therefore checked in and parsed here, so a change to the restart policy has to
break a test rather than a session.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ib_execution import execution_host

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_UNIT = ROOT / "deploy" / "ibems-execution.service"
WINDOWS_SCRIPT = ROOT / "deploy" / "ibems-execution-service.ps1"


def _systemd_directives() -> dict[str, str]:
    """Last-wins, the way systemd itself resolves repeated directives."""
    out: dict[str, str] = {}
    for line in SYSTEMD_UNIT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "[")):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def test_the_supervisor_configuration_is_checked_in():
    assert SYSTEMD_UNIT.exists(), "Gate B1.3a requires the frozen production unit file"
    assert WINDOWS_SCRIPT.exists(), "the Windows runtime needs its policy captured too"


def test_systemd_never_restarts_the_execution_host():
    """Restart=on-failure turns every fail-closed exit into a broker reconnect loop."""
    assert _systemd_directives().get("Restart") == "no"


def test_windows_service_exits_rather_than_restarting():
    text = WINDOWS_SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"nssm set \$Service AppExit Default Exit", text)
    assert "AppExit Default Restart" not in text
    # The SCM's own recovery actions default to restarting and would override
    # the NSSM setting above.
    assert re.search(r"sc\.exe failure \$Service reset= 0 actions= \"\"", text)


def test_journal_and_fence_are_deployed_to_different_volumes():
    """The fence has to outlive the journal's volume filling up.

    The host also enforces this at runtime via st_dev, but a deployment that
    only passes because of the runtime check is one restart away from an
    operator "fixing" it by moving the fence.
    """
    directives = _systemd_directives()
    journal = directives["Environment"] if "Environment" in directives else ""
    text = SYSTEMD_UNIT.read_text(encoding="utf-8")
    journal_path = re.search(r"IBEMS_JOURNAL=(\S+)", text).group(1)
    fence_path = re.search(r"IBEMS_FENCE=(\S+)", text).group(1)
    assert journal_path != fence_path
    assert Path(journal_path).parts[:3] != Path(fence_path).parts[:3], (
        f"{journal_path} and {fence_path} share a top-level path; they must be "
        "on different volumes"
    )
    assert journal  # the unit does declare Environment= directives

    windows = WINDOWS_SCRIPT.read_text(encoding="utf-8")
    win_journal = re.search(r"\$Journal\s*=\s*'([^']+)'", windows).group(1)
    win_fence = re.search(r"\$Fence\s*=\s*'([^']+)'", windows).group(1)
    assert win_journal[:2].upper() != win_fence[:2].upper(), (
        f"{win_journal} and {win_fence} are on the same Windows drive"
    )


def test_the_unit_documents_every_exit_code_the_host_can_return():
    """An operator reading the unit should not have to guess what a code means."""
    text = SYSTEMD_UNIT.read_text(encoding="utf-8")
    for name in dir(execution_host):
        if not name.startswith("EXIT_") or name == "EXIT_OK":
            continue
        code = getattr(execution_host, name)
        assert re.search(rf"^#\s+{code}\s", text, re.MULTILINE), (
            f"{name}={code} is undocumented in {SYSTEMD_UNIT.name}"
        )


def test_stop_is_given_time_to_be_a_clean_stop():
    """A stop must not look like a crash: a killed host leaves no clean finalize."""
    assert int(_systemd_directives()["TimeoutStopSec"]) >= 30


@pytest.mark.parametrize("path", [SYSTEMD_UNIT, WINDOWS_SCRIPT])
def test_deployment_files_carry_no_credentials(path):
    text = path.read_text(encoding="utf-8").casefold()
    for token in ("password", "passwd", "api_key", "secret"):
        assert token not in text, f"{path.name} must never carry credentials"
