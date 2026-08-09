from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from ib_execution import attestation, provenance


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return out.stdout.strip()


def _init_repo(root: Path) -> str:
    _git(root, "init")
    _git(root, "config", "user.email", "gate-test@example.invalid")
    _git(root, "config", "user.name", "Gate Test")
    (root / "README.md").write_text("freeze\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "freeze")
    return _git(root, "rev-parse", "HEAD")


def _write_evidence(root: Path, freeze: str, run_id: int = 12345) -> tuple[Path, str]:
    signoff, evidence = attestation.paths_for(root, freeze)
    evidence.parent.mkdir(parents=True, exist_ok=True)
    common = {
        "commit_sha": freeze,
        "passed": True,
        "worktree_clean": True,
        "source_tree_sha256": "1" * 64,
        "dependency_lock_sha256": "2" * 64,
        "resolved_environment_sha256": "3" * 64,
    }
    payload = {
        "schema_version": 1,
        "freeze_commit": freeze,
        "workflow": {
            "name": "b1-freeze-campaign",
            "run_id": run_id,
            "artifact_name": f"gate-b1-freeze-{freeze}",
            "artifact_digest": "sha256:" + "4" * 64,
        },
        "manifest_sha256": {"formal": "5" * 64, "storage": "6" * 64},
        "formal_manifest": dict(common),
        "storage_manifest": {**common, "inconclusive": []},
    }
    evidence.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    return evidence, digest


def _write_signoff(root: Path, freeze: str, evidence_sha: str, run_id: int = 12345) -> Path:
    signoff, _ = attestation.paths_for(root, freeze)
    signoff.write_text(
        "\n".join(
            [
                "# Gate B1 sign-off",
                "",
                "| Field | Value |",
                "|---|---|",
                f"| `commit_sha` | {freeze} |",
                f"| Freeze campaign run | {run_id} |",
                f"| Freeze artifact digest | sha256:{'4' * 64} |",
                f"| Evidence snapshot sha256 | {evidence_sha} |",
                "| Reviewer | Independent Reviewer |",
                "| Reviewed at (UTC) | 2026-08-09T03:00:00Z |",
                "| Decision | `PASS` |",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return signoff


def test_pass_is_derived_and_survives_state_regeneration(tmp_path: Path):
    freeze = _init_repo(tmp_path)
    evidence, evidence_sha = _write_evidence(tmp_path, freeze)
    signoff = _write_signoff(tmp_path, freeze, evidence_sha)

    # At the exact tested freeze, only the two attestation inputs are dirty.
    assert attestation.derive_signed_off_commit(tmp_path) == freeze
    provenance.write_state(tmp_path)
    first = provenance.load_state(tmp_path)
    assert first is not None
    assert first["gate_status"]["gate_b1"] == "PASS"
    assert first["gate_status"]["signed_off_commit"] == freeze

    _git(tmp_path, "add", "STATE.json", signoff.relative_to(tmp_path).as_posix(), evidence.relative_to(tmp_path).as_posix())
    _git(tmp_path, "commit", "-m", "attest B1")

    # The attestation commit has a different HEAD, but regeneration must derive
    # the same PASS instead of silently resetting it to NOT_PASSED.
    provenance.write_state(tmp_path)
    second = provenance.load_state(tmp_path)
    assert second is not None
    assert second["gate_status"]["gate_b1"] == "PASS"
    assert second["gate_status"]["signed_off_commit"] == freeze
    assert provenance.stale_fields(tmp_path) == {}


def test_any_behavior_commit_after_attestation_invalidates_old_pass(tmp_path: Path):
    freeze = _init_repo(tmp_path)
    evidence, evidence_sha = _write_evidence(tmp_path, freeze)
    signoff = _write_signoff(tmp_path, freeze, evidence_sha)
    provenance.write_state(tmp_path)
    _git(tmp_path, "add", "STATE.json", signoff.relative_to(tmp_path).as_posix(), evidence.relative_to(tmp_path).as_posix())
    _git(tmp_path, "commit", "-m", "attest B1")

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "behavior.py").write_text("CHANGED = True\n", encoding="utf-8")
    _git(tmp_path, "add", "src/behavior.py")
    _git(tmp_path, "commit", "-m", "change behavior")

    stale = provenance.stale_fields(tmp_path)
    assert stale["gate_status.gate_b1"] == ("PASS", "NOT_PASSED")
    assert stale["gate_status.signed_off_commit"] == (freeze, None)

    provenance.write_state(tmp_path)
    state = provenance.load_state(tmp_path)
    assert state is not None
    assert state["gate_status"]["gate_b1"] == "NOT_PASSED"
    assert state["gate_status"]["signed_off_commit"] is None


def test_bad_evidence_hash_cannot_derive_pass(tmp_path: Path):
    freeze = _init_repo(tmp_path)
    _write_evidence(tmp_path, freeze)
    _write_signoff(tmp_path, freeze, "0" * 64)
    assert attestation.derive_signed_off_commit(tmp_path) is None
