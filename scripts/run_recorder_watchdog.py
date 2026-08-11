"""External supervisor for a QuoteRecorder event-loop heartbeat."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from ib_execution.watchdog import Watchdog, WatchdogConfig


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status-path", required=True)
    ap.add_argument("--heartbeat-timeout", type=float, default=15.0)
    ap.add_argument("--grace-before-kill", type=float, default=15.0)
    ap.add_argument("--poll-seconds", type=float, default=1.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument(
        "--observe-only",
        action="store_true",
        help="alert but never terminate the recorder; intended for validation",
    )
    ns = ap.parse_args(argv)

    def alert(severity: str, message: str) -> None:
        print(
            json.dumps(
                {"severity": severity, "message": message, "wall_time": time.time()},
                sort_keys=True,
            ),
            flush=True,
        )

    watchdog = Watchdog(
        WatchdogConfig(
            status_path=Path(ns.status_path),
            heartbeat_timeout_seconds=ns.heartbeat_timeout,
            grace_seconds_before_kill=ns.grace_before_kill,
            poll_seconds=ns.poll_seconds,
        ),
        alert=alert,
    )
    if ns.observe_only:
        watchdog.kill_engine = lambda _status: False  # type: ignore[method-assign]
    if ns.once:
        verdict = watchdog.run_once(time.monotonic())
        print(json.dumps(verdict.__dict__, sort_keys=True), flush=True)
        return 0 if verdict.healthy else 2
    watchdog.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
