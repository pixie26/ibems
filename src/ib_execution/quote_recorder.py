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
import statistics
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional
from uuid import uuid4

from .processlock import ProcessLock, ProcessLockUnavailable

# Market-data failures that reconnecting cannot repair. 10197 belongs here and
# was missing: "no market data during competing session" is what a live and a
# paper login contending for one live subscription looks like, and retrying it
# just produces the same error at a slower rate.
FATAL_MARKET_DATA_CODES = frozenset({354, 10089, 10189, 10197})

MARKET_STREAMS = ("BID_ASK", "ALL_LAST", "BAR_5S")

# Per-stream gap thresholds. One number cannot serve all three: 5-second bars
# arrive on a fixed cadence, quotes arrive continuously, prints do not.
DEFAULT_GAP_THRESHOLDS = {"BID_ASK": 5.0, "ALL_LAST": 30.0, "BAR_5S": 15.0}


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

    def __init__(self, root: str | Path, session: Optional[date] = None,
                 roll_seconds: int = 300, sync_seconds: float = 1.0,
                 run_id: Optional[str] = None, lock: bool = True):
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
        # A same-day process restart must not overwrite earlier segments.
        self.run_id = run_id or uuid4().hex[:10]
        self._lock: Optional[ProcessLock] = None
        if lock:
            self._lock = ProcessLock(self.dir / ".recorder.lock")
            self._lock.acquire(note=f"session={self.session.isoformat()} run={self.run_id}")
        self._recover_crashed_segments()

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
        self._last_sync = now_mono
        self._seq += 1

    def _close_segment(self) -> None:
        if self._fh is None or self._current is None:
            return
        self._fh.close()
        final = self._current.with_name(self._current.name.replace(".partial-", "segment-"))
        os.replace(self._current, final)   # atomic
        self._fh = None
        self._current = None

    def append(self, tick: RawTick, now_mono: float) -> None:
        if self._fh is None or (
            self._opened_at is not None and now_mono - self._opened_at >= self.roll_seconds
        ):
            self._open_segment(now_mono)
        assert self._fh is not None
        self._fh.write(json.dumps(asdict(tick), separators=(",", ":")) + "\n")
        self._count += 1
        self._fh.flush()
        if self._last_sync is None or now_mono - self._last_sync >= self.sync_seconds:
            # Bound crash loss without forcing one fsync for every market tick.
            raw = getattr(getattr(self._fh, "buffer", None), "fileobj", None)
            if raw is not None:
                os.fsync(raw.fileno())
            self._last_sync = now_mono

    def close(self) -> None:
        self._close_segment()
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    def segments(self) -> list[Path]:
        return sorted(
            list(self.dir.glob("segment-*.jsonl.gz"))
            + list(self.dir.glob("crashed-*.jsonl.gz"))
        )

    def read_all(self) -> Iterator[dict[str, Any]]:
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
        return self._count


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
    )


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
) -> dict[str, Any]:
    """Close raw capture, write atomic Parquet, health JSON and a hash manifest.

    Reads every segment in the session directory, including ones written by an
    earlier run of the same day. That is intended -- one session is one Parquet
    file -- and it is only correct because ``recorder_run_id`` makes rows from
    different runs distinguishable.
    """
    log.close()
    rows = list(log.read_all())
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
    os.replace(parquet_tmp, parquet)

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
    health_tmp = log.dir / ".health.json.tmp"
    health_tmp.write_text(
        json.dumps(health.as_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )
    os.replace(health_tmp, health_path)

    manifest = {
        "schema_version": 2,
        "session": log.session.isoformat(),
        "rows": len(rows),
        "parquet_rows_verified": readback.num_rows,
        "recorder_run_ids": sorted(health.recorder_run_ids),
        "health_ok": health.ok(),
        "problems": health.problems(),
        "files": {**hashes, health_path.name: _sha256(health_path)},
    }
    manifest_path = log.dir / "manifest.json"
    manifest_tmp = log.dir / ".manifest.json.tmp"
    manifest_tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(manifest_tmp, manifest_path)
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
        ib.sleep(pause)
        t0 = time.time()
        server = ib.reqCurrentTime()
        t1 = time.time()
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

    def _note_handled(self, event_type: str) -> None:
        self.handled_events[event_type] = self.handled_events.get(event_type, 0) + 1

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
        if self._fatal_prerequisite_error is not None:
            raise RecorderPrerequisiteError(self._fatal_prerequisite_error)

    @staticmethod
    def _is_fatal_market_data_error(code: int, message: str) -> bool:
        """Recognize entitlement failures across IB's localized messages."""
        if code in FATAL_MARKET_DATA_CODES:
            return True
        text = message.casefold()
        return code == 420 and "market data permissions" in text

    def _append(self, event_type: str, broker_ts: datetime | str, **values: Any) -> None:
        if self.log is None:
            return
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
        self.log.append(tick, now_mono=now_mono / 1e9)

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
                        self._append(
                            "BID_ASK", tick.time, contract_id=updated.contract.conId,
                            bid=float(tick.bidPrice), ask=float(tick.askPrice),
                            bid_size=float(tick.bidSize), ask_size=float(tick.askSize),
                            special_conditions=(
                                f"bidPastLow={tick.tickAttribBidAsk.bidPastLow};"
                                f"askPastHigh={tick.tickAttribBidAsk.askPastHigh}"
                            ),
                        )
                    elif isinstance(tick, TickByTickAllLast):
                        self._note_handled("ALL_LAST")
                        self._append(
                            "ALL_LAST", tick.time, contract_id=updated.contract.conId,
                            last=float(tick.price), last_size=float(tick.size),
                            exchange=tick.exchange, special_conditions=tick.specialConditions,
                        )
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
                self._append(
                    "BAR_5S", bar.time, contract_id=updated.contract.conId,
                    open=float(bar.open_), high=float(bar.high), low=float(bar.low),
                    close=float(bar.close), volume=float(bar.volume), wap=float(bar.wap),
                    trade_count=int(bar.count),
                )
            except Exception as exc:  # eventkit would swallow this
                self._handler_failed("realtime-bar", exc)

        bars.updateEvent += on_bar

    def _subscribe(self, ib, contract):
        self._wired.clear()
        self._limiter.wait(ib.sleep)
        probe = ib.reqMktData(contract, "", False, False)
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
        return finalize_day(
            self.log,
            session_open=session.start,
            session_close=session.end,
            clock_skew_samples=self._clock_skew_samples,
        )

    def run(self) -> dict[str, Any]:  # pragma: no cover - requires a real Gateway/session
        from ib_async import IB, StartupFetchNONE, Stock

        cfg = self.config
        session = None
        while True:
            ib = IB()
            enforce_request_deadline(ib, cfg.request_deadline_seconds)
            self._fatal_prerequisite_error = None
            self._intentional_disconnect = False
            try:
                ib.connect(
                    cfg.host, cfg.port, clientId=cfg.client_id, timeout=10,
                    readonly=True, fetchFields=StartupFetchNONE,
                )
                self._connection_epoch += 1
                contract = Stock(cfg.symbol, "SMART", "USD", primaryExchange="ARCA")
                qualified = ib.qualifyContracts(contract)
                if len(qualified) != 1:
                    raise RuntimeError(f"could not uniquely qualify {cfg.symbol}: {qualified}")
                contract = qualified[0]
                details_list = ib.reqContractDetails(contract)
                if not details_list:
                    raise RuntimeError("IB returned no contract details/liquid hours")
                server_now = ib.reqCurrentTime()
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
                        roll_seconds=cfg.roll_seconds, run_id=self.run_id,
                    )
                self._clock_skew_samples.extend(measure_clock_skew(ib))
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
                    if code in {1101, 10225}:
                        self._resubscribe = True
                    if self._is_fatal_market_data_error(code, message):
                        self._fatal_prerequisite_error = (
                            f"IB market-data prerequisite failed ({code}): {message}"
                        )

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

                while datetime.now(session.start.tzinfo) < session.start:
                    if not cfg.wait_for_rth:
                        raise RuntimeError("RTH has not started and wait_for_rth is false")
                    ib.sleep(min(1.0, (session.start - datetime.now(session.start.tzinfo)).total_seconds()))
                    self._raise_if_fatal_error()

                probe, _tickers, bars = self._subscribe(ib, contract)
                last_mdt = None
                last_server_probe = time.monotonic()
                while datetime.now(session.end.tzinfo) < session.end:
                    if not ib.isConnected():
                        raise ConnectionError("IB disconnected during RTH")
                    ib.sleep(0.25)
                    # eventkit catches callback exceptions.  The callback
                    # records them; this loop must turn that state into the
                    # non-retryable/finalized failure path.
                    self._raise_if_fatal_error()
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
                        self._clock_skew_samples.extend(measure_clock_skew(ib, samples=3))
                        self._append(
                            "SYSTEM", datetime.now(timezone.utc), contract_id=contract.conId,
                            special_conditions="SERVER_TIME",
                        )
                        last_server_probe = time.monotonic()

                ib.cancelTickByTickData(contract, "BidAsk")
                ib.cancelTickByTickData(contract, "AllLast")
                ib.cancelRealTimeBars(bars)
                ib.cancelMktData(contract)
                self._intentional_disconnect = True
                ib.disconnect()
                assert session is not None
                return self._finalize(session)

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
                time.sleep(delay)
                self._resubscribe = False


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - operator CLI
    ap = argparse.ArgumentParser(description="Read-only Full-RTH IB recorder")
    ap.add_argument("--root", default="data/recordings")
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4002)
    ap.add_argument("--client-id", type=int, default=33)
    ap.add_argument("--session-reconnect-limit", type=int, default=20)
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
            wait_for_rth=not args.no_wait,
        ).run()
    except ProcessLockUnavailable as exc:
        print(f"refusing to start: {exc}")
        return 3
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if manifest.get("health_ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
