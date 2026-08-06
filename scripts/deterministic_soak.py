"""Dependency-free randomized lifecycle soak test.

Hypothesis remains the stronger Gate B1 tool. This script exists so a review
machine without the dev dependency can still exercise restart, reconnect,
callback ordering, partial fills and target churn reproducibly.

Usage:
    python scripts/deterministic_soak.py --seeds 20 --actions 50
"""
from __future__ import annotations

import argparse
import random
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from ib_execution.ack_halt import find_unacknowledged_halt
from ib_execution.auditor import JournalAuditor
from ib_execution.calendar import TradingCalendar
from ib_execution.clock import ManualClock
from ib_execution.controller import Controller, ExecutionPolicy
from ib_execution.fake_broker import FakeBroker, Faults
from ib_execution.journal import Journal
from ib_execution.models import EventType, Quote, TargetPosition
from ib_execution.risk import RiskConfig, RiskEngine

START = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)
ACTIONS = (
    ["target"] * 5
    + ["pump"] * 5
    + ["quote"] * 3
    + ["tick"] * 3
    + ["advance"] * 2
    + ["reconcile"] * 2
    + ["disconnect", "reconnect"]
    + ["restart"] * 2
    + ["halt", "ack_halt"]
)


def quote(clock: ManualClock) -> Quote:
    return Quote(
        "SPY",
        Decimal("599.98"),
        Decimal("600.02"),
        500,
        500,
        clock.now(),
    )


def build_controller(
    journal: Journal,
    broker: FakeBroker,
    clock: ManualClock,
    config: RiskConfig,
) -> Controller:
    controller = Controller(
        journal=journal,
        broker=broker,
        risk=RiskEngine(config, clock),
        clock=clock,
        calendar=TradingCalendar(),
        policy=ExecutionPolicy(),
    )
    journal.commit(EventType.PROCESS_STARTED, {})
    controller.restore_from_journal()
    if broker.is_connected():
        controller.on_connected(broker.connection_epoch)
        controller.on_quote(quote(clock))
        controller.reconcile()
    return controller


def run_seed(seed: int, action_count: int) -> None:
    rng = random.Random(seed)
    clock = ManualClock(START)
    faults = Faults(
        duplicate_callbacks=rng.choice([False, False, True]),
        reorder_callbacks=rng.choice([False, False, True]),
        partial_fill_qty=rng.choice([None, None, 2]),
        cancel_rejects=rng.choice([False, False, True]),
        no_fill=rng.choice([False, True]),
    )
    broker = FakeBroker(clock, faults)
    config = RiskConfig(
        strategy_whitelist=("manual_test",),
        max_position_shares=5,
        max_order_shares=10,
        max_daily_shares=500,
        max_daily_notional=Decimal("1000000"),
        max_orders_per_day=200,
        max_orders_per_minute=10,
    )

    with tempfile.TemporaryDirectory(prefix=f"ib-soak-{seed}-") as tmp:
        journal = Journal(Path(tmp) / "journal.db", clock=clock)
        try:
            controller = build_controller(journal, broker, clock, config)
            for index in range(action_count):
                action = rng.choice(ACTIONS)
                if action == "target":
                    controller.submit_target(
                        TargetPosition(
                            strategy_id="manual_test",
                            symbol="SPY",
                            target_quantity=rng.randint(-5, 5),
                            decision_id=f"seed-{seed}-decision-{index}",
                            valid_until=clock.now()
                            + timedelta(seconds=rng.randint(20, 180)),
                        )
                    )
                elif action == "pump":
                    broker.pump()
                elif action == "quote":
                    controller.on_quote(quote(clock))
                elif action == "tick":
                    controller.tick()
                elif action == "advance":
                    clock.advance(rng.randint(1, 12))
                elif action == "disconnect":
                    broker.disconnect("deterministic-soak")
                elif action == "reconnect" and not broker.is_connected():
                    broker.reconnect()
                    controller.on_market_data_restored()
                    controller.on_quote(quote(clock))
                elif action == "reconcile" and broker.is_connected():
                    controller.reconcile()
                elif action == "halt":
                    controller.halt(f"deterministic-soak seed={seed} step={index}")
                elif action == "ack_halt":
                    halt = find_unacknowledged_halt(journal)
                    if halt is not None:
                        journal.acknowledge_halt(
                            halt["seq"],
                            "deterministic-soak",
                            f"synthetic resolution seed={seed} step={index}",
                        )
                        controller = build_controller(journal, broker, clock, config)
                elif action == "restart":
                    controller = build_controller(journal, broker, clock, config)

                if broker.is_connected():
                    live = [
                        order
                        for order in broker.snapshot().open_orders
                        if order.status in ("PreSubmitted", "Submitted")
                    ]
                    if len(live) > 1:
                        raise AssertionError(f"seed {seed}: {len(live)} live orders")
                for leg in controller.legs.values():
                    if abs(leg.position) > config.max_position_shares:
                        raise AssertionError(
                            f"seed {seed}: position {leg.position} exceeds "
                            f"limit {config.max_position_shares}"
                        )

            broker.pump()
            if controller.violations:
                raise AssertionError(
                    f"seed {seed}: runtime invariant violations {controller.violations}"
                )
            findings = JournalAuditor(
                journal.replay(),
                max_orders_per_minute=config.max_orders_per_minute,
                max_orders_per_day=config.max_orders_per_day,
                max_daily_shares=config.max_daily_shares,
                max_daily_notional=config.max_daily_notional,
            ).audit()
            if findings:
                details = "\n".join(str(finding) for finding in findings[:20])
                raise AssertionError(f"seed {seed}: auditor findings\n{details}")
        finally:
            journal.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=20)
    parser.add_argument("--actions", type=int, default=50)
    args = parser.parse_args()
    if args.seeds <= 0 or args.actions <= 0:
        parser.error("--seeds and --actions must be positive")

    for seed in range(args.seeds):
        run_seed(seed, args.actions)
    print(f"PASS: {args.seeds} seeds x {args.actions} actions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
