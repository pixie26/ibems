"""
The Gate B1 requirement registry.

    ####################################################################
    #  One list, in code.  STATE.json is generated from it and the     #
    #  sign-off template is checked against it, so a requirement       #
    #  cannot exist in one place and be missing from another.          #
    ####################################################################

WHY THIS IS A MODULE AND NOT A MARKDOWN LIST
--------------------------------------------
B1.6 was added to the blocker list in three documents and in
``provenance.DEFAULT_GATE_STATUS``, and ``STATE.json`` -- the file the README
calls the single authoritative machine-readable state -- kept reporting seven
blockers. Two reasons, both structural:

* ``write_state`` carried the previous ``gate_status`` forward verbatim, so a
  new requirement in the defaults never reached a repository that already had
  a ``STATE.json``.
* ``stale_fields`` compared tree hashes only, so nothing failed.

That is the same drift the provenance module was written to stop, reproduced
inside the provenance module. The fix is not more care: it is to have exactly
one definition and to derive everything else from it.

STATUS VALUES
-------------
``OPEN``
    Not done. No evidence exists.

``PARTIAL``
    Some evidence exists and is recorded, but the requirement is not
    satisfied. Deliberately distinct from OPEN: "we ran the drill and one of
    three sub-drills fails" is a different situation from "we never ran it",
    and collapsing them is how a gate creeps forward on vibes.

``READY_FOR_FREEZE``
    The implementation and its evidence are complete *as code*. Not the same
    as PASS -- the evidence still has to be regenerated against the frozen
    commit, and a human still has to sign it.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

OPEN = "OPEN"
PARTIAL = "PARTIAL"
READY_FOR_FREEZE = "READY_FOR_FREEZE"

VALID_STATUSES = (OPEN, PARTIAL, READY_FOR_FREEZE)


@dataclass(frozen=True)
class Requirement:
    id: str
    title: str
    status: str
    evidence: str
    note: str = ""

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise ValueError(f"{self.id}: unknown status {self.status!r}")


B1_REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement(
        id="B1.0",
        title="reproducible environment",
        status=READY_FOR_FREEZE,
        evidence="uv.lock + gate manifest resolved_environment_sha256",
    ),
    Requirement(
        id="B1.1",
        title="single-writer process ownership",
        status=READY_FOR_FREEZE,
        evidence="tests/test_journal_ownership.py (two real processes, SIGKILL)",
    ),
    Requirement(
        id="B1.2",
        title="calendar fail-closed outside verified coverage",
        status=READY_FOR_FREEZE,
        evidence="tests/test_calendar_coverage.py",
    ),
    Requirement(
        id="B1.3a",
        title="fatal host exit and frozen supervisor policy",
        status=READY_FOR_FREEZE,
        evidence="tests/test_fatal_fence.py, deploy/, tests/test_supervisor.py",
    ),
    Requirement(
        id="B1.3b",
        title="durable fatal fence survives repair and restart",
        status=READY_FOR_FREEZE,
        evidence="tests/test_fatal_fence.py::test_a_repaired_journal_still_refuses_to_trade",
    ),
    Requirement(
        id="B1.4",
        title="real storage faults on a constrained volume",
        status=READY_FOR_FREEZE,
        evidence=(
            "scripts/run_storage_fault_drill.py, artifacts/gate_b1_storage/, "
            "GitHub Actions b1-storage-fsync run 31268292501"
        ),
        note=(
            "All three real-storage sub-drills have passed. On 96MB loop ext4 "
            "(-m 0), fence on a separate volume: disk_full PASS (real ENOSPC -> "
            "fence -> exit 10); wal_corruption PASS (real WAL rollback measured, "
            "loss above the witness tolerated and forced crossing refused with "
            "exit 15 + fence). fsync_stall PASS on Ubuntu 24.04 / Linux 6.17 Azure "
            "with real dm-delay v1.5.0 and separate constrained filesystems: 200ms "
            "healthy control reached one observed place_order while remaining alive "
            "and unfenced; live reload to 45s before the target crossed the 30s "
            "journal timeout, raised the durable fence, exited 10, and recorded zero "
            "broker writes after the fault. The first real dm-delay run also found "
            "and fixed a teardown defect: unmounting while delay remained 45s could "
            "time out and mask the behavioral result; teardown now resets delay to "
            "0 and is bounded/best-effort. This is pre-freeze engineering evidence; "
            "the complete campaign must still be regenerated against the exact "
            "freeze commit for B1.5."
        ),
    ),
    Requirement(
        id="B1.5",
        title="independent sign-off bound to an exact commit",
        status=OPEN,
        evidence="docs/GATE_B1_SIGNOFF_TEMPLATE.md",
        note="unsigned; must be redone against the freeze commit",
    ),
    Requirement(
        id="B1.6",
        title="out-of-band witness that committed events still exist",
        status=READY_FOR_FREEZE,
        evidence=(
            "src/ib_execution/journal_witness.py, tests/test_journal_witness.py, "
            "artifacts/gate_b1_storage/ (forced_crossing -> exit 15 + fence)"
        ),
    ),
)


def requirements() -> tuple[Requirement, ...]:
    return B1_REQUIREMENTS


def requirement_ids() -> list[str]:
    return [r.id for r in B1_REQUIREMENTS]


def open_requirements() -> list[Requirement]:
    return [r for r in B1_REQUIREMENTS if r.status != READY_FOR_FREEZE]


def ready_for_freeze() -> bool:
    """Every requirement implemented. Still not PASS -- see the module docstring."""
    return not open_requirements()


def as_state() -> dict[str, Any]:
    """The gate section of STATE.json, derived rather than remembered."""
    return {
        "gate_b1": "NOT_PASSED",
        "gate_b2": "NOT_STARTED",
        "trading_adapter": "NOT_IMPLEMENTED",
        "signed_off_commit": None,
        "ready_for_freeze": ready_for_freeze(),
        "requirements": [asdict(r) for r in B1_REQUIREMENTS],
    }
