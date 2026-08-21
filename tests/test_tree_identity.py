from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ib_execution import b2_evidence_material, provenance, tree_identity


def _write_minimal_tree(root: Path) -> None:
    for folder in tree_identity.SOURCE_ROOTS:
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "src" / "logic.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "scripts" / "fault.ps1").write_text("$ErrorActionPreference='Stop'\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='identity-fixture'\n", encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "paper.example.yml").write_text("read_only: true\n", encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    # Match the production repository: identity bytes must not be rewritten by Git.
    (root / ".gitattributes").write_text("* -text\n", encoding="utf-8")


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_provenance_registry_is_the_canonical_registry() -> None:
    assert provenance.SOURCE_ROOTS is tree_identity.SOURCE_ROOTS
    assert provenance.SOURCE_SUFFIXES is tree_identity.SOURCE_SUFFIXES
    assert provenance.SOURCE_EXTRA is tree_identity.SOURCE_EXTRA_FILES
    assert provenance.CONFIG_GLOBS is tree_identity.CONFIG_GLOBS
    assert provenance.LOCK_FILENAME == tree_identity.DEPENDENCY_LOCK_FILE


def test_worktree_and_exact_git_object_identity_match(tmp_path: Path) -> None:
    _write_minimal_tree(tmp_path)
    worktree = tree_identity.derive_from_worktree(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "identity")
    # The B2 consumer reads exact objects while provenance reads worktree bytes.
    (tmp_path / "STATE.json").write_text(
        '{"tree":{},"gate_status":{}}\n', encoding="utf-8"
    )
    # Commit a valid STATE produced from the independently computed worktree identity.
    import json

    state = {
        "tree": worktree.as_state(),
        "gate_status": {
            "gate_b2": "READ_ONLY_IN_PROGRESS",
            "order_authorization": "NONE",
            "trading_adapter": "NOT_IMPLEMENTED",
        },
    }
    (tmp_path / "STATE.json").write_text(json.dumps(state) + "\n", encoding="utf-8")
    _git(tmp_path, "add", "STATE.json")
    _git(tmp_path, "commit", "-qm", "state")
    commit = _git(tmp_path, "rev-parse", "HEAD")

    observed, _ = b2_evidence_material.derive_candidate_from_git(tmp_path, commit)
    assert observed["source_tree_sha256"] == worktree.source_tree_sha256
    assert observed["config_tree_sha256"] == worktree.config_tree_sha256
    assert observed["dependency_lock_sha256"] == worktree.dependency_lock_sha256


def test_power_shell_is_part_of_source_identity(tmp_path: Path) -> None:
    _write_minimal_tree(tmp_path)
    before = tree_identity.derive_from_worktree(tmp_path)
    (tmp_path / "scripts" / "fault.ps1").write_text("throw 'changed'\n", encoding="utf-8")
    after = tree_identity.derive_from_worktree(tmp_path)
    assert "scripts/fault.ps1" in before.source_files
    assert before.source_tree_sha256 != after.source_tree_sha256


def test_unregistered_example_config_fails_in_worktree_path(tmp_path: Path) -> None:
    _write_minimal_tree(tmp_path)
    (tmp_path / "config" / "risk.example.yaml").write_text("risk: 1\n", encoding="utf-8")
    with pytest.raises(tree_identity.TreeIdentityError, match="unclassified config"):
        tree_identity.derive_from_worktree(tmp_path)


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda root: (root / "scripts" / "unknown.sh").write_text("exit 0\n"), "unclassified"),
        (lambda root: (root / "pyproject.toml").unlink(), "missing required"),
        (
            lambda root: (root / "config" / "risk.example.yaml").write_text("risk: 1\n"),
            "unclassified config",
        ),
    ],
)
def test_identity_definition_drift_fails_closed(tmp_path: Path, mutation, match: str) -> None:
    _write_minimal_tree(tmp_path)
    mutation(tmp_path)
    paths = [
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*")
        if path.is_file()
    ]
    with pytest.raises(tree_identity.TreeIdentityError, match=match):
        tree_identity.derive_from_versioned_paths(
            paths, lambda path: (tmp_path / path).read_bytes()
        )
