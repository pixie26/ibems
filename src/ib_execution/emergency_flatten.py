"""
Emergency flatten -- human-triggered only.

    ####################################################################
    #  STATUS: the safety scaffolding (confirmation, plan, journal) is  #
    #  implemented and tested. The IB calls are UNVERIFIED.            #
    ####################################################################

Runs as its own process with its own clientId, so it works when the engine is
dead, wedged, or has been killed by the watchdog. That is the entire point.

It is human-triggered by design. See watchdog.py for why automatic takeover is
not in V1.

MONTHLY DRILL IS MANDATORY. A recovery tool that has never been run is not a
recovery tool, it is a file. Put the drill in the runbook and do it on paper on
the first trading day of each month. The failure mode it protects against --
"the tool exists but nobody has ever used it and it does not work" -- has taken
down desks that had every other control in place.

ORDER OF OPERATIONS (never varies):
    1. confirm live vs paper OUT LOUD
    2. connect on the dedicated clientId
    3. pull positions AND open orders from the broker
    4. cancel our open orders
    5. WAIT for terminal state on each
    6. re-pull positions (a cancel may have raced a fill)
    7. only then send closing orders
    8. verify flat
    9. write the whole thing to the journal

Step 6 is the one people skip, and it is the one that produces a double-sized
opposite position.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .journal import Journal
from .models import EventType, FlattenReason, Side


@dataclass
class FlattenPlan:
    """Computed from broker truth, never from the engine's memory."""

    account: str
    symbol: str
    broker_position: int
    open_order_refs: list[str] = field(default_factory=list)
    is_live: bool = False

    @property
    def closing_side(self) -> Optional[Side]:
        if self.broker_position > 0:
            return Side.SELL
        if self.broker_position < 0:
            return Side.BUY
        return None

    @property
    def closing_quantity(self) -> int:
        return abs(self.broker_position)

    def describe(self) -> str:
        env = "*** LIVE ***" if self.is_live else "paper"
        if self.closing_side is None:
            return f"[{env}] {self.account} {self.symbol}: already flat"
        return (
            f"[{env}] {self.account} {self.symbol}: position {self.broker_position:+d}, "
            f"{len(self.open_order_refs)} open order(s) to cancel, then "
            f"{self.closing_side.value} {self.closing_quantity}"
        )


def confirm(plan: FlattenPlan, stream=None, out=None) -> bool:
    """
    Explicit typed confirmation. 'y' is not enough for a live account.

    Deliberately annoying. The cost of a slow flatten is bounded by invariant 19;
    the cost of flattening the wrong account is not.
    """
    stream = stream or sys.stdin
    out = out or sys.stdout
    print(plan.describe(), file=out)
    token = "FLATTEN LIVE" if plan.is_live else "FLATTEN"
    print(f"Type exactly: {token}", file=out)
    return stream.readline().strip() == token


def build_plan_from_snapshot(snapshot, symbol: str, account: str, is_live: bool) -> FlattenPlan:
    return FlattenPlan(
        account=account,
        symbol=symbol,
        broker_position=snapshot.positions.get(symbol, 0),
        open_order_refs=[o.order_ref for o in snapshot.open_orders if o.symbol == symbol],
        is_live=is_live,
    )


def journal_attempt(journal: Journal, plan: FlattenPlan, stage: str, detail: dict) -> None:
    journal.commit(
        EventType.EOD_FLATTEN_STARTED if stage == "start" else EventType.OPERATING_MODE_CHANGED,
        {
            "tool": "emergency_flatten",
            "stage": stage,
            "reason": FlattenReason.MANUAL_FLATTEN.value,
            "account": plan.account,
            "position": plan.broker_position,
            **detail,
        },
        symbol=plan.symbol,
    )


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    ap = argparse.ArgumentParser(description="Emergency flatten (human-triggered)")
    ap.add_argument("--account", required=True)
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--journal", required=True)
    ap.add_argument("--port", type=int, default=4002, help="4002 paper, 4001 live")
    ap.add_argument("--live", action="store_true", help="required for a live account")
    ap.add_argument("--drill", action="store_true", help="monthly drill: plan only, no orders")
    args = ap.parse_args(argv)

    raise NotImplementedError(
        "Gate B2. Requires IbAdapter with CLIENT_ID_EMERGENCY. "
        "The plan/confirm/journal scaffolding above is implemented and tested; "
        "only the broker calls are outstanding. Until then, flatten manually in "
        "TWS and record it in the journal by hand."
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
