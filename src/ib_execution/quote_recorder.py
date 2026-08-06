"""
Read-only market data recorder.

    ####################################################################
    #  STATUS: UNVERIFIED skeleton. Storage layer and daily health     #
    #  report are implemented and tested; the IB subscription is not.  #
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
"""

from __future__ import annotations

import gzip
import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from uuid import uuid4


@dataclass(frozen=True)
class RawTick:
    """
    One market data event, exactly as received.

    Both timestamps are mandatory. Later we must be able to separate "when the
    market did something" from "when we found out", and that distinction is
    unrecoverable if only one is stored.
    """

    event_id: int
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


class RawEventLog:
    """
    Append-only rolling event log.

    Never hold one Parquet file open all session: a crash at 15:45 costs the
    whole day. Roll every few minutes, atomic-rename on completion, compact to
    Parquet after the close. Raw logs are never modified in place -- derived
    tables are built beside them, and the manifest records hashes.
    """

    def __init__(self, root: str | Path, session: Optional[date] = None,
                 roll_seconds: int = 300):
        self.root = Path(root)
        self.session = session or datetime.now(timezone.utc).date()
        self.dir = self.root / self.session.isoformat()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.roll_seconds = roll_seconds
        self._fh = None
        self._current: Optional[Path] = None
        self._opened_at: Optional[float] = None
        self._seq = 0
        self._count = 0
        # A same-day process restart must not overwrite earlier segments.
        self.run_id = uuid4().hex[:10]

    def _open_segment(self, now_mono: float) -> None:
        self._close_segment()
        stamp = datetime.now(timezone.utc).strftime("%H%M%S")
        self._current = self.dir / (
            f".partial-{stamp}-{self.run_id}-{self._seq:05d}.jsonl.gz"
        )
        self._fh = gzip.open(self._current, "wt", encoding="utf-8")
        self._opened_at = now_mono
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

    def close(self) -> None:
        self._close_segment()

    def segments(self) -> list[Path]:
        return sorted(self.dir.glob("segment-*.jsonl.gz"))

    def read_all(self) -> Iterator[dict[str, Any]]:
        for seg in self.segments():
            with gzip.open(seg, "rt", encoding="utf-8") as fh:
                for line in fh:
                    yield json.loads(line)

    @property
    def count(self) -> int:
        return self._count


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
    coverage_fraction: float
    max_gap_seconds: float
    clock_skew_seconds: float
    disconnects: int

    def problems(self) -> list[str]:
        out = []
        if self.market_data_type != "LIVE":
            out.append(f"market_data_type is {self.market_data_type}, not LIVE")
        if self.coverage_fraction < 0.99:
            out.append(f"session coverage only {self.coverage_fraction:.3%}")
        if self.max_gap_seconds > 30:
            out.append(f"largest data gap {self.max_gap_seconds:.0f}s")
        if abs(self.clock_skew_seconds) > 2:
            out.append(f"clock skew {self.clock_skew_seconds:.2f}s")
        if self.events == 0:
            out.append("no events recorded")
        return out

    def ok(self) -> bool:
        return not self.problems()


def compute_health(
    log: RawEventLog,
    session_seconds: float,
    clock_skew_seconds: float = 0.0,
    required_streams: tuple[str, ...] = (),
) -> DailyHealth:
    """Compute health from market events only; SYSTEM heartbeats cannot mask gaps."""
    events = 0
    disconnects = 0
    data_types: set[str] = set()
    stamps: list[int] = []
    streams_seen: set[str] = set()
    for row in log.read_all():
        events += 1
        event_type = row.get("event_type")
        if event_type == "SYSTEM":
            # Only explicit disconnect-like system events count. Generic
            # heartbeat rows must not fabricate availability.
            condition = str(row.get("special_conditions") or "").upper()
            if any(token in condition for token in ("DISCONNECT", "1100", "1101")):
                disconnects += 1
            continue
        streams_seen.add(str(event_type))
        data_types.add(str(row.get("market_data_type", "UNKNOWN")))
        stamps.append(int(row["local_wall_ns"]))

    stamps.sort()
    max_gap = 0.0
    for a, b in zip(stamps, stamps[1:]):
        max_gap = max(max_gap, (b - a) / 1e9)
    covered = ((stamps[-1] - stamps[0]) / 1e9) if len(stamps) >= 2 else 0.0

    if data_types == {"LIVE"}:
        mdt = "LIVE"
    elif not data_types:
        mdt = "UNKNOWN"
    else:
        mdt = "MIXED:" + ",".join(sorted(data_types))

    health = DailyHealth(
        session=log.session.isoformat(),
        events=events,
        market_data_type=mdt,
        coverage_fraction=(covered / session_seconds) if session_seconds else 0.0,
        max_gap_seconds=max_gap,
        clock_skew_seconds=clock_skew_seconds,
        disconnects=disconnects,
    )
    missing = sorted(set(required_streams) - streams_seen)
    if missing:
        # Preserve the compact public dataclass while making the failure visible
        # through a non-LIVE status that DailyHealth.problems already rejects.
        health.market_data_type = f"MISSING_STREAMS:{','.join(missing)}"
    return health


class QuoteRecorder:
    """
    UNVERIFIED. Subscribes read-only and writes RawTicks.

    Subscribe to all three, deliberately:
      tick-by-tick BidAsk   -> quoted spread, arrival benchmark
      tick-by-tick AllLast  -> trade bars, volume, VWAP
      5-second TRADES bars  -> independent checksum, gap detection

    Connect with the API in read-only mode. The recorder must be structurally
    incapable of placing an order.

    Prerequisite to verify before Week 0 ships: live L1 market data entitlement
    for SPY. Tick-by-tick needs it, and delayed data silently produces a
    worthless dataset.
    """

    def __init__(self, root: str | Path, symbol: str = "SPY"):
        self.root = Path(root)
        self.symbol = symbol
        self.log: Optional[RawEventLog] = None

    def run(self) -> None:  # pragma: no cover
        raise NotImplementedError(
            "Week 0 task. Requires ib_async connection with readonly=True, "
            "reqTickByTickData(BidAsk), reqTickByTickData(AllLast), "
            "reqRealTimeBars(TRADES), its own token bucket, bounded backoff, "
            "and a daily health report pushed to phone."
        )
