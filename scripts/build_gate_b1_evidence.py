"""Build a durable, self-contained Gate B1 evidence snapshot from one artifact ZIP.

GitHub Actions artifacts expire. The signed evidence snapshot therefore embeds
the exact formal/storage manifest bytes plus the compact human-reviewable
transcripts that establish Hypothesis, crash-window, auditor and storage-domain
results. Their hashes are checked while building the snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _single(zf: zipfile.ZipFile, prefix: str, suffix: str) -> tuple[str, bytes]:
    matches = [name for name in zf.namelist() if name.startswith(prefix) and name.endswith(suffix)]
    if len(matches) != 1:
        raise SystemExit(f"expected one {prefix}*{suffix} in artifact, found {matches}")
    name = matches[0]
    return name, zf.read(name)


def _supplemental_hashes(zf: zipfile.ZipFile) -> dict[str, str]:
    try:
        raw = zf.read("gate_b1_extra/SHA256SUMS").decode("utf-8")
    except KeyError as exc:
        raise SystemExit("artifact missing gate_b1_extra/SHA256SUMS") from exc
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        digest, path = line.split(None, 1)
        path = path.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SystemExit(f"invalid supplemental SHA-256 line: {line!r}")
        out[path] = digest
    return out


def _read_text(zf: zipfile.ZipFile, path: str) -> str:
    try:
        raw = zf.read(path)
    except KeyError as exc:
        raise SystemExit(f"artifact missing required evidence text: {path}") from exc
    return raw.decode("utf-8")


def build_snapshot(
    artifact_zip: Path,
    *,
    freeze_commit: str,
    run_id: int,
    artifact_digest: str,
    artifact_name: str,
) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", freeze_commit):
        raise SystemExit("freeze commit must be a full lowercase 40-character SHA")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_digest):
        raise SystemExit("artifact digest must be sha256:<64 lowercase hex>")

    with zipfile.ZipFile(artifact_zip) as zf:
        formal_name, formal_raw = _single(zf, "gate_b1/", "/manifest.json")
        storage_name, storage_raw = _single(zf, "gate_b1_storage/", "/manifest.json")
        formal = json.loads(formal_raw)
        storage = json.loads(storage_raw)
        supplemental_hashes = _supplemental_hashes(zf)

        formal_dir = formal_name.rsplit("/", 1)[0]
        formal_paths = {
            "deterministic": f"{formal_dir}/deterministic.txt",
            "property_default": f"{formal_dir}/property_default.txt",
            "property_gate": f"{formal_dir}/property_gate.txt",
        }
        supplemental_paths = {
            "process_crash": "gate_b1_extra/process_crash.txt",
            "deterministic_soak_auditor": "gate_b1_extra/deterministic_soak_auditor.txt",
            "dm_targets": "gate_b1_extra/dm-targets.txt",
            "storage_domains": "gate_b1_extra/storage_domains.txt",
        }
        evidence_text: dict[str, dict[str, str]] = {}
        for key, path in {**formal_paths, **supplemental_paths}.items():
            text = _read_text(zf, path)
            digest = _sha256(text.encode("utf-8"))
            evidence_text[key] = {"path": path, "sha256": digest, "text": text}

    if formal.get("commit_sha") != freeze_commit or storage.get("commit_sha") != freeze_commit:
        raise SystemExit("artifact manifests do not bind to the requested freeze commit")
    if formal.get("passed") is not True or formal.get("worktree_clean") is not True:
        raise SystemExit("formal Gate B1 manifest is not a clean PASS")
    if storage.get("passed") is not True or storage.get("worktree_clean") is not True:
        raise SystemExit("storage manifest is not a clean PASS")
    if storage.get("inconclusive") != []:
        raise SystemExit(f"storage manifest is inconclusive: {storage.get('inconclusive')!r}")
    for key in ("source_tree_sha256", "dependency_lock_sha256", "resolved_environment_sha256"):
        if formal.get(key) != storage.get(key):
            raise SystemExit(f"formal/storage manifests disagree on {key}")

    artifact_hashes = formal.get("artifact_hashes", {})
    for key, filename in (
        ("deterministic", "deterministic.txt"),
        ("property_default", "property_default.txt"),
        ("property_gate", "property_gate.txt"),
    ):
        if evidence_text[key]["sha256"] != artifact_hashes.get(filename):
            raise SystemExit(f"formal transcript hash mismatch: {filename}")
    for key in ("process_crash", "deterministic_soak_auditor", "dm_targets", "storage_domains"):
        path = "artifacts/" + evidence_text[key]["path"]
        if evidence_text[key]["sha256"] != supplemental_hashes.get(path):
            raise SystemExit(f"supplemental transcript hash mismatch: {path}")

    return {
        "schema_version": 1,
        "freeze_commit": freeze_commit,
        "workflow": {
            "name": "b1-freeze-campaign",
            "run_id": run_id,
            "artifact_name": artifact_name,
            "artifact_digest": artifact_digest,
        },
        "manifest_paths": {"formal": formal_name, "storage": storage_name},
        "manifest_sha256": {"formal": _sha256(formal_raw), "storage": _sha256(storage_raw)},
        "formal_manifest_raw": formal_raw.decode("utf-8"),
        "storage_manifest_raw": storage_raw.decode("utf-8"),
        "evidence_text": evidence_text,
        "supplemental_sha256": supplemental_hashes,
        "scope_limits": {
            "windows_real_faults": "NOT_RUN_ACCEPTED_NON_BLOCKER",
            "windows_note": (
                "B1 real-storage evidence is Linux-only. msvcrt.locking, Windows volume-serial "
                "failure-domain checks, NTFS ENOSPC/stall behavior, and "
                "deploy/ibems-execution-service.ps1 were not exercised. Owner accepts this as "
                "a non-blocker for B2 read-only/paper progression; validate the production OS "
                "before any order-capable Windows deployment."
            ),
            "fsync_backing_store": (
                "dm-delay block-layer stall is real; the GitHub-hosted constrained filesystem "
                "is tmpfs-backed rather than persistent physical media."
            ),
            "broker_scope": (
                "B1 broker behavior uses FakeBroker; real IB portions of invariants 10, 14 and "
                "18 remain Gate B2."
            ),
            "recorder_scope": "No complete Full-RTH recorder session is claimed by Gate B1.",
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build durable Gate B1 evidence snapshot")
    ap.add_argument("--artifact-zip", required=True, type=Path)
    ap.add_argument("--freeze-commit", required=True)
    ap.add_argument("--run-id", required=True, type=int)
    ap.add_argument("--artifact-digest", required=True)
    ap.add_argument("--artifact-name", required=True)
    ns = ap.parse_args(argv)

    snapshot = build_snapshot(
        ns.artifact_zip,
        freeze_commit=ns.freeze_commit,
        run_id=ns.run_id,
        artifact_digest=ns.artifact_digest,
        artifact_name=ns.artifact_name,
    )
    output = ROOT / "docs" / f"GATE_B1_EVIDENCE_{ns.freeze_commit[:12]}.json"
    output.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print(output)
    print(f"sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
