"""Finalize Gate B1 after an independent exact-freeze review.

This command does not perform the review and cannot manufacture a signature.
It validates the completed sign-off and durable evidence snapshot, then asks
``provenance`` to regenerate STATE.json. PASS is never written as an override;
it must be re-derived from the attestation and therefore survives future
regeneration only while that attestation remains valid.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

from ib_execution import attestation, gate, provenance

ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Finalize an independently reviewed Gate B1")
    ap.add_argument("--freeze-commit", required=True)
    ns = ap.parse_args(argv)
    freeze = ns.freeze_commit.strip()

    if len(freeze) != 40 or not re.fullmatch(r"[0-9a-f]{40}", freeze):
        raise SystemExit("--freeze-commit must be a full 40-character lowercase git SHA")

    head = provenance.commit_sha(ROOT)
    if head != freeze:
        raise SystemExit(
            f"finalization must start with the tested freeze at HEAD; HEAD={head}, freeze={freeze}"
        )

    if not gate.ready_for_freeze():
        open_ids = [r.id for r in gate.open_requirements()]
        raise SystemExit(f"Gate B1 is not ready for freeze; open requirements: {open_ids}")

    signoff, evidence = attestation.paths_for(ROOT, freeze)
    if not signoff.exists():
        raise SystemExit(f"missing independent sign-off: {signoff.relative_to(ROOT)}")
    if not evidence.exists():
        raise SystemExit(f"missing durable evidence snapshot: {evidence.relative_to(ROOT)}")

    # Before STATE is regenerated, only the two attestation inputs may differ
    # from the exact tested freeze. Any implementation/config/test/dependency
    # edit invalidates the campaign and requires a new freeze.
    status = _git("status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0:
        raise SystemExit(status.stderr.strip() or "git status failed")
    changed: set[str] = set()
    for line in status.stdout.splitlines():
        if len(line) >= 4:
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            changed.add(path)
    expected = {
        signoff.relative_to(ROOT).as_posix(),
        evidence.relative_to(ROOT).as_posix(),
    }
    if changed - expected:
        raise SystemExit(
            "refusing to attest a dirty freeze; unexpected changes: "
            + ", ".join(sorted(changed - expected))
        )

    validated = attestation.validate(ROOT, freeze)
    if validated is None:
        raise SystemExit(
            "sign-off/evidence attestation is invalid; verify exact commit, reviewer/time/PASS, "
            "workflow run, artifact digest and evidence snapshot SHA-256"
        )

    provenance.write_state(ROOT)
    state = provenance.load_state(ROOT)
    if state is None or state["gate_status"].get("gate_b1") != "PASS":
        raise SystemExit("derived provenance did not produce Gate B1 PASS")
    if state["gate_status"].get("signed_off_commit") != freeze:
        raise SystemExit("derived provenance signed_off_commit does not match freeze")

    print(f"validated independent sign-off: {signoff.relative_to(ROOT)}")
    print(f"validated durable evidence: {evidence.relative_to(ROOT)}")
    print("regenerated STATE.json with derived Gate B1 PASS")
    print("commit ONLY STATE.json + sign-off + evidence snapshot as the attestation commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
