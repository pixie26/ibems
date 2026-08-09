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
    The implementation and its evidence mechanism are complete *as code*. Not
    the same as PASS -- the evidence still has to be regenerated against the
    frozen commit, and the project owner still has to accept the exact-freeze
    residual risks and scope. For B1.5 in particular, READY_FOR_FREEZE means
    the attestation protocol is enforceable; the actual owner decision is
    represented by the committed sign-off and evidence snapshot, from which
    STATE.json re-derives PASS.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

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
            ".github/workflows/b1-freeze-campaign.yml"
        ),
        note=(
            "All three real-storage mechanisms have passed in engineering runs: "
            "disk_full (real ENOSPC -> fence -> exit 10), wal_corruption (measured "
            "rollback plus forced witness crossing -> exit 15 + fence), and "
            "fsync_stall (real dm-delay, healthy control then 45s live stall -> "
            "30s journal timeout -> fence -> exit 10 with zero post-fault broker "
            "writes). Final attestation must bind one exact unified freeze campaign; "
            "the exact run and artifact digest live in its durable evidence snapshot."
        ),
    ),
    Requirement(
        id="B1.5",
        title="owner risk acceptance bound to an exact freeze",
        status=READY_FOR_FREEZE,
        evidence=(
            "docs/GATE_B1_SIGNOFF_TEMPLATE.md, scripts/finalize_gate_b1.py, "
            "src/ib_execution/attestation.py, tests/test_attestation.py"
        ),
        note=(
            "B1 technical evidence is produced by the frozen test/fault campaign and "
            "adversarial code review. The human step is explicitly owner risk acceptance, "
            "not a claim that the owner independently audited every line of code. The owner "
            "must accept the B1 scope, invariant-19 overnight limits, the Linux-only Windows "
            "gap, and the deferral of real-IB behavior to B2; the owner must also state "
            "whether any additional B1-level hazard is known. Gate B1 becomes PASS only "
            "when that exact-freeze owner acceptance and durable evidence snapshot exist and "
            "the attestation diff contains metadata only. PASS is re-derived on every "
            "STATE.json regeneration; it is never carried forward by hand."
        ),
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
    """Every requirement implemented. Still not PASS without an attestation."""
    return not open_requirements()


def as_state(signed_off_commit: Optional[str] = None) -> dict[str, Any]:
    """Derive the Gate section of STATE.json.

    ``signed_off_commit`` is itself derived by ``attestation`` from the exact
    freeze owner acceptance, durable evidence snapshot and Git history. Passing
    an attestation cannot override an incomplete registry.
    """
    passed = signed_off_commit is not None and ready_for_freeze()
    return {
        "gate_b1": "PASS" if passed else "NOT_PASSED",
        "gate_b2": "NOT_STARTED",
        "trading_adapter": "NOT_IMPLEMENTED",
        "signed_off_commit": signed_off_commit if passed else None,
        "ready_for_freeze": ready_for_freeze(),
        "requirements": [asdict(r) for r in B1_REQUIREMENTS],
    }
