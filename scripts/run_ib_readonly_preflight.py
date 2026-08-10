"""Auditable, broker-write-free IB Gateway preflight.

This script deliberately has no order placement/cancellation path.  It verifies
the API handshake, reads three account-truth streams repeatedly, and probes the
SPY market-data entitlement.  Raw account identifiers and positions never enter
the report; only counts and canonical hashes are persisted.

TICKS ARE COUNTED AS THEY ARRIVE
--------------------------------
The 2026-08-07 run of this script slept for twenty seconds and then read
``Ticker.tickByTicks``.  ib_async clears that buffer between network updates,
so what it actually measured was "ticks in the final flush", not "ticks during
the window".  It reported ``AllLast=0`` and that number was mistaken for
evidence that IB was not delivering trades.  It was not evidence of anything.
``bars_5s`` was correct in the same run only by accident: ``reqRealTimeBars``
returns a ``RealTimeBarList``, which accumulates.

Counting now happens in event handlers, and inter-arrival statistics are
reported alongside the totals so a low count can be distinguished from a dead
subscription without running the script again.

Note what this still cannot tell you: ``reqRealTimeBars`` and
``reqTickByTickData`` are different request paths, so a healthy bar stream is
not evidence about the tick stream.  Each stream is reported on its own terms.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ib_async import IB, StartupFetchNONE, Stock
from ib_async.objects import TickByTickAllLast, TickByTickBidAsk

# 10197 ("no market data during competing session") is fatal for the same
# reason as the entitlement codes: a live and a paper login contending for one
# subscription is fixed by a human closing one, never by retrying.
ENTITLEMENT_CODES = {354, 10089, 10189, 10197}

# The health verdict uses a median because IB's server clock is quantized to
# whole seconds and a single sample cannot separate drift from quantization.
CLOCK_SKEW_SAMPLES = 7
CLOCK_REQUEST_MIN_INTERVAL_SECONDS = 1.1
MAX_MEDIAN_CLOCK_SKEW_SECONDS = 2.0


class StreamCounter:
    """Accumulates arrivals for one stream, and enough to explain a low count."""

    def __init__(self, name: str):
        self.name = name
        self.count = 0
        self.first_mono: float | None = None
        self.last_mono: float | None = None
        self._gaps: list[float] = []

    def record(self, now_mono: float | None = None) -> None:
        now = time.monotonic() if now_mono is None else now_mono
        self.count += 1
        if self.first_mono is None:
            self.first_mono = now
        else:
            assert self.last_mono is not None
            self._gaps.append(now - self.last_mono)
        self.last_mono = now

    def report(self, window_seconds: float) -> dict[str, Any]:
        ordered = sorted(self._gaps)
        return {
            "count": self.count,
            "per_second": (self.count / window_seconds) if window_seconds else 0.0,
            "first_offset_seconds": self.first_mono,
            "last_offset_seconds": self.last_mono,
            "max_inter_arrival_seconds": max(ordered) if ordered else None,
            "median_inter_arrival_seconds": statistics.median(ordered) if ordered else None,
        }


def measure_clock_skew(ib: IB, samples: int = CLOCK_SKEW_SAMPLES) -> list[float]:
    """Round-trip-compensated skew samples; see quote_recorder.measure_clock_skew."""
    out: list[float] = []
    for _ in range(samples):
        # The real Gateway can leave rapid repeated reqCurrentTime requests
        # unanswered.  ib_async's blocking wrapper otherwise waits forever
        # when RequestTimeout is zero.  Pace every request, including the first
        # one after the separate server-time observation made by run().
        ib.sleep(CLOCK_REQUEST_MIN_INTERVAL_SECONDS)
        t0 = time.time()
        server = ib.reqCurrentTime()
        t1 = time.time()
        if server is None:
            continue
        if server.tzinfo is None:
            server = server.replace(tzinfo=timezone.utc)
        out.append(((t0 + t1) / 2.0) - server.timestamp())
    return out


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _trade_row(trade) -> dict[str, Any]:
    """Canonical broker order fact used only as input to a local digest."""
    return {
        "account": trade.order.account,
        "con_id": trade.contract.conId,
        "order_id": trade.order.orderId,
        "perm_id": trade.order.permId,
        "client_id": trade.order.clientId,
        "order_ref": trade.order.orderRef,
        "parent_id": trade.order.parentId,
        "action": trade.order.action,
        "quantity": str(trade.order.totalQuantity),
        "order_type": trade.order.orderType,
        "limit_price": trade.order.lmtPrice,
        "aux_price": trade.order.auxPrice,
        "status": trade.orderStatus.status,
        "filled": str(trade.orderStatus.filled),
        "remaining": str(trade.orderStatus.remaining),
    }


def _identifier_coverage(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Report whether broker identifiers exist without persisting the IDs."""
    return {
        name: sum(value not in (None, "", 0) for value in (row[name] for row in rows))
        for name in ("order_id", "perm_id", "client_id", "order_ref")
    }


def _snapshot(
    ib: IB,
    index: int,
    account_summary: list[Any] | None,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    positions = sorted(
        [
            {
                "account": position.account,
                "con_id": position.contract.conId,
                "position": str(position.position),
                "average_cost": position.avgCost,
            }
            for position in ib.reqPositions()
        ],
        key=lambda row: (row["account"], row["con_id"]),
    )
    orders = sorted(
        [_trade_row(trade) for trade in ib.reqAllOpenOrders()],
        key=lambda row: (row["account"], row["perm_id"], row["order_id"]),
    )
    executions = sorted(
        [
            {
                "account": fill.execution.acctNumber,
                "con_id": fill.contract.conId,
                "exec_id": fill.execution.execId,
                "order_id": fill.execution.orderId,
                "perm_id": fill.execution.permId,
                "client_id": fill.execution.clientId,
                "time": str(fill.execution.time),
                "side": fill.execution.side,
                "shares": str(fill.execution.shares),
                "price": fill.execution.price,
            }
            for fill in ib.reqExecutions()
        ],
        key=lambda row: row["exec_id"],
    )
    ended = datetime.now(timezone.utc)
    account_values = sorted(
        [
            {
                "account": value.account,
                "tag": value.tag,
                "value": value.value,
                "currency": value.currency,
            }
            for value in account_summary
        ],
        key=lambda row: (row["account"], row["tag"], row["currency"]),
    ) if account_summary is not None else None
    components = {
        "account_summary": account_values,
        "positions": positions,
        "open_orders": orders,
        "executions": executions,
    }
    complete = all(rows is not None for rows in components.values())
    return {
        "index": index,
        "started_utc": started.isoformat(),
        "ended_utc": ended.isoformat(),
        "duration_seconds": (ended - started).total_seconds(),
        "counts": {
            name: len(rows) if rows is not None else None for name, rows in components.items()
        },
        "hashes": {
            name: _digest(rows) if rows is not None else None
            for name, rows in components.items()
        },
        "missing_components": [name for name, rows in components.items() if rows is None],
        "identifier_coverage": {
            "open_orders": _identifier_coverage(orders),
        },
        "snapshot_hash": _digest(components) if complete else None,
    }


def _fatal_entitlement_error(error: dict[str, Any]) -> bool:
    if error["code"] in ENTITLEMENT_CODES:
        return True
    return error["code"] == 420 and "market data permissions" in error["message"].casefold()


def _market_checks(
    *,
    stable_snapshot: bool,
    market_type: int,
    sample_counts: dict[str, int],
    clock_ok: bool,
    entitlement_blocked: bool,
    sample_window: float,
    required_sample_seconds: float,
) -> dict[str, bool]:
    """Build fail-closed market checks from independently recorded facts."""
    return {
        "stable_snapshot": stable_snapshot,
        "live_market_data": market_type == 1,
        "all_streams_nonzero": all(sample_counts.values()),
        "clock_skew_within_threshold": clock_ok,
        "no_fatal_entitlement_error": not entitlement_blocked,
        "sample_window_complete": sample_window >= required_sample_seconds,
    }


def _bounded_read(name: str, request) -> tuple[list[Any] | None, dict[str, Any]]:
    """Turn an absent IB completion callback into explicit UNKNOWN evidence."""
    started = time.monotonic()
    try:
        rows = list(request())
    except Exception as exc:
        return None, {
            "name": name,
            "completed": False,
            "duration_seconds": time.monotonic() - started,
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
        }
    return rows, {
        "name": name,
        "completed": True,
        "duration_seconds": time.monotonic() - started,
        "count": len(rows),
    }


def _validate_session_exchange(session_label: str, exchange: str) -> None:
    """Prevent an evidence label from being paired with the wrong data route."""
    expected = {"OVERNIGHT": "OVERNIGHT", "RTH": "SMART"}.get(session_label)
    if expected is not None and exchange != expected:
        raise ValueError(
            f"session label {session_label} requires --market-data-exchange {expected}"
        )


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    ib = IB()
    # Bound every synchronous IB request.  readonly=True prevents broker
    # writes; RequestTimeout prevents a missing callback from hanging the
    # operator's preflight indefinitely.
    ib.RequestTimeout = args.request_timeout
    errors: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "schema_version": 3,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "session_label": args.session_label,
        "host": args.host,
        "port": args.port,
        "client_id": args.client_id,
        "readonly": True,
        "python": platform.python_version(),
        "platform": platform.platform(),
    }

    def on_error(req_id, code, message, contract):
        errors.append(
            {
                "req_id": req_id,
                "code": code,
                "message": message,
                "con_id": getattr(contract, "conId", 0) if contract else 0,
            }
        )

    ib.errorEvent += on_error
    probe = bidask = alllast = bars = contract = None
    try:
        ib.connect(
            args.host,
            args.port,
            clientId=args.client_id,
            timeout=10,
            readonly=True,
            fetchFields=StartupFetchNONE,
        )
        server_time = ib.reqCurrentTime().astimezone(timezone.utc)
        skew_samples = measure_clock_skew(ib)
        skew_median = statistics.median(skew_samples) if skew_samples else float("nan")
        report["connection"] = {
            "connected": ib.isConnected(),
            "server_version": ib.client.serverVersion(),
            "server_time_utc": server_time.isoformat(),
            "clock_skew": {
                "samples": len(skew_samples),
                "median_seconds": skew_median,
                "max_abs_seconds": max((abs(v) for v in skew_samples), default=None),
                "raw_seconds": skew_samples,
                "note": (
                    "round-trip compensated; IB's server clock is quantized to whole "
                    "seconds, so a single sample cannot separate drift from quantization"
                ),
            },
            "account_count": len(ib.managedAccounts()),
        }

        contract = Stock(
            args.symbol,
            args.market_data_exchange,
            "USD",
            primaryExchange="ARCA",
        )
        qualified = ib.qualifyContracts(contract)
        if len(qualified) != 1:
            raise RuntimeError(f"could not uniquely qualify {args.symbol}: {qualified}")
        contract = qualified[0]
        details = ib.reqContractDetails(contract)
        report["contract"] = {
            "con_id": contract.conId,
            "symbol": contract.symbol,
            "exchange": contract.exchange,
            "primary_exchange": contract.primaryExchange,
            "currency": contract.currency,
            "requested_market_data_exchange": args.market_data_exchange,
            "details_count": len(details),
        }

        # This request has an explicit accountSummaryEnd completion callback.
        # Values are then read from ib_async's cache; raw values enter only the
        # canonical digest and are never serialized in the report.
        account_summary, account_summary_completion = _bounded_read(
            "account_summary",
            lambda: (ib.reqAccountSummary(), ib.accountSummary())[1],
        )
        report["request_completions"] = {
            "account_summary": account_summary_completion,
            "completed_orders": {
                "completed": False,
                "status": "BLOCKED_BY_GATEWAY_READ_ONLY_POLICY",
                "note": (
                    "not requested: a live 2026-08-10 probe caused Gateway to prompt "
                    "for disabling read-only API; operator kept read-only enabled"
                ),
            },
        }
        rounds = []
        for index in range(1, args.snapshot_rounds + 1):
            rounds.append(_snapshot(ib, index, account_summary))
            if index < args.snapshot_rounds:
                ib.sleep(args.snapshot_interval)
        pairs = [
            {
                "left": left["index"],
                "right": right["index"],
                "equal": (
                    left["snapshot_hash"] is not None
                    and right["snapshot_hash"] is not None
                    and left["snapshot_hash"] == right["snapshot_hash"]
                ),
                "component_equal": {
                    name: (
                        left["hashes"][name] is not None
                        and right["hashes"][name] is not None
                        and left["hashes"][name] == right["hashes"][name]
                    )
                    for name in left["hashes"]
                },
            }
            for left, right in zip(rounds, rounds[1:])
        ]
        stable = any(pair["equal"] for pair in pairs)
        report["stable_snapshot"] = {
            "protocol": (
                "account-summary completion -> positions -> all-open-orders -> "
                "executions; require two consecutive "
                "equal canonical snapshots"
            ),
            "rounds": rounds,
            "pairs": pairs,
            "observed_stable": stable,
        }

        probe = ib.reqMktData(contract, "", False, False)
        probe.marketDataType = 0
        deadline = time.monotonic() + args.market_data_timeout
        while int(probe.marketDataType) == 0 and time.monotonic() < deadline:
            if any(_fatal_entitlement_error(error) for error in errors):
                break
            ib.sleep(0.1)
        market_type = int(probe.marketDataType)
        counters = {
            name: StreamCounter(name) for name in ("bid_ask", "all_last", "bars_5s")
        }
        window = 0.0
        if market_type == 1:
            # Each request's Ticker handle is kept explicitly. ib_async returns
            # one Ticker per contract object today, so these are currently the
            # same object -- which is exactly why the handler is attached once
            # per distinct object rather than once per request: two handlers on
            # one buffer would double-count every tick.
            bidask = ib.reqTickByTickData(contract, "BidAsk", 0, False)
            alllast = ib.reqTickByTickData(contract, "AllLast", 0, False)
            bars = ib.reqRealTimeBars(contract, 5, "TRADES", True)

            started = time.monotonic()

            def on_ticker_update(ticker) -> None:
                for tick in ticker.tickByTicks:
                    if isinstance(tick, TickByTickBidAsk):
                        counters["bid_ask"].record(time.monotonic() - started)
                    elif isinstance(tick, TickByTickAllLast):
                        counters["all_last"].record(time.monotonic() - started)

            def on_bar(updated, has_new_bar) -> None:
                if has_new_bar:
                    counters["bars_5s"].record(time.monotonic() - started)

            for ticker in {id(t): t for t in (bidask, alllast)}.values():
                ticker.updateEvent += on_ticker_update
            bars.updateEvent += on_bar

            while time.monotonic() - started < args.sample_seconds:
                if any(_fatal_entitlement_error(error) for error in errors):
                    break
                ib.sleep(0.25)
            window = time.monotonic() - started

        samples = {name: counter.report(window) for name, counter in counters.items()}
        sample_counts = {name: counter.count for name, counter in counters.items()}
        entitlement_blocked = any(_fatal_entitlement_error(error) for error in errors)
        report["market_data"] = {
            "market_data_type": market_type,
            "live": market_type == 1,
            "sample_seconds": window,
            "sample_counts": sample_counts,
            "samples": samples,
            "measurement": (
                "counted in updateEvent handlers as ticks arrive; Ticker.tickByTicks "
                "is a per-update buffer and is never polled"
            ),
            "entitlement_blocked": entitlement_blocked,
        }
        report["errors"] = errors
        clock_ok = bool(skew_samples) and abs(skew_median) <= MAX_MEDIAN_CLOCK_SKEW_SECONDS
        report["checks"] = _market_checks(
            stable_snapshot=stable,
            market_type=market_type,
            sample_counts=sample_counts,
            clock_ok=clock_ok,
            entitlement_blocked=entitlement_blocked,
            sample_window=window,
            required_sample_seconds=args.sample_seconds,
        )
        report["passed"] = all(report["checks"].values())
        return report, 0 if report["passed"] else 2
    finally:
        if ib.isConnected():
            if contract is not None:
                if bidask is not None:
                    ib.cancelTickByTickData(contract, "BidAsk")
                if alllast is not None:
                    ib.cancelTickByTickData(contract, "AllLast")
                if bars is not None:
                    ib.cancelRealTimeBars(bars)
                if probe is not None:
                    ib.cancelMktData(contract)
            ib.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only IB Gateway preflight")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id", type=int, default=933)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument(
        "--session-label",
        choices=("UNSPECIFIED", "OVERNIGHT", "RTH"),
        default="UNSPECIFIED",
        help="Evidence label; routing must be selected explicitly and match the label",
    )
    parser.add_argument(
        "--market-data-exchange",
        choices=("SMART", "OVERNIGHT"),
        default="SMART",
        help="Explicit market-data route; OVERNIGHT is distinct from SMART",
    )
    parser.add_argument("--snapshot-rounds", type=int, default=3)
    parser.add_argument("--snapshot-interval", type=float, default=1.0)
    parser.add_argument("--market-data-timeout", type=float, default=10.0)
    parser.add_argument("--request-timeout", type=float, default=10.0)
    parser.add_argument("--sample-seconds", type=float, default=90.0)
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.snapshot_rounds < 2:
        parser.error("--snapshot-rounds must be at least 2")
    if args.request_timeout <= 0:
        parser.error("--request-timeout must be greater than zero")
    try:
        _validate_session_exchange(args.session_label, args.market_data_exchange)
    except ValueError as exc:
        parser.error(str(exc))

    report, exit_code = run(args)
    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    output = Path(args.output) if args.output else (
        Path("artifacts")
        / "ib_preflight"
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        / "report.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"report": str(output), **report}, indent=2, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
