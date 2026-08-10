"""Observe an IB-server network fault while the local Gateway stays alive.

This probe has no broker-write path. It connects with ``readonly=True``, holds
three SPY market-data subscriptions across the fault, and reads server time,
positions, all open orders, and executions. A recovery is accepted only after
a real 1101/1102 callback, an explicit per-stream post-recovery observation,
and a fresh server-time plus broker-state snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ib_async import IB, StartupFetchNONE, Stock
from ib_async.objects import TickByTickAllLast, TickByTickBidAsk


RECOVERY_CODES = {1101, 1102}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _snapshot(ib: IB) -> dict[str, Any]:
    components = {
        "positions": sorted(
            (p.account, p.contract.conId, str(p.position), p.avgCost)
            for p in ib.reqPositions()
        ),
        "open_orders": sorted(
            (
                t.order.account,
                t.contract.conId,
                t.order.orderId,
                t.order.permId,
                t.order.clientId,
                t.order.orderRef,
                t.orderStatus.status,
            )
            for t in ib.reqAllOpenOrders()
        ),
        "executions": sorted(
            (
                f.execution.acctNumber,
                f.contract.conId,
                f.execution.execId,
                f.execution.orderId,
                f.execution.permId,
                f.execution.clientId,
            )
            for f in ib.reqExecutions()
        ),
    }
    return {
        "counts": {name: len(rows) for name, rows in components.items()},
        "hashes": {name: _digest(rows) for name, rows in components.items()},
        "snapshot_hash": _digest(components),
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


def _stream_snapshot(counts: dict[str, int], last_seen: dict[str, str | None]) -> dict[str, Any]:
    return {
        name: {"count": counts[name], "last_seen_utc": last_seen[name]}
        for name in ("bid_ask", "all_last", "bars_5s")
    }


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    timeline: list[dict[str, Any]] = []
    error_codes: list[int] = []
    disconnected = False
    counts = {name: 0 for name in ("bid_ask", "all_last", "bars_5s")}
    last_seen: dict[str, str | None] = {name: None for name in counts}
    report: dict[str, Any] = {
        "schema_version": 1,
        "started_utc": _now(),
        "host": args.host,
        "port": args.port,
        "client_id": args.client_id,
        "readonly": True,
        "session_label": args.session_label,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "expected_fault": "process-specific outbound firewall block; Gateway remains alive",
        "broker_write_calls": [],
        "timeline": timeline,
    }
    ib = IB()
    ib.RequestTimeout = args.request_timeout

    def on_error(req_id: int, code: int, message: str, contract: Any) -> None:
        error_codes.append(code)
        timeline.append(
            {
                "utc": _now(),
                "event": "error",
                "req_id": req_id,
                "code": code,
                "message": message,
            }
        )

    def on_disconnect() -> None:
        nonlocal disconnected
        disconnected = True
        timeline.append({"utc": _now(), "event": "local_api_socket_disconnected"})

    ib.errorEvent += on_error
    ib.disconnectedEvent += on_disconnect
    contract = None
    probe = None
    tickers: tuple[Any, Any] | None = None
    bars = None
    try:
        ib.connect(
            args.host,
            args.port,
            clientId=args.client_id,
            timeout=args.request_timeout,
            readonly=True,
            fetchFields=StartupFetchNONE,
        )
        timeline.append({"utc": _now(), "event": "connected_readonly"})
        report["before_server_time"] = ib.reqCurrentTime().isoformat()
        report["before_snapshot"] = _snapshot(ib)

        requested = Stock(
            args.symbol,
            args.market_data_exchange,
            args.currency,
            primaryExchange=args.primary_exchange,
        )
        qualified = ib.qualifyContracts(requested)
        if len(qualified) != 1:
            raise RuntimeError(f"could not uniquely qualify {args.symbol}: {qualified}")
        contract = qualified[0]
        probe = ib.reqMktData(contract, "", False, False)
        probe.marketDataType = 0
        market_deadline = time.monotonic() + args.request_timeout
        while int(probe.marketDataType) == 0 and time.monotonic() < market_deadline:
            ib.sleep(0.1)
        if int(probe.marketDataType) != 1:
            raise RuntimeError(
                f"fault probe requires explicit LIVE market data; observed {probe.marketDataType}"
            )

        bidask = ib.reqTickByTickData(contract, "BidAsk", 0, False)
        alllast = ib.reqTickByTickData(contract, "AllLast", 0, False)
        bars = ib.reqRealTimeBars(contract, 5, "TRADES", True)
        tickers = (bidask, alllast)

        def on_ticker_update(ticker) -> None:
            for tick in ticker.tickByTicks:
                if isinstance(tick, TickByTickBidAsk):
                    name = "bid_ask"
                elif isinstance(tick, TickByTickAllLast):
                    name = "all_last"
                else:
                    continue
                counts[name] += 1
                last_seen[name] = _now()

        def on_bar(updated, has_new_bar) -> None:
            if has_new_bar:
                counts["bars_5s"] += 1
                last_seen["bars_5s"] = _now()

        for ticker in {id(item): item for item in tickers}.values():
            ticker.updateEvent += on_ticker_update
        bars.updateEvent += on_bar

        warmup_deadline = time.monotonic() + args.stream_warmup_timeout
        while not all(counts.values()) and time.monotonic() < warmup_deadline:
            ib.sleep(0.1)
        if not all(counts.values()):
            raise RuntimeError(f"not all streams became active before fault: {counts}")
        report["contract"] = {
            "con_id": contract.conId,
            "symbol": contract.symbol,
            "exchange": contract.exchange,
            "primary_exchange": contract.primaryExchange,
            "currency": contract.currency,
        }
        report["pre_fault_streams"] = _stream_snapshot(counts, last_seen)
        report["ready_utc"] = _now()
        _write_report(Path(args.output), report)
        print(json.dumps({"event": "READY_APPLY_FIREWALL", "utc": report["ready_utc"]}), flush=True)

        deadline = time.monotonic() + args.observation_timeout
        recovery_seen_at: float | None = None
        recovery_counts: dict[str, int] | None = None
        while time.monotonic() < deadline:
            try:
                ib.sleep(0.1)
            except ConnectionError as exc:
                timeline.append(
                    {
                        "utc": _now(),
                        "event": "local_api_socket_exception",
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                    }
                )
                break
            if recovery_seen_at is None and any(code in RECOVERY_CODES for code in error_codes):
                recovery_seen_at = time.monotonic()
                recovery_counts = dict(counts)
                report["streams_at_recovery"] = _stream_snapshot(counts, last_seen)
            if (
                recovery_seen_at is not None
                and time.monotonic() - recovery_seen_at >= args.post_recovery_observation
            ):
                break

        report["observed_codes"] = error_codes
        report["checks"] = {
            "local_api_socket_remained_connected": ib.isConnected() and not disconnected,
            "observed_1100": 1100 in error_codes,
            "observed_1101_or_1102": any(code in RECOVERY_CODES for code in error_codes),
            "all_streams_active_before_fault": all(
                report.get("pre_fault_streams", {}).get(name, {}).get("count", 0) > 0
                for name in counts
            ),
        }
        report["post_recovery_streams"] = _stream_snapshot(counts, last_seen)
        deltas = {
            name: counts[name] - (recovery_counts or counts)[name]
            for name in counts
        }
        report["post_recovery_stream_deltas"] = deltas
        recovery_code = next((code for code in reversed(error_codes) if code in RECOVERY_CODES), None)
        report["recovery_code"] = recovery_code
        report["resubscribe_required"] = recovery_code == 1101
        # For 1102, IB says requests were recovered; direct stream resumption
        # is therefore part of the check. For 1101, zero deltas are an expected
        # possible observation and the report explicitly requires resubscribe.
        report["checks"]["recovery_stream_semantics_evaluated"] = (
            all(value > 0 for value in deltas.values())
            if recovery_code == 1102
            else recovery_code == 1101
        )
        if all(report["checks"].values()):
            report["after_server_time"] = ib.reqCurrentTime().isoformat()
            report["after_snapshot"] = _snapshot(ib)
            report["checks"]["post_recovery_server_time_completed"] = True
            report["checks"]["post_recovery_snapshot_completed"] = True
            report["checks"]["snapshot_equal"] = (
                report["before_snapshot"]["snapshot_hash"]
                == report["after_snapshot"]["snapshot_hash"]
            )
        else:
            report["checks"]["post_recovery_server_time_completed"] = False
            report["checks"]["post_recovery_snapshot_completed"] = False
            report["checks"]["snapshot_equal"] = False
        report["passed"] = all(report["checks"].values())
        return report, 0 if report["passed"] else 2
    finally:
        if ib.isConnected():
            if contract is not None:
                for ticker_type in ("BidAsk", "AllLast"):
                    try:
                        ib.cancelTickByTickData(contract, ticker_type)
                    except Exception:
                        pass
                if bars is not None:
                    try:
                        ib.cancelRealTimeBars(bars)
                    except Exception:
                        pass
                if probe is not None:
                    try:
                        ib.cancelMktData(contract)
                    except Exception:
                        pass
            timeline.append({"utc": _now(), "event": "intentional_cleanup_disconnect"})
            ib.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id", type=int, default=942)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--session-label", choices=("RTH", "OVERNIGHT"), default="RTH")
    parser.add_argument("--market-data-exchange", default="SMART")
    parser.add_argument("--primary-exchange", default="ARCA")
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--request-timeout", type=float, default=10.0)
    parser.add_argument("--observation-timeout", type=float, default=240.0)
    parser.add_argument("--stream-warmup-timeout", type=float, default=30.0)
    parser.add_argument("--post-recovery-observation", type=float, default=20.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    expected_exchange = "SMART" if args.session_label == "RTH" else "OVERNIGHT"
    if args.market_data_exchange.upper() != expected_exchange:
        parser.error(
            f"{args.session_label} requires --market-data-exchange {expected_exchange}"
        )
    report: dict[str, Any]
    try:
        report, code = run(args)
    except Exception as exc:
        report = {
            "schema_version": 1,
            "finished_utc": _now(),
            "passed": False,
            "uncaught_exception": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
        code = 1
    report["finished_utc"] = _now()
    output = Path(args.output)
    _write_report(output, report)
    print(json.dumps({"event": "FINISHED", "report": str(output), **report}, ensure_ascii=False), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
