"""Finalize Gate B1 after an independent exact-freeze review.

This command does not perform the review and cannot manufacture a signature.
It validates a completed sign-off document, then writes the only machine-readable
PASS claim to STATE.json.

The tested commit and the attestation commit are deliberately different:

    freeze commit      -- code/config/dependencies/tests exercised by campaign
    attestation commit -- only STATE.json + docs/GATE_B1_SIGNOFF_<freeze>.md

A commit cannot contain its own hash, so requiring those identities to be the
same is impossible. tests/test_provenance.py enforces that the freeze commit is
an ancestor of the attestation commit and that no behavioural file changed in
between.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

from ib_execution import gate, provenance

ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
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


def _validate_signoff(path: Path, freeze: str) -> None:
    if not path.exists():
        raise SystemExit(f"missing independent sign-off: {path.relative_to(ROOT)}")
    text = path.read_text(encoding="utf-8")
    if _table_value(text, "`commit_sha`") != freeze:
        raise SystemExit("sign-off commit_sha does not match --freeze-commit")
    reviewer = _table_value(text, "Reviewer")
    reviewed_at = _table_value(text, "Reviewed at (UTC)")
    decision = _table_value(text, "Decision").strip("`")
    if not reviewer or reviewer in {"—", "TBD"}:
        raise SystemExit("sign-off requires a named independent reviewer")
    if not reviewed_at or reviewed_at in {"—", "TBD"}:
        raise SystemExit("sign-off requires Reviewed at (UTC)")
    if decision != "PASS":
        raise SystemExit(f"sign-off Decision must be PASS, got {decision!r}")


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

    signoff = ROOT / "docs" / f"GATE_B1_SIGNOFF_{freeze[:12]}.md"
    _validate_signoff(signoff, freeze)

    # Before writing STATE, only the sign-off document may differ from the
    # tested freeze. This catches an operator who edited code after the campaign
    # but before running this command.
    status = _git("status", "--porcelain")
    if status.returncode != 0:
        raise SystemExit(status.stderr.strip() or "git status failed")
    changed: set[str] = set()
    for line in status.stdout.splitlines():
        if len(line) >= 4:
            changed.add(line[3:])
    expected = {signoff.relative_to(ROOT).as_posix()}
    if changed - expected:
        raise SystemExit(
            "refusing to attest a dirty freeze; unexpected changes: "
            + ", ".join(sorted(changed - expected))
        )

    gate_status = gate.as_state()
    gate_status["gate_b1"] = "PASS"
    gate_status["signed_off_commit"] = freeze
    provenance.write_state(ROOT, gate_status=gate_status)

    print(f"validated independent sign-off: {signoff.relative_to(ROOT)}")
    print("wrote STATE.json with Gate B1 PASS")
    print("commit ONLY the sign-off document and STATE.json as the attestation commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
