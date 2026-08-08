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
        status=PARTIAL,
        evidence="scripts/run_storage_fault_drill.py, artifacts/gate_b1_storage/",
        note=(
            "disk_full PASS on 96MB loop ext4 (exit 10, fence raised on the other "
            "volume). wal_corruption FAIL -> B1.6. fsync_stall implemented but not "
            "yet run on the production host."
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
        status=OPEN,
        evidence="src/ib_execution/journal_witness.py, tests/test_journal_witness.py",
        note=(
            "WAL recovery silently discards committed frames: 27 of 4,406 events "
            "vanished with no error. Breaks invariant 2, since a commit()ed "
            "SEND_ATTEMPT_STARTED can disappear while the order is live at IB."
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
