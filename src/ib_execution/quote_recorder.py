"""
Read-only market data recorder.

    ####################################################################
    #  STATUS: READ-ONLY API HANDSHAKE VERIFIED. No qualifying         #
    #  Full-RTH session has been captured yet. Live entitlement was    #
    #  confirmed on 2026-08-07 (marketDataType=1); the AllLast         #
    #  subscription path has no valid observation yet -- see           #
    #  "MEASUREMENT" below.                                            #
    ####################################################################

WHY THIS IS WEEK 0 AND NOT LATER
--------------------------------
This is the only calendar-constrained item in the whole plan. Tail days -- the
days that carry this strategy's entire P&L -- happen maybe 5-10 times a year.
The platform takes months. Every tail day that passes before the recorder is
running is a sample that cannot be recovered, ever.

Half of what Phase 4 needs (arrival spread as a function of volatility state)
requires no order at all. Start collecting it now.

SCOPE: full regular trading hours, not a window around decisions. The recorder
also feeds L2 (IB feed vs research parquet) and L3 (does the edge survive a
data source change), and neither can be done from 60-second snippets.

ISOLATION: this process must not be able to hurt the trading path. It shares a
Gateway with the execution engine only if that Gateway is separate from the
trading one, or it gets its own token bucket and bounded backoff. A recorder
crash-looping its subscriptions can trip pacing limits and take the Gateway
down underneath the execution engine. See ADR-005.

MEASUREMENT
-----------
A recorder that cannot measure itself is not evidence, it is a hope. Two
lessons are built into this file because both were learned the expensive way
on 2026-08-07:

1.  ``Ticker.tickByTicks`` is a per-update buffer that ib_async clears between
    network updates. Sleeping and then reading it counts "whatever arrived in
    the last flush", not "what arrived during the window". The preflight did
    exactly that and reported ``AllLast=0``; that number was never evidence
    about the subscription. Ticks are consumed in an event handler here, and
    nowhere is a tick buffer polled.

2.  ``datetime.now() - reqCurrentTime()`` measures skew plus one round trip,
    against a server clock with one-second granularity. A single sample of it
    decided a whole day's health verdict. Skew is now a round-trip-compensated
    median over many samples, and the distribution is reported, because
    failing a tail day on quantization noise is unrecoverable.

The 5-second TRADES stream exists as an independent checksum against the
tick-by-tick trade stream, and that comparison is now actually computed --
see ``CrossStreamDiagnostics``. It reports distributions and does not judge:
the transform between the two streams (units, trade-condition filtering) is
an IB behaviour that has to be measured before it can be asserted, and a
validator built on an assumed constant would be worse than none.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import queue
import statistics
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional
from uuid import uuid4

from . import market_liveness
from .durable_io import durable_atomic_write, durable_replace
from .event_loop_heartbeat import EventLoopHeartbeat
from .market_liveness import (
    LivenessAction,
    LivenessIncidentTracker,
    MarketLiveness,
)
from .processlock import ProcessLock, ProcessLockUnavailable
from .recorder_modes import CapturePolicy, DataMode

# Market-data failures that reconnecting cannot repair. 10197 belongs here and
# was missing: "no market data during competing session" is what a live and a
# paper login contending for one live subscription looks like, and retrying it
# just produces the same error at a slower rate.
FATAL_MARKET_DATA_CODES = frozenset({354, 10089, 10189, 10197})

# Generic tick 49 carries the halt state. A real Gateway rejected it for a
# STK contract with error 321 on 2026-08-12 and the whole reqMktData probe
# came back empty, so it is requested only when an operator opts in.
HALT_GENERIC_TICK = "49"
REQUEST_VALIDATION_ERROR = 321

MARKET_STREAMS = ("BID_ASK", "ALL_LAST", "BAR_5S")

# OFFLINE, post-hoc. compute_health() reports gaps in a session that has
# already been written, where a long gap is a fact about the recorded data
# that a reader should see. Deliberately NOT the live decision: nothing in
# the run loop may stop the session on these numbers, because during the run
# a long BID_ASK/ALL_LAST gap is indistinguishable from a quiet tape. The
# live split lives in market_liveness -- BAR_5S is time-driven and decidable,
# the other two are event-driven and only ever recorded.
DEFAULT_GAP_THRESHOLDS = {"BID_ASK": 5.0, "ALL_LAST": 30.0, "BAR_5S": 15.0}

HEARTBEAT_STREAM = market_liveness.HEARTBEAT_STREAM
ADVISORY_GAP_THRESHOLDS = dict(market_liveness.DEFAULT_ADVISORY_THRESHOLDS)


@dataclass(frozen=True)
class RawTick:
    """
    One market data event, exactly as received.

    Both timestamps are mandatory. Later we must be able to separate "when the
    market did something" from "when we found out", and that distinction is
    unrecoverable if only one is stored.

    Row identity is ``(session, recorder_run_id, receive_sequence)``.
    ``event_id`` alone is not an identity: it restarts at 1 in every process,
    and ``finalize_day`` folds every run of a given session into one Parquet
    file, so a same-day restart used to produce two rows claiming to be the
    same event.
    """

    event_id: int
    recorder_run_id: str
    connection_epoch: int
    contract_id: int
    event_type: str                  # BID_ASK | ALL_LAST | BAR_5S | SYSTEM
    broker_timestamp: str
    local_wall_ns: int
    local_monotonic_ns: int
    market_data_type: str            # LIVE | DELAYED | FROZEN
    receive_sequence: int
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    last: Optional[float] = None
    last_size: Optional[float] = None
    exchange: Optional[str] = None
    special_conditions: Optional[str] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    wap: Optional[float] = None
    trade_count: Optional[int] = None


# Column types are declared, never inferred. pyarrow infers a `null`-typed
# column when every value in a day is None, so the first day whose AllLast
# stream dies produces a Parquet file that will not concatenate with any other
# day -- the failure corrupts the shape of the archive, not just that day's
# completeness, and multi-day concatenation is precisely what L2/L3 needs.
PARQUET_FIELD_TYPES: dict[str, str] = {
    "event_id": "int64",
    "recorder_run_id": "string",
    "connection_epoch": "int64",
    "contract_id": "int64",
    "event_type": "string",
    "broker_timestamp": "string",
    "local_wall_ns": "int64",
    "local_monotonic_ns": "int64",
    "market_data_type": "string",
    "receive_sequence": "int64",
    "bid": "float64",
    "ask": "float64",
    "bid_size": "float64",
    "ask_size": "float64",
    "last": "float64",
    "last_size": "float64",
    "exchange": "string",
    "special_conditions": "string",
    "open": "float64",
    "high": "float64",
    "low": "float64",
    "close": "float64",
    "volume": "float64",
    "wap": "float64",
    "trade_count": "int64",
}


def parquet_schema():
    """The explicit Arrow schema. Import-time-free so core tests need no pyarrow."""
    import pyarrow as pa

    mapping = {"int64": pa.int64(), "float64": pa.float64(), "string": pa.string()}
    declared = [f.name for f in fields(RawTick)]
    missing = [name for name in declared if name not in PARQUET_FIELD_TYPES]
    extra = [name for name in PARQUET_FIELD_TYPES if name not in declared]
    if missing or extra:
        raise RuntimeError(
            f"PARQUET_FIELD_TYPES is out of sync with RawTick: missing={missing} extra={extra}"
        )
    return pa.schema([(name, mapping[PARQUET_FIELD_TYPES[name]]) for name in declared])


class RecorderWriteFailed(RuntimeError):
    """The async raw writer failed; the capture must not be reported healthy."""


class RecorderQueueFull(RecorderWriteFailed):
    """The bounded recorder queue overflowed; at least one event was rejected."""


@dataclass(frozen=True)
class _QueuedTick:
    tick: RawTick
    now_mono: float
    enqueued_mono: float


class _WriterBarrier:
    def __init__(self, publish: bool):
        self.publish = publish
        self.done = threading.Event()
        self.error: Optional[BaseException] = None


_WRITER_STOP = object()


class RawEventLog:
    """
    Append-only rolling event log for exactly one exchange session.

    Never hold one Parquet file open all session: a crash at 15:45 costs the
    whole day. Roll every few minutes, atomic-rename on completion, compact to
    Parquet after the close. Raw logs are never modified in place -- derived
    tables are built beside them, and the manifest records hashes.

    A session directory is owned by one live process at a time. The lock is
    not there to stop a same-day restart -- that is normal and supported, the
    successor simply takes a new ``run_id`` and keeps recording the same
    session. It is there because ``_recover_crashed_segments`` renames every
    partial segment it can see, and without the lock a second recorder would
    rename the file the first one is still writing to.
    """

    def __init__(
        self,
        root: str | Path,
        session: Optional[date] = None,
        roll_seconds: int = 300,
        sync_seconds: float = 1.0,
        run_id: Optional[str] = None,
        lock: bool = True,
        *,
        queue_capacity: int = 100_000,
        batch_records: int = 512,
        batch_max_latency_seconds: float = 0.05,
        close_timeout_seconds: float = 30.0,
    ):
        if queue_capacity <= 0 or batch_records <= 0:
            raise ValueError("recorder queue_capacity and batch_records must be positive")
        if batch_max_latency_seconds <= 0 or close_timeout_seconds <= 0:
            raise ValueError("recorder batch/close timeouts must be positive")
        self.root = Path(root)
        self.session = session or datetime.now(timezone.utc).date()
        self.dir = self.root / self.session.isoformat()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.roll_seconds = roll_seconds
        self.sync_seconds = sync_seconds
        self._fh = None
        self._current: Optional[Path] = None
        self._opened_at: Optional[float] = None
        self._seq = 0
        self._count = 0
        self._last_sync: Optional[float] = None
        self.queue_capacity = int(queue_capacity)
        self.batch_records = int(batch_records)
        self.batch_max_latency_seconds = float(batch_max_latency_seconds)
        self.close_timeout_seconds = float(close_timeout_seconds)
        self._queue: queue.Queue[object] = queue.Queue(maxsize=self.queue_capacity)
        self._state_lock = threading.Lock()
        self._failure: Optional[BaseException] = None
        self._accepting = True
        self._closed = False
        self._accepted = 0
        self._dropped = 0
        self._queue_high_water = 0
        self._max_writer_lag_ms = 0.0
        self._fsync_latencies_ms: list[float] = []
        self._accepted_by_stream: dict[str, int] = defaultdict(int)
        self._persisted_by_stream: dict[str, int] = defaultdict(int)
        self._accepted_by_run_id: dict[str, int] = defaultdict(int)
        self._persisted_by_run_id: dict[str, int] = defaultdict(int)
        # A same-day process restart must not overwrite earlier segments.
        self.run_id = run_id or uuid4().hex[:10]
        self._lock: Optional[ProcessLock] = None
        if lock:
            self._lock = ProcessLock(self.dir / ".recorder.lock")
            self._lock.acquire(note=f"session={self.session.isoformat()} run={self.run_id}")
        self._recover_crashed_segments()
        self._thread = threading.Thread(
            target=self._writer_loop,
            name=f"raw-event-writer-{self.run_id}",
            daemon=True,
        )
        self._thread.start()

    def _recover_crashed_segments(self) -> None:
        """Preserve abruptly-terminated gzip streams for best-effort row salvage.

        Only safe because the session lock is already held: every ``.partial-``
        file visible here belongs to a process that is no longer running.
        """
        for partial in sorted(self.dir.glob(".partial-*.jsonl.gz")):
            recovered = partial.with_name(partial.name.replace(".partial-", "crashed-"))
            os.replace(partial, recovered)

    def _open_segment(self, now_mono: float) -> None:
        self._close_segment()
        stamp = datetime.now(timezone.utc).strftime("%H%M%S")
        self._current = self.dir / (
            f".partial-{stamp}-{self.run_id}-{self._seq:05d}.jsonl.gz"
        )
        self._fh = gzip.open(self._current, "wt", encoding="utf-8")
        self._opened_at = now_mono
        self._last_sync = time.monotonic()
        self._seq += 1

    def _close_segment(self) -> None:
        if self._fh is None or self._current is None:
            return
        self._fh.close()
        # gzip.close writes the footer. Flush the complete stream before the
        # atomic rename rather than fsyncing a prefix and then adding a footer.
        sync_flags = os.O_RDWR if os.name == "nt" else os.O_RDONLY
        fd = os.open(self._current, sync_flags)
        try:
            self._fsync_fd(fd)
        finally:
            os.close(fd)
        final = self._current.with_name(self._current.name.replace(".partial-", "segment-"))
        durable_replace(self._current, final, source_is_synced=True)
        self._fh = None
        self._current = None

    def _latch_failure(self, exc: BaseException) -> None:
        with self._state_lock:
            if self._failure is None:
                self._failure = exc

    def raise_if_failed(self) -> None:
        with self._state_lock:
            failure = self._failure
        if failure is not None:
            raise RecorderWriteFailed(f"raw event writer failed: {failure}") from failure

    def _fsync_fd(self, fd: int) -> None:
        started = time.monotonic()
        os.fsync(fd)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        with self._state_lock:
            self._fsync_latencies_ms.append(elapsed_ms)

    def _flush_file(self, *, force_sync: bool = False) -> None:
        if self._fh is None:
            return
        self._fh.flush()
        now = time.monotonic()
        if force_sync or self._last_sync is None or now - self._last_sync >= self.sync_seconds:
            raw = getattr(getattr(self._fh, "buffer", None), "fileobj", None)
            if raw is not None:
                self._fsync_fd(raw.fileno())
            self._last_sync = now

    def _write_batch(self, batch: list[_QueuedTick]) -> None:
        for item in batch:
            if self._fh is None or (
                self._opened_at is not None
                and item.now_mono - self._opened_at >= self.roll_seconds
            ):
                self._open_segment(item.now_mono)
            assert self._fh is not None
            self._fh.write(json.dumps(asdict(item.tick), separators=(",", ":")) + "\n")
        self._flush_file()
        written_at = time.monotonic()
        with self._state_lock:
            for item in batch:
                self._count += 1
                self._persisted_by_stream[item.tick.event_type] += 1
                self._persisted_by_run_id[item.tick.recorder_run_id] += 1
                self._max_writer_lag_ms = max(
                    self._max_writer_lag_ms,
                    (written_at - item.enqueued_mono) * 1000.0,
                )

    def _finish_control(self, control: object) -> bool:
        if control is _WRITER_STOP:
            self._close_segment()
            return True
        assert isinstance(control, _WriterBarrier)
        try:
            self._flush_file(force_sync=True)
            if control.publish:
                self._close_segment()
        except BaseException as exc:
            control.error = exc
            raise
        finally:
            control.done.set()
        return False

    def _writer_loop(self) -> None:
        try:
            while True:
                try:
                    first = self._queue.get(timeout=self.batch_max_latency_seconds)
                except queue.Empty:
                    self._flush_file()
                    continue

                if not isinstance(first, _QueuedTick):
                    self._queue.task_done()
                    if self._finish_control(first):
                        return
                    continue

                batch = [first]
                control: Optional[object] = None
                while len(batch) < self.batch_records:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if isinstance(item, _QueuedTick):
                        batch.append(item)
                    else:
                        control = item
                        break

                self._write_batch(batch)
                for _ in batch:
                    self._queue.task_done()
                if control is not None:
                    self._queue.task_done()
                    if self._finish_control(control):
                        return
        except BaseException as exc:
            self._latch_failure(exc)
            # Release any caller waiting on a publication barrier. Accepted
            # ticks remain accounted as not persisted and make the run fail.
            while True:
                try:
                    pending = self._queue.get_nowait()
                except queue.Empty:
                    break
                if isinstance(pending, _WriterBarrier):
                    pending.error = exc
                    pending.done.set()
                self._queue.task_done()

    def append(self, tick: RawTick, now_mono: float) -> None:
        item = _QueuedTick(tick, float(now_mono), time.monotonic())
        with self._state_lock:
            if not self._accepting:
                raise RecorderWriteFailed("raw event log is closing or closed")
            if self._failure is not None:
                raise RecorderWriteFailed(
                    f"raw event writer failed: {self._failure}"
                ) from self._failure
            try:
                self._queue.put_nowait(item)
            except queue.Full as exc:
                self._dropped += 1
                failure = RecorderQueueFull(
                    f"raw event queue reached capacity {self.queue_capacity}; capture is incomplete"
                )
                self._failure = failure
                raise failure from exc
            self._accepted += 1
            self._accepted_by_stream[tick.event_type] += 1
            self._accepted_by_run_id[tick.recorder_run_id] += 1
            self._queue_high_water = max(self._queue_high_water, self._queue.qsize())

    def flush(self, *, publish: bool = True, timeout: Optional[float] = None) -> None:
        timeout = self.close_timeout_seconds if timeout is None else float(timeout)
        self.raise_if_failed()
        with self._state_lock:
            if self._closed:
                return
            if not self._accepting:
                raise RecorderWriteFailed("raw event log is already closing")
        barrier = _WriterBarrier(publish)
        try:
            self._queue.put(barrier, timeout=timeout)
        except queue.Full as exc:
            raise RecorderWriteFailed("timed out enqueueing raw writer flush barrier") from exc
        if not barrier.done.wait(timeout):
            failure = TimeoutError(f"raw event writer flush timed out after {timeout}s")
            self._latch_failure(failure)
            raise RecorderWriteFailed(str(failure)) from failure
        if barrier.error is not None:
            raise RecorderWriteFailed(
                f"raw event writer flush failed: {barrier.error}"
            ) from barrier.error
        self.raise_if_failed()

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._accepting = False
        try:
            self._queue.put(_WRITER_STOP, timeout=self.close_timeout_seconds)
        except queue.Full:
            self._latch_failure(
                TimeoutError("raw event writer queue did not drain before close timeout")
            )
        self._thread.join(self.close_timeout_seconds)
        if self._thread.is_alive():
            self._latch_failure(
                TimeoutError("raw event writer did not stop before close timeout")
            )
            # Do not release session ownership while the writer can still touch
            # its segment; the kernel will release it when the process exits.
            self.raise_if_failed()
        with self._state_lock:
            self._closed = True
        if self._lock is not None:
            self._lock.release()
            self._lock = None
        self.raise_if_failed()

    def segments(self) -> list[Path]:
        return sorted(
            list(self.dir.glob("segment-*.jsonl.gz"))
            + list(self.dir.glob("crashed-*.jsonl.gz"))
        )

    def read_all(self) -> Iterator[dict[str, Any]]:
        with self._state_lock:
            open_for_writes = self._accepting and not self._closed
        if open_for_writes:
            self.flush(publish=True)
        for seg in self.segments():
            with gzip.open(seg, "rt", encoding="utf-8") as fh:
                try:
                    for line in fh:
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            break
                except (EOFError, OSError):
                    # A forced kill can leave a valid prefix with no gzip footer.
                    # The immutable crashed segment remains in the manifest.
                    continue

    @property
    def count(self) -> int:
        with self._state_lock:
            return self._count

    def write_stats(self) -> dict[str, Any]:
        with self._state_lock:
            failure = None if self._failure is None else repr(self._failure)
            fsyncs = sorted(self._fsync_latencies_ms)
            p95_index = max(0, math.ceil(len(fsyncs) * 0.95) - 1) if fsyncs else 0
            return {
                "accepted": self._accepted,
                "persisted": self._count,
                "dropped": self._dropped,
                "enqueued_count": self._accepted,
                "persisted_count": self._count,
                "dropped_count": self._dropped,
                "queue_capacity": self.queue_capacity,
                "queue_high_water": self._queue_high_water,
                "max_writer_lag_ms": self._max_writer_lag_ms,
                "fsync_count": len(fsyncs),
                "fsync_latency_ms": {
                    "max": max(fsyncs) if fsyncs else 0.0,
                    "mean": statistics.fmean(fsyncs) if fsyncs else 0.0,
                    "p95": fsyncs[p95_index] if fsyncs else 0.0,
                },
                "accepted_by_stream": dict(sorted(self._accepted_by_stream.items())),
                "persisted_by_stream": dict(sorted(self._persisted_by_stream.items())),
                "accepted_by_run_id": dict(sorted(self._accepted_by_run_id.items())),
                "persisted_by_run_id": dict(sorted(self._persisted_by_run_id.items())),
                "writer_error": failure,
            }


# ----------------------------------------------------------------------------
# health
# ----------------------------------------------------------------------------


@dataclass
class StreamHealth:
    """One row of the per-stream table.

    Whole-session aggregates cannot express the failure that matters. Quotes
    arrive orders of magnitude more often than prints, so a single pooled
    ``max_gap`` is dominated by the quote stream and stays small even after
    the trade stream has been dead for hours; a single pooled coverage number
    is ~100% for the same reason. Every number here is per stream.
    """

    stream: str
    rows: int
    first_utc: Optional[str]
    last_utc: Optional[str]
    max_gap_seconds: float
    gap_threshold_seconds: float
    gaps_over_threshold: int
    missing_seconds: float
    coverage_fraction: float

    def problems(self, min_coverage: float) -> list[str]:
        if self.rows == 0:
            return [f"{self.stream}: no rows"]
        out = []
        if self.coverage_fraction < min_coverage:
            out.append(f"{self.stream}: coverage {self.coverage_fraction:.3%}")
        if self.gaps_over_threshold:
            out.append(
                f"{self.stream}: {self.gaps_over_threshold} gaps over "
                f"{self.gap_threshold_seconds:.0f}s, worst {self.max_gap_seconds:.0f}s"
            )
        return out


def _stream_health(
    stamps_ns: list[int],
    stream: str,
    session_open_ns: int,
    session_close_ns: int,
    gap_threshold: float,
) -> StreamHealth:
    """True coverage, anchored to the session window rather than to the data.

    ``(last - first) / session_seconds`` -- what this used to compute -- is a
    span, not a coverage: a recorder that ran 09:30 to 16:00 with a two-hour
    hole in the middle scores ~100%. Missing time is measured against the
    session boundaries, and an internal gap past the threshold counts in full
    rather than only its excess, because a five-minute hole is five minutes of
    missing market, not four and a half.
    """
    session_seconds = max((session_close_ns - session_open_ns) / 1e9, 0.0)
    if not stamps_ns:
        return StreamHealth(
            stream=stream, rows=0, first_utc=None, last_utc=None,
            max_gap_seconds=session_seconds, gap_threshold_seconds=gap_threshold,
            gaps_over_threshold=1 if session_seconds else 0,
            missing_seconds=session_seconds, coverage_fraction=0.0,
        )

    ordered = sorted(stamps_ns)
    clipped = [min(max(ts, session_open_ns), session_close_ns) for ts in ordered]
    opening_gap = max((clipped[0] - session_open_ns) / 1e9, 0.0)
    closing_gap = max((session_close_ns - clipped[-1]) / 1e9, 0.0)

    max_gap = 0.0
    internal_missing = 0.0
    over = 0
    for a, b in zip(clipped, clipped[1:]):
        gap = (b - a) / 1e9
        max_gap = max(max_gap, gap)
        if gap > gap_threshold:
            over += 1
            internal_missing += gap

    if opening_gap > gap_threshold:
        over += 1
        max_gap = max(max_gap, opening_gap)
    if closing_gap > gap_threshold:
        over += 1
        max_gap = max(max_gap, closing_gap)

    missing = opening_gap + closing_gap + internal_missing
    coverage = 1.0 - (missing / session_seconds) if session_seconds else 0.0
    return StreamHealth(
        stream=stream,
        rows=len(ordered),
        first_utc=datetime.fromtimestamp(ordered[0] / 1e9, timezone.utc).isoformat(),
        last_utc=datetime.fromtimestamp(ordered[-1] / 1e9, timezone.utc).isoformat(),
        max_gap_seconds=max_gap,
        gap_threshold_seconds=gap_threshold,
        gaps_over_threshold=over,
        missing_seconds=missing,
        coverage_fraction=max(0.0, min(1.0, coverage)),
    )


@dataclass
class ClockSkew:
    """Round-trip-compensated skew samples, reported as a distribution.

    IB's server clock has one-second granularity, so a single uncompensated
    sample cannot distinguish real drift from quantization. The health verdict
    uses the median; the tails are recorded so a marginal day can be judged by
    a human instead of by one unlucky reading taken at 15:59.
    """

    samples: int = 0
    median_seconds: float = math.nan
    p95_seconds: float = math.nan
    max_abs_seconds: float = math.nan

    @classmethod
    def from_samples(cls, values: Iterable[float]) -> "ClockSkew":
        data = [v for v in values if not math.isnan(v)]
        if not data:
            return cls()
        ordered = sorted(data)
        idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
        return cls(
            samples=len(ordered),
            median_seconds=statistics.median(ordered),
            p95_seconds=ordered[idx],
            max_abs_seconds=max(abs(v) for v in ordered),
        )


@dataclass
class CrossStreamDiagnostics:
    """5-second TRADES bars measured against tick-by-tick AllLast.

    OBSERVATION ONLY. This deliberately does not decide anything.

    The two streams are not guaranteed to agree: their volume units and their
    trade-condition filtering are IB behaviours, not documented constants, and
    a validator built on an assumed transform (``bar.volume * 100``, say) can
    be confidently wrong in both directions. So day one measures the ratio
    distribution, and the transform and tolerance get frozen only once the
    distribution has been seen. What is worth noticing immediately is the
    shape: a stable mode means the streams agree up to a unit, and no mode at
    all means they are filtering different trades.

    Once calibrated this is a far stronger control than per-stream row counts,
    because it catches dropped ticks, duplicated ticks and a silently dying
    stream, none of which change a row count in any recognisable way.
    """

    bars: int = 0
    bars_with_trades: int = 0
    bars_with_volume_but_no_ticks: int = 0
    bars_with_ticks_but_no_volume: int = 0
    volume_ratio_median: Optional[float] = None
    volume_ratio_p10: Optional[float] = None
    volume_ratio_p90: Optional[float] = None
    count_ratio_median: Optional[float] = None
    price_containment_fraction: Optional[float] = None
    calibrated: bool = False
    note: str = (
        "observation only; the bar/tick transform is unmeasured, so no "
        "PASS/FAIL is derived from these numbers"
    )


def _quantile(ordered: list[float], q: float) -> float:
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def _parse_broker_ts(text: str) -> Optional[float]:
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def compute_cross_stream_diagnostics(
    rows: Iterable[dict[str, Any]], bar_seconds: float = 5.0
) -> CrossStreamDiagnostics:
    bars: list[dict[str, Any]] = []
    trades: list[tuple[float, float, float]] = []   # (ts, size, price)
    for row in rows:
        kind = row.get("event_type")
        if kind == "BAR_5S":
            ts = _parse_broker_ts(str(row.get("broker_timestamp", "")))
            if ts is not None:
                bars.append({"ts": ts, "row": row})
        elif kind == "ALL_LAST":
            ts = _parse_broker_ts(str(row.get("broker_timestamp", "")))
            if ts is not None:
                trades.append((ts, float(row.get("last_size") or 0.0), float(row.get("last") or 0.0)))

    if not bars:
        return CrossStreamDiagnostics()

    trades.sort()
    trade_ts = [t[0] for t in trades]
    import bisect

    diag = CrossStreamDiagnostics(bars=len(bars))
    volume_ratios: list[float] = []
    count_ratios: list[float] = []
    contained = 0
    considered = 0

    for entry in sorted(bars, key=lambda b: b["ts"]):
        start = entry["ts"]
        row = entry["row"]
        lo = bisect.bisect_left(trade_ts, start)
        hi = bisect.bisect_left(trade_ts, start + bar_seconds)
        bucket = trades[lo:hi]
        bar_volume = float(row.get("volume") or 0.0)
        bar_count = float(row.get("trade_count") or 0.0)
        tick_volume = sum(size for _, size, _ in bucket)
        if bucket:
            diag.bars_with_trades += 1
        if bar_volume > 0 and not bucket:
            diag.bars_with_volume_but_no_ticks += 1
        if bucket and bar_volume <= 0:
            diag.bars_with_ticks_but_no_volume += 1
        if tick_volume > 0 and bar_volume > 0:
            volume_ratios.append(bar_volume / tick_volume)
        if bucket and bar_count > 0:
            count_ratios.append(bar_count / len(bucket))
        low, high = row.get("low"), row.get("high")
        if bucket and low is not None and high is not None:
            for _, _, price in bucket:
                considered += 1
                if float(low) <= price <= float(high):
                    contained += 1

    if volume_ratios:
        ordered = sorted(volume_ratios)
        diag.volume_ratio_median = statistics.median(ordered)
        diag.volume_ratio_p10 = _quantile(ordered, 0.10)
        diag.volume_ratio_p90 = _quantile(ordered, 0.90)
    if count_ratios:
        diag.count_ratio_median = statistics.median(sorted(count_ratios))
    if considered:
        diag.price_containment_fraction = contained / considered
    return diag


@dataclass
class DailyHealth:
    """
    Checked EVERY day and pushed, not just on the day the recorder ships.

    The classic failure is discovering three months later that the feed silently
    switched to delayed data -- and that every L2/L3 conclusion built on it is
    void. A daily report is what makes that a one-day loss.
    """

    session: str
    events: int
    market_data_type: str
    clock_skew: ClockSkew
    disconnects: int
    recorder_run_ids: list[str] = field(default_factory=list)
    streams: dict[str, StreamHealth] = field(default_factory=dict)
    cross_stream: CrossStreamDiagnostics = field(default_factory=CrossStreamDiagnostics)
    file_hashes: Optional[dict[str, str]] = None
    required_streams: tuple[str, ...] = ()
    fatal_errors: Optional[list[str]] = None
    #: Segments salvaged from a process that died mid-write. The rows are
    #: real, but the tail of each one is gone by definition.
    salvaged_segments: list[str] = field(default_factory=list)
    min_coverage: float = 0.99
    max_median_skew_seconds: float = 2.0

    def problems(self) -> list[str]:
        out = []
        if self.market_data_type != "LIVE":
            out.append(f"market_data_type is {self.market_data_type}, not LIVE")
        if self.events == 0:
            out.append("no events recorded")
        skew = self.clock_skew.median_seconds
        if math.isnan(skew):
            out.append("clock skew was never measured")
        elif abs(skew) > self.max_median_skew_seconds:
            out.append(f"median clock skew {skew:.2f}s over {self.clock_skew.samples} samples")
        for name in self.required_streams:
            health = self.streams.get(name)
            if health is None:
                out.append(f"{name}: stream absent")
            else:
                out.extend(health.problems(self.min_coverage))
        if self.fatal_errors:
            out.extend(f"fatal recorder error: {error}" for error in self.fatal_errors)
        if self.salvaged_segments:
            # The rows in a salvaged segment are genuine, so the recovery is
            # worth doing -- but a segment whose writer was killed mid-stream
            # is missing an unknown number of events at its tail, and no
            # count taken from it can be complete. Before this, the only
            # trace was a "crashed-" filename inside `file_hashes`, which a
            # reader had to notice: `health_ok` stayed true and `problems`
            # said nothing. A day that lost its tail must not report itself
            # as a clean day, whatever the coverage arithmetic works out to.
            out.append(
                "capture truncated: "
                + ", ".join(sorted(self.salvaged_segments))
                + " were salvaged from a killed writer and end at an unknown point"
            )
        return out

    def ok(self) -> bool:
        return not self.problems()

    def as_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "events": self.events,
            "market_data_type": self.market_data_type,
            "clock_skew": asdict(self.clock_skew),
            "disconnects": self.disconnects,
            "recorder_run_ids": sorted(self.recorder_run_ids),
            "streams": {name: asdict(h) for name, h in sorted(self.streams.items())},
            "cross_stream": asdict(self.cross_stream),
            "file_hashes": self.file_hashes,
            "required_streams": list(self.required_streams),
            "fatal_errors": self.fatal_errors or [],
            "salvaged_segments": sorted(self.salvaged_segments),
            "min_coverage": self.min_coverage,
            "max_median_skew_seconds": self.max_median_skew_seconds,
            "problems": self.problems(),
            "ok": self.ok(),
        }


def compute_health(
    log: RawEventLog,
    session_open: datetime,
    session_close: datetime,
    clock_skew_samples: Iterable[float] = (),
    required_streams: tuple[str, ...] = MARKET_STREAMS,
    gap_thresholds: Optional[dict[str, float]] = None,
) -> DailyHealth:
    """Compute health from market events only; SYSTEM heartbeats cannot mask gaps."""
    thresholds = {**DEFAULT_GAP_THRESHOLDS, **(gap_thresholds or {})}
    events = 0
    disconnects = 0
    data_types: set[str] = set()
    fatal_errors: list[str] = []
    run_ids: set[str] = set()
    per_stream: dict[str, list[int]] = {name: [] for name in required_streams}
    rows: list[dict[str, Any]] = []

    for row in log.read_all():
        events += 1
        run_id = row.get("recorder_run_id")
        if run_id:
            run_ids.add(str(run_id))
        event_type = str(row.get("event_type"))
        if event_type == "SYSTEM":
            # Only explicit disconnect-like system events count. Generic
            # heartbeat rows must not fabricate availability.
            condition = str(row.get("special_conditions") or "").upper()
            if any(token in condition for token in ("DISCONNECT", "1100", "1101")):
                disconnects += 1
            if condition.startswith("RECORDER_ERROR:"):
                fatal_errors.append(str(row.get("special_conditions")))
            continue
        rows.append(row)
        per_stream.setdefault(event_type, []).append(int(row["local_wall_ns"]))
        data_types.add(str(row.get("market_data_type", "UNKNOWN")))

    if data_types == {"LIVE"}:
        mdt = "LIVE"
    elif not data_types:
        mdt = "UNKNOWN"
    else:
        mdt = "MIXED:" + ",".join(sorted(data_types))

    open_ns = int(session_open.timestamp() * 1e9)
    close_ns = int(session_close.timestamp() * 1e9)
    streams = {
        name: _stream_health(
            stamps, name, open_ns, close_ns, thresholds.get(name, 30.0)
        )
        for name, stamps in per_stream.items()
    }

    return DailyHealth(
        session=log.session.isoformat(),
        events=events,
        market_data_type=mdt,
        clock_skew=ClockSkew.from_samples(clock_skew_samples),
        disconnects=disconnects,
        recorder_run_ids=sorted(run_ids),
        streams=streams,
        cross_stream=compute_cross_stream_diagnostics(rows),
        required_streams=required_streams,
        fatal_errors=fatal_errors,
        # The health maths is also driven by in-memory row sources in tests,
        # which have no segments at all; absent segments means nothing was
        # salvaged, not that the question is unanswerable.
        salvaged_segments=[
            path.name
            for path in (getattr(log, "segments", None) or (lambda: []))()
            if path.name.startswith("crashed-")
        ],
    )


def _same_halt_state(previous: float, current: float) -> bool:
    """NaN != NaN, so "still unknown" must not read as a transition."""
    previous_unknown = previous is None or math.isnan(previous)
    current_unknown = current is None or math.isnan(current)
    if previous_unknown or current_unknown:
        return previous_unknown and current_unknown
    return int(previous) == int(current)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


class ParquetVerificationError(RuntimeError):
    """The Parquet file on disk does not match the rows we meant to write."""


def finalize_day(
    log: RawEventLog,
    *,
    session_open: datetime,
    session_close: datetime,
    clock_skew_samples: Iterable[float] = (),
    handler_counts: Optional[dict[str, int]] = None,
    selected_counts: Optional[dict[str, int]] = None,
    filtered_counts: Optional[dict[str, int]] = None,
    capture_policy: Optional[dict[str, Any]] = None,
    liveness: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Close raw capture, write atomic Parquet, health JSON and a hash manifest.

    Reads every segment in the session directory, including ones written by an
    earlier run of the same day. That is intended -- one session is one Parquet
    file -- and it is only correct because ``recorder_run_id`` makes rows from
    different runs distinguishable.
    """
    log.close()
    write_accounting = log.write_stats()
    if (
        write_accounting["accepted"] != write_accounting["persisted"]
        or write_accounting["dropped"]
        or write_accounting["writer_error"] is not None
        or write_accounting["accepted_by_stream"]
        != write_accounting["persisted_by_stream"]
    ):
        raise RecorderWriteFailed(
            f"raw writer accounting mismatch: {write_accounting}"
        )
    rows = list(log.read_all())
    current_run_ids = set(write_accounting["persisted_by_run_id"])
    current_run_rows = [row for row in rows if row["recorder_run_id"] in current_run_ids]
    readback_by_stream: dict[str, int] = defaultdict(int)
    for row in current_run_rows:
        readback_by_stream[row["event_type"]] += 1
    readback_by_stream = dict(sorted(readback_by_stream.items()))
    if write_accounting["persisted"] != len(current_run_rows):
        raise RecorderWriteFailed(
            "raw writer/readback count mismatch: "
            f"persisted={write_accounting['persisted']} readback={len(current_run_rows)}"
        )
    if write_accounting["persisted_by_stream"] != readback_by_stream:
        raise RecorderWriteFailed(
            "raw writer/readback stream mismatch: "
            f"persisted={write_accounting['persisted_by_stream']} "
            f"readback={readback_by_stream}"
        )
    readback_by_run_id: dict[str, int] = defaultdict(int)
    for row in current_run_rows:
        readback_by_run_id[row["recorder_run_id"]] += 1
    readback_by_run_id = dict(sorted(readback_by_run_id.items()))
    if write_accounting["persisted_by_run_id"] != readback_by_run_id:
        raise RecorderWriteFailed(
            "raw writer/readback run mismatch: "
            f"persisted={write_accounting['persisted_by_run_id']} "
            f"readback={readback_by_run_id}"
        )

    handled = dict(sorted((handler_counts or {}).items()))
    selected = dict(sorted((selected_counts or {}).items()))
    filtered = dict(sorted((filtered_counts or {}).items()))
    if handled or selected or filtered:
        market_enqueued = {
            stream: write_accounting["accepted_by_stream"].get(stream, 0)
            for stream in MARKET_STREAMS
            if write_accounting["accepted_by_stream"].get(stream, 0)
        }
        if selected != market_enqueued:
            raise RecorderWriteFailed(
                f"callback selection/enqueue mismatch: selected={selected} "
                f"enqueued={market_enqueued}"
            )
        for stream in MARKET_STREAMS:
            if handled.get(stream, 0) != selected.get(stream, 0) + filtered.get(stream, 0):
                raise RecorderWriteFailed(
                    f"callback accounting mismatch for {stream}: handled={handled.get(stream, 0)} "
                    f"selected={selected.get(stream, 0)} filtered={filtered.get(stream, 0)}"
                )

    write_accounting.update(
        {
            "handled_count": sum(handled.values()),
            "handled_by_stream": handled,
            "selected_count": sum(selected.values()),
            "selected_by_stream": selected,
            "filtered_count": sum(filtered.values()),
            "filtered_by_stream": filtered,
            "readback_count": len(current_run_rows),
            "readback_by_stream": readback_by_stream,
            "readback_by_run_id": readback_by_run_id,
            "session_readback_count": len(rows),
        }
    )
    parquet = log.dir / "events.parquet"
    parquet_tmp = log.dir / ".events.parquet.tmp"
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - packaging/preflight failure
        raise RuntimeError("pyarrow is required to finalize recorder output") from exc

    schema = parquet_schema()
    table = pa.Table.from_pylist(rows, schema=schema)
    pq.write_table(table, parquet_tmp, compression="zstd")
    durable_replace(parquet_tmp, parquet)

    # Verify what is on disk, not what we handed to the writer. The point of
    # this whole subsystem is a dataset someone will trust months from now.
    readback = pq.read_table(parquet)
    if readback.num_rows != len(rows):
        raise ParquetVerificationError(
            f"{parquet} holds {readback.num_rows} rows, expected {len(rows)}"
        )
    if readback.schema != schema:
        raise ParquetVerificationError(f"{parquet} schema differs from the declared schema")

    hashes = {p.name: _sha256(p) for p in [*log.segments(), parquet]}
    health = compute_health(
        log,
        session_open=session_open,
        session_close=session_close,
        clock_skew_samples=clock_skew_samples,
        required_streams=MARKET_STREAMS,
    )
    health.file_hashes = hashes
    health_path = log.dir / "health.json"
    durable_atomic_write(
        health_path,
        json.dumps(health.as_dict(), indent=2, sort_keys=True).encode("utf-8"),
    )

    manifest = {
        "schema_version": 3,
        "session": log.session.isoformat(),
        "rows": len(rows),
        "parquet_rows_verified": readback.num_rows,
        "recorder_run_ids": sorted(health.recorder_run_ids),
        "health_ok": health.ok(),
        "problems": health.problems(),
        "write_accounting": write_accounting,
        "capture_policy": capture_policy,
        "liveness": liveness,
        "files": {**hashes, health_path.name: _sha256(health_path)},
    }
    manifest_path = log.dir / "manifest.json"
    durable_atomic_write(
        manifest_path,
        json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
    )
    return manifest


# ----------------------------------------------------------------------------
# runtime
# ----------------------------------------------------------------------------


class SubscriptionLimiter:
    """Small token bucket for all recorder-originated IB requests."""

    def __init__(self, rate_per_second: float = 2.0, burst: int = 4):
        self.rate = rate_per_second
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.last = time.monotonic()

    def wait(self, sleeper: Callable[[float], Any]) -> None:
        while True:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            sleeper(max(0.01, (1 - self.tokens) / self.rate))


class ReconnectBudgetExhausted(RuntimeError):
    """Too many reconnects; the fault is not transient."""


class ReconnectBudget:
    """A rolling budget, not a session-long running total.

    A monotonically increasing counter capped at 8 means eight ordinary blips
    spread across a normal day retire the recorder at 10:30 and finalize half
    a session. A burst is what indicates a real fault, so the short window is
    what should stop a crash loop; the session cap is the backstop for a slow
    bleed that never bursts.
    """

    def __init__(
        self,
        short_window_seconds: float = 900.0,
        short_limit: int = 5,
        session_limit: int = 20,
        now: Callable[[], float] = time.monotonic,
    ):
        self.short_window_seconds = short_window_seconds
        self.short_limit = short_limit
        self.session_limit = session_limit
        self._now = now
        self._recent: list[float] = []
        self.session_total = 0

    def record(self) -> None:
        now = self._now()
        self._recent = [t for t in self._recent if now - t < self.short_window_seconds]
        self._recent.append(now)
        self.session_total += 1
        if len(self._recent) > self.short_limit:
            raise ReconnectBudgetExhausted(
                f"{len(self._recent)} reconnects within "
                f"{self.short_window_seconds:.0f}s (limit {self.short_limit})"
            )
        if self.session_total > self.session_limit:
            raise ReconnectBudgetExhausted(
                f"{self.session_total} reconnects this session (limit {self.session_limit})"
            )

    @property
    def recent(self) -> int:
        now = self._now()
        return len([t for t in self._recent if now - t < self.short_window_seconds])


class RecorderPrerequisiteError(RuntimeError):
    """A configuration/entitlement defect that reconnecting cannot repair."""


class SessionRolloverError(RuntimeError):
    """The exchange session changed underneath a running recorder."""


CLOCK_REQUEST_MIN_INTERVAL_SECONDS = 1.1
REQUEST_DEADLINE_SECONDS = 10.0


def enforce_request_deadline(ib, seconds: float = REQUEST_DEADLINE_SECONDS) -> float:
    """Bound every blocking ib_async request. Fail-closed prerequisite.

    ``IB.RequestTimeout`` defaults to ``0``, and ib_async spells ``0`` as "wait
    forever": ``IB._run`` forwards it straight to ``util.run(..., timeout=...)``.
    Every blocking call resolves a future that only a broker callback completes,
    and a real Gateway has been *directly observed* omitting the ``currentTime``
    callback when requests were paced 0.2s apart.

    Pacing lowers the trigger rate; it does not bound the wait. An unbounded
    wait is the worst possible failure shape here, because the thing that
    blocks is the single event loop -- so the recorder stops reading market
    data, stops running its own health checks, and still looks connected. There
    is nothing left running that could notice. Every read-only probe set this
    attribute itself, which is exactly why no probe ever hung; the deadline
    therefore belonged to the harness rather than to the code under test.
    """
    if seconds <= 0:
        raise ValueError("request deadline must be positive; ib_async reads 0 as 'wait forever'")
    ib.RequestTimeout = float(seconds)
    return float(seconds)


def measure_clock_skew(
    ib,
    samples: int = 5,
    pause: float = CLOCK_REQUEST_MIN_INTERVAL_SECONDS,
    pulse: Optional[Callable[..., None]] = None,
) -> list[float]:
    """Round-trip-compensated skew samples.

    ``local_midpoint - server_time``, where the midpoint of the local clock
    readings either side of the call removes the round trip that a naive
    ``now() - reqCurrentTime()`` folds into the estimate. IB's one-second
    granularity remains, which is exactly why the caller keeps every sample
    and reports a median instead of trusting any one of them.
    """
    out: list[float] = []
    for _ in range(samples):
        # Real Gateway requests made in rapid succession can omit a completion
        # callback. Pace the first request too because callers commonly make a
        # separate reqCurrentTime immediately before this measurement.
        if pulse is not None:
            pulse(phase="CLOCK_PACING")
        ib.sleep(pause)
        if pulse is not None:
            pulse(phase="CLOCK_REQUEST")
        t0 = time.time()
        server = ib.reqCurrentTime()
        t1 = time.time()
        if pulse is not None:
            pulse(phase="CLOCK_RESPONSE")
        if server is None:
            continue
        if server.tzinfo is None:
            server = server.replace(tzinfo=timezone.utc)
        out.append(((t0 + t1) / 2.0) - server.timestamp())
    return out


@dataclass(frozen=True)
class RecorderConfig:
    root: Path
    symbol: str = "SPY"
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 33
    max_backoff_seconds: float = 60.0
    wait_for_rth: bool = True
    roll_seconds: int = 300
    short_window_seconds: float = 900.0
    short_reconnect_limit: int = 5
    session_reconnect_limit: int = 20
    request_deadline_seconds: float = REQUEST_DEADLINE_SECONDS
    queue_capacity: int = 100_000
    writer_batch_records: int = 512
    writer_batch_max_latency_seconds: float = 0.05
    writer_close_timeout_seconds: float = 30.0
    mode: DataMode | str = DataMode.RESEARCH_FULL
    bidask_sample_interval_seconds: float = 1.0
    bidask_on_price_change: bool = True
    decision_window_seconds: float = 30.0
    decision_pre_window_seconds: float = 30.0
    status_path: Optional[Path] = None
    heartbeat_publish_seconds: float = 1.0
    bar_heartbeat_timeout_seconds: float = market_liveness.DEFAULT_BAR_TIMEOUT_SECONDS
    transport_idle_timeout_seconds: float = market_liveness.DEFAULT_TRANSPORT_IDLE_SECONDS
    # Generic tick 49 is the halt state. Requesting it was argued to
    # "strictly dominate" hoping it arrives by default -- harmless if IB
    # sends it anyway, necessary if it does not. A real Gateway disproved
    # that on 2026-08-12: it answered error 321 for a STK contract, the
    # reqMktData probe never produced a LIVE marketDataType callback, and
    # the whole run failed its prerequisites with three zero streams
    # (docs/GATE_B2_CONTROLLED_DISCONNECT_20260812_ZH.md section 1).
    # Requesting an unsupported tick is not free: it can cost the entire
    # subscription. Default back to the set the Gateway is known to serve,
    # and let an operator opt in where tick 49 is actually supported.
    market_data_generic_ticks: str = ""


class QuoteRecorder:
    """
    Subscribes read-only and writes RawTicks.

    Subscribe to all three, deliberately:
      tick-by-tick BidAsk   -> quoted spread, arrival benchmark
      tick-by-tick AllLast  -> trade bars, volume, VWAP
      5-second TRADES bars  -> independent checksum, gap detection

    Connect with the API in read-only mode. The recorder must be structurally
    incapable of placing an order.

    ONE PROCESS RECORDS ONE SESSION. If a reconnect lands in a different
    exchange session the recorder finalizes what it has and exits rather than
    writing tomorrow's ticks into today's log. Daemon-style rollover buys
    nothing a scheduler does not already provide, and costs a whole class of
    date-attribution bugs.
    """

    DATA_TYPE = {0: "UNKNOWN", 1: "LIVE", 2: "FROZEN", 3: "DELAYED", 4: "DELAYED_FROZEN"}

    def __init__(self, root: str | Path, symbol: str = "SPY", **kwargs):
        self.config = RecorderConfig(root=Path(root), symbol=symbol, **kwargs)
        if DataMode(self.config.mode) is DataMode.EXECUTION_MINIMAL:
            raise ValueError(
                "execution_minimal does not start RawEventLog; run the execution host instead"
            )
        self.root = self.config.root
        self.symbol = self.config.symbol
        self.log: Optional[RawEventLog] = None
        self.run_id = uuid4().hex[:10]
        self._event_id = 0
        self._receive_sequence = 0
        self._connection_epoch = 0
        self._market_data_type = "UNKNOWN"
        self._clock_skew_samples: list[float] = []
        self._resubscribe = False
        self._fatal_prerequisite_error: Optional[str] = None
        self._intentional_disconnect = False
        self._limiter = SubscriptionLimiter()
        self._budget = ReconnectBudget(
            short_window_seconds=self.config.short_window_seconds,
            short_limit=self.config.short_reconnect_limit,
            session_limit=self.config.session_reconnect_limit,
        )
        self._session_key: Optional[str] = None
        self._wired: set[int] = set()
        # Counted in the callback, before the write path. A readback that
        # disagrees with these locates the loss on one side of _append.
        self.handled_events: dict[str, int] = {}
        self.selected_events: dict[str, int] = {}
        self.filtered_events: dict[str, int] = {}
        self.capture_policy = CapturePolicy(
            mode=self.config.mode,
            bidask_sample_interval_seconds=self.config.bidask_sample_interval_seconds,
            bidask_on_price_change=self.config.bidask_on_price_change,
            decision_pre_window_seconds=self.config.decision_pre_window_seconds,
            decision_window_seconds=self.config.decision_window_seconds,
        )
        self._heartbeat: Optional[EventLoopHeartbeat] = None
        self._subscription_started_mono: Optional[float] = None
        self._last_handled_mono: dict[str, float] = {}
        if self.config.transport_idle_timeout_seconds <= 0:
            raise ValueError("transport_idle_timeout_seconds must be positive")
        self.liveness = MarketLiveness(
            bar_timeout_seconds=self.config.bar_heartbeat_timeout_seconds,
        )
        self._liveness_incidents = LivenessIncidentTracker()
        self._sample_lock = threading.Lock()
        self._bidask_prebuffer: deque[tuple[float, RawTick]] = deque()

    def _note_handled(self, event_type: str) -> None:
        self.handled_events[event_type] = self.handled_events.get(event_type, 0) + 1
        now = time.monotonic()
        self._last_handled_mono[event_type] = now
        self.liveness.note_event(event_type, now)

    def stream_staleness(self, now_mono: Optional[float] = None) -> dict[str, float]:
        """Over-threshold ages of the *event-driven* streams. Report only.

        Deliberately excludes BAR_5S and deliberately drives nothing. These
        two streams arrive when the market has something to say, so a long
        age is a fact about the tape, not about the subscription -- an
        OVERNIGHT window on 2026-08-10 went 29.6s between prints while the
        bar cadence never faltered. Acting on these numbers would make a
        quiet tape indistinguishable in the report from a dead subscription,
        which is precisely the condition the heartbeat exists to detect.
        Ask :meth:`MarketLiveness.assess` what to *do*.
        """

        return self.liveness.advisory_ages(now_mono)

    def _note_selected(self, event_type: str) -> None:
        self.selected_events[event_type] = self.selected_events.get(event_type, 0) + 1

    def _note_filtered(self, event_type: str) -> None:
        self.filtered_events[event_type] = self.filtered_events.get(event_type, 0) + 1

    def mark_decision(self, *, now_mono: Optional[float] = None, seconds: float | None = None) -> None:
        """Persist the pre-decision ring and open a full-fidelity post window."""

        now = time.monotonic() if now_mono is None else float(now_mono)
        with self._sample_lock:
            self.capture_policy.open_decision_window(now, seconds)
            cutoff = now - self.capture_policy.decision_pre_window_seconds
            while self._bidask_prebuffer and self._bidask_prebuffer[0][0] < cutoff:
                self._bidask_prebuffer.popleft()
            if self.log is None:
                return
            while self._bidask_prebuffer:
                enqueued_mono, tick = self._bidask_prebuffer.popleft()
                remaining = self.filtered_events.get("BID_ASK", 0) - 1
                if remaining > 0:
                    self.filtered_events["BID_ASK"] = remaining
                else:
                    self.filtered_events.pop("BID_ASK", None)
                self._note_selected("BID_ASK")
                self.log.append(tick, now_mono=enqueued_mono)

    def _pulse(self, **state: Any) -> None:
        if self._heartbeat is not None:
            self._heartbeat.pulse(**state)

    def _handler_failed(self, stream: str, exc: BaseException) -> None:
        """A market-data callback raised. That is fatal, not a log line.

        These handlers run inside eventkit's dispatch, which catches every
        exception and either forwards it to an unwired ``error_event`` or calls
        ``logger.exception`` -- then continues. So an exception silently drops
        the rest of that TCP update's tick buffer and the run still finishes
        reporting success. A short log that claims to be complete is worse than
        a failed run, so record it as a fatal prerequisite failure; the run loop
        and the bounded probes already poll this field.
        """
        if self._fatal_prerequisite_error is None:
            self._fatal_prerequisite_error = (
                f"{stream} callback raised {type(exc).__name__}: {exc}"
            )

    def _raise_if_fatal_error(self) -> None:
        """Turn callback state into control flow on the recorder loop.

        Event handlers cannot safely raise through eventkit because its
        dispatcher catches handler exceptions.  Merely storing the failure is
        not fail-closed either: the production loop must observe the stored
        state and stop the run.  Keeping this check in one method makes that
        contract testable and keeps entitlement and data-integrity failures on
        the same non-retryable path.
        """
        if self.log is not None:
            self.log.raise_if_failed()
        if self._heartbeat is not None:
            self._heartbeat.raise_if_failed()
        if self._fatal_prerequisite_error is not None:
            raise RecorderPrerequisiteError(self._fatal_prerequisite_error)

    @staticmethod
    def _is_fatal_market_data_error(code: int, message: str) -> bool:
        """Recognize entitlement failures across IB's localized messages."""
        if code in FATAL_MARKET_DATA_CODES:
            return True
        text = message.casefold()
        return code == 420 and "market data permissions" in text

    def _make_tick(
        self, event_type: str, broker_ts: datetime | str, **values: Any
    ) -> tuple[RawTick, float]:
        self._event_id += 1
        self._receive_sequence += 1
        now_wall = time.time_ns()
        now_mono = time.monotonic_ns()
        if isinstance(broker_ts, datetime):
            broker_text = broker_ts.astimezone(timezone.utc).isoformat()
        else:
            broker_text = str(broker_ts)
        tick = RawTick(
            event_id=self._event_id,
            recorder_run_id=self.run_id,
            connection_epoch=self._connection_epoch,
            contract_id=int(values.pop("contract_id", 0)),
            event_type=event_type,
            broker_timestamp=broker_text,
            local_wall_ns=now_wall,
            local_monotonic_ns=now_mono,
            market_data_type=self._market_data_type,
            receive_sequence=self._receive_sequence,
            **values,
        )
        return tick, now_mono / 1e9

    def _append(self, event_type: str, broker_ts: datetime | str, **values: Any) -> None:
        if self.log is None:
            return
        tick, now_mono = self._make_tick(event_type, broker_ts, **values)
        self.log.append(tick, now_mono=now_mono)

    def _record_market_event(
        self, event_type: str, broker_ts: datetime | str, **values: Any
    ) -> None:
        if self.log is None:
            return
        tick, now_mono = self._make_tick(event_type, broker_ts, **values)
        with self._sample_lock:
            selected = self.capture_policy.should_persist(
                event_type,
                now_mono=now_mono,
                bid=values.get("bid"),
                ask=values.get("ask"),
            )
            if selected:
                self._note_selected(event_type)
                self.log.append(tick, now_mono=now_mono)
                return
            self._note_filtered(event_type)
            if event_type == "BID_ASK":
                self._bidask_prebuffer.append((now_mono, tick))
                cutoff = now_mono - self.capture_policy.decision_pre_window_seconds
                while self._bidask_prebuffer and self._bidask_prebuffer[0][0] < cutoff:
                    self._bidask_prebuffer.popleft()

    @staticmethod
    def _session(details, now: datetime):
        sessions = sorted(details.liquidSessions(), key=lambda s: s.start)
        for session in sessions:
            if session.end >= now:
                return session
        raise RuntimeError("IB contract details contain no current/future liquid session")

    @staticmethod
    def _session_id(session) -> str:
        return f"{session.start.isoformat()}/{session.end.isoformat()}"

    def _wire_ticker(self, ticker) -> None:
        """Drain each ticker's buffer from exactly one handler.

        ib_async returns one Ticker per contract object, so the BidAsk and
        AllLast requests below currently hand back the same object. That is a
        library implementation detail and not a contract the recorder should
        depend on either way: the handles are kept explicitly, and identity
        dedupe here means the code is correct whether one Ticker or two comes
        back -- and never attaches two handlers that would drain one buffer
        twice.
        """
        if ticker is None or id(ticker) in self._wired:
            return
        self._wired.add(id(ticker))

        from ib_async.objects import TickByTickAllLast, TickByTickBidAsk

        def on_update(updated) -> None:
            try:
                for tick in updated.tickByTicks:
                    if isinstance(tick, TickByTickBidAsk):
                        self._note_handled("BID_ASK")
                        values = {
                            "contract_id": updated.contract.conId,
                            "bid": float(tick.bidPrice),
                            "ask": float(tick.askPrice),
                            "bid_size": float(tick.bidSize),
                            "ask_size": float(tick.askSize),
                            "special_conditions": (
                                f"bidPastLow={tick.tickAttribBidAsk.bidPastLow};"
                                f"askPastHigh={tick.tickAttribBidAsk.askPastHigh}"
                            ),
                        }
                        self._record_market_event("BID_ASK", tick.time, **values)
                    elif isinstance(tick, TickByTickAllLast):
                        self._note_handled("ALL_LAST")
                        values = {
                            "contract_id": updated.contract.conId,
                            "last": float(tick.price),
                            "last_size": float(tick.size),
                            "exchange": tick.exchange,
                            "special_conditions": tick.specialConditions,
                        }
                        self._record_market_event("ALL_LAST", tick.time, **values)
            except Exception as exc:  # eventkit would swallow this
                self._handler_failed("tick-by-tick", exc)

        ticker.updateEvent += on_update

    def _wire_bars(self, bars) -> None:
        def on_bar(updated, has_new_bar) -> None:
            if not has_new_bar or not updated:
                return
            try:
                bar = updated[-1]
                self._note_handled("BAR_5S")
                values = {
                    "contract_id": updated.contract.conId,
                    "open": float(bar.open_),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": float(bar.volume),
                    "wap": float(bar.wap),
                    "trade_count": int(bar.count),
                }
                self._record_market_event("BAR_5S", bar.time, **values)
            except Exception as exc:  # eventkit would swallow this
                self._handler_failed("realtime-bar", exc)

        bars.updateEvent += on_bar

    def _subscribe(self, ib, contract):
        self._wired.clear()
        self._last_handled_mono.clear()
        self._subscription_started_mono = time.monotonic()
        self.liveness.subscription_started(self._subscription_started_mono)
        self._limiter.wait(ib.sleep)
        generic_ticks = self.config.market_data_generic_ticks
        self.liveness.note_halt_state_source(
            HALT_GENERIC_TICK in [t.strip() for t in generic_ticks.split(",") if t.strip()],
            detail=f"market_data_generic_ticks={generic_ticks!r}",
        )
        probe = ib.reqMktData(contract, generic_ticks, False, False)
        probe.marketDataType = 0  # distinguish an actual callback from ib_async's default
        deadline = time.monotonic() + 10.0
        while int(probe.marketDataType) == 0 and time.monotonic() < deadline:
            self._raise_if_fatal_error()
            ib.sleep(0.10)
        self._raise_if_fatal_error()
        observed = int(probe.marketDataType)
        self._market_data_type = self.DATA_TYPE.get(observed, f"UNKNOWN:{observed}")
        self._append(
            "SYSTEM", datetime.now(timezone.utc), contract_id=contract.conId,
            special_conditions=f"MARKET_DATA_TYPE:{self._market_data_type}",
        )
        if observed != 1:
            raise RecorderPrerequisiteError(
                f"Recorder requires an explicit LIVE marketDataType callback; observed "
                f"{self._market_data_type}. Tick-by-tick delayed data is unsupported."
            )
        self._limiter.wait(ib.sleep)
        bidask_ticker = ib.reqTickByTickData(contract, "BidAsk", 0, False)
        self._limiter.wait(ib.sleep)
        alllast_ticker = ib.reqTickByTickData(contract, "AllLast", 0, False)
        self._limiter.wait(ib.sleep)
        bars = ib.reqRealTimeBars(contract, 5, "TRADES", True)
        self._wire_ticker(bidask_ticker)
        self._wire_ticker(alllast_ticker)
        self._wire_bars(bars)
        return probe, (bidask_ticker, alllast_ticker), bars

    def _finalize(self, session) -> dict[str, Any]:
        assert self.log is not None
        liveness_manifest = self.liveness.manifest()
        liveness_manifest["incidents"] = self._liveness_incidents.manifest()
        return finalize_day(
            self.log,
            session_open=session.start,
            session_close=session.end,
            clock_skew_samples=self._clock_skew_samples,
            handler_counts=self.handled_events,
            selected_counts=self.selected_events,
            filtered_counts=self.filtered_events,
            capture_policy=self.capture_policy.manifest(),
            liveness=liveness_manifest,
        )

    def run(self) -> dict[str, Any]:  # pragma: no cover - requires a real Gateway/session
        cfg = self.config
        status_path = cfg.status_path or (
            cfg.root / f".{cfg.symbol.lower()}-{cfg.client_id}-recorder-status.json"
        )
        heartbeat = EventLoopHeartbeat(
            status_path,
            component=f"quote-recorder-{cfg.symbol}-{cfg.client_id}",
            publish_seconds=cfg.heartbeat_publish_seconds,
        )
        self._heartbeat = heartbeat
        failed = False
        heartbeat.start()
        self._pulse(phase="STARTING", data_mode=self.capture_policy.mode.value)
        try:
            result = self._run_session_loop()
            self._pulse(phase="FINALIZED", health_ok=result.get("health_ok"))
            return result
        except BaseException:
            failed = True
            try:
                self._pulse(phase="FAILED")
            except BaseException:
                # Preserve the primary Recorder/Gateway failure. A heartbeat
                # failure is already visible as a missing/stale status file.
                pass
            raise
        finally:
            try:
                heartbeat.close(phase="FAILED" if failed else "STOPPED")
            except BaseException:
                if not failed:
                    raise
            finally:
                self._heartbeat = None

    def _run_session_loop(self) -> dict[str, Any]:
        from ib_async import IB, StartupFetchNONE, Stock

        cfg = self.config
        session = None
        while True:
            self._pulse(phase="CONNECTING")
            ib = IB()
            enforce_request_deadline(ib, cfg.request_deadline_seconds)
            self._fatal_prerequisite_error = None
            self._intentional_disconnect = False
            try:
                ib.connect(
                    cfg.host, cfg.port, clientId=cfg.client_id, timeout=10,
                    readonly=True, fetchFields=StartupFetchNONE,
                )
                self._pulse(phase="CONNECTED")
                self._connection_epoch += 1
                contract = Stock(cfg.symbol, "SMART", "USD", primaryExchange="ARCA")
                qualified = ib.qualifyContracts(contract)
                if len(qualified) != 1:
                    raise RuntimeError(f"could not uniquely qualify {cfg.symbol}: {qualified}")
                contract = qualified[0]
                details_list = ib.reqContractDetails(contract)
                if not details_list:
                    raise RuntimeError("IB returned no contract details/liquid hours")
                self._pulse(phase="SERVER_TIME_REQUEST")
                server_now = ib.reqCurrentTime()
                self._pulse(phase="SERVER_TIME_RESPONSE")
                session = self._session(details_list[0], server_now)
                session_key = self._session_id(session)

                if self._session_key is None:
                    self._session_key = session_key
                elif session_key != self._session_key:
                    # One process records one session. Hand the next one to the
                    # scheduler rather than writing it into today's directory.
                    raise SessionRolloverError(
                        f"session rolled over from {self._session_key} to {session_key}"
                    )

                if self.log is None:
                    self.log = RawEventLog(
                        cfg.root, session=session.start.date(),
                        roll_seconds=cfg.roll_seconds,
                        run_id=self.run_id,
                        queue_capacity=cfg.queue_capacity,
                        batch_records=cfg.writer_batch_records,
                        batch_max_latency_seconds=cfg.writer_batch_max_latency_seconds,
                        close_timeout_seconds=cfg.writer_close_timeout_seconds,
                    )
                self._clock_skew_samples.extend(measure_clock_skew(ib, pulse=self._pulse))
                self._append(
                    "SYSTEM", server_now, contract_id=contract.conId,
                    special_conditions="CONNECTED;READ_ONLY=true;SERVER_TIME",
                )

                def on_error(req_id, code, message, error_contract):
                    self._append(
                        "SYSTEM", datetime.now(timezone.utc),
                        contract_id=getattr(error_contract, "conId", 0) or 0,
                        special_conditions=f"IB_ERROR:{code}:{req_id}:{message}",
                    )
                    # IB tells us about halts, farm outages and the
                    # connectivity triple directly. Listening beats inferring
                    # the same facts from durations several seconds later.
                    self.liveness.note_status(code, message)
                    if code == REQUEST_VALIDATION_ERROR:
                        # 321 is IB's generic "error validating request", so
                        # it is not proof that the halt tick specifically was
                        # refused -- but if we asked for it and the Gateway
                        # rejected the request, the suppressor has no input
                        # either way, and the report must not let a reader
                        # read a missing halt marker as "not halted".
                        self.liveness.note_halt_state_unavailable(
                            f"IB rejected the market-data request ({code}): {message}"
                        )
                    if code in {1101, 10225}:
                        self._resubscribe = True
                    if self._is_fatal_market_data_error(code, message):
                        self._fatal_prerequisite_error = (
                            f"IB market-data prerequisite failed ({code}): {message}"
                        )

                def on_transport_idle(idle_seconds):
                    # ib_async disarms itself after firing (wrapper.setTimeout(0)),
                    # so re-arm or this only ever reports once per connection.
                    self.liveness.note_transport_idle(float(idle_seconds))
                    self._append(
                        "SYSTEM", datetime.now(timezone.utc), contract_id=contract.conId,
                        special_conditions=f"TRANSPORT_IDLE:{float(idle_seconds):.1f}s",
                    )
                    ib.setTimeout(cfg.transport_idle_timeout_seconds)

                def on_disconnect():
                    self._append(
                        "SYSTEM", datetime.now(timezone.utc), contract_id=contract.conId,
                        special_conditions=(
                            "CONNECTION_CLOSED_INTENTIONAL"
                            if self._intentional_disconnect
                            else "DISCONNECT"
                        ),
                    )

                ib.errorEvent += on_error
                ib.disconnectedEvent += on_disconnect
                ib.timeoutEvent += on_transport_idle
                # Detects a silent *peer*. It runs on the event loop, so it
                # cannot detect a stuck loop -- EventLoopHeartbeat owns that,
                # from its own thread. Two failure domains, two detectors.
                ib.setTimeout(cfg.transport_idle_timeout_seconds)

                while datetime.now(session.start.tzinfo) < session.start:
                    if not cfg.wait_for_rth:
                        raise RuntimeError("RTH has not started and wait_for_rth is false")
                    ib.sleep(min(1.0, (session.start - datetime.now(session.start.tzinfo)).total_seconds()))
                    self._pulse(phase="WAITING_FOR_SESSION")
                    self._raise_if_fatal_error()

                probe, _tickers, bars = self._subscribe(ib, contract)
                last_mdt = None
                last_halted = float("nan")
                last_server_probe = time.monotonic()
                while datetime.now(session.end.tzinfo) < session.end:
                    if not ib.isConnected():
                        raise ConnectionError("IB disconnected during RTH")
                    ib.sleep(0.25)
                    halted = getattr(probe, "halted", float("nan"))
                    if not _same_halt_state(last_halted, halted):
                        # A halt cannot be scheduled -- SPY essentially never
                        # halts -- so "do bars keep flowing through one?"
                        # cannot be answered by an experiment. Recording each
                        # transition turns it into passive collection: if a
                        # halt ever does happen on a Gateway that serves tick
                        # 49, the evidence lands in the raw log beside the bar
                        # timeline. On a Gateway that rejects tick 49 this
                        # simply never fires.
                        self._append(
                            "SYSTEM", datetime.now(timezone.utc), contract_id=contract.conId,
                            special_conditions=f"HALT_STATE:{halted}",
                        )
                        last_halted = halted
                    self.liveness.note_halted(halted)
                    state = self.liveness.assess()
                    self._pulse(
                        phase="CAPTURING",
                        bar_age=state.heartbeat_age,
                        liveness=state.action.value,
                        expected_silence=state.expected_silence,
                        advisory_ages=state.advisory_ages,
                    )
                    # eventkit catches callback exceptions.  The callback
                    # records them; this loop must turn that state into the
                    # non-retryable/finalized failure path.
                    self._raise_if_fatal_error()
                    # Assess four times per second, but record sustained states
                    # as incidents rather than turning the poll cadence into
                    # hundreds of duplicate audit rows.
                    for marker in self._liveness_incidents.observe(state):
                        self._append(
                            "SYSTEM",
                            datetime.now(timezone.utc),
                            contract_id=contract.conId,
                            special_conditions=marker,
                        )
                    if state.action is LivenessAction.RECOVER_SUBSCRIPTION:
                        # Recovery is a reconnect, which the reconnect budget
                        # already bounds and escalates. Repeated heartbeat
                        # loss therefore terminates the session on its own,
                        # without a second escalation policy here.
                        raise ConnectionError(f"market data not live: {state.reason}")
                    mdt = int(probe.marketDataType)
                    if mdt != last_mdt:
                        self._market_data_type = self.DATA_TYPE.get(mdt, f"UNKNOWN:{mdt}")
                        self._append(
                            "SYSTEM", datetime.now(timezone.utc), contract_id=contract.conId,
                            special_conditions=f"MARKET_DATA_TYPE:{self._market_data_type}",
                        )
                        last_mdt = mdt
                    if self._resubscribe:
                        self._append(
                            "SYSTEM", datetime.now(timezone.utc), contract_id=contract.conId,
                            special_conditions="RESUBSCRIBE_REQUIRED",
                        )
                        raise ConnectionError("subscription reset required")
                    if time.monotonic() - last_server_probe >= 60:
                        self._clock_skew_samples.extend(
                            measure_clock_skew(ib, samples=3, pulse=self._pulse)
                        )
                        self._append(
                            "SYSTEM", datetime.now(timezone.utc), contract_id=contract.conId,
                            special_conditions="SERVER_TIME",
                        )
                        last_server_probe = time.monotonic()

                for marker in self._liveness_incidents.close("session ended"):
                    self._append(
                        "SYSTEM",
                        datetime.now(timezone.utc),
                        contract_id=contract.conId,
                        special_conditions=marker,
                    )
                ib.cancelTickByTickData(contract, "BidAsk")
                ib.cancelTickByTickData(contract, "AllLast")
                ib.cancelRealTimeBars(bars)
                ib.cancelMktData(contract)
                self._intentional_disconnect = True
                ib.disconnect()
                assert session is not None
                return self._finalize(session)

            except RecorderWriteFailed:
                # The storage path itself is poisoned, so trying to append a
                # RECORDER_ERROR row through it would only mask the first
                # failure. Disconnect and surface the error to the supervisor.
                if ib.isConnected():
                    self._intentional_disconnect = True
                    ib.disconnect()
                if self.log is not None:
                    try:
                        self.log.close()
                    except RecorderWriteFailed:
                        pass
                raise
            except (RecorderPrerequisiteError, SessionRolloverError, ProcessLockUnavailable) as exc:
                self._append(
                    "SYSTEM", datetime.now(timezone.utc),
                    special_conditions=f"RECORDER_ERROR:{type(exc).__name__}:{exc}",
                )
                if ib.isConnected():
                    self._intentional_disconnect = True
                    ib.disconnect()
                if self.log is not None and session is not None:
                    return self._finalize(session)
                raise
            except Exception as exc:
                self._append(
                    "SYSTEM", datetime.now(timezone.utc),
                    special_conditions=f"RECORDER_ERROR:{type(exc).__name__}:{exc}",
                )
                if ib.isConnected():
                    self._intentional_disconnect = True
                    ib.disconnect()
                try:
                    self._budget.record()
                except ReconnectBudgetExhausted as fatal:
                    self._append(
                        "SYSTEM", datetime.now(timezone.utc),
                        special_conditions=f"RECORDER_ERROR:{type(fatal).__name__}:{fatal}",
                    )
                    if self.log is not None and session is not None:
                        return self._finalize(session)
                    raise fatal from exc
                delay = min(cfg.max_backoff_seconds, 2 ** (self._budget.recent - 1))
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    self._pulse(phase="RECONNECT_BACKOFF")
                    time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
                self._resubscribe = False


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - operator CLI
    ap = argparse.ArgumentParser(description="Read-only Full-RTH IB recorder")
    ap.add_argument("--root", default="data/recordings")
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4002)
    ap.add_argument("--client-id", type=int, default=33)
    ap.add_argument("--session-reconnect-limit", type=int, default=20)
    ap.add_argument("--queue-capacity", type=int, default=100_000)
    ap.add_argument("--writer-batch-records", type=int, default=512)
    ap.add_argument(
        "--mode",
        choices=[DataMode.EVIDENCE_SAMPLED.value, DataMode.RESEARCH_FULL.value],
        default=DataMode.RESEARCH_FULL.value,
    )
    ap.add_argument("--bidask-sample-seconds", type=float, default=1.0)
    ap.add_argument("--decision-window-seconds", type=float, default=30.0)
    ap.add_argument("--decision-pre-window-seconds", type=float, default=30.0)
    ap.add_argument("--status-path", default=None)
    ap.add_argument("--no-wait", action="store_true")
    args = ap.parse_args(argv)
    try:
        manifest = QuoteRecorder(
            args.root,
            args.symbol,
            host=args.host,
            port=args.port,
            client_id=args.client_id,
            session_reconnect_limit=args.session_reconnect_limit,
            queue_capacity=args.queue_capacity,
            writer_batch_records=args.writer_batch_records,
            mode=args.mode,
            bidask_sample_interval_seconds=args.bidask_sample_seconds,
            decision_window_seconds=args.decision_window_seconds,
            decision_pre_window_seconds=args.decision_pre_window_seconds,
            status_path=Path(args.status_path) if args.status_path else None,
            wait_for_rth=not args.no_wait,
        ).run()
    except ProcessLockUnavailable as exc:
        print(f"refusing to start: {exc}")
        return 3
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if manifest.get("health_ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
