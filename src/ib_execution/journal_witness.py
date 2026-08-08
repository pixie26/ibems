"""
Out-of-band witness that a specific committed event still exists.

    ####################################################################
    #  Gate B1.6.  `commit()` returning success does not mean the      #
    #  event is still there after a crash.  Measured on a real ext4    #
    #  volume: 27 of 4,406 committed events gone after WAL header      #
    #  damage, no error from SQLite, engine started normally.          #
    ####################################################################

WHAT BREAKS WITHOUT THIS
------------------------
Invariant 2 says every broker write is preceded by a durable intent, and the
controller implements it exactly:

    commit(SEND_ATTEMPT_STARTED)   -> returns, so the intent is durable
    broker.place_order(...)        -> the order now exists at IB

A crash between the two is survivable: the restart finds the event, knows a
send may have happened, and reconciles instead of resending. That argument
assumes the committed event is still there afterwards.

WAL recovery does not guarantee that. SQLite validates frame checksums and
recovers up to the last frame that verifies; damage to the WAL header or to a
frame invalidates everything from that point on. What is left is a database
that is *internally consistent and simply shorter*. ``PRAGMA integrity_check``
passes, because nothing is corrupt -- there are just fewer rows, and SQLite has
no idea there should be more. ``synchronous=FULL`` governs how a commit reaches
the medium; it cannot protect the medium afterwards.

So the worst case is:

    commit(SEND_ATTEMPT_STARTED)  seq 4380, durable
    place_order(X)                X is live at IB
    crash + WAL damage
    restart                       seq 4380 is gone
    -> the engine believes it never sent X

WHY A BARE HIGH-WATER NUMBER IS NOT ENOUGH
------------------------------------------
"the journal has at least 4380 rows" is weaker than what is actually needed.
The witness therefore binds the *identity* of the event that authorised the
side effect: the journal it belongs to, its sequence, its type, the intent and
order it refers to, and a digest of its content. Startup then proves the
specific piece of durable evidence is still present and still says the same
thing -- not merely that the file is long enough. It also catches a restored
backup or a swapped journal file, which a sequence number alone cannot.

WHERE IT IS WRITTEN, AND HOW OFTEN
----------------------------------
On the fence volume: a separate failure domain from the journal, for the same
reason the fatal fence lives there.

Not on every commit. A second cross-volume fsync per commit would double
commit latency to protect heartbeats and telemetry. It is written at the
boundary that matters -- immediately before a broker write, after the
authorising event is durable. Sends are rare on a single-symbol platform and
that path already pays a full fsync.

Coverage beyond broker writes was an open question, and the adversarial drill
in ``tests/test_journal_witness.py`` answered it: a rollback can drop a HALT
while leaving ``max_seq`` above the witnessed send, so a broker-write-only
witness is satisfied while invariant 22 -- "a restart cannot clear a HALT" --
is broken by storage. ``SAFETY_CRITICAL_TYPES`` therefore also covers the HALT
events. Still one record: WAL recovery truncates a tail rather than punching
holes, so pinning the latest safety-critical event bounds the earlier ones.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import EventType

WITNESS_SCHEMA_VERSION = 1

# Event types whose loss is dangerous even though they are not broker writes.
#
# This started empty, on the theory that a witness pinned to the last broker
# write covered everything that mattered. The HALT tail-loss drill in
# tests/test_journal_witness.py disproved that: a rollback can drop a HALT
# while still leaving max_seq above the witnessed send, so the witness passes,
# the restart replays a journal with no HALT in it, and invariant 22 -- "a
# restart cannot clear a HALT" -- is broken by storage rather than by logic.
#
# One record still suffices. WAL recovery truncates a tail rather than punching
# holes, so pinning the *latest* safety-critical event bounds the loss of every
# earlier one too.
SAFETY_CRITICAL_TYPES: frozenset[EventType] = frozenset(
    {EventType.OPERATING_MODE_CHANGED, EventType.HALT_CAUSE_ADDED}
)


class WitnessWriteFailed(RuntimeError):
    """The witness could not be persisted, so the broker write must not happen."""


class WitnessViolation(RuntimeError):
    """The journal no longer contains the evidence that authorised a broker write."""


@dataclass(frozen=True)
class WitnessRecord:
    journal_id: str
    seq: int
    event_type: str
    digest: str
    written_utc: str
    intent_id: Optional[str] = None
    order_ref: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {"schema_version": WITNESS_SCHEMA_VERSION, **asdict(self)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WitnessRecord":
        return cls(
            journal_id=str(payload["journal_id"]),
            seq=int(payload["seq"]),
            event_type=str(payload["event_type"]),
            digest=str(payload["digest"]),
            written_utc=str(payload.get("written_utc", "")),
            intent_id=payload.get("intent_id"),
            order_ref=payload.get("order_ref"),
        )


def event_digest(event) -> str:
    """A canonical digest of the parts that make the event mean what it means.

    Timestamps are excluded: they are recorded, but they are not what the
    invariant-2 argument rests on, and including them would make the digest
    sensitive to clock representation rather than to content.
    """
    material = {
        "seq": event.seq,
        "event_type": event.event_type.value,
        "strategy_id": event.strategy_id,
        "symbol": event.symbol,
        "decision_id": event.decision_id,
        "intent_id": event.intent_id,
        "order_ref": event.order_ref,
        "perm_id": event.perm_id,
        "exec_id": event.exec_id,
        "payload": event.payload,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class JournalWitness:
    """Records, and later verifies, the evidence behind each broker write."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    # -- reading ---------------------------------------------------------

    def read(self) -> Optional[WitnessRecord]:
        if not self.path.exists():
            return None
        try:
            return WitnessRecord.from_dict(
                json.loads(self.path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            # An unreadable witness cannot clear the journal, and treating it as
            # absent would make corruption of *this* file the way to bypass the
            # check it performs.
            raise WitnessViolation(
                f"the journal witness at {self.path} exists but could not be read: {exc}"
            ) from exc

    # -- writing ---------------------------------------------------------

    def record(self, journal, seq: int) -> WitnessRecord:
        """Pin the event at ``seq``. Must succeed before the broker write.

        Reads the event back out of the journal rather than trusting an
        in-memory copy: what has to be proved later is that *the database*
        holds this evidence, so that is what gets digested.
        """
        event = journal.event_at(seq)
        if event is None:
            raise WitnessWriteFailed(
                f"cannot witness seq {seq}: it is not in the journal immediately "
                "after being committed"
            )
        record = WitnessRecord(
            journal_id=journal.journal_id,
            seq=seq,
            event_type=event.event_type.value,
            digest=event_digest(event),
            written_utc=datetime.now(timezone.utc).isoformat(),
            intent_id=event.intent_id,
            order_ref=event.order_ref,
        )
        try:
            self._durable_write(record)
        except OSError as exc:
            raise WitnessWriteFailed(
                f"could not persist the journal witness at {self.path}: {exc}"
            ) from exc
        return record

    def _durable_write(self, record: WitnessRecord) -> None:
        payload = json.dumps(record.as_dict(), indent=2, sort_keys=True).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)
        dir_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        except OSError:  # pragma: no cover - unsupported on some platforms
            pass
        finally:
            os.close(dir_fd)

    # -- verification ----------------------------------------------------

    def verify(self, journal) -> Optional[WitnessRecord]:
        """Startup check. Raises ``WitnessViolation`` if the evidence is gone.

        Runs before the broker is constructed. Four distinct ways to fail, all
        of which mean the journal cannot be trusted to say what was already
        sent:
        """
        record = self.read()
        if record is None:
            return None                       # nothing was ever witnessed

        if record.journal_id != journal.journal_id:
            raise WitnessViolation(
                f"the witness belongs to journal {record.journal_id} but this journal "
                f"is {journal.journal_id}. A restored backup or a swapped file cannot "
                "account for broker writes the witness recorded."
            )

        max_seq = journal.max_seq()
        if max_seq < record.seq:
            raise WitnessViolation(
                f"the journal ends at seq {max_seq} but a broker write was authorised "
                f"by seq {record.seq}. {record.seq - max_seq} committed event(s) are "
                "missing -- WAL recovery discards frames it cannot verify, and an "
                "order may be live at the broker with no local record of it."
            )

        event = journal.event_at(record.seq)
        if event is None:
            raise WitnessViolation(
                f"seq {record.seq} authorised a broker write and is no longer in the "
                "journal, although later sequences are."
            )

        actual = event_digest(event)
        if actual != record.digest:
            raise WitnessViolation(
                f"seq {record.seq} is present but differs from the event that "
                f"authorised a broker write (digest {actual} != {record.digest})."
            )
        return record
