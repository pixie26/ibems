"""Auditable, broker-write-free IB Gateway preflight.

This script deliberately has no order placement/cancellation path.  It verifies
the API handshake, reads three account-truth streams repeatedly, and probes the
SPY market-data entitlement.  Raw account identifiers and positions never enter
the report; only counts and canonical hashes are persisted.
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


ENTITLEMENT_CODES = {354, 10089, 10189}


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _snapshot(ib: IB, index: int) -> dict[str, Any]:
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
        [
            {
                "account": trade.order.account,
                "con_id": trade.contract.conId,
                "order_id": trade.order.orderId,
                "perm_id": trade.order.permId,
                "client_id": trade.order.clientId,
                "action": trade.order.action,
                "quantity": str(trade.order.totalQuantity),
                "order_type": trade.order.orderType,
                "limit_price": trade.order.lmtPrice,
                "aux_price": trade.order.auxPrice,
                "status": trade.orderStatus.status,
                "filled": str(trade.orderStatus.filled),
                "remaining": str(trade.orderStatus.remaining),
            }
            for trade in ib.reqAllOpenOrders()
        ],
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
    components = {
        "positions": positions,
        "open_orders": orders,
        "executions": executions,
    }
    return {
        "index": index,
        "started_utc": started.isoformat(),
        "ended_utc": ended.isoformat(),
        "duration_seconds": (ended - started).total_seconds(),
        "counts": {name: len(rows) for name, rows in components.items()},
        "hashes": {name: _digest(rows) for name, rows in components.items()},
        "snapshot_hash": _digest(components),
    }


def _fatal_entitlement_error(error: dict[str, Any]) -> bool:
    if error["code"] in ENTITLEMENT_CODES:
        return True
    return error["code"] == 420 and "market data permissions" in error["message"].casefold()


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    ib = IB()
    errors: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "schema_version": 1,
        "started_utc": datetime.now(timezone.utc).isoformat(),
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
        report["connection"] = {
            "connected": ib.isConnected(),
            "server_version": ib.client.serverVersion(),
            "server_time_utc": server_time.isoformat(),
            "clock_skew_seconds": (
                datetime.now(timezone.utc) - server_time
            ).total_seconds(),
            "account_count": len(ib.managedAccounts()),
        }

        contract = Stock(args.symbol, "SMART", "USD", primaryExchange="ARCA")
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
            "details_count": len(details),
        }

        rounds = []
        for index in range(1, args.snapshot_rounds + 1):
            rounds.append(_snapshot(ib, index))
            if index < args.snapshot_rounds:
                ib.sleep(args.snapshot_interval)
        pairs = [
            {
                "left": left["index"],
                "right": right["index"],
                "equal": left["snapshot_hash"] == right["snapshot_hash"],
                "component_equal": {
                    name: left["hashes"][name] == right["hashes"][name]
                    for name in left["hashes"]
                },
            }
            for left, right in zip(rounds, rounds[1:])
        ]
        stable = any(pair["equal"] for pair in pairs)
        report["stable_snapshot"] = {
            "protocol": "positions -> all-open-orders -> executions; require two consecutive equal canonical snapshots",
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
        sample_counts = {"bid_ask": 0, "all_last": 0, "bars_5s": 0}
        if market_type == 1:
            bidask = ib.reqTickByTickData(contract, "BidAsk", 0, False)
            alllast = ib.reqTickByTickData(contract, "AllLast", 0, False)
            bars = ib.reqRealTimeBars(contract, 5, "TRADES", True)
            started = time.monotonic()
            while time.monotonic() - started < args.sample_seconds:
                ib.sleep(0.25)
            unique_ticks = {
                id(tick): tick for tick in [*bidask.tickByTicks, *alllast.tickByTicks]
            }.values()
            sample_counts = {
                "bid_ask": sum(isinstance(tick, TickByTickBidAsk) for tick in unique_ticks),
                "all_last": sum(isinstance(tick, TickByTickAllLast) for tick in unique_ticks),
                "bars_5s": len(bars),
            }
        report["market_data"] = {
            "market_data_type": market_type,
            "live": market_type == 1,
            "sample_seconds": args.sample_seconds if market_type == 1 else 0,
            "sample_counts": sample_counts,
            "entitlement_blocked": any(_fatal_entitlement_error(error) for error in errors),
        }
        report["errors"] = errors
        report["passed"] = stable and market_type == 1 and all(sample_counts.values())
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
    parser.add_argument("--snapshot-rounds", type=int, default=3)
    parser.add_argument("--snapshot-interval", type=float, default=1.0)
    parser.add_argument("--market-data-timeout", type=float, default=10.0)
    parser.add_argument("--sample-seconds", type=float, default=20.0)
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.snapshot_rounds < 2:
        parser.error("--snapshot-rounds must be at least 2")

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
