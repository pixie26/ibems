"""Bounded, broker-write-free session probe through the real Recorder path.

This is deliberately not a Full-RTH health run.  It reuses QuoteRecorder's
three subscriptions, event handlers and RawEventLog, but records for a bounded
window on an explicit, mechanically validated RTH/SMART or
OVERNIGHT/OVERNIGHT route. It is never a Full-RTH health report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ib_async import IB, StartupFetchNONE, Stock

from ib_execution.quote_recorder import (
    QuoteRecorder,
    RawEventLog,
    measure_clock_skew,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    session_label = getattr(args, "session_label", "OVERNIGHT")
    market_data_exchange = getattr(args, "market_data_exchange", "OVERNIGHT")
    expected_exchange = {"OVERNIGHT": "OVERNIGHT", "RTH": "SMART"}.get(session_label)
    if expected_exchange != market_data_exchange:
        raise ValueError(
            f"session label {session_label} requires exchange {expected_exchange}"
        )
    started = datetime.now(timezone.utc)
    report: dict[str, Any] = {
        "schema_version": 1,
        "started_utc": started.isoformat(),
        "session_label": session_label,
        "market_data_exchange": market_data_exchange,
        "full_rth_health_not_run": True,
        "host": args.host,
        "port": args.port,
        "client_id": args.client_id,
        "readonly": True,
        "sample_seconds_requested": args.sample_seconds,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "broker_write_calls": [],
    }
    errors: list[dict[str, Any]] = []
    ib = IB()
    ib.RequestTimeout = args.request_timeout
    recorder = QuoteRecorder(
        args.raw_root,
        args.symbol,
        client_id=args.client_id,
        wait_for_rth=False,
        roll_seconds=max(30, int(args.sample_seconds)),
    )
    log: RawEventLog | None = None
    contract = probe = bars = None
    tickers: tuple[Any, ...] = ()
    sample_started: float | None = None
    exception: Exception | None = None

    def on_error(req_id, code, message, error_contract):
        row = {
            "req_id": req_id,
            "code": code,
            "message": message,
            "con_id": getattr(error_contract, "conId", 0) if error_contract else 0,
        }
        errors.append(row)
        recorder._append(
            "SYSTEM",
            datetime.now(timezone.utc),
            contract_id=row["con_id"],
            special_conditions=f"IB_ERROR:{code}:{req_id}:{message}",
        )
        if recorder._is_fatal_market_data_error(code, message):
            recorder._fatal_prerequisite_error = (
                f"IB market-data prerequisite failed ({code}): {message}"
            )

    ib.errorEvent += on_error
    try:
        ib.connect(
            args.host,
            args.port,
            clientId=args.client_id,
            timeout=10,
            readonly=True,
            fetchFields=StartupFetchNONE,
        )
        recorder._connection_epoch = 1
        contract = Stock(
            args.symbol,
            market_data_exchange,
            "USD",
            primaryExchange="ARCA",
        )
        qualified = ib.qualifyContracts(contract)
        if len(qualified) != 1:
            raise RuntimeError(f"could not uniquely qualify {args.symbol}: {qualified}")
        contract = qualified[0]
        server_time = ib.reqCurrentTime().astimezone(timezone.utc)
        log = RawEventLog(
            args.raw_root,
            session=server_time.date(),
            roll_seconds=max(30, int(args.sample_seconds)),
            run_id=recorder.run_id,
        )
        recorder.log = log
        recorder._clock_skew_samples.extend(measure_clock_skew(ib))
        recorder._append(
            "SYSTEM",
            server_time,
            contract_id=contract.conId,
            special_conditions=(
                f"CONNECTED;READ_ONLY=true;SESSION_LABEL={session_label};"
                f"EXCHANGE={market_data_exchange}"
            ),
        )
        probe, tickers, bars = recorder._subscribe(ib, contract)
        sample_started = time.monotonic()
        while time.monotonic() - sample_started < args.sample_seconds:
            if recorder._fatal_prerequisite_error:
                raise RuntimeError(recorder._fatal_prerequisite_error)
            if not ib.isConnected():
                raise ConnectionError(
                    f"IB disconnected during bounded {session_label} probe"
                )
            ib.sleep(0.25)
    except Exception as exc:  # report the failure as evidence, then exit fail-closed
        exception = exc
    finally:
        if ib.isConnected() and contract is not None:
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
            recorder._append(
                "SYSTEM",
                datetime.now(timezone.utc),
                contract_id=contract.conId,
                special_conditions="CONNECTION_CLOSED_INTENTIONAL",
            )
            ib.disconnect()
        if log is not None:
            log.close()

    rows = list(log.read_all()) if log is not None else []
    counts = Counter(str(row.get("event_type")) for row in rows)
    market_types = sorted({str(row.get("market_data_type")) for row in rows})
    segments = log.segments() if log is not None else []
    report.update(
        {
            "finished_utc": datetime.now(timezone.utc).isoformat(),
            "sample_seconds_observed": (
                time.monotonic() - sample_started if sample_started is not None else 0.0
            ),
            "contract": (
                {
                    "con_id": contract.conId,
                    "symbol": contract.symbol,
                    "exchange": contract.exchange,
                    "primary_exchange": contract.primaryExchange,
                    "currency": contract.currency,
                }
                if contract is not None
                else None
            ),
            "market_data_types_in_raw_log": market_types,
            # Counted inside the callback, before _append. Comparing this with
            # the readback splits "the writer dropped it" from "it never
            # arrived" -- two bounded runs recorded ~40% fewer BidAsk rows than
            # the paired preflight, and one number cannot tell those apart.
            "handler_counts": dict(sorted(recorder.handled_events.items())),
            "raw_event_count": len(rows),
            "stream_counts": {
                "bid_ask": counts["BID_ASK"],
                "all_last": counts["ALL_LAST"],
                "bars_5s": counts["BAR_5S"],
                "system": counts["SYSTEM"],
            },
            "errors": errors,
            "exception": (
                {"type": type(exception).__name__, "message": str(exception)}
                if exception is not None
                else None
            ),
            "raw_segments": [
                {
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
                for path in segments
            ],
        }
    )
    stream_counts = report["stream_counts"]
    fatal_errors = [
        error
        for error in errors
        if recorder._is_fatal_market_data_error(error["code"], error["message"])
    ]
    report["checks"] = {
        "explicit_session_route": bool(
            report["contract"]
            and report["contract"]["exchange"] == market_data_exchange
        ),
        "readonly": report["readonly"] is True,
        "live_market_data_observed": "LIVE" in market_types,
        "all_streams_written_nonzero": all(
            stream_counts[name] > 0 for name in ("bid_ask", "all_last", "bars_5s")
        ),
        "raw_segment_present": bool(segments),
        # A callback exception is caught by eventkit and logged, never raised,
        # so `no_exception` alone cannot see a truncated tick buffer. The
        # recorder now records it as a fatal prerequisite instead.
        "no_swallowed_callback_failure": recorder._fatal_prerequisite_error is None,
        "write_path_lost_nothing": all(
            report["handler_counts"].get(event_type, 0) == written
            for event_type, written in (
                ("BID_ASK", stream_counts["bid_ask"]),
                ("ALL_LAST", stream_counts["all_last"]),
                ("BAR_5S", stream_counts["bars_5s"]),
            )
        ),
        "no_exception": exception is None,
        "no_fatal_market_data_error": not fatal_errors,
        "sample_window_complete": report["sample_seconds_observed"] >= args.sample_seconds,
        "no_broker_write_calls": report["broker_write_calls"] == [],
    }
    report["passed"] = all(report["checks"].values())
    return report, 0 if report["passed"] else 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bounded read-only session trial through QuoteRecorder"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id", type=int, default=945)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--session-label", choices=("RTH", "OVERNIGHT"), default="OVERNIGHT")
    parser.add_argument("--market-data-exchange", default=None)
    parser.add_argument("--sample-seconds", type=float, default=120.0)
    parser.add_argument("--request-timeout", type=float, default=10.0)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    expected_exchange = "SMART" if args.session_label == "RTH" else "OVERNIGHT"
    if args.market_data_exchange is None:
        args.market_data_exchange = expected_exchange
    elif args.market_data_exchange.upper() != expected_exchange:
        parser.error(
            f"{args.session_label} requires --market-data-exchange {expected_exchange}"
        )
    else:
        args.market_data_exchange = args.market_data_exchange.upper()
    if args.sample_seconds <= 0:
        parser.error("--sample-seconds must be greater than zero")
    if args.request_timeout <= 0:
        parser.error("--request-timeout must be greater than zero")
    if args.output.exists():
        parser.error(f"refusing to overwrite existing report: {args.output}")

    report, exit_code = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps({"report": str(args.output), **report}, indent=2, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
