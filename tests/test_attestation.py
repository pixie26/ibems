from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

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
    # Exact-freeze evidence hashes committed bytes.  Do not let a developer's
    # global Windows autocrlf setting silently rewrite the fixture on add.
    _git(root, "config", "core.autocrlf", "false")
    (root / "README.md").write_text("freeze\n", encoding="utf-8")
    config = root / "config" / "risk.example.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "max_position_shares: 5\n"
        "overnight_gap_stress_pct: 0.15\n"
        "max_overnight_loss: 500\n",
        encoding="utf-8",
    )
    _git(root, "add", "README.md", "config/risk.example.yml")
    _git(root, "commit", "-m", "freeze")
    return _git(root, "rev-parse", "HEAD")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_evidence(root: Path, freeze: str, run_id: int = 12345) -> tuple[Path, str]:
    _, evidence = attestation.paths_for(root, freeze)
    evidence.parent.mkdir(parents=True, exist_ok=True)

    texts = {
        "deterministic": {"path": "gate_b1/x/deterministic.txt", "text": "303 passed, 1 skipped\n"},
        "property_default": {"path": "gate_b1/x/property_default.txt", "text": "5 passed\n"},
        "property_gate": {
            "path": "gate_b1/x/property_gate.txt",
            "text": (
                "test_no_sequence_violates_invariants: 1500 passing, 0 failing\n"
                "test_never_two_orders_in_flight: 1500 passing, 0 failing\n"
            ),
        },
        "process_crash": {"path": "gate_b1_extra/process_crash.txt", "text": "....... [100%]\n"},
        "deterministic_soak_auditor": {
            "path": "gate_b1_extra/deterministic_soak_auditor.txt",
            "text": "PASS: 20 seeds x 50 actions\n",
        },
        "dm_targets": {"path": "gate_b1_extra/dm-targets.txt", "text": "delay v1.5.0\n"},
        "storage_domains": {
            "path": "gate_b1_extra/storage_domains.txt",
            "text": "journal_st_dev=48\nfence_st_dev=49\n",
        },
    }
    for item in texts.values():
        item["sha256"] = _digest(item["text"])

    common = {
        "commit_sha": freeze,
        "passed": True,
        "worktree_clean": True,
        "source_tree_sha256": "1" * 64,
        "dependency_lock_sha256": "2" * 64,
        "resolved_environment_sha256": "3" * 64,
    }
    formal = {
        **common,
        "artifact_hashes": {
            "deterministic.txt": texts["deterministic"]["sha256"],
            "property_default.txt": texts["property_default"]["sha256"],
            "property_gate.txt": texts["property_gate"]["sha256"],
        },
    }
    storage = {
        **common,
        "inconclusive": [],
        "drills": {
            "disk_full": {"passed": True, "exit_code": 10, "fence_present": True},
            "wal_corruption": {
                "passed": True,
                "forced_crossing": {"passed": True, "exit_code": 15},
            },
            "fsync_stall": {
                "passed": True,
                "mechanism": "dm-delay",
                "healthy": {"passed": True, "broker_write_count": 1},
                "stalling": {
                    "passed": True,
                    "exit_code": 10,
                    "post_fault_broker_writes": 0,
                    "fence_present": True,
                },
            },
        },
    }
    formal_raw = json.dumps(formal, indent=2, sort_keys=True) + "\n"
    storage_raw = json.dumps(storage, indent=2, sort_keys=True) + "\n"

    supplemental = {
        "artifacts/" + texts[key]["path"]: texts[key]["sha256"]
        for key in ("process_crash", "deterministic_soak_auditor", "dm_targets", "storage_domains")
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
        "manifest_sha256": {"formal": _digest(formal_raw), "storage": _digest(storage_raw)},
        "formal_manifest_raw": formal_raw,
        "storage_manifest_raw": storage_raw,
        "evidence_text": texts,
        "supplemental_sha256": supplemental,
        "scope_limits": {"windows_real_faults": "NOT_RUN_ACCEPTED_NON_BLOCKER"},
    }
    evidence.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    return evidence, digest


def _write_signoff(
    root: Path,
    freeze: str,
    evidence_sha: str,
    run_id: int = 12345,
    *,
    overrides: dict[str, str] | None = None,
) -> Path:
    values = {
        "Owner": "Project Owner",
        "Accepted at (UTC)": "2026-08-09T05:13:10Z",
        "B1 scope acceptance": "ACCEPT",
        "Overnight risk acceptance": "ACCEPT",
        "Accepted max_position_shares": "5",
        "Recorded overnight_gap_stress_pct": "0.15",
        "Recorded max_overnight_loss": "500",
        "Windows gap acceptance": "ACCEPT",
        "Real IB scope": "DEFER_TO_B2",
        "Additional B1-level hazard identified": "NO",
        "Decision": "PASS",
    }
    if overrides:
        values.update(overrides)

    signoff, _ = attestation.paths_for(root, freeze)
    lines = [
        "# Gate B1 owner acceptance",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| `commit_sha` | {freeze} |",
        f"| Freeze campaign run | {run_id} |",
        f"| Freeze artifact digest | sha256:{'4' * 64} |",
        f"| Evidence snapshot sha256 | {evidence_sha} |",
    ]
    lines.extend(f"| {field} | `{value}` |" for field, value in values.items())
    signoff.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return signoff


def test_pass_is_derived_and_survives_state_regeneration(tmp_path: Path):
    freeze = _init_repo(tmp_path)
    evidence, evidence_sha = _write_evidence(tmp_path, freeze)
    signoff = _write_signoff(tmp_path, freeze, evidence_sha)

    assert attestation.derive_signed_off_commit(tmp_path) == freeze
    provenance.write_state(tmp_path)
    first = provenance.load_state(tmp_path)
    assert first is not None
    assert first["gate_status"]["gate_b1_covers_worktree"] is True
    assert first["gate_status"]["gate_b1_attested_freeze"] is None

    _git(
        tmp_path,
        "add",
        "STATE.json",
        signoff.relative_to(tmp_path).as_posix(),
        evidence.relative_to(tmp_path).as_posix(),
    )
    _git(tmp_path, "commit", "-m", "attest B1")

    provenance.write_state(tmp_path)
    second = provenance.load_state(tmp_path)
    assert second is not None
    assert second["gate_status"]["gate_b1_covers_worktree"] is True
    assert second["gate_status"]["gate_b1_attested_freeze"] == freeze
    historical = attestation.derive_historical_attested_freeze(tmp_path)
    assert historical is not None
    assert historical.freeze_commit == freeze
    assert provenance.stale_fields(tmp_path) == {}


def test_any_behavior_commit_after_attestation_invalidates_old_pass(tmp_path: Path):
    freeze = _init_repo(tmp_path)
    evidence, evidence_sha = _write_evidence(tmp_path, freeze)
    signoff = _write_signoff(tmp_path, freeze, evidence_sha)
    provenance.write_state(tmp_path)
    _git(
        tmp_path,
        "add",
        "STATE.json",
        signoff.relative_to(tmp_path).as_posix(),
        evidence.relative_to(tmp_path).as_posix(),
    )
    _git(tmp_path, "commit", "-m", "attest B1")

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "behavior.py").write_text("CHANGED = True\n", encoding="utf-8")
    _git(tmp_path, "add", "src/behavior.py")
    _git(tmp_path, "commit", "-m", "change behavior")

    stale = provenance.stale_fields(tmp_path)
    assert stale["gate_status.gate_b1_covers_worktree"] == (True, False)
    assert stale["gate_status.gate_b1_attested_freeze"] == (None, freeze)

    provenance.write_state(tmp_path)
    state = provenance.load_state(tmp_path)
    assert state is not None
    assert state["gate_status"]["gate_b1_covers_worktree"] is False
    assert state["gate_status"]["gate_b1_attested_freeze"] == freeze


def test_bad_evidence_hash_cannot_derive_pass(tmp_path: Path):
    freeze = _init_repo(tmp_path)
    _write_evidence(tmp_path, freeze)
    _write_signoff(tmp_path, freeze, "0" * 64)
    assert attestation.derive_signed_off_commit(tmp_path) is None


def test_tampered_embedded_manifest_cannot_derive_pass(tmp_path: Path):
    freeze = _init_repo(tmp_path)
    evidence, evidence_sha = _write_evidence(tmp_path, freeze)
    _write_signoff(tmp_path, freeze, evidence_sha)
    data = json.loads(evidence.read_text(encoding="utf-8"))
    data["formal_manifest_raw"] += " "
    evidence.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert attestation.derive_signed_off_commit(tmp_path) is None


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("B1 scope acceptance", ""),
        ("Overnight risk acceptance", ""),
        ("Windows gap acceptance", ""),
        ("Real IB scope", "TREAT_AS_VERIFIED"),
        ("Additional B1-level hazard identified", "UNKNOWN"),
        ("Decision", "ENTER_B2"),
    ],
)
def test_owner_decision_fields_are_mandatory(tmp_path: Path, field: str, bad_value: str):
    freeze = _init_repo(tmp_path)
    _, evidence_sha = _write_evidence(tmp_path, freeze)
    _write_signoff(tmp_path, freeze, evidence_sha, overrides={field: bad_value})
    assert attestation.derive_signed_off_commit(tmp_path) is None


def test_owner_accepted_position_must_match_frozen_risk_config(tmp_path: Path):
    freeze = _init_repo(tmp_path)
    _, evidence_sha = _write_evidence(tmp_path, freeze)
    _write_signoff(
        tmp_path,
        freeze,
        evidence_sha,
        overrides={"Accepted max_position_shares": "6"},
    )
    assert attestation.derive_signed_off_commit(tmp_path) is None


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("Recorded overnight_gap_stress_pct", "0.20"),
        ("Recorded max_overnight_loss", "600"),
    ],
)
def test_recorded_overnight_model_parameters_must_match_freeze(
    tmp_path: Path, field: str, bad_value: str
):
    freeze = _init_repo(tmp_path)
    _, evidence_sha = _write_evidence(tmp_path, freeze)
    _write_signoff(tmp_path, freeze, evidence_sha, overrides={field: bad_value})
    assert attestation.derive_signed_off_commit(tmp_path) is None


def test_self_consistent_but_semantically_wrong_storage_packet_is_rejected(tmp_path: Path):
    freeze = _init_repo(tmp_path)
    evidence, _ = _write_evidence(tmp_path, freeze)
    data = json.loads(evidence.read_text(encoding="utf-8"))
    storage = json.loads(data["storage_manifest_raw"])
    storage["drills"]["fsync_stall"]["stalling"]["post_fault_broker_writes"] = 1
    storage_raw = json.dumps(storage, indent=2, sort_keys=True) + "\n"
    data["storage_manifest_raw"] = storage_raw
    data["manifest_sha256"]["storage"] = _digest(storage_raw)
    evidence.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    evidence_sha = hashlib.sha256(evidence.read_bytes()).hexdigest()
    _write_signoff(tmp_path, freeze, evidence_sha)
    assert attestation.derive_signed_off_commit(tmp_path) is None
