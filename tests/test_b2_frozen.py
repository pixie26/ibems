from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from ib_execution import b2_evidence, b2_frozen, provenance, tree_identity

ROOT = Path(__file__).resolve().parents[1]


def _git(*arguments: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(ROOT), *arguments],
        capture_output=True,
        check=True,
    ).stdout


def _named_digest(commit: str, paths: list[str]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.encode("utf-8") + b"\0")
        digest.update(_git("show", f"{commit}:{path}"))
        digest.update(b"\0")
    return digest.hexdigest()


def test_frozen_manifest_is_loaded_from_git_and_validated_under_v2() -> None:
    frozen = b2_frozen.load_frozen_read_only_evidence(ROOT)
    raw = _git("show", f"{frozen.evidence_commit}:{b2_frozen.FROZEN_MANIFEST_PATH}")
    manifest = b2_evidence.loads_manifest(raw)
    b2_evidence.validate_manifest_v2(manifest, require_owner_acceptance=True)
    assert manifest == frozen.manifest
    parent = _git("rev-parse", f"{frozen.evidence_commit}^").decode().strip()
    assert parent == frozen.candidate_commit


def test_frozen_candidate_identity_is_independently_recomputed_from_git_blobs() -> None:
    frozen = b2_frozen.load_frozen_read_only_evidence(ROOT)
    candidate = frozen.candidate_commit
    paths = _git("ls-tree", "-r", "-z", "--name-only", candidate).split(b"\0")
    names = [path.decode("utf-8") for path in paths if path]
    source = [
        path
        for path in names
        if path == "pyproject.toml"
        or (path.endswith(".py") and path.split("/", 1)[0] in {"src", "tests", "scripts"})
    ]
    config = [
        path
        for path in names
        if path.startswith("config/")
        and "/" not in path[len("config/") :]
        and path.endswith(".example.yml")
    ]
    expected = {
        "source_tree_sha256": _named_digest(candidate, source),
        "config_tree_sha256": _named_digest(candidate, config),
        "dependency_lock_sha256": hashlib.sha256(_git("show", f"{candidate}:uv.lock")).hexdigest(),
    }
    assert {key: frozen.manifest["candidate"][key] for key in expected} == expected


def test_state_b2_identity_fields_are_dynamic_invariants() -> None:
    state = provenance.load_state(ROOT)
    assert state is not None
    recorded = state["gate_status"]
    frozen = b2_frozen.load_frozen_read_only_evidence(ROOT)
    current = tree_identity.derive_from_worktree(ROOT)
    candidate = frozen.manifest["candidate"]
    independently_derived_drift = [
        component
        for component, key in (
            ("source", "source_tree_sha256"),
            ("config", "config_tree_sha256"),
            ("dependency_lock", "dependency_lock_sha256"),
        )
        if getattr(current, key) != candidate[key]
    ]
    assert recorded["gate_b2_read_only_evidence_candidate"] == frozen.candidate_commit
    assert recorded["gate_b2_read_only_evidence_commit"] == frozen.evidence_commit
    assert recorded["gate_b2_read_only_evidence_drift_components"] == independently_derived_drift
    assert recorded["gate_b2_read_only_evidence_code_identity_matches_current_tree"] == (
        not independently_derived_drift
    )
    assert (not independently_derived_drift) == (
        current.source_tree_sha256 == candidate["source_tree_sha256"]
        and current.config_tree_sha256 == candidate["config_tree_sha256"]
        and current.dependency_lock_sha256 == candidate["dependency_lock_sha256"]
    )


def test_structural_validation_does_not_promote_external_evidence_boundaries() -> None:
    frozen = b2_frozen.load_frozen_read_only_evidence(ROOT)
    evidence = frozen.manifest["evidence"]
    reference_only = [item for item in evidence if item["binding"] == "REFERENCE_ONLY"]
    assert reference_only
    assert all(
        item["id"] not in frozen.manifest["authority_evidence_ids"]
        for item in reference_only
    )
    assert frozen.manifest["safety_boundary"] == {
        "gate_b2": "READ_ONLY_IN_PROGRESS",
        "order_authorization": "NONE",
        "trading_adapter": "NOT_IMPLEMENTED",
    }
