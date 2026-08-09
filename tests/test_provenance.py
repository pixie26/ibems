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

from ib_execution import gate, provenance

ROOT = Path(__file__).resolve().parents[1]


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, timeout=30
    )
    if out.returncode != 0:
        pytest.skip("not a git checkout")
    return [ROOT / line for line in out.stdout.splitlines() if line]


def test_state_json_matches_worktree():
    """The one machine-readable state file must describe the tree it ships with."""
    stale = provenance.stale_fields(ROOT)
    assert not stale, (
        "STATE.json is stale; regenerate with `python -m ib_execution.provenance`.\n"
        + "\n".join(
            f"  {key}: recorded={recorded!r} actual={actual!r}"
            for key, (recorded, actual) in sorted(stale.items())
        )
    )


def test_no_hand_maintained_checksum_file():
    """SHA256SUMS was deleted on purpose. Reintroducing it reintroduces the drift.

    A repository-wide checksum manifest cannot be kept correct by hand, and a
    wrong one is worse than none: it looks like provenance. Gate manifests are
    generated per campaign and record what actually ran.
    """
    assert not (ROOT / "SHA256SUMS").exists(), (
        "SHA256SUMS is hand-maintained and drifted from the worktree. "
        "Provenance belongs in STATE.json and artifacts/gate_b1/*/manifest.json, "
        "both generated."
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
# This file necessarily contains the patterns it searches for.
SENSITIVE_SCAN_EXEMPT = {"tests/test_provenance.py"}

# An exemption must be written down next to the line it exempts, with a reason.
# A hidden allowlist inside the test would be exactly the kind of unreviewable
# control this file exists to replace.
ALLOW_MARKER = re.compile(r"provenance-allow:\s*(\S.*)$")


@pytest.mark.parametrize("pattern", ACCOUNT_PATTERNS + SECRET_PATTERNS, ids=lambda p: p.pattern[:40])
def test_no_sensitive_data_in_tracked_files(pattern):
    """Operational account identifiers and credentials never enter the repository.

    A paper account number is low-impact, but a public repository is a public
    repository; the control has to exist before the day it matters. This does
    not clean history -- see the Gate B1 sign-off, which records that incident
    honestly rather than rewriting every commit sha the sign-off depends on.

    To exempt a documentation placeholder, put ``provenance-allow: <reason>``
    on the same line.
    """
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
    """Gate B1.0: a recorded Hypothesis seed only reproduces within one version."""
    assert (ROOT / "uv.lock").exists(), "uv.lock is a Gate B1.0 blocker"
    assert provenance.dependency_lock_sha256(ROOT) is not None


def test_direct_dependencies_are_exactly_pinned():
    """Gate B1.0. Read the parsed table, not the prose -- comments explain the rule."""
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
# the Gate B1 requirement registry
# --------------------------------------------------------------------------


def test_state_json_gate_status_is_derived_from_the_registry():
    """The drift that this whole module exists to prevent, reproduced inside it.

    B1.6 was added to the registry and to three prose documents, and STATE.json
    -- the file the README calls authoritative -- kept reporting seven
    blockers, because ``write_state`` carried the old ``gate_status`` forward
    and ``stale_fields`` only compared tree hashes.
    """
    state = provenance.load_state(ROOT)
    assert state is not None
    recorded = [r["id"] for r in state["gate_status"]["requirements"]]
    assert recorded == gate.requirement_ids()


def test_the_signoff_template_covers_exactly_the_registry():
    """A blocker that is not in the template cannot be signed off."""
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


# "8 项 blocker" / "8 blockers", but not the "1" inside "Gate B1 blockers".
BLOCKER_COUNT = re.compile(r"(?<![B\w.])(\d+)\s*(?:项\s*)?blockers?\b")


def test_docs_agree_with_the_registry_on_how_many_blockers_there_are():
    """The blocker count is copied into prose in three places, and drifted once."""
    expected = len(gate.requirement_ids())
    for name in ("README.md", "docs/IMPLEMENTATION_STATUS.md", "docs/INVARIANT_COVERAGE.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        counts = [int(c) for c in BLOCKER_COUNT.findall(text)]
        assert all(c == expected for c in counts), (
            f"{name} states blocker counts {counts}; the registry has {expected}"
        )


def _signoff_table_value(text: str, field: str) -> str:
    match = re.search(
        rf"^\|\s*{re.escape(field)}\s*\|\s*(.*?)\s*\|\s*$",
        text,
        re.MULTILINE,
    )
    return match.group(1).strip() if match else ""


def test_gate_b1_is_not_claimed_passed_without_a_valid_freeze_attestation():
    """PASS binds tested code, while a later attestation commit may record the proof.

    A git commit cannot contain its own hash. Therefore requiring the signed-off
    freeze SHA to equal the commit that *contains* the signature is impossible:
    writing STATE/sign-off changes HEAD. The safe model is two identities:

    * signed_off_commit = exact commit exercised by the frozen campaign;
    * current HEAD = an attestation descendant that may change only STATE.json
      and that frozen commit's sign-off document.

    Any behaviour/config/dependency/test edit between them invalidates PASS.
    """
    state = provenance.load_state(ROOT)
    assert state is not None
    status = state["gate_status"]
    if status["gate_b1"] != "PASS":
        return

    freeze = status.get("signed_off_commit")
    assert freeze, "Gate B1 PASS requires an exact frozen signed_off_commit"
    assert all(r.status == gate.READY_FOR_FREEZE for r in gate.requirements()), (
        "Gate B1 PASS requires every registry requirement READY_FOR_FREEZE"
    )

    ancestor = subprocess.run(
        ["git", "-C", str(ROOT), "merge-base", "--is-ancestor", freeze, "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ancestor.returncode == 0, f"signed_off_commit {freeze} is not an ancestor of HEAD"

    signoff_rel = f"docs/GATE_B1_SIGNOFF_{freeze[:12]}.md"
    allowed = {"STATE.json", signoff_rel}
    diff = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--name-only", f"{freeze}..HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    changed = {line for line in diff.stdout.splitlines() if line}
    assert changed <= allowed, (
        "Gate B1 attestation changed files outside the allowed metadata-only set: "
        f"{sorted(changed - allowed)}"
    )
    assert signoff_rel in changed, "PASS requires a committed exact-freeze sign-off document"

    signoff = ROOT / signoff_rel
    assert signoff.exists(), f"missing sign-off document {signoff_rel}"
    text = signoff.read_text(encoding="utf-8")
    assert _signoff_table_value(text, "`commit_sha`") == freeze
    reviewer = _signoff_table_value(text, "Reviewer")
    reviewed_at = _signoff_table_value(text, "Reviewed at (UTC)")
    decision = _signoff_table_value(text, "Decision").strip("`")
    assert reviewer and reviewer not in {"—", "TBD"}, "PASS requires a named reviewer"
    assert reviewed_at and reviewed_at not in {"—", "TBD"}, "PASS requires review time"
    assert decision == "PASS", "STATE cannot claim PASS unless the sign-off decision is PASS"


# Only *strong affirmative* claim forms. Trying to parse negation out of free
# prose ("这不等于 Gate B1 通过") is fragile in both languages and would make
# the check a nuisance rather than a control; the failure mode worth stopping
# is a document flatly asserting the gate passed.
GATE_PASS_CLAIMS = re.compile(
    r"(?i)(gate\s*b1\s*[:=]\s*pass"
    r"|gate\s*b1\s+pass(ed)?\b"
    r"|gate\s*b1\s*已通过"
    r"|b1\s*已\s*正式通过)"
)


def test_docs_do_not_restate_gate_status_by_hand():
    """Prose may reference STATE.json; it may not assert a passing gate itself."""
    state = provenance.load_state(ROOT)
    assert state is not None
    if state["gate_status"]["gate_b1"] == "PASS":
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
        f"{state['gate_status']['gate_b1']}: {offenders}"
    )


def test_resolved_environment_is_observable():
    """`uv.lock` says what should be installed; this says what actually is."""
    env = provenance.resolved_environment()
    assert any(item.startswith("pytest==") for item in env)
    assert provenance.resolved_environment_sha256() == provenance.resolved_environment_sha256()
