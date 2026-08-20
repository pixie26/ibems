from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ib_execution import b2_evidence_material


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str, dict[str, str]]:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "scripts").mkdir()
    (repo / "config").mkdir()
    (repo / "src" / "logic.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests" / "test_logic.py").write_text("def test_value(): pass\n", encoding="utf-8")
    (repo / "scripts" / "tool.py").write_text("print('ok')\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (repo / "config" / "paper.example.yml").write_text("read_only: true\n", encoding="utf-8")
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "base"],
        check=True,
        env={
            **os.environ,
            "GIT_COMMITTER_DATE": "2026-08-19T00:00:00Z",
            "GIT_AUTHOR_DATE": "2026-08-19T00:00:00Z",
        },
    )

    commit = _git(repo, "rev-parse", "HEAD")
    source_names = ["pyproject.toml", "scripts/tool.py", "src/logic.py", "tests/test_logic.py"]

    def named_digest(names: list[str]) -> str:
        digest = hashlib.sha256()
        for name in sorted(names):
            digest.update(name.encode() + b"\0")
            digest.update(
                subprocess.run(
                    ["git", "-C", str(repo), "show", f"HEAD:{name}"],
                    capture_output=True,
                    check=True,
                ).stdout
            )
            digest.update(b"\0")
        return digest.hexdigest()

    hashes = {
        "source_tree_sha256": named_digest(source_names),
        "config_tree_sha256": named_digest(["config/paper.example.yml"]),
        "dependency_lock_sha256": hashlib.sha256(
            subprocess.run(
                ["git", "-C", str(repo), "show", "HEAD:uv.lock"],
                capture_output=True,
                check=True,
            ).stdout
        ).hexdigest(),
    }
    state = {
        "tree": hashes,
        "gate_status": {
            "gate_b2": "READ_ONLY_IN_PROGRESS",
            "order_authorization": "NONE",
            "trading_adapter": "NOT_IMPLEMENTED",
        },
    }
    (repo / "STATE.json").write_text(json.dumps(state) + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "STATE.json"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "state"],
        check=True,
        env={
            **os.environ,
            "GIT_COMMITTER_DATE": "2026-08-19T00:01:00Z",
            "GIT_AUTHOR_DATE": "2026-08-19T00:01:00Z",
        },
    )
    commit = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "show", "-s", "--format=%T", "HEAD")
    return repo, commit, tree, hashes


def _entry(evidence_id: str, kind: str, verdict: str, commit: str) -> dict[str, object]:
    return {
        "id": evidence_id,
        "claim_key": f"CLAIM_{evidence_id}",
        "evidence_kind": kind,
        "binding": "BOUND_AUTHORITY",
        "relative_path": f"evidence/{evidence_id.lower()}.json",
        "sha256": "0" * 64,
        "bytes": 1,
        "captured_at_utc": "2026-08-20T00:00:00Z",
        "capture_commit": commit,
        "verdict": verdict,
        "scope": "Read-only test evidence only.",
        "sensitivity": "REDACTED",
    }


def _manifest(commit: str, hashes: dict[str, str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "freeze_kind": "B2_READ_ONLY_EVIDENCE",
        "candidate": {"commit_sha": commit, "tree_sha": "0" * 40, **hashes},
        "safety_boundary": {
            "gate_b2": "READ_ONLY_IN_PROGRESS",
            "order_authorization": "NONE",
            "trading_adapter": "NOT_IMPLEMENTED",
        },
        "ci_runs": [
            {
                "provider": "GITHUB_ACTIONS",
                "workflow": "CI",
                "run_id": 100,
                "run_attempt": 1,
                "commit_sha": commit,
                "conclusion": "SUCCESS",
            }
        ],
        "evidence": [
            _entry("V4_HEALTH", "FULL_RTH_HEALTH", "PASS", commit),
            _entry("V3_FAIL", "HISTORICAL_FAILURE", "FAIL", commit),
        ],
        "authority_evidence_ids": ["V4_HEALTH", "V3_FAIL"],
        "required_failures": ["V3_FAIL"],
        "unknowns": [
            {
                "id": "OFFICIAL_AMBIGUITY",
                "statement": "A documented boundary remains ambiguous.",
                "status": "AMBIGUOUS",
                "blocks_b2_read_only_freeze": False,
                "review_before": "PRODUCTION_OR_ORDER_CAPABLE",
            }
        ],
        "risk_assumptions": [
            {
                "id": "D1_EVENT_DRIVEN_30S",
                "statement": "Threshold is scope-bound.",
                "failure_mode": "Short outage may remain advisory.",
                "mitigation": "Review before production.",
                "status": "OPEN_REVIEW_REQUIRED",
                "blocks_b2_read_only_freeze": False,
                "required_review_before": "PRODUCTION_OR_ORDER_CAPABLE_PAPER_LIVE",
            },
            {
                "id": "D2_WRITER_LAG_ROOT_CAUSE",
                "statement": "Root cause remains open.",
                "failure_mode": "Lag may recur.",
                "mitigation": "Run bounded review before production.",
                "status": "OPEN_REVIEW_REQUIRED",
                "blocks_b2_read_only_freeze": False,
                "required_review_before": "PRODUCTION_OR_ORDER_CAPABLE_PAPER_LIVE",
            },
        ],
        "owner_acceptance": None,
    }


def _lookup(commit: str):
    def lookup(repository: str, run_id: int, attempt: int) -> dict[str, object]:
        assert repository == "owner/repo"
        return {
            "run": {
                "id": run_id,
                "run_attempt": attempt,
                "head_sha": commit,
                "name": "CI",
                "status": "completed",
                "conclusion": "success",
            },
            "jobs": [
                {
                    "id": 1000 + run_id,
                    "run_id": run_id,
                    "run_attempt": attempt,
                    "head_sha": commit,
                    "name": "verify",
                    "status": "completed",
                    "conclusion": "success",
                    "steps": [{"name": "tests", "conclusion": "success"}],
                }
            ],
            "jobs_total_count": 1,
        }

    return lookup


def _material(tmp_path: Path, commit: str, hashes: dict[str, str]):
    root = tmp_path / "evidence"
    root.mkdir()
    (root / "v4_health.json").write_bytes(b"{\"health_ok\":true}\n")
    (root / "v3_fail.json").write_bytes(b"{\"health_ok\":false}\n")
    return _manifest(commit, hashes), {"evidence": root}


def test_populates_from_git_objects_and_streamed_files(tmp_path: Path) -> None:
    repo, commit, tree, hashes = _repository(tmp_path)
    payload, roots = _material(tmp_path, commit, hashes)
    report = b2_evidence_material.verify_materials(
        payload,
        repo_root=repo,
        controlled_roots=roots,
        github_repository="owner/repo",
        github_lookup=_lookup(commit),
        populate_observed=True,
        now=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )
    assert payload["candidate"]["tree_sha"] == tree  # type: ignore[index]
    assert payload["evidence"][0]["bytes"] == 19  # type: ignore[index]
    assert report.evidence_files_verified == 2
    assert report.evidence_bytes_streamed == 39
    assert report.ci_jobs_verified == 1

    b2_evidence_material.verify_materials(
        payload,
        repo_root=repo,
        controlled_roots=roots,
        github_repository="owner/repo",
        github_lookup=_lookup(commit),
        now=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )


def test_dirty_worktree_cannot_change_candidate_identity(tmp_path: Path) -> None:
    repo, commit, _, hashes = _repository(tmp_path)
    (repo / "src" / "logic.py").write_text("VALUE = 999\n", encoding="utf-8")
    observed, _ = b2_evidence_material.derive_candidate_from_git(repo, commit)
    assert observed["source_tree_sha256"] == hashes["source_tree_sha256"]


def test_hash_mismatch_fails_closed_without_rewriting(tmp_path: Path) -> None:
    repo, commit, _, hashes = _repository(tmp_path)
    payload, roots = _material(tmp_path, commit, hashes)
    built = copy.deepcopy(payload)
    b2_evidence_material.verify_materials(
        built,
        repo_root=repo,
        controlled_roots=roots,
        github_repository="owner/repo",
        github_lookup=_lookup(commit),
        populate_observed=True,
        now=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )
    (roots["evidence"] / "v4_health.json").write_bytes(b"changed")
    with pytest.raises(
        b2_evidence_material.B2MaterialVerificationError, match="bytes do not match"
    ):
        b2_evidence_material.verify_materials(
            built,
            repo_root=repo,
            controlled_roots=roots,
            github_repository="owner/repo",
            github_lookup=_lookup(commit),
            now=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )


def test_root_escape_and_unknown_alias_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(b2_evidence_material.B2MaterialVerificationError, match="unapproved"):
        b2_evidence_material.resolve_evidence_path("other/file.json", {"allowed": root})
    with pytest.raises(
        b2_evidence_material.B2MaterialVerificationError, match="unavailable|escapes"
    ):
        b2_evidence_material.resolve_evidence_path("allowed/../outside.json", {"allowed": root})


def test_unbound_reference_is_indexed_without_touching_disk(tmp_path: Path) -> None:
    repo, commit, _, hashes = _repository(tmp_path)
    payload, roots = _material(tmp_path, commit, hashes)
    payload["evidence"].append(  # type: ignore[union-attr]
        {
            "id": "INCIDENT_REFERENCE",
            "claim_key": "INCIDENT_REFERENCE_ONLY",
            "evidence_kind": "INCIDENT",
            "binding": "REFERENCE_ONLY",
            "relative_path": "missing/incident.json",
            "sha256": None,
            "bytes": None,
            "captured_at_utc": "2026-08-20T00:00:00Z",
            "capture_commit": None,
            "verdict": "INFORMATIONAL",
            "scope": "Historical index only.",
            "sensitivity": "CONTROLLED_EXTERNAL",
        }
    )
    report = b2_evidence_material.verify_materials(
        payload,
        repo_root=repo,
        controlled_roots=roots,
        github_repository="owner/repo",
        github_lookup=_lookup(commit),
        populate_observed=True,
        now=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )
    assert report.evidence_files_verified == 2


def test_github_identity_and_manifest_budget_are_enforced(tmp_path: Path) -> None:
    repo, commit, _, hashes = _repository(tmp_path)
    payload, roots = _material(tmp_path, commit, hashes)

    def wrong_lookup(repository: str, run_id: int, attempt: int) -> dict[str, object]:
        observed = _lookup(commit)(repository, run_id, attempt)
        observed["run"]["head_sha"] = "9" * 40  # type: ignore[index]
        return observed

    with pytest.raises(b2_evidence_material.B2MaterialVerificationError, match="head_sha"):
        b2_evidence_material.verify_materials(
            payload,
            repo_root=repo,
            controlled_roots=roots,
            github_repository="owner/repo",
            github_lookup=wrong_lookup,
            populate_observed=True,
            now=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )
    with pytest.raises(b2_evidence_material.B2MaterialVerificationError, match="budget"):
        b2_evidence_material.verify_materials(
            payload,
            repo_root=repo,
            controlled_roots=roots,
            github_repository="owner/repo",
            github_lookup=_lookup(commit),
            populate_observed=True,
            manifest_budget_bytes=100,
            now=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )


def test_create_only_output_refuses_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "candidate.json"
    b2_evidence_material.publish_create_only(output, b"first\n")
    with pytest.raises(
        b2_evidence_material.B2MaterialVerificationError, match="refusing to overwrite"
    ):
        b2_evidence_material.publish_create_only(output, b"second\n")
    assert output.read_bytes() == b"first\n"
