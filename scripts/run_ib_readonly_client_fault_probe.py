"""Read-only B2 probe for concurrent clients and abrupt client death.

This file contains no order placement, cancellation, or completed-order request.
It reads only positions, all open orders, and executions, and persists only
counts and canonical hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ib_async import IB, StartupFetchNONE


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _snapshot(ib: IB) -> dict[str, Any]:
    positions = sorted(
        (
            position.account,
            position.contract.conId,
            str(position.position),
            position.avgCost,
        )
        for position in ib.reqPositions()
    )
    orders = sorted(
        (
            trade.order.account,
            trade.contract.conId,
            trade.order.orderId,
            trade.order.permId,
            trade.order.clientId,
            trade.order.orderRef,
            trade.orderStatus.status,
        )
        for trade in ib.reqAllOpenOrders()
    )
    executions = sorted(
        (
            fill.execution.acctNumber,
            fill.contract.conId,
            fill.execution.execId,
            fill.execution.orderId,
            fill.execution.permId,
            fill.execution.clientId,
        )
        for fill in ib.reqExecutions()
    )
    components = {
        "positions": positions,
        "open_orders": orders,
        "executions": executions,
    }
    return {
        "counts": {name: len(rows) for name, rows in components.items()},
        "hashes": {name: _digest(rows) for name, rows in components.items()},
        "snapshot_hash": _digest(components),
    }


def _connect(host: str, port: int, client_id: int, timeout: float) -> IB:
    ib = IB()
    ib.RequestTimeout = timeout
    ib.connect(
        host,
        port,
        clientId=client_id,
        timeout=timeout,
        readonly=True,
        fetchFields=StartupFetchNONE,
    )
    return ib


def _child(args: argparse.Namespace) -> int:
    ib = _connect(args.host, args.port, args.child_client_id, args.request_timeout)
    print(json.dumps({"ready": True, "utc": _utc_now()}), flush=True)
    try:
        while True:
            ib.sleep(0.25)
    finally:
        if ib.isConnected():
            ib.disconnect()


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    report: dict[str, Any] = {
        "schema_version": 1,
        "started_utc": _utc_now(),
        "host": args.host,
        "port": args.port,
        "readonly": True,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "client_ids": {
            "concurrent_a": args.client_id_a,
            "concurrent_b": args.client_id_b,
            "abrupt_child": args.child_client_id,
            "collision": args.collision_client_id,
        },
        "timeline": [],
    }
    clients: list[IB] = []
    child: subprocess.Popen[str] | None = None
    try:
        a = _connect(args.host, args.port, args.client_id_a, args.request_timeout)
        clients.append(a)
        report["timeline"].append({"utc": _utc_now(), "event": "client_a_connected"})
        b = _connect(args.host, args.port, args.client_id_b, args.request_timeout)
        clients.append(b)
        report["timeline"].append({"utc": _utc_now(), "event": "client_b_connected"})
        snap_a = _snapshot(a)
        snap_b = _snapshot(b)
        report["concurrent_clients"] = {
            "both_connected": a.isConnected() and b.isConnected(),
            "snapshot_a": snap_a,
            "snapshot_b": snap_b,
            "snapshots_equal": snap_a["snapshot_hash"] == snap_b["snapshot_hash"],
        }
        b.disconnect()
        a.disconnect()
        clients.clear()

        collision_errors: list[dict[str, Any]] = []
        first = _connect(args.host, args.port, args.collision_client_id, args.request_timeout)
        clients.append(first)
        second = IB()
        second.RequestTimeout = args.request_timeout
        second.errorEvent += lambda req_id, code, message, contract: collision_errors.append(
            {"req_id": req_id, "code": code, "message": message}
        )
        second_exception = None
        try:
            second.connect(
                args.host,
                args.port,
                clientId=args.collision_client_id,
                timeout=args.request_timeout,
                readonly=True,
                fetchFields=StartupFetchNONE,
            )
        except Exception as exc:
            second_exception = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
        ib_sleep = first if first.isConnected() else second
        if ib_sleep.isConnected():
            ib_sleep.sleep(0.5)
        collision_state = {
            "first_connected": first.isConnected(),
            "second_connected": second.isConnected(),
            "both_connected": first.isConnected() and second.isConnected(),
            "second_exception": second_exception,
            "errors": collision_errors,
        }
        report["same_client_id_collision"] = collision_state
        if second.isConnected():
            second.disconnect()
        if first.isConnected():
            first.disconnect()
        clients.clear()

        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--child",
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--child-client-id",
            str(args.child_client_id),
            "--request-timeout",
            str(args.request_timeout),
        ]
        child = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        ready_line = child.stdout.readline() if child.stdout else ""
        ready = json.loads(ready_line)
        report["timeline"].append({"utc": _utc_now(), "event": "child_connected"})
        child.terminate()
        child.wait(timeout=args.request_timeout)
        report["timeline"].append(
            {
                "utc": _utc_now(),
                "event": "child_terminated_without_disconnect",
                "returncode": child.returncode,
            }
        )

        attempts = []
        recovered = None
        deadline = time.monotonic() + args.reconnect_deadline
        while time.monotonic() < deadline:
            started = time.monotonic()
            try:
                recovered = _connect(
                    args.host, args.port, args.child_client_id, args.request_timeout
                )
            except Exception as exc:
                attempts.append(
                    {
                        "utc": _utc_now(),
                        "success": False,
                        "duration_seconds": time.monotonic() - started,
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                    }
                )
                time.sleep(0.5)
            else:
                attempts.append(
                    {
                        "utc": _utc_now(),
                        "success": True,
                        "duration_seconds": time.monotonic() - started,
                    }
                )
                break
        if recovered is not None:
            clients.append(recovered)
            recovered_snapshot = _snapshot(recovered)
            recovered.disconnect()
            clients.clear()
        else:
            recovered_snapshot = None
        report["abrupt_client_death"] = {
            "child_ready": bool(ready.get("ready")),
            "termination_returncode": child.returncode,
            "reconnect_attempts": attempts,
            "reconnected_same_client_id": recovered is not None,
            "recovered_snapshot": recovered_snapshot,
        }
        report["checks"] = {
            "two_readonly_clients_connected": report["concurrent_clients"]["both_connected"],
            "concurrent_snapshots_equal": report["concurrent_clients"]["snapshots_equal"],
            "same_client_id_not_simultaneously_connected": not collision_state["both_connected"],
            "same_client_id_recovered_after_abrupt_death": recovered is not None,
        }
        report["passed"] = all(report["checks"].values())
        return report, 0 if report["passed"] else 2
    finally:
        for ib in clients:
            if ib.isConnected():
                ib.disconnect()
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=args.request_timeout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id-a", type=int, default=934)
    parser.add_argument("--client-id-b", type=int, default=935)
    parser.add_argument("--child-client-id", type=int, default=936)
    parser.add_argument("--collision-client-id", type=int, default=937)
    parser.add_argument("--request-timeout", type=float, default=10.0)
    parser.add_argument("--reconnect-deadline", type=float, default=10.0)
    parser.add_argument("--output")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        return _child(args)
    report, code = run(args)
    report["finished_utc"] = _utc_now()
    output = Path(args.output) if args.output else Path("artifacts/ib_preflight/client_fault.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"report": str(output), **report}, indent=2, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
