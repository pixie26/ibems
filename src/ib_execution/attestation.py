"""Gate B1 attestation derivation.

A Gate B1 PASS is not mutable state. It is a conclusion that can be recomputed
from three durable facts:

* every B1 registry requirement is freeze-ready (enforced by ``gate.as_state``);
* an exact-freeze sign-off document records a named reviewer, UTC review time,
  PASS decision and the evidence snapshot hash;
* the current commit is either that freeze (while finalizing) or a descendant
  whose diff from the freeze contains only the approved metadata files.

This module deliberately does not import ``gate`` to avoid a cycle. It answers
only the factual question "is there a valid signed freeze attestation here?".
``gate.as_state`` decides whether the registry is complete enough for that
attestation to imply PASS.
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
    if not isinstance(workflow.get("run_id"), int) or workflow["run_id"] <= 0:
        return False
    if not ARTIFACT_DIGEST.fullmatch(str(workflow.get("artifact_digest", ""))):
        return False

    formal = data.get("formal_manifest", {})
    storage = data.get("storage_manifest", {})
    hashes = data.get("manifest_sha256", {})
    if not HEX64.fullmatch(str(hashes.get("formal", ""))):
        return False
    if not HEX64.fullmatch(str(hashes.get("storage", ""))):
        return False
    if formal.get("commit_sha") != freeze or storage.get("commit_sha") != freeze:
        return False
    if formal.get("passed") is not True or formal.get("worktree_clean") is not True:
        return False
    if storage.get("passed") is not True or storage.get("worktree_clean") is not True:
        return False
    if storage.get("inconclusive") != []:
        return False

    # These identities must describe the same tested tree/environment.
    for key in (
        "source_tree_sha256",
        "dependency_lock_sha256",
        "resolved_environment_sha256",
    ):
        if not formal.get(key) or formal.get(key) != storage.get(key):
            return False
    return True


def validate(root: Path, freeze: str) -> Optional[Attestation]:
    """Return the valid attestation for ``freeze`` or ``None``.

    At the tested freeze HEAD the sign-off/evidence files may be untracked while
    the finalizer is preparing STATE. After the attestation commit, the freeze
    must be an ancestor and the committed diff may contain only STATE, sign-off
    and evidence snapshot. Any source/config/test/dependency change makes the
    old PASS non-derivable.
    """
    if not HEX40.fullmatch(freeze):
        return None
    signoff, evidence = paths_for(root, freeze)
    if not signoff.exists() or not evidence.exists() or not _evidence_is_valid(evidence, freeze):
        return None

    text = signoff.read_text(encoding="utf-8")
    if _table_value(text, "`commit_sha`") != freeze:
        return None
    reviewer = _table_value(text, "Reviewer")
    reviewed_at = _table_value(text, "Reviewed at (UTC)")
    decision = _table_value(text, "Decision").strip("`")
    if not reviewer or reviewer in {"—", "TBD"}:
        return None
    if not reviewed_at or reviewed_at in {"—", "TBD"}:
        return None
    if decision != "PASS":
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
    """Find the unique valid Gate B1 signed freeze for this checkout."""
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
