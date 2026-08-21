"""
Provenance is enforced by tests, not by discipline.

The repository previously carried a hand-maintained ``SHA256SUMS`` and four
prose documents restating validation status. All four drifted apart, and the
checksum file did not match its own worktree. Every check in this file exists
because the corresponding human process demonstrably failed at least once.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from ib_execution import attestation, gate, provenance

ROOT = Path(__file__).resolve().parents[1]


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, timeout=30
    )
    if out.returncode != 0:
        pytest.skip("not a git checkout")
    return [ROOT / line for line in out.stdout.splitlines() if line]


def test_state_json_matches_worktree():
    """STATE must describe both the tree and the currently derivable attestation."""
    stale = provenance.stale_fields(ROOT)
    assert not stale, (
        "STATE.json is stale; regenerate with `python -m ib_execution.provenance`.\n"
        + "\n".join(
            f"  {key}: recorded={recorded!r} actual={actual!r}"
            for key, (recorded, actual) in sorted(stale.items())
        )
    )


def test_b1_freeze_workflow_preserves_attestation_history_and_failure_evidence_roots():
    """The formal campaign must not lose ancestry or mask an early failure."""
    import yaml

    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "b1-freeze-campaign.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = workflow["jobs"]["freeze-campaign"]["steps"]
    checkout = next(step for step in steps if step["name"] == "Check out exact freeze candidate")
    assert checkout["with"]["fetch-depth"] == 0

    record = next(step for step in steps if step["name"] == "Record exact commit")
    script = record["run"]
    for evidence_root in (
        "artifacts/gate_b1",
        "artifacts/gate_b1_storage",
        "artifacts/gate_b1_extra",
    ):
        assert evidence_root in script


def test_state_writer_uses_platform_independent_lf(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "tree_state", lambda root: {"root": str(root)})
    monkeypatch.setattr(provenance, "derived_gate_status", lambda root: {"gate": str(root)})

    data = provenance.write_state(tmp_path).read_bytes()

    assert data.endswith(b"\n")
    assert b"\r" not in data


def test_no_hand_maintained_checksum_file():
    """SHA256SUMS was deleted on purpose. Reintroducing it reintroduces drift."""
    assert not (ROOT / "SHA256SUMS").exists(), (
        "SHA256SUMS is hand-maintained and drifted from the worktree. "
        "Provenance belongs in STATE.json and exact-freeze evidence snapshots."
    )


# IB account identifiers. DU/DF prefixes are paper, U is live.
ACCOUNT_PATTERNS = (
    re.compile(r"\bD[UF][A-Z]?\d{6,}\b"),
    re.compile(r"\bU\d{7,}\b"),
)
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(password|passwd|api[_-]?key|secret|token)\s*[:=]\s*['\"][^'\"]{6,}"),
)
TEXT_SUFFIXES = {".py", ".md", ".txt", ".yml", ".yaml", ".toml", ".json", ".cfg", ".ini"}
SENSITIVE_SCAN_EXEMPT = {"tests/test_provenance.py"}
ALLOW_MARKER = re.compile(r"provenance-allow:\s*(\S.*)$")


@pytest.mark.parametrize("pattern", ACCOUNT_PATTERNS + SECRET_PATTERNS, ids=lambda p: p.pattern[:40])
def test_no_sensitive_data_in_tracked_files(pattern):
    offenders = []
    for path in tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in SENSITIVE_SCAN_EXEMPT or path.suffix not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line) and not ALLOW_MARKER.search(line):
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        f"sensitive data matching {pattern.pattern} at: {offenders}\n"
        "If this is a documentation placeholder, append `provenance-allow: <reason>`."
    )


def test_dependency_lock_is_present_and_recorded():
    assert (ROOT / "uv.lock").exists(), "uv.lock is a Gate B1.0 blocker"
    assert provenance.dependency_lock_sha256(ROOT) is not None


def test_direct_dependencies_are_exactly_pinned():
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    specs: list[str] = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        specs.extend(extra)

    assert specs, "no dependencies declared"
    for spec in specs:
        assert "==" in spec and ">=" not in spec, (
            f"{spec!r} is not exactly pinned. A recorded Hypothesis seed only "
            "reproduces within one Hypothesis version, and every Gate B2 "
            "observation is a claim about one exact ib_async implementation."
        )
    named = {re.split(r"[=<>!~ ]", spec, maxsplit=1)[0].lower() for spec in specs}
    assert {"pyyaml", "ib_async", "pyarrow", "pytest", "hypothesis"} <= named
    assert project["requires-python"] == ">=3.12,<3.13"


# --------------------------------------------------------------------------
# Gate B1 requirement registry and attestation
# --------------------------------------------------------------------------


def test_state_json_gate_status_is_derived_from_the_registry():
    state = provenance.load_state(ROOT)
    assert state is not None
    recorded = [r["id"] for r in state["gate_status"]["requirements"]]
    assert recorded == gate.requirement_ids()


def test_the_signoff_template_covers_exactly_the_registry():
    text = (ROOT / "docs" / "GATE_B1_SIGNOFF_TEMPLATE.md").read_text(encoding="utf-8")
    rows = set(re.findall(r"^\|\s*(B1\.\w+)\s*\|", text, re.MULTILINE))
    assert rows == set(gate.requirement_ids()), (
        f"sign-off template and gate.B1_REQUIREMENTS disagree: "
        f"template-only={sorted(rows - set(gate.requirement_ids()))}, "
        f"registry-only={sorted(set(gate.requirement_ids()) - rows)}"
    )


def test_every_requirement_status_is_valid_and_evidenced():
    for requirement in gate.requirements():
        assert requirement.status in gate.VALID_STATUSES
        assert requirement.evidence, f"{requirement.id} claims no evidence"
        if requirement.status != gate.READY_FOR_FREEZE:
            assert requirement.note, f"{requirement.id} is {requirement.status} without a note"


def test_ready_for_freeze_requires_every_requirement_complete():
    assert gate.ready_for_freeze() == (not gate.open_requirements())


def test_gate_state_requires_a_derived_signed_commit_for_pass():
    unsigned = gate.as_state()
    assert unsigned["gate_b1_attested_freeze"] is None
    assert unsigned["gate_b1_covers_worktree"] is False

    freeze = "a" * 40
    signed = gate.as_state(freeze, freeze)
    assert signed["gate_b1_attested_freeze"] == freeze
    if gate.ready_for_freeze():
        assert signed["gate_b1_covers_worktree"] is True
    else:
        assert signed["gate_b1_covers_worktree"] is False


BLOCKER_COUNT = re.compile(r"(?<![B\w.])(\d+)\s*(?:项\s*)?blockers?\b")


def test_docs_agree_with_the_registry_on_how_many_blockers_there_are():
    expected = len(gate.requirement_ids())
    for name in ("README.md", "docs/IMPLEMENTATION_STATUS.md", "docs/INVARIANT_COVERAGE.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        counts = [int(c) for c in BLOCKER_COUNT.findall(text)]
        assert all(c == expected for c in counts), (
            f"{name} states blocker counts {counts}; the registry has {expected}"
        )


def test_gate_b1_history_and_worktree_coverage_are_independently_derived():
    state = provenance.load_state(ROOT)
    assert state is not None
    status = state["gate_status"]
    assert provenance.derived_gate_status(ROOT) == status
    freeze = status.get("gate_b1_attested_freeze")
    if freeze is not None:
        historical = attestation.validate_historical(ROOT, freeze)
        assert historical is not None
        assert historical.freeze_commit == freeze

    if status["gate_b1_covers_worktree"]:
        assert freeze is not None
        assert all(r.status == gate.READY_FOR_FREEZE for r in gate.requirements())
        assert attestation.validate(ROOT, freeze) is not None


GATE_PASS_CLAIMS = re.compile(
    r"(?i)(gate\s*b1\s*[:=]\s*pass"
    r"|gate\s*b1\s+pass(ed)?\b"
    r"|gate\s*b1\s*已通过"
    r"|b1\s*已\s*正式通过)"
)


def test_docs_do_not_restate_gate_status_by_hand():
    state = provenance.load_state(ROOT)
    assert state is not None
    if state["gate_status"]["gate_b1_attested_freeze"] is not None:
        return
    offenders = []
    for path in tracked_files():
        if path.suffix != ".md" or not path.exists():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if GATE_PASS_CLAIMS.search(line) and not ALLOW_MARKER.search(line):
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{lineno}")
    assert not offenders, (
        f"docs claim Gate B1 passed while STATE.json records "
        f"no historical attested freeze: {offenders}"
    )


def test_resolved_environment_is_observable():
    env = provenance.resolved_environment()
    assert any(item.startswith("pytest==") for item in env)
    assert provenance.resolved_environment_sha256() == provenance.resolved_environment_sha256()
