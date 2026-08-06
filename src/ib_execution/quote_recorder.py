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
import hashlib
import json
import os
import argparse
import math
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
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
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    wap: Optional[float] = None
    trade_count: Optional[int] = None


class RawEventLog:
    """
    Append-only rolling event log.

    Never hold one Parquet file open all session: a crash at 15:45 costs the
    whole day. Roll every few minutes, atomic-rename on completion, compact to
    Parquet after the close. Raw logs are never modified in place -- derived
    tables are built beside them, and the manifest records hashes.
    """

    def __init__(self, root: str | Path, session: Optional[date] = None,
                 roll_seconds: int = 300, sync_seconds: float = 1.0):
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
        self.run_id = uuid4().hex[:10]
        self._recover_crashed_segments()

    def _recover_crashed_segments(self) -> None:
        """Preserve abruptly-terminated gzip streams for best-effort row salvage."""
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
    stream_rows: Optional[dict[str, int]] = None
    file_hashes: Optional[dict[str, str]] = None
    required_streams: tuple[str, ...] = ()

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
        if self.stream_rows is not None and self.required_streams:
            missing = [s for s in self.required_streams if not self.stream_rows.get(s)]
            if missing:
                out.append(f"missing required streams: {','.join(missing)}")
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
    stream_rows: dict[str, int] = {}
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
        stream_rows[str(event_type)] = stream_rows.get(str(event_type), 0) + 1
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
        stream_rows=stream_rows,
        required_streams=required_streams,
    )
    missing = sorted(set(required_streams) - streams_seen)
    if missing:
        # Preserve the compact public dataclass while making the failure visible
        # through a non-LIVE status that DailyHealth.problems already rejects.
        health.market_data_type = f"MISSING_STREAMS:{','.join(missing)}"
    return health


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def finalize_day(
    log: RawEventLog,
    *,
    session_seconds: float,
    clock_skew_seconds: float,
) -> dict[str, Any]:
    """Close raw capture, write atomic Parquet, health JSON and a hash manifest."""
    log.close()
    rows = list(log.read_all())
    parquet = log.dir / "events.parquet"
    parquet_tmp = log.dir / ".events.parquet.tmp"
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - packaging/preflight failure
        raise RuntimeError("pyarrow is required to finalize recorder output") from exc
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, parquet_tmp, compression="zstd")
    os.replace(parquet_tmp, parquet)

    hashes = {p.name: _sha256(p) for p in [*log.segments(), parquet]}
    health = compute_health(
        log,
        session_seconds=session_seconds,
        clock_skew_seconds=clock_skew_seconds,
        required_streams=("BID_ASK", "ALL_LAST", "BAR_5S"),
    )
    health.file_hashes = hashes
    health_path = log.dir / "health.json"
    health_tmp = log.dir / ".health.json.tmp"
    health_tmp.write_text(json.dumps(asdict(health), indent=2, sort_keys=True), encoding="utf-8")
    os.replace(health_tmp, health_path)

    manifest = {
        "schema_version": 1,
        "session": log.session.isoformat(),
        "rows": len(rows),
        "health_ok": health.ok(),
        "problems": health.problems(),
        "files": {**hashes, health_path.name: _sha256(health_path)},
    }
    manifest_path = log.dir / "manifest.json"
    manifest_tmp = log.dir / ".manifest.json.tmp"
    manifest_tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(manifest_tmp, manifest_path)
    return manifest


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


@dataclass(frozen=True)
class RecorderConfig:
    root: Path
    symbol: str = "SPY"
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 33
    max_reconnects: int = 8
    max_backoff_seconds: float = 60.0
    wait_for_rth: bool = True
    roll_seconds: int = 300


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

    DATA_TYPE = {0: "UNKNOWN", 1: "LIVE", 2: "FROZEN", 3: "DELAYED", 4: "DELAYED_FROZEN"}

    def __init__(self, root: str | Path, symbol: str = "SPY", **kwargs):
        self.config = RecorderConfig(root=Path(root), symbol=symbol, **kwargs)
        self.root = self.config.root
        self.symbol = self.config.symbol
        self.log: Optional[RawEventLog] = None
        self._event_id = 0
        self._receive_sequence = 0
        self._connection_epoch = 0
        self._market_data_type = "UNKNOWN"
        self._clock_skew_seconds = math.nan
        self._resubscribe = False
        self._limiter = SubscriptionLimiter()
        self._ticker_types = None

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

    def _wire_ticker(self, ticker) -> None:
        from ib_async.objects import TickByTickAllLast, TickByTickBidAsk
        self._ticker_types = (TickByTickAllLast, TickByTickBidAsk)

        def on_update(updated) -> None:
            for tick in updated.tickByTicks:
                if isinstance(tick, TickByTickBidAsk):
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
                    self._append(
                        "ALL_LAST", tick.time, contract_id=updated.contract.conId,
                        last=float(tick.price), last_size=float(tick.size),
                        exchange=tick.exchange, special_conditions=tick.specialConditions,
                    )

        ticker.updateEvent += on_update

    def _wire_bars(self, bars) -> None:
        def on_bar(updated, has_new_bar) -> None:
            if not has_new_bar or not updated:
                return
            bar = updated[-1]
            self._append(
                "BAR_5S", bar.time, contract_id=updated.contract.conId,
                open=float(bar.open_), high=float(bar.high), low=float(bar.low),
                close=float(bar.close), volume=float(bar.volume), wap=float(bar.wap),
                trade_count=int(bar.count),
            )

        bars.updateEvent += on_bar

    def _subscribe(self, ib, contract):
        self._limiter.wait(ib.sleep)
        probe = ib.reqMktData(contract, "", False, False)
        probe.marketDataType = 0  # distinguish an actual callback from ib_async's default
        deadline = time.monotonic() + 10.0
        while int(probe.marketDataType) == 0 and time.monotonic() < deadline:
            ib.sleep(0.10)
        observed = int(probe.marketDataType)
        self._market_data_type = self.DATA_TYPE.get(observed, f"UNKNOWN:{observed}")
        self._append(
            "SYSTEM", datetime.now(timezone.utc), contract_id=contract.conId,
            special_conditions=f"MARKET_DATA_TYPE:{self._market_data_type}",
        )
        if observed != 1:
            raise RuntimeError(
                f"Recorder requires an explicit LIVE marketDataType callback; observed "
                f"{self._market_data_type}. Tick-by-tick delayed data is unsupported."
            )
        self._limiter.wait(ib.sleep)
        ticker = ib.reqTickByTickData(contract, "BidAsk", 0, False)
        self._limiter.wait(ib.sleep)
        ib.reqTickByTickData(contract, "AllLast", 0, False)
        self._limiter.wait(ib.sleep)
        bars = ib.reqRealTimeBars(contract, 5, "TRADES", True)
        self._wire_ticker(ticker)
        self._wire_bars(bars)
        return probe, ticker, bars

    def run(self) -> dict[str, Any]:  # pragma: no cover - requires a real Gateway/session
        from ib_async import IB, StartupFetchNONE, Stock

        cfg = self.config
        attempts = 0
        session = None
        while attempts <= cfg.max_reconnects:
            ib = IB()
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
                if self.log is None:
                    self.log = RawEventLog(
                        cfg.root, session=session.start.date(), roll_seconds=cfg.roll_seconds
                    )
                self._clock_skew_seconds = (datetime.now(timezone.utc) - server_now).total_seconds()
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

                def on_disconnect():
                    self._append(
                        "SYSTEM", datetime.now(timezone.utc), contract_id=contract.conId,
                        special_conditions="DISCONNECT",
                    )

                ib.errorEvent += on_error
                ib.disconnectedEvent += on_disconnect

                while datetime.now(session.start.tzinfo) < session.start:
                    if not cfg.wait_for_rth:
                        raise RuntimeError("RTH has not started and wait_for_rth is false")
                    ib.sleep(min(1.0, (session.start - datetime.now(session.start.tzinfo)).total_seconds()))

                probe, ticker, bars = self._subscribe(ib, contract)
                last_mdt = None
                last_server_probe = time.monotonic()
                while datetime.now(session.end.tzinfo) < session.end:
                    if not ib.isConnected():
                        raise ConnectionError("IB disconnected during RTH")
                    ib.sleep(0.25)
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
                        server_probe = ib.reqCurrentTime()
                        self._clock_skew_seconds = (
                            datetime.now(timezone.utc) - server_probe
                        ).total_seconds()
                        self._append(
                            "SYSTEM", server_probe, contract_id=contract.conId,
                            special_conditions="SERVER_TIME",
                        )
                        last_server_probe = time.monotonic()

                ib.cancelTickByTickData(contract, "BidAsk")
                ib.cancelTickByTickData(contract, "AllLast")
                ib.cancelRealTimeBars(bars)
                ib.cancelMktData(contract)
                ib.disconnect()
                assert self.log is not None and session is not None
                return finalize_day(
                    self.log,
                    session_seconds=(session.end - session.start).total_seconds(),
                    clock_skew_seconds=self._clock_skew_seconds,
                )
            except Exception as exc:
                self._append(
                    "SYSTEM", datetime.now(timezone.utc),
                    special_conditions=f"RECORDER_ERROR:{type(exc).__name__}:{exc}",
                )
                if ib.isConnected():
                    ib.disconnect()
                attempts += 1
                if attempts > cfg.max_reconnects:
                    if self.log is not None and session is not None:
                        return finalize_day(
                            self.log,
                            session_seconds=(session.end - session.start).total_seconds(),
                            clock_skew_seconds=self._clock_skew_seconds,
                        )
                    raise
                delay = min(cfg.max_backoff_seconds, 2 ** (attempts - 1))
                time.sleep(delay)
                self._resubscribe = False
        raise RuntimeError("unreachable")


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - operator CLI
    ap = argparse.ArgumentParser(description="Read-only Full-RTH IB recorder")
    ap.add_argument("--root", default="data/recordings")
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4002)
    ap.add_argument("--client-id", type=int, default=33)
    ap.add_argument("--max-reconnects", type=int, default=8)
    ap.add_argument("--no-wait", action="store_true")
    args = ap.parse_args(argv)
    manifest = QuoteRecorder(
        args.root,
        args.symbol,
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        max_reconnects=args.max_reconnects,
        wait_for_rth=not args.no_wait,
    ).run()
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if manifest.get("health_ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
