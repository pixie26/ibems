"""Bounded, broker-write-free RTH probe through the real Recorder path."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from run_ib_readonly_overnight_recorder_probe import run


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bounded read-only RTH trial through QuoteRecorder"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id", type=int, default=949)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--sample-seconds", type=float, default=120.0)
    parser.add_argument("--request-timeout", type=float, default=10.0)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.sample_seconds <= 0:
        parser.error("--sample-seconds must be greater than zero")
    if args.request_timeout <= 0:
        parser.error("--request-timeout must be greater than zero")
    if args.output.exists():
        parser.error(f"refusing to overwrite existing report: {args.output}")

    args.session_label = "RTH"
    args.market_data_exchange = "SMART"
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
