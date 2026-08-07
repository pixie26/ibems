"""
Append-only event journal.

Three properties this module exists to guarantee:

1. DURABLE-BEFORE-SEND (invariant 2). commit() does not return until the event
   is on disk. The controller calls commit() before every broker write. If we
   crash in between, we have a record saying "we may have sent something",
   which is recoverable. The reverse order is not.

2. IDEMPOTENCY BY CONSTRAINT (invariants 1, 12). decision_id and exec_id
   uniqueness is enforced by the database, not by application logic. Application
   logic can be bypassed by a code path nobody thought about; a UNIQUE index
   cannot.

3. MEASURED DURABILITY LATENCY. SQLite I/O runs on a dedicated writer thread,
   but synchronous commit() still waits for durability. Therefore Controller
   must not execute on the IB event-loop thread. Gate B2 uses
   AsyncControllerBridge: one FIFO queue and one controller worker thread.

   Fsync latency is sampled and published. A writer thread alone is not an
   asynchronous API and must never be described as one.

Append-only means: no UPDATE, no DELETE, ever. Current state is a fold over
the event log. Execution corrections are new events (reversal + corrected),
never a mutation of the original row.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import statistics
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .processlock import ProcessLock, ProcessLockUnavailable

from .models import DuplicateDecision, EventType


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS events (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc        TEXT    NOT NULL,
    ts_mono_ns    INTEGER NOT NULL,
    event_type    TEXT    NOT NULL,
    strategy_id   TEXT,
    symbol        TEXT,
    decision_id   TEXT,
    intent_id     TEXT,
    order_ref     TEXT,
    perm_id       INTEGER,
    exec_id       TEXT,
    payload       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_events_type    ON events(event_type);
CREATE INDEX IF NOT EXISTS ix_events_intent  ON events(intent_id);
CREATE INDEX IF NOT EXISTS ix_events_ref     ON events(order_ref);
CREATE INDEX IF NOT EXISTS ix_events_ts      ON events(ts_utc);

-- Idempotency enforced by the database (invariant 1).
CREATE TABLE IF NOT EXISTS consumed_decisions (
    decision_id TEXT PRIMARY KEY,
    seq         INTEGER NOT NULL,
    ts_utc      TEXT    NOT NULL
);

-- Each raw execId booked at most once (invariant 12).
CREATE TABLE IF NOT EXISTS booked_executions (
    exec_id TEXT PRIMARY KEY,
    seq     INTEGER NOT NULL,
    ts_utc  TEXT    NOT NULL
);
"""


@dataclass(frozen=True)
class JournalEvent:
    seq: int
    ts_utc: datetime
    ts_mono_ns: int
    event_type: EventType
    payload: dict[str, Any]
    strategy_id: Optional[str] = None
    symbol: Optional[str] = None
    decision_id: Optional[str] = None
    intent_id: Optional[str] = None
    order_ref: Optional[str] = None
    perm_id: Optional[int] = None
    exec_id: Optional[str] = None


class _WriteRequest:
    __slots__ = ("kind", "args", "done", "result", "error")

    def __init__(self, kind: str, args: tuple):
        self.kind = kind
        self.args = args
        self.done = threading.Event()
        self.result: Any = None
        self.error: Optional[BaseException] = None


class HaltAcknowledgementConflict(RuntimeError):
    """The journal changed between reading a HALT and acknowledging it."""


class JournalUnavailable(RuntimeError):
    """
    The journal cannot be written.

    There is exactly one correct response: stop trading. We do not continue and
    promise to write the log later -- an unlogged broker write is precisely the
    thing this system is built to prevent.
    """


class JournalOwnershipError(RuntimeError):
    """
    Another live process already owns this journal.

    INVARIANT 0 / PLATFORM OWNERSHIP PREREQUISITE
    ---------------------------------------------
    At most one execution host holds writer ownership of one journal (one
    account execution domain) at any instant.

    Before this existed, single-writer was an architectural convention and
    nothing more. The only lock here was a ``threading.Lock``, which is
    per-process, and SQLite in WAL mode admits a second writing process
    happily. Two hosts on one journal therefore each kept their own in-memory
    state machine and each sent orders: invariants 1-4 are all stated
    per-process, and none of them survive that. Invariant 1 in particular is
    enforced by a primary key, which cannot help when the two processes mint
    different decision ids for the same intent.

    This is a startup refusal, deliberately not a subclass of
    ``JournalUnavailable``: nothing is wrong with the journal, and there is no
    fail-closed runtime state to enter. The correct response is to exit
    non-zero without connecting to the broker.
    """


class Journal:
    def __init__(
        self,
        path: str | Path,
        clock=None,
        *,
        write_timeout_seconds: float = 30.0,
        sqlite_timeout_seconds: float = 5.0,
        owner: bool = True,
    ):
        self.path = str(path)
        self._clock = clock
        self._write_timeout_seconds = float(write_timeout_seconds)
        self._sqlite_timeout_seconds = float(sqlite_timeout_seconds)
        if self._write_timeout_seconds <= 0 or self._sqlite_timeout_seconds <= 0:
            raise ValueError("journal timeouts must be positive")
        self._q: "queue.Queue[Optional[_WriteRequest]]" = queue.Queue()
        self._fsync_samples: list[float] = []
        self._lock = threading.Lock()
        self._failed: Optional[BaseException] = None
        self._closed = False

        # Cross-process writer ownership. Taken before the schema bootstrap so
        # a rejected process never touches the database file at all.
        #
        # `owner=False` exists for handles that are not the execution host:
        # the offline auditor (read-only), and tests that deliberately hold two
        # handles to exercise the halt-acknowledgement CAS. Every production
        # writing path -- execution_host and the ack_halt CLI -- takes
        # ownership, which also means an operator cannot acknowledge a halt on
        # a journal whose engine is still running.
        self._ownership: Optional[ProcessLock] = None
        if owner:
            lock = ProcessLock(Path(self.path).with_name(Path(self.path).name + ".lock"))
            try:
                lock.acquire(note=f"journal={Path(self.path).name}")
            except ProcessLockUnavailable as exc:
                raise JournalOwnershipError(str(exc)) from exc
            self._ownership = lock

        # Bootstrap schema on the calling thread, then hand the connection to
        # the writer thread which owns it exclusively from then on.
        conn = sqlite3.connect(self.path, timeout=self._sqlite_timeout_seconds)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()

        self._thread = threading.Thread(
            target=self._writer_loop, name="journal-writer", daemon=True
        )
        self._thread.start()

    # -- writer thread ----------------------------------------------------

    def _writer_loop(self) -> None:
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(self.path, timeout=self._sqlite_timeout_seconds)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            while True:
                req = self._q.get()
                if req is None:
                    break
                t0 = time.perf_counter()
                try:
                    req.result = self._apply(conn, req)
                except BaseException as exc:  # noqa: BLE001 -- must not escape
                    req.error = exc
                finally:
                    dt = time.perf_counter() - t0
                    with self._lock:
                        self._fsync_samples.append(dt)
                        if len(self._fsync_samples) > 10_000:
                            del self._fsync_samples[:5_000]
                    req.done.set()
        except BaseException as exc:  # an unexpected writer death poisons the journal
            with self._lock:
                self._failed = exc
            while True:
                try:
                    pending = self._q.get_nowait()
                except queue.Empty:
                    break
                if pending is not None:
                    pending.error = exc
                    pending.done.set()
        finally:
            if conn is not None:
                conn.close()

    def _apply(self, conn: sqlite3.Connection, req: _WriteRequest) -> Any:
        if req.kind == "append":
            return self._do_append(conn, *req.args)
        if req.kind == "claim_decision":
            return self._do_claim_decision(conn, *req.args)
        if req.kind == "claim_exec":
            return self._do_claim_exec(conn, *req.args)
        if req.kind == "accept_decision":
            return self._do_accept_decision(conn, *req.args)
        if req.kind == "book_execution":
            return self._do_book_execution(conn, *req.args)
        if req.kind == "ack_halt":
            return self._do_ack_halt(conn, *req.args)
        raise ValueError(f"unknown write kind {req.kind}")

    def _insert_event(self, conn: sqlite3.Connection, ev: dict) -> int:
        cur = conn.execute(
            """INSERT INTO events
               (ts_utc, ts_mono_ns, event_type, strategy_id, symbol,
                decision_id, intent_id, order_ref, perm_id, exec_id, payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ev["ts_utc"],
                ev["ts_mono_ns"],
                ev["event_type"],
                ev.get("strategy_id"),
                ev.get("symbol"),
                ev.get("decision_id"),
                ev.get("intent_id"),
                ev.get("order_ref"),
                ev.get("perm_id"),
                ev.get("exec_id"),
                json.dumps(ev.get("payload", {}), sort_keys=True, default=str),
            ),
        )
        return int(cur.lastrowid)

    def _do_append(self, conn: sqlite3.Connection, ev: dict) -> int:
        seq = self._insert_event(conn, ev)
        conn.commit()
        return seq

    def _do_accept_decision(
        self, conn: sqlite3.Connection, decision_id: str, ev: dict
    ) -> bool:
        """Atomically consume decision_id and append TARGET_RECEIVED."""
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO consumed_decisions (decision_id, seq, ts_utc) VALUES (?,?,?)",
                (decision_id, 0, ev["ts_utc"]),
            )
            seq = self._insert_event(conn, ev)
            conn.execute(
                "UPDATE consumed_decisions SET seq=? WHERE decision_id=?",
                (seq, decision_id),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
            return False
        except BaseException:
            conn.rollback()
            raise

    def _do_book_execution(
        self, conn: sqlite3.Connection, exec_id: str, ev: dict
    ) -> bool:
        """Atomically claim execId and append its primary ledger event."""
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO booked_executions (exec_id, seq, ts_utc) VALUES (?,?,?)",
                (exec_id, 0, ev["ts_utc"]),
            )
            seq = self._insert_event(conn, ev)
            conn.execute(
                "UPDATE booked_executions SET seq=? WHERE exec_id=?",
                (seq, exec_id),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
            return False
        except BaseException:
            conn.rollback()
            raise


    @staticmethod
    def _active_halt_seq(conn: sqlite3.Connection) -> Optional[int]:
        """Fold HALT/ACK events. Only an ACK for the exact active HALT clears it."""
        active: Optional[int] = None
        rows = conn.execute(
            """SELECT seq, event_type, payload FROM events
               WHERE event_type IN (?, ?, ?) ORDER BY seq ASC""",
            (
                EventType.OPERATING_MODE_CHANGED.value,
                EventType.HALT_CAUSE_ADDED.value,
                EventType.HALT_ACKNOWLEDGED.value,
            ),
        )
        for row in rows:
            payload = json.loads(row["payload"] if isinstance(row, sqlite3.Row) else row[2])
            event_type = row["event_type"] if isinstance(row, sqlite3.Row) else row[1]
            seq = int(row["seq"] if isinstance(row, sqlite3.Row) else row[0])
            if event_type == EventType.OPERATING_MODE_CHANGED.value:
                if payload.get("to") == "HALTED":
                    active = seq
            elif event_type == EventType.HALT_CAUSE_ADDED.value:
                active = seq
            elif event_type == EventType.HALT_ACKNOWLEDGED.value:
                acked = payload.get("acknowledged_halt_seq")
                if active is not None and acked is not None and int(acked) == active:
                    active = None
        return active

    def _do_ack_halt(
        self,
        conn: sqlite3.Connection,
        expected_halt_seq: int,
        ev: dict,
    ) -> int:
        """Atomically compare-and-append a HALT acknowledgement."""
        try:
            conn.execute("BEGIN IMMEDIATE")
            active = self._active_halt_seq(conn)
            if active != expected_halt_seq:
                conn.rollback()
                raise HaltAcknowledgementConflict(
                    f"active HALT changed: expected seq {expected_halt_seq}, found {active}"
                )
            seq = self._insert_event(conn, ev)
            conn.commit()
            return seq
        except HaltAcknowledgementConflict:
            raise
        except BaseException:
            conn.rollback()
            raise

    def _do_claim_decision(self, conn: sqlite3.Connection, decision_id: str, ts: str) -> bool:
        try:
            conn.execute(
                "INSERT INTO consumed_decisions (decision_id, seq, ts_utc) VALUES (?,?,?)",
                (decision_id, 0, ts),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
            return False

    def _do_claim_exec(self, conn: sqlite3.Connection, exec_id: str, ts: str) -> bool:
        try:
            conn.execute(
                "INSERT INTO booked_executions (exec_id, seq, ts_utc) VALUES (?,?,?)",
                (exec_id, 0, ts),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
            return False

    # -- caller side ------------------------------------------------------

    def _submit(self, kind: str, args: tuple, timeout: Optional[float] = None) -> Any:
        if self._failed is not None:
            raise JournalUnavailable(f"journal already failed: {self._failed}")
        if self._closed:
            raise JournalUnavailable("journal is closed")
        if not self._thread.is_alive():
            self._failed = RuntimeError("journal writer thread is not alive")
            raise JournalUnavailable(str(self._failed))
        timeout = self._write_timeout_seconds if timeout is None else float(timeout)
        req = _WriteRequest(kind, args)
        self._q.put(req)
        if not req.done.wait(timeout):
            self._failed = TimeoutError(f"journal write timed out after {timeout}s")
            raise JournalUnavailable(str(self._failed))
        if req.error is not None:
            # A compare-and-swap conflict is an expected business outcome, not
            # a journal failure. Do not poison the writer for a stale operator
            # screen; all real storage/transaction errors remain fail-closed.
            if isinstance(req.error, HaltAcknowledgementConflict):
                raise req.error
            self._failed = req.error
            raise JournalUnavailable(f"journal write failed: {req.error}") from req.error
        return req.result

    def is_healthy(self) -> bool:
        """Cheap write-boundary health check; a successful commit remains the proof."""
        return not self._closed and self._failed is None and self._thread.is_alive()

    @property
    def failure(self) -> Optional[BaseException]:
        return self._failed

    def _event_dict(
        self,
        event_type: EventType,
        payload: Optional[dict] = None,
        *,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        decision_id: Optional[str] = None,
        intent_id: Optional[str] = None,
        order_ref: Optional[str] = None,
        perm_id: Optional[int] = None,
        exec_id: Optional[str] = None,
    ) -> dict:
        now = self._clock.now() if self._clock else datetime.now(timezone.utc)
        mono = self._clock.monotonic_ns() if self._clock else time.monotonic_ns()
        return {
            "ts_utc": now.isoformat(),
            "ts_mono_ns": mono,
            "event_type": event_type.value,
            "strategy_id": strategy_id,
            "symbol": symbol,
            "decision_id": decision_id,
            "intent_id": intent_id,
            "order_ref": order_ref,
            "perm_id": perm_id,
            "exec_id": exec_id,
            "payload": payload or {},
        }

    def accept_decision(
        self,
        decision_id: str,
        payload: dict,
        *,
        strategy_id: str,
        symbol: str,
    ) -> bool:
        """Atomically enforce decision idempotency and journal acceptance."""
        ev = self._event_dict(
            EventType.TARGET_RECEIVED,
            payload,
            strategy_id=strategy_id,
            symbol=symbol,
            decision_id=decision_id,
        )
        return bool(self._submit("accept_decision", (decision_id, ev)))

    def commit_execution_once(
        self,
        exec_id: str,
        payload: dict,
        *,
        symbol: str,
        order_ref: str,
        perm_id: Optional[int],
    ) -> bool:
        """Atomically enforce execId uniqueness and append the ledger event."""
        ev = self._event_dict(
            EventType.EXECUTION_RECEIVED,
            payload,
            symbol=symbol,
            order_ref=order_ref,
            perm_id=perm_id,
            exec_id=exec_id,
        )
        return bool(self._submit("book_execution", (exec_id, ev)))


    def acknowledge_halt(
        self,
        expected_halt_seq: int,
        operator: str,
        resolution: str,
    ) -> int:
        """Atomically acknowledge the exact currently-active HALT."""
        if expected_halt_seq <= 0:
            raise ValueError("expected_halt_seq must be positive")
        if not operator.strip() or not resolution.strip():
            raise ValueError("HALT acknowledgement requires operator and resolution")
        ev = self._event_dict(
            EventType.HALT_ACKNOWLEDGED,
            {
                "operator": operator.strip(),
                "resolution": resolution.strip(),
                "acknowledged_halt_seq": int(expected_halt_seq),
            },
        )
        return int(self._submit("ack_halt", (int(expected_halt_seq), ev)))

    def commit(
        self,
        event_type: EventType,
        payload: Optional[dict] = None,
        *,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        decision_id: Optional[str] = None,
        intent_id: Optional[str] = None,
        order_ref: Optional[str] = None,
        perm_id: Optional[int] = None,
        exec_id: Optional[str] = None,
    ) -> int:
        """Durably append one event. Returns on-disk sequence number."""
        ev = self._event_dict(
            event_type,
            payload,
            strategy_id=strategy_id,
            symbol=symbol,
            decision_id=decision_id,
            intent_id=intent_id,
            order_ref=order_ref,
            perm_id=perm_id,
            exec_id=exec_id,
        )
        return self._submit("append", (ev,))

    def claim_decision(self, decision_id: str) -> None:
        """
        Consume a decision_id exactly once (invariant 1).

        Raises DuplicateDecision if already consumed. Enforced by a PRIMARY KEY,
        so a duplicate cannot slip through a logic error upstream.
        """
        now = self._clock.now() if self._clock else datetime.now(timezone.utc)
        ok = self._submit("claim_decision", (decision_id, now.isoformat()))
        if not ok:
            raise DuplicateDecision(decision_id)

    def claim_execution(self, exec_id: str) -> bool:
        """Book a raw execId at most once (invariant 12). False == already seen."""
        now = self._clock.now() if self._clock else datetime.now(timezone.utc)
        return bool(self._submit("claim_exec", (exec_id, now.isoformat())))

    # -- read side (own connection, read-only) ----------------------------

    def _read_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def replay(self) -> Iterator[JournalEvent]:
        conn = self._read_conn()
        try:
            for row in conn.execute("SELECT * FROM events ORDER BY seq ASC"):
                yield JournalEvent(
                    seq=row["seq"],
                    ts_utc=datetime.fromisoformat(row["ts_utc"]),
                    ts_mono_ns=row["ts_mono_ns"],
                    event_type=EventType(row["event_type"]),
                    payload=json.loads(row["payload"]),
                    strategy_id=row["strategy_id"],
                    symbol=row["symbol"],
                    decision_id=row["decision_id"],
                    intent_id=row["intent_id"],
                    order_ref=row["order_ref"],
                    perm_id=row["perm_id"],
                    exec_id=row["exec_id"],
                )
        finally:
            conn.close()

    def events_of(self, *types: EventType) -> list[JournalEvent]:
        wanted = {t.value for t in types}
        return [e for e in self.replay() if e.event_type.value in wanted]

    def count(self) -> int:
        conn = self._read_conn()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        finally:
            conn.close()

    # -- health -----------------------------------------------------------

    def fsync_stats(self) -> dict[str, float]:
        """
        Published in status.json and sampled at startup.

        If p99 breaches the configured threshold the controller trips STOP_NEW.
        We do NOT silently downgrade to async writes under load -- that trades a
        visible latency problem for an invisible correctness one.
        """
        with self._lock:
            s = sorted(self._fsync_samples)
        if not s:
            return {"n": 0}
        def pct(p: float) -> float:
            idx = min(len(s) - 1, int(len(s) * p))
            return s[idx]
        return {
            "n": float(len(s)),
            "p50_ms": pct(0.50) * 1000,
            "p95_ms": pct(0.95) * 1000,
            "p99_ms": pct(0.99) * 1000,
            "max_ms": s[-1] * 1000,
            "mean_ms": statistics.fmean(s) * 1000,
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._q.put(None)
        self._thread.join(timeout=10)
        if self._ownership is not None:
            self._ownership.release()
            self._ownership = None
