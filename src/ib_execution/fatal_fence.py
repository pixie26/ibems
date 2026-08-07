"""
Durable fatal fence.

    ####################################################################
    #  The journal is the component that failed.  It cannot also be    #
    #  the thing that remembers the failure.                           #
    ####################################################################

THE GAP THIS CLOSES
-------------------
``Controller._fail_closed_journal`` deliberately does not journal -- the
journal is unavailable, so another write would either block again or create
false confidence. ``_fail_closed_runtime`` therefore sets ``HALTED`` and
``fatal_shutdown_requested`` in memory and bypasses ``set_mode`` entirely, and
every ``commit`` on the surrounding failure paths is wrapped in
``except JournalUnavailable: pass``.

All of that is right. The consequence was not:

    1. journal writer dies (disk full, fsync stall, unexpected exception)
    2. the write gate fences: in-memory HALTED, fatal_shutdown_requested,
       and by design nothing durable
    3. the host exits non-zero
    4. an operator frees the disk; storage is healthy again
    5. restart: restore_from_journal replays, finds no HALT event -- because
       none could be written -- and returns NORMAL
    6. the engine trades again, and nobody decided that

Invariant 22 ("a restart cannot clear a HALT") holds throughout, and is
simply never reached: it can only protect a HALT that reached the disk. The
worst case is the journal dying mid ``ORDER_SENT`` commit, where IB may hold a
working order that the restarted engine has no durable record of -- invariants
2, 3 and 4 broken across a restart, which is the exact class of failure this
platform exists to prevent.

DESIGN
------
Out-of-band by necessity, and on a *separate failure domain* by requirement:
the fence lives on a different volume from the journal, because the journal's
volume being full is the most likely reason we are writing a fence at all. The
check is enforced at startup, not documented -- a fence configured onto the
journal's own volume silently provides no protection.

It is cheap where it has to be: ~200 bytes, ``O_CREAT|O_EXCL``, one write, two
fsyncs (file and directory). A filesystem that cannot extend a SQLite WAL can
usually still absorb that. When even this fails, the caller still has a
non-zero exit and a CRITICAL alert -- strictly better than before, and the
failure is reported as a failure rather than dressed up as a durable fence.

TWO-PHASE RETIREMENT
--------------------
An operator acknowledgement does not delete the fence:

    RAISED -> (operator acknowledges, with attribution) -> ACKNOWLEDGED
           -> (broker reconciliation verifies the account) -> retired

If acknowledgement retired the fence directly, clicking "yes" would itself be
the route back to trading, which is the same shortcut ``ack_halt`` exists to
close. The gap between raising and retiring is where somebody has to look at
the actual broker state -- that is the entire point of the fence.
"""

from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

FENCE_SCHEMA_VERSION = 1

STATE_RAISED = "RAISED"
STATE_ACKNOWLEDGED = "ACKNOWLEDGED"


class FenceWriteFailed(RuntimeError):
    """Even the fence could not be persisted. Never silently downgraded."""


class FenceDomainError(RuntimeError):
    """The fence shares a failure domain with the journal it is meant to outlive."""


class FenceStillRaised(RuntimeError):
    """A fence is present and has not been through acknowledgement + reconciliation."""

    def __init__(self, fence: "FatalFenceRecord"):
        self.fence = fence
        super().__init__(
            f"a durable fatal fence is present ({fence.state}): {fence.reason}\n"
            f"  raised at {fence.raised_utc} by pid {fence.pid}\n"
            "  Broker writes are prohibited. Acknowledge with "
            "`python -m ib_execution.fatal_fence --acknowledge`, then let a "
            "reconciliation pass retire it."
        )


@dataclass(frozen=True)
class FatalFenceRecord:
    state: str
    reason: str
    raised_utc: str
    pid: int
    host: str
    journal_path: str
    acknowledged_by: Optional[str] = None
    acknowledged_utc: Optional[str] = None
    resolution: Optional[str] = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FatalFenceRecord":
        return cls(
            state=str(payload.get("state", STATE_RAISED)),
            reason=str(payload.get("reason", "")),
            raised_utc=str(payload.get("raised_utc", "")),
            pid=int(payload.get("pid", 0)),
            host=str(payload.get("host", "")),
            journal_path=str(payload.get("journal_path", "")),
            acknowledged_by=payload.get("acknowledged_by"),
            acknowledged_utc=payload.get("acknowledged_utc"),
            resolution=payload.get("resolution"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"schema_version": FENCE_SCHEMA_VERSION, **self.__dict__}


def _same_failure_domain(a: Path, b: Path) -> bool:
    """Best-effort "same volume" test.

    ``st_dev`` is the device id on POSIX and the volume serial number on
    Windows, which is exactly the granularity that matters: a full or failing
    volume takes down everything on it.
    """
    def anchor(path: Path) -> Path:
        candidate = path if path.exists() else path.parent
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        return candidate

    try:
        return os.stat(anchor(a)).st_dev == os.stat(anchor(b)).st_dev
    except OSError:
        # If it cannot be determined, say so by refusing rather than assuming.
        raise FenceDomainError(f"cannot determine the failure domain of {a} or {b}")


class FatalFence:
    """A fence file on its own volume, with two-phase retirement."""

    def __init__(
        self,
        path: str | Path,
        journal_path: str | Path,
        *,
        require_separate_domain: bool = True,
    ):
        self.path = Path(path)
        self.journal_path = Path(journal_path)
        self.require_separate_domain = require_separate_domain

    # -- configuration --------------------------------------------------

    def verify_domain(self) -> None:
        """Enforced, not documented.

        A fence pointed at the journal's own volume looks configured and
        protects nothing, and the day it matters is the day the volume is
        full. ``require_separate_domain=False`` exists only for tests, which
        run entirely inside one tmp_path.
        """
        if not self.require_separate_domain:
            return
        if _same_failure_domain(self.path, self.journal_path):
            raise FenceDomainError(
                f"the fatal fence ({self.path}) is on the same volume as the journal "
                f"({self.journal_path}). The journal's volume filling up is the most "
                "likely reason to write a fence, so it must live elsewhere."
            )

    # -- reading --------------------------------------------------------

    def read(self) -> Optional[FatalFenceRecord]:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # An unreadable fence is still a fence. Refusing to trade on a
            # corrupt one is the only safe reading.
            return FatalFenceRecord(
                state=STATE_RAISED,
                reason=f"fence file at {self.path} exists but could not be parsed",
                raised_utc="",
                pid=0,
                host="",
                journal_path=str(self.journal_path),
            )
        return FatalFenceRecord.from_dict(payload)

    def require_clear(self) -> None:
        """Startup gate. Raises unless no fence is present."""
        fence = self.read()
        if fence is not None:
            raise FenceStillRaised(fence)

    # -- writing --------------------------------------------------------

    def _write(self, record: FatalFenceRecord) -> None:
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
        # The rename itself has to be durable, or a power loss can lose the
        # directory entry while the file's contents are safely on disk.
        dir_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        except OSError:  # pragma: no cover - not supported on some platforms
            pass
        finally:
            os.close(dir_fd)

    def raise_fence(self, reason: str) -> FatalFenceRecord:
        """Persist a fence. Idempotent: an existing fence is never overwritten.

        The first cause is the one worth keeping -- later failures on the way
        down are consequences of it.
        """
        existing = self.read()
        if existing is not None:
            return existing
        record = FatalFenceRecord(
            state=STATE_RAISED,
            reason=reason,
            raised_utc=datetime.now(timezone.utc).isoformat(),
            pid=os.getpid(),
            host=platform.node(),
            journal_path=str(self.journal_path),
        )
        try:
            self._write(record)
        except OSError as exc:
            raise FenceWriteFailed(
                f"could not persist the fatal fence at {self.path}: {exc}"
            ) from exc
        return record

    def acknowledge(self, operator: str, resolution: str) -> FatalFenceRecord:
        """Phase one. Records who and why; does NOT retire the fence."""
        if not operator or not resolution:
            raise ValueError("acknowledging a fatal fence requires an operator and a resolution")
        fence = self.read()
        if fence is None:
            raise FileNotFoundError(f"no fatal fence at {self.path}")
        if fence.state == STATE_ACKNOWLEDGED:
            return fence
        updated = FatalFenceRecord(
            state=STATE_ACKNOWLEDGED,
            reason=fence.reason,
            raised_utc=fence.raised_utc,
            pid=fence.pid,
            host=fence.host,
            journal_path=fence.journal_path,
            acknowledged_by=operator,
            acknowledged_utc=datetime.now(timezone.utc).isoformat(),
            resolution=resolution,
        )
        self._write(updated)
        return updated

    def retire(self, *, reconciled: bool) -> None:
        """Phase two. Only a verified broker reconciliation removes the fence.

        ``reconciled`` is the caller's assertion that it compared the account
        against durable state and found it explainable. Retiring on
        acknowledgement alone would make the confirmation prompt the route back
        to trading.
        """
        fence = self.read()
        if fence is None:
            return
        if fence.state != STATE_ACKNOWLEDGED:
            raise FenceStillRaised(fence)
        if not reconciled:
            raise FenceStillRaised(fence)
        self.path.unlink(missing_ok=True)


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - operator CLI
    import argparse

    ap = argparse.ArgumentParser(description="Inspect or acknowledge a durable fatal fence")
    ap.add_argument("--fence", required=True)
    ap.add_argument("--journal", default="")
    ap.add_argument("--acknowledge", action="store_true")
    ap.add_argument("--operator")
    ap.add_argument("--resolution")
    ns = ap.parse_args(argv)

    fence = FatalFence(ns.fence, ns.journal or ns.fence, require_separate_domain=False)
    record = fence.read()
    if record is None:
        print("No fatal fence. Nothing to do.")
        return 0

    print(f"FATAL FENCE {record.state}")
    print(f"  reason:    {record.reason}")
    print(f"  raised:    {record.raised_utc} by pid {record.pid} on {record.host}")
    print(f"  journal:   {record.journal_path}")
    if record.acknowledged_by:
        print(f"  acked by:  {record.acknowledged_by} at {record.acknowledged_utc}")
        print(f"  resolution:{record.resolution}")

    if not ns.acknowledge:
        return 1
    if not ns.operator or not ns.resolution:
        print(
            "\nRefusing to acknowledge without --operator and --resolution.\n"
            "Read the cause above and the account state in TWS first."
        )
        return 1
    fence.acknowledge(ns.operator, ns.resolution)
    print(
        f"\nAcknowledged by {ns.operator}. The fence is NOT retired: start the "
        "execution host, which will retire it only after a reconciliation pass "
        "explains the account."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
