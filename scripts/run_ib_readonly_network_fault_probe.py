"""Observe an IB-server network fault while the local Gateway stays alive.

This probe has no broker-write path. It connects with ``readonly=True`` and
reads only server time, positions, all open orders, and executions. A recovery
is accepted only after a real 1101/1102 callback and a fresh post-recovery
server-time plus broker-state snapshot.
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

from ib_async import IB, StartupFetchNONE


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


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    timeline: list[dict[str, Any]] = []
    error_codes: list[int] = []
    disconnected = False
    report: dict[str, Any] = {
        "schema_version": 1,
        "started_utc": _now(),
        "host": args.host,
        "port": args.port,
        "client_id": args.client_id,
        "readonly": True,
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
        report["ready_utc"] = _now()
        _write_report(Path(args.output), report)
        print(json.dumps({"event": "READY_APPLY_FIREWALL", "utc": report["ready_utc"]}), flush=True)

        deadline = time.monotonic() + args.observation_timeout
        recovery_seen_at: float | None = None
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
            if recovery_seen_at is not None and time.monotonic() - recovery_seen_at >= args.recovery_settle:
                break

        report["observed_codes"] = error_codes
        report["checks"] = {
            "local_api_socket_remained_connected": ib.isConnected() and not disconnected,
            "observed_1100": 1100 in error_codes,
            "observed_1101_or_1102": any(code in RECOVERY_CODES for code in error_codes),
        }
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
            timeline.append({"utc": _now(), "event": "intentional_cleanup_disconnect"})
            ib.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id", type=int, default=942)
    parser.add_argument("--request-timeout", type=float, default=10.0)
    parser.add_argument("--observation-timeout", type=float, default=240.0)
    parser.add_argument("--recovery-settle", type=float, default=3.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
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
