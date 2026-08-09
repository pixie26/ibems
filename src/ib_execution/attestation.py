"""Gate B1 attestation derivation.

A Gate B1 PASS is not mutable state. It is a conclusion that can be recomputed
from durable facts: a freeze-ready registry, an exact-freeze owner acceptance,
a self-contained evidence snapshot, and Git history showing that only
attestation metadata changed after the tested freeze.

The human attestation is intentionally an OWNER RISK ACCEPTANCE, not a claim
that the owner independently audited every implementation line. Technical
claims remain grounded in the frozen automated/fault evidence. The owner must
explicitly accept the residual-risk boundaries that tests cannot decide.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

STATE_FILENAME = "STATE.json"
SIGNOFF_PREFIX = "GATE_B1_SIGNOFF_"
EVIDENCE_PREFIX = "GATE_B1_EVIDENCE_"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
ARTIFACT_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class Attestation:
    freeze_commit: str
    signoff_path: Path
    evidence_path: Path
    evidence_sha256: str


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _table_value(text: str, field: str) -> str:
    match = re.search(
        rf"^\|\s*{re.escape(field)}\s*\|\s*(.*?)\s*\|\s*$",
        text,
        re.MULTILINE,
    )
    return match.group(1).strip() if match else ""


def _config_scalar(root: Path, key: str) -> str:
    """Read one simple scalar from the frozen example risk config."""
    try:
        text = (root / "config" / "risk.example.yml").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(rf"^{re.escape(key)}:\s*([^#\n]+?)\s*(?:#.*)?$", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def paths_for(root: Path, freeze: str) -> tuple[Path, Path]:
    short = freeze[:12]
    return (
        root / "docs" / f"{SIGNOFF_PREFIX}{short}.md",
        root / "docs" / f"{EVIDENCE_PREFIX}{short}.json",
    )


def allowed_attestation_paths(root: Path, freeze: str) -> set[str]:
    signoff, evidence = paths_for(root, freeze)
    return {
        STATE_FILENAME,
        signoff.relative_to(root).as_posix(),
        evidence.relative_to(root).as_posix(),
    }


def _changed_worktree_paths(root: Path) -> Optional[set[str]]:
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0:
        return None
    changed: set[str] = set()
    for line in status.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        changed.add(path)
    return changed


def _evidence_is_valid(path: Path, freeze: str) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    if data.get("schema_version") != 1 or data.get("freeze_commit") != freeze:
        return False
    workflow = data.get("workflow", {})
    if workflow.get("name") != "b1-freeze-campaign":
        return False
    if workflow.get("artifact_name") != f"gate-b1-freeze-{freeze}":
        return False
    if not isinstance(workflow.get("run_id"), int) or workflow["run_id"] <= 0:
        return False
    if not ARTIFACT_DIGEST.fullmatch(str(workflow.get("artifact_digest", ""))):
        return False

    formal_raw = data.get("formal_manifest_raw")
    storage_raw = data.get("storage_manifest_raw")
    if not isinstance(formal_raw, str) or not isinstance(storage_raw, str):
        return False
    try:
        formal = json.loads(formal_raw)
        storage = json.loads(storage_raw)
    except json.JSONDecodeError:
        return False

    hashes = data.get("manifest_sha256", {})
    formal_hash = hashlib.sha256(formal_raw.encode("utf-8")).hexdigest()
    storage_hash = hashlib.sha256(storage_raw.encode("utf-8")).hexdigest()
    if hashes.get("formal") != formal_hash or hashes.get("storage") != storage_hash:
        return False
    if not HEX64.fullmatch(formal_hash) or not HEX64.fullmatch(storage_hash):
        return False

    if formal.get("commit_sha") != freeze or storage.get("commit_sha") != freeze:
        return False
    if formal.get("passed") is not True or formal.get("worktree_clean") is not True:
        return False
    if storage.get("passed") is not True or storage.get("worktree_clean") is not True:
        return False
    if storage.get("inconclusive") != []:
        return False

    for key in (
        "source_tree_sha256",
        "dependency_lock_sha256",
        "resolved_environment_sha256",
    ):
        value = str(formal.get(key, ""))
        if not HEX64.fullmatch(value) or value != storage.get(key):
            return False

    evidence_text = data.get("evidence_text", {})
    required = {
        "deterministic",
        "property_default",
        "property_gate",
        "process_crash",
        "deterministic_soak_auditor",
        "dm_targets",
        "storage_domains",
    }
    if set(evidence_text) != required:
        return False
    for item in evidence_text.values():
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            return False
        digest = hashlib.sha256(item["text"].encode("utf-8")).hexdigest()
        if item.get("sha256") != digest:
            return False

    artifact_hashes = formal.get("artifact_hashes", {})
    for key, filename in (
        ("deterministic", "deterministic.txt"),
        ("property_default", "property_default.txt"),
        ("property_gate", "property_gate.txt"),
    ):
        if evidence_text[key]["sha256"] != artifact_hashes.get(filename):
            return False

    supplemental = data.get("supplemental_sha256", {})
    for key in ("process_crash", "deterministic_soak_auditor", "dm_targets", "storage_domains"):
        source_path = "artifacts/" + str(evidence_text[key].get("path", ""))
        if evidence_text[key]["sha256"] != supplemental.get(source_path):
            return False

    # Semantic checks prevent a structurally valid but empty evidence packet
    # from satisfying the gate. They do not replace the exact GitHub run/artifact
    # provenance recorded in the sign-off.
    property_gate = evidence_text["property_gate"]["text"]
    if property_gate.count("1500 passing, 0 failing") < 2:
        return False
    if "PASS: 20 seeds x 50 actions" not in evidence_text["deterministic_soak_auditor"]["text"]:
        return False
    if not re.search(r"^delay\s", evidence_text["dm_targets"]["text"], re.MULTILINE):
        return False

    drills = storage.get("drills", {})
    disk = drills.get("disk_full", {})
    wal = drills.get("wal_corruption", {})
    fsync = drills.get("fsync_stall", {})
    if disk.get("passed") is not True or disk.get("exit_code") != 10 or disk.get("fence_present") is not True:
        return False
    crossing = wal.get("forced_crossing", {})
    if wal.get("passed") is not True or crossing.get("passed") is not True or crossing.get("exit_code") != 15:
        return False
    stalling = fsync.get("stalling", {})
    healthy = fsync.get("healthy", {})
    if (
        fsync.get("passed") is not True
        or fsync.get("mechanism") != "dm-delay"
        or healthy.get("passed") is not True
        or healthy.get("broker_write_count") != 1
        or stalling.get("passed") is not True
        or stalling.get("exit_code") != 10
        or stalling.get("post_fault_broker_writes") != 0
        or stalling.get("fence_present") is not True
    ):
        return False

    scope = data.get("scope_limits", {})
    if scope.get("windows_real_faults") != "NOT_RUN_ACCEPTED_NON_BLOCKER":
        return False
    return True


def validate(root: Path, freeze: str) -> Optional[Attestation]:
    """Return the valid owner attestation for ``freeze`` or ``None``."""
    if not HEX40.fullmatch(freeze):
        return None
    signoff, evidence = paths_for(root, freeze)
    if not signoff.exists() or not evidence.exists() or not _evidence_is_valid(evidence, freeze):
        return None

    text = signoff.read_text(encoding="utf-8")
    if _table_value(text, "`commit_sha`") != freeze:
        return None

    owner = _table_value(text, "Owner")
    accepted_at = _table_value(text, "Accepted at (UTC)")
    if not owner or owner in {"—", "TBD"}:
        return None
    if not accepted_at or accepted_at in {"—", "TBD"}:
        return None

    required_owner_decisions = {
        "B1 scope acceptance": "ACCEPT",
        "Overnight risk acceptance": "ACCEPT",
        "Windows gap acceptance": "ACCEPT",
        "Real IB scope": "DEFER_TO_B2",
        "Additional B1-level hazard identified": "NO",
        "Decision": "PASS",
    }
    for field, expected in required_owner_decisions.items():
        if _table_value(text, field).strip("`") != expected:
            return None

    # The owner explicitly accepted the current five-share SPY maximum. The
    # 15% stress and $500 budget are recorded frozen mechanism parameters, not
    # overclaimed as separately derived human judgements. All three still bind
    # the sign-off to the exact risk configuration and force re-review on change.
    frozen_risk = {
        "Accepted max_position_shares": _config_scalar(root, "max_position_shares"),
        "Recorded overnight_gap_stress_pct": _config_scalar(root, "overnight_gap_stress_pct"),
        "Recorded max_overnight_loss": _config_scalar(root, "max_overnight_loss"),
    }
    if not all(frozen_risk.values()):
        return None
    for field, expected in frozen_risk.items():
        if _table_value(text, field).strip("`") != expected:
            return None

    evidence_hash = sha256_file(evidence)
    if _table_value(text, "Evidence snapshot sha256") != evidence_hash:
        return None
    data = json.loads(evidence.read_text(encoding="utf-8"))
    workflow = data["workflow"]
    if _table_value(text, "Freeze campaign run") != str(workflow["run_id"]):
        return None
    if _table_value(text, "Freeze artifact digest") != workflow["artifact_digest"]:
        return None

    head = _git(root, "rev-parse", "HEAD")
    if head.returncode != 0:
        return None
    current = head.stdout.strip()
    allowed = allowed_attestation_paths(root, freeze)

    if current != freeze:
        ancestor = _git(root, "merge-base", "--is-ancestor", freeze, current)
        if ancestor.returncode != 0:
            return None
        diff = _git(root, "diff", "--name-only", f"{freeze}..{current}")
        if diff.returncode != 0:
            return None
        committed = {line for line in diff.stdout.splitlines() if line}
        if not committed <= allowed:
            return None
        signoff_rel = signoff.relative_to(root).as_posix()
        evidence_rel = evidence.relative_to(root).as_posix()
        if signoff_rel not in committed or evidence_rel not in committed:
            return None

    dirty = _changed_worktree_paths(root)
    if dirty is None or not dirty <= allowed:
        return None

    return Attestation(freeze, signoff, evidence, evidence_hash)


def derive_signed_off_commit(root: Path) -> Optional[str]:
    """Find the unique valid Gate B1 owner-accepted freeze for this checkout."""
    docs = root / "docs"
    if not docs.exists():
        return None
    valid: list[str] = []
    for path in sorted(docs.glob(f"{SIGNOFF_PREFIX}*.md")):
        suffix = path.stem.removeprefix(SIGNOFF_PREFIX)
        if not re.fullmatch(r"[0-9a-f]{12}", suffix):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        freeze = _table_value(text, "`commit_sha`")
        if freeze.startswith(suffix) and validate(root, freeze) is not None:
            valid.append(freeze)
    unique = sorted(set(valid))
    if len(unique) > 1:
        raise RuntimeError(f"multiple valid Gate B1 attestations: {unique}")
    return unique[0] if unique else None
