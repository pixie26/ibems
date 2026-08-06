"""
Acknowledge a durable HALT.

    python -m ib_execution.ack_halt --journal data/journal.db \
        --operator olivia --resolution "position mismatch was a manual TWS trade; account cleared"

WHY THIS EXISTS
---------------
A HALT is a durable statement: we found something we could not explain, so we
stopped. Before v0.1.4 a process restart silently cleared it, which meant
restarting was a way to launder a HALT. The most likely path was the worst one:
watchdog kills the engine, an operator (or systemd, or a well-meaning cron)
restarts it, and trading resumes with the root cause undiagnosed.

The RUNBOOK already said "HALT is correct behaviour, do not restart to clear
it." ADR-004 already made restart manual for the same reason. Neither was
enforced by anything. **Documentation is not a control.**

Clearing a HALT now requires a named operator and a written resolution, both
journalled, so that "who cleared this and what did they find" is answerable
months later when the same symptom recurs.

This tool is never invoked automatically. Not by the watchdog, not by a
supervisor, not by a retry loop. If you find yourself wanting to script it, the
thing to fix is whatever keeps producing HALTs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from .journal import HaltAcknowledgementConflict, Journal
from .models import EventType, OperatingMode


def find_unacknowledged_halt(journal: Journal) -> Optional[dict]:
    halt: Optional[dict] = None
    for ev in journal.replay():
        if (
            ev.event_type is EventType.OPERATING_MODE_CHANGED
            and ev.payload.get("to") == OperatingMode.HALTED.value
        ):
            halt = {
                "seq": ev.seq,
                "started_seq": ev.seq,
                "ts": ev.ts_utc.isoformat(),
                "why": ev.payload.get("why", ""),
            }
        elif ev.event_type is EventType.HALT_CAUSE_ADDED:
            started = halt["started_seq"] if halt is not None else ev.seq
            halt = {
                "seq": ev.seq,
                "started_seq": started,
                "ts": ev.ts_utc.isoformat(),
                "why": ev.payload.get("why", ""),
            }
        elif ev.event_type is EventType.HALT_ACKNOWLEDGED:
            acked = ev.payload.get("acknowledged_halt_seq")
            if halt is not None and acked is not None and int(acked) == halt["seq"]:
                halt = None
    return halt


def context_for(journal: Journal, seq: int, before: int = 12) -> list[str]:
    """The events leading up to the HALT, so the operator reads them first."""
    out = []
    for ev in journal.replay():
        if seq - before <= ev.seq <= seq:
            detail = ""
            if ev.event_type in (
                EventType.INVARIANT_VIOLATION,
                EventType.RECONCILIATION_FAILED,
                EventType.CALLBACK_FAILURE,
                EventType.EXTERNAL_ORDER_DETECTED,
                EventType.OPERATING_MODE_CHANGED,
                EventType.HALT_CAUSE_ADDED,
            ):
                detail = f"  {ev.payload}"
            out.append(f"  seq{ev.seq:5d}  {ev.event_type.value:28s}{detail}")
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Acknowledge a durable HALT")
    ap.add_argument("--journal", required=True)
    ap.add_argument("--operator", help="who is clearing this")
    ap.add_argument("--resolution", help="what you found and what you did")
    ap.add_argument("--show", action="store_true", help="show the HALT and exit")
    args = ap.parse_args(argv)

    path = Path(args.journal)
    if not path.exists():
        print(f"journal not found: {path}")
        return 2

    journal = Journal(path)
    try:
        halt = find_unacknowledged_halt(journal)
        if halt is None:
            print("No unacknowledged HALT. Nothing to do.")
            return 0

        print(
            f"UNACKNOWLEDGED HALT generation {halt['started_seq']}..{halt['seq']} "
            f"(latest cause at {halt['ts']})"
        )
        print(f"  reason: {halt['why']}\n")
        print("Leading events:")
        for line in context_for(journal, halt["seq"]):
            print(line)

        if args.show:
            return 0

        if not args.operator or not args.resolution:
            print(
                "\nRefusing to clear without --operator and --resolution.\n"
                "Read the events above and the broker state in TWS first.\n"
                "If you cannot explain the cause, do not clear it."
            )
            return 1

        try:
            journal.acknowledge_halt(halt["seq"], args.operator, args.resolution)
        except HaltAcknowledgementConflict as exc:
            print(f"\nRefusing to acknowledge: {exc}")
            print("The journal changed after it was inspected. Re-run --show and diagnose the current HALT.")
            return 3
        print(f"\nHALT at seq {halt['seq']} acknowledged by {args.operator}.")
        print(
            "The running engine remains HALTED. Stop/restart it manually; the new "
            "process must still restore residual state and reconcile broker truth before any write."
        )
        return 0
    finally:
        journal.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
