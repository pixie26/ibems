"""Scheduled Recorder throughput/durability soak; never connects to IB."""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ib_execution.quote_recorder import RawEventLog, RawTick


def _tick(i: int, run_id: str) -> RawTick:
    stream = ("BID_ASK", "ALL_LAST", "BAR_5S")[i % 3]
    common = {
        "event_id": i + 1,
        "recorder_run_id": run_id,
        "connection_epoch": 1,
        "contract_id": 756733,
        "event_type": stream,
        "broker_timestamp": "2026-08-11T13:30:00+00:00",
        "local_wall_ns": time.time_ns(),
        "local_monotonic_ns": time.monotonic_ns(),
        "market_data_type": "LIVE",
        "receive_sequence": i + 1,
    }
    if stream == "BID_ASK":
        return RawTick(**common, bid=600.0 + (i % 10) / 100, ask=600.02 + (i % 10) / 100)
    if stream == "ALL_LAST":
        return RawTick(**common, last=600.01, last_size=1.0, exchange="ARCA")
    return RawTick(
        **common,
        open=600.0,
        high=600.02,
        low=599.99,
        close=600.01,
        volume=10.0,
        wap=600.01,
        trade_count=2,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="artifacts/recorder_soak")
    ap.add_argument("--events", type=int, default=1_000_000)
    ap.add_argument("--target-rate", type=float, default=10_000.0)
    ap.add_argument("--queue-capacity", type=int, default=100_000)
    ap.add_argument("--batch-records", type=int, default=512)
    ap.add_argument("--max-writer-lag-ms", type=float, default=5_000.0)
    ap.add_argument("--stall-budget-seconds", type=float, default=10.0)
    ns = ap.parse_args(argv)
    if ns.events <= 0 or ns.target_rate <= 0 or ns.stall_budget_seconds <= 0:
        ap.error("events, target-rate and stall-budget-seconds must be positive")

    root = Path(ns.root)
    root.mkdir(parents=True, exist_ok=True)
    log = RawEventLog(
        root,
        queue_capacity=ns.queue_capacity,
        batch_records=ns.batch_records,
    )
    started = time.monotonic()
    error: str | None = None
    try:
        for i in range(ns.events):
            log.append(_tick(i, log.run_id), now_mono=time.monotonic())
            if i and i % 1_000 == 0:
                due = started + (i + 1) / ns.target_rate
                if due > time.monotonic():
                    time.sleep(due - time.monotonic())
        log.close()
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        try:
            log.close()
        except BaseException:
            pass

    elapsed = time.monotonic() - started
    stats = log.write_stats()
    readback_count = sum(1 for _ in log.read_all())
    bytes_on_disk = sum(path.stat().st_size for path in log.segments())
    passed = bool(
        error is None
        and stats["enqueued_count"] == ns.events
        and stats["persisted_count"] == ns.events
        and readback_count == ns.events
        and stats["dropped_count"] == 0
        and stats["writer_error"] is None
        and stats["max_writer_lag_ms"] <= ns.max_writer_lag_ms
    )
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version,
        "platform": platform.platform(),
        "requested_events": ns.events,
        "target_rate_events_per_second": ns.target_rate,
        "elapsed_seconds": elapsed,
        "observed_events_per_second": stats["persisted_count"] / max(elapsed, 1e-9),
        "bytes_on_disk": bytes_on_disk,
        "readback_count": readback_count,
        "max_writer_lag_budget_ms": ns.max_writer_lag_ms,
        "stall_budget_seconds": ns.stall_budget_seconds,
        "recommended_queue_capacity": int(ns.target_rate * ns.stall_budget_seconds),
        "write_accounting": stats,
        "error": error,
        "passed": passed,
    }
    report_path = root / "soak-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
