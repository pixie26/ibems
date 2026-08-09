"""Observe a manual IB Gateway restart without any broker-write path."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ib_async import IB, StartupFetchNONE


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


def _attach_events(ib: IB, timeline: list[dict[str, Any]], label: str) -> None:
    ib.errorEvent += lambda req_id, code, message, contract: timeline.append(
        {
            "utc": _now(),
            "source": label,
            "event": "error",
            "req_id": req_id,
            "code": code,
            "message": message,
        }
    )
    ib.disconnectedEvent += lambda: timeline.append(
        {"utc": _now(), "source": label, "event": "disconnected"}
    )


def _connect(args: argparse.Namespace, timeline: list[dict[str, Any]], label: str) -> IB:
    ib = IB()
    ib.RequestTimeout = args.request_timeout
    _attach_events(ib, timeline, label)
    ib.connect(
        args.host,
        args.port,
        clientId=args.client_id,
        timeout=args.request_timeout,
        readonly=True,
        fetchFields=StartupFetchNONE,
    )
    timeline.append({"utc": _now(), "source": label, "event": "connected"})
    return ib


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    timeline: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "schema_version": 2,
        "started_utc": _now(),
        "host": args.host,
        "port": args.port,
        "client_id": args.client_id,
        "readonly": True,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "timeline": timeline,
    }
    before: IB | None = None
    after: IB | None = None
    try:
        before = _connect(args, timeline, "before_restart")
        report["before_snapshot"] = _snapshot(before)
        report["ready_utc"] = _now()
        print(json.dumps({"event": "READY_CLOSE_GATEWAY", "utc": report["ready_utc"]}), flush=True)

        deadline = time.monotonic() + args.disconnect_timeout
        while before.isConnected() and time.monotonic() < deadline:
            try:
                before.sleep(0.1)
            except ConnectionError as exc:
                timeline.append(
                    {
                        "utc": _now(),
                        "source": "before_restart",
                        "event": "socket_disconnect_exception",
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                    }
                )
                break
        report["disconnect_observed"] = not before.isConnected()
        if before.isConnected():
            report["failure"] = "Gateway disconnect was not observed before deadline"
            return report, 2

        deadline = time.monotonic() + args.restart_timeout
        attempts: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            if not _port_open(args.host, args.port):
                time.sleep(0.5)
                continue
            started = time.monotonic()
            candidate: IB | None = None
            try:
                candidate = _connect(args, timeline, "after_restart")
                candidate_snapshot = _snapshot(candidate)
            except Exception as exc:
                attempts.append(
                    {
                        "utc": _now(),
                        "success": False,
                        "duration_seconds": time.monotonic() - started,
                        "stage": "connect_or_broker_snapshot",
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                    }
                )
                if candidate is not None and candidate.isConnected():
                    timeline.append(
                        {
                            "utc": _now(),
                            "source": "after_restart",
                            "event": "failed_attempt_cleanup_disconnect",
                        }
                    )
                    candidate.disconnect()
                time.sleep(0.5)
            else:
                after = candidate
                report["after_snapshot"] = candidate_snapshot
                attempts.append(
                    {
                        "utc": _now(),
                        "success": True,
                        "duration_seconds": time.monotonic() - started,
                        "stage": "connect_and_broker_snapshot",
                    }
                )
                break
        report["reconnect_attempts"] = attempts
        report["reconnected"] = after is not None
        if after is None:
            report["failure"] = "Gateway did not become API-ready before deadline"
            return report, 2
        report["checks"] = {
            "disconnect_observed": report["disconnect_observed"],
            "reconnected_same_client_id": report["reconnected"],
            "snapshot_equal": (
                report["before_snapshot"]["snapshot_hash"]
                == report["after_snapshot"]["snapshot_hash"]
            ),
        }
        report["passed"] = all(report["checks"].values())
        return report, 0 if report["passed"] else 2
    finally:
        if after is not None and after.isConnected():
            timeline.append({"utc": _now(), "source": "after_restart", "event": "intentional_cleanup_disconnect"})
            after.disconnect()
        if before is not None and before.isConnected():
            timeline.append({"utc": _now(), "source": "before_restart", "event": "intentional_cleanup_disconnect"})
            before.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id", type=int, default=938)
    parser.add_argument("--request-timeout", type=float, default=10.0)
    parser.add_argument("--disconnect-timeout", type=float, default=180.0)
    parser.add_argument("--restart-timeout", type=float, default=300.0)
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
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"event": "FINISHED", "report": str(output), **report}, ensure_ascii=False), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
