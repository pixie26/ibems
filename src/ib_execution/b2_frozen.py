"""Derive machine state from the committed Gate B2 read-only evidence freeze.

Only repository Git objects are validated here. Controlled external evidence
and CI artifact archives are intentionally not re-fetched, so this module does
not claim that historical observations apply to the current code tree.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import b2_evidence, b2_evidence_material, tree_identity

FROZEN_MANIFEST_PATH = "docs/GATE_B2_READ_ONLY_EVIDENCE_C88CF246_FROZEN.json"


class B2FrozenEvidenceError(RuntimeError):
    """Committed B2 read-only freeze structure cannot be proven."""


@dataclass(frozen=True)
class FrozenReadOnlyEvidence:
    candidate_commit: str
    evidence_commit: str
    manifest: dict[str, Any]


def _git(root: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments], capture_output=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise B2FrozenEvidenceError(f"cannot execute Git: {exc}") from exc
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise B2FrozenEvidenceError(f"Git {' '.join(arguments)} failed: {message}")
    return result.stdout


def _git_text(root: Path, *arguments: str) -> str:
    try:
        return _git(root, *arguments).decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise B2FrozenEvidenceError("Git returned non-UTF-8 metadata") from exc


def _addition_commits(root: Path) -> list[str]:
    return [
        line
        for line in _git_text(
            root,
            "log",
            "--format=%H",
            "--diff-filter=A",
            "--",
            FROZEN_MANIFEST_PATH,
        ).splitlines()
        if line
    ]


def load_frozen_read_only_evidence(root: Path) -> FrozenReadOnlyEvidence:
    additions = _addition_commits(root)
    if len(additions) != 1:
        raise B2FrozenEvidenceError(
            f"expected one frozen-manifest addition commit, found {len(additions)}"
        )
    evidence_commit = additions[0]
    lineage = _git_text(root, "rev-list", "--parents", "-n", "1", evidence_commit).split()
    if len(lineage) != 2 or lineage[0] != evidence_commit:
        raise B2FrozenEvidenceError("frozen-manifest commit must have exactly one parent")
    parent = lineage[1]
    changes = {
        tuple(line.split("\t", 1))
        for line in _git_text(
            root, "diff-tree", "--no-commit-id", "--name-status", "-r", evidence_commit
        ).splitlines()
        if line
    }
    if changes != {("A", FROZEN_MANIFEST_PATH)}:
        raise B2FrozenEvidenceError(
            "frozen-manifest commit must add only the immutable manifest"
        )

    raw = _git(root, "show", f"{evidence_commit}:{FROZEN_MANIFEST_PATH}")
    try:
        manifest = b2_evidence.loads_manifest(raw)
        b2_evidence.validate_manifest(manifest, require_owner_acceptance=True)
    except b2_evidence.B2EvidenceValidationError as exc:
        raise B2FrozenEvidenceError(f"frozen manifest is invalid: {exc}") from exc
    candidate = manifest.get("candidate")
    if not isinstance(candidate, dict):
        raise B2FrozenEvidenceError("frozen manifest candidate is invalid")
    candidate_commit = str(candidate.get("commit_sha", ""))
    if parent != candidate_commit:
        raise B2FrozenEvidenceError(
            "frozen-manifest parent does not equal its candidate commit"
        )
    try:
        observed, _ = b2_evidence_material.derive_candidate_from_git_v2(
            root, candidate_commit
        )
    except b2_evidence_material.B2MaterialVerificationError as exc:
        raise B2FrozenEvidenceError(f"candidate Git identity is invalid: {exc}") from exc
    if candidate != observed:
        raise B2FrozenEvidenceError(
            "frozen manifest candidate does not match exact schema-v2 Git-object identity"
        )
    return FrozenReadOnlyEvidence(
        candidate_commit=candidate_commit,
        evidence_commit=evidence_commit,
        manifest=manifest,
    )


def derived_state_fields(root: Path) -> dict[str, Any]:
    if not _addition_commits(root):
        return {
            "gate_b2_read_only_evidence_candidate": None,
            "gate_b2_read_only_evidence_commit": None,
            "gate_b2_read_only_evidence_code_identity_matches_current_tree": False,
            "gate_b2_read_only_evidence_drift_components": ["frozen_evidence_missing"],
        }
    frozen = load_frozen_read_only_evidence(root)
    candidate = frozen.manifest["candidate"]
    current = tree_identity.derive_from_worktree(root)
    drift = tree_identity.drift_components(current, candidate)
    return {
        "gate_b2_read_only_evidence_candidate": frozen.candidate_commit,
        "gate_b2_read_only_evidence_commit": frozen.evidence_commit,
        "gate_b2_read_only_evidence_code_identity_matches_current_tree": not drift,
        "gate_b2_read_only_evidence_drift_components": list(drift),
    }
