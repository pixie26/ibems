#!/usr/bin/env python3
"""
Runnable demonstration. No IB required.

    python scripts/demo.py

Walks a target through a normal round trip, then through the failure that IB
paper cannot reproduce -- a fill landing in the window between our cancel and
its acknowledgement -- and shows the system refusing to guess.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ib_execution import (  # noqa: E402
    Controller,
    ExecutionPolicy,
    FakeBroker,
    Faults,
    Journal,
    ManualClock,
    Quote,
    RiskConfig,
    RiskEngine,
    TargetPosition,
    TradingCalendar,
)
from ib_execution.auditor import JournalAuditor  # noqa: E402
from ib_execution.models import EventType  # noqa: E402


def banner(text: str) -> None:
    print(f"\n{'=' * 66}\n{text}\n{'=' * 66}")


def show(ctl, broker, label: str) -> None:
    leg = ctl.leg("manual_test", "SPY")
    print(
        f"  {label:<26} order_state={leg.order_state.value:<22} "
        f"sync={ctl.sync_state.value:<10} pos={leg.position:+d} "
        f"orders_sent={len(broker.place_calls)}"
    )


def build(tmp: Path, faults: Faults):
    clock = ManualClock(datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc))
    journal = Journal(tmp / "demo.db", clock=clock)
    broker = FakeBroker(clock, faults)
    ctl = Controller(
        journal=journal,
        broker=broker,
        risk=RiskEngine(RiskConfig(strategy_whitelist=("manual_test",)), clock),
        clock=clock,
        calendar=TradingCalendar(),
        policy=ExecutionPolicy(),
    )
    journal.commit(EventType.PROCESS_STARTED, {})
    ctl.on_connected(1)
    ctl.on_quote(Quote("SPY", Decimal("599.98"), Decimal("600.02"), 500, 500, clock.now()))
    ctl.reconcile()
    return clock, journal, broker, ctl


def tgt(clock, qty: int, n: int) -> TargetPosition:
    return TargetPosition(
        strategy_id="manual_test",
        symbol="SPY",
        target_quantity=qty,
        decision_id=f"demo-{n:03d}",
        valid_until=clock.now() + timedelta(minutes=5),
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp())

    banner("1. Normal round trip:  0 -> +3 -> -3")
    clock, journal, broker, ctl = build(tmp, Faults())
    show(ctl, broker, "start")

    ctl.submit_target(tgt(clock, 3, 1))
    broker.pump()
    show(ctl, broker, "after target +3")

    ctl.submit_target(tgt(clock, -3, 2))
    broker.pump()
    show(ctl, broker, "after target -3")
    sells = [c for c in broker.place_calls if c.side.value == "SELL"]
    print(f"\n  Reversal sent SELL {sells[-1].quantity if sells else '<none>'} "
          f"(must be 6, not 3 -- the classic flip bug)")
    print("  Note: this requires max_order_shares >= 2 x max_position_shares.")
    print("  Config validation now rejects anything less; the demo found it.")
    journal.close()

    banner("2. Fill lands while our cancel is in flight")
    print("  IB paper cannot reproduce this. It is the failure that matters.\n")
    tmp2 = Path(tempfile.mkdtemp())
    clock, journal, broker, ctl = build(tmp2, Faults(no_fill=True))

    ctl.submit_target(tgt(clock, 3, 1))
    broker.pump()
    show(ctl, broker, "order working, unfilled")

    broker.faults.fill_before_cancel = True
    broker.faults.no_fill = False
    ctl.submit_target(tgt(clock, -3, 2))
    broker.pump()
    show(ctl, broker, "after the race")

    sells = [c for c in broker.place_calls if c.side.value == "SELL"]
    print(f"\n  SELL orders issued: {len(sells)}")
    print("  The naive implementation computes SELL 6 when it sends the cancel,")
    print("  then discovers the position was already +3. This one stops instead:")
    print("  TERMINAL_UNRECONCILED, sync=UNVERIFIED, no order until it re-reads")
    print("  the broker. Safety over liveness.")

    banner("3. Independent audit of the log")
    findings = JournalAuditor(journal.replay()).audit()
    summary = JournalAuditor(journal.replay()).summary()
    print(f"  events written: {summary['events']}")
    if summary["decision_misses"]:
        print(f"  decision misses (cost model availability term): "
              f"{summary['decision_misses']}")
    print(f"  invariant violations: {len(findings)}")
    for f in findings:
        print(f"    {f}")
    journal.close()

    print("\n  The auditor independently checks the journal-observable invariants")
    print("  currently implemented. Coverage is partial; Gate B1 remains blocked")
    print("  until all 22 rows in docs/INVARIANT_COVERAGE.md are complete.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
