"""Subprocess worker used by Gate B1 force-kill crash-window tests."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from ib_execution.calendar import TradingCalendar
from ib_execution.clock import ManualClock
from ib_execution.controller import Controller, ExecutionPolicy
from ib_execution.journal import Journal
from ib_execution.models import (
    BrokerOrder,
    BrokerSnapshot,
    EventType,
    Execution,
    Quote,
    Side,
    TargetPosition,
)
from ib_execution.risk import RiskConfig, RiskEngine
from conftest import SESSION_START


def _durable_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def checkpoint(path: Path, label: str) -> None:
    _durable_json(path, {"label": label, "pid": os.getpid()})
    while True:
        time.sleep(1)


class DurableBroker:
    """Tiny broker whose truth survives when the controller process is killed."""

    def __init__(self, path: Path, clock):
        self.path = path
        self.clock = clock
        self._cb = None
        self.block_place_at: Path | None = None
        self.block_cancel_at: Path | None = None
        self.block_snapshot_at: Path | None = None
        if not path.exists():
            _durable_json(
                path,
                {"positions": {}, "orders": [], "executions": [], "place_calls": 0,
                 "cancel_calls": 0},
            )

    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, state: dict) -> None:
        _durable_json(self.path, state)

    def register(self, callbacks) -> None:
        self._cb = callbacks

    def is_connected(self) -> bool:
        return True

    def place_order(self, intent) -> int:
        state = self._read()
        state["place_calls"] += 1
        order_id = state["place_calls"]
        state["orders"].append(
            {
                "order_ref": intent.order_ref,
                "perm_id": 700_000 + order_id,
                "broker_order_id": order_id,
                "symbol": intent.symbol,
                "side": intent.side.value,
                "total_quantity": intent.quantity,
                "filled_quantity": 0,
                "status": "Submitted",
            }
        )
        self._write(state)
        if self.block_place_at is not None:
            checkpoint(self.block_place_at, "after_place_before_ack")
        return order_id

    def cancel_order(self, order_ref: str) -> None:
        state = self._read()
        state["cancel_calls"] += 1
        state["orders"] = [o for o in state["orders"] if o["order_ref"] != order_ref]
        self._write(state)
        if self.block_cancel_at is not None:
            checkpoint(self.block_cancel_at, "after_cancel_request")

    def apply_partial(self, order_ref: str, quantity: int = 1) -> Execution:
        state = self._read()
        order = next(o for o in state["orders"] if o["order_ref"] == order_ref)
        order["filled_quantity"] += quantity
        signed = quantity if order["side"] == "BUY" else -quantity
        state["positions"][order["symbol"]] = state["positions"].get(order["symbol"], 0) + signed
        raw = {
            "exec_id": "CRASH-EXEC-1",
            "order_ref": order_ref,
            "perm_id": order["perm_id"],
            "symbol": order["symbol"],
            "side": order["side"],
            "quantity": quantity,
            "price": "600.00",
            "ts": self.clock.now().isoformat(),
        }
        state["executions"].append(raw)
        self._write(state)
        return Execution(
            raw["exec_id"], order_ref, raw["perm_id"], raw["symbol"], Side(raw["side"]),
            quantity, Decimal(raw["price"]), self.clock.now(),
        )

    def snapshot(self) -> BrokerSnapshot:
        if self.block_snapshot_at is not None:
            checkpoint(self.block_snapshot_at, "stable_snapshot_midway")
        state = self._read()
        orders = [BrokerOrder(**{**o, "side": Side(o["side"])}) for o in state["orders"]]
        executions = [
            Execution(
                e["exec_id"], e["order_ref"], e["perm_id"], e["symbol"], Side(e["side"]),
                e["quantity"], Decimal(e["price"]),
                __import__("datetime").datetime.fromisoformat(e["ts"]),
            )
            for e in state["executions"]
        ]
        return BrokerSnapshot(
            positions={k: int(v) for k, v in state["positions"].items()},
            open_orders=orders,
            executions=executions,
            server_time=self.clock.now(),
            is_stable=True,
            account="CRASH-TEST",
        )


def build(journal_path: Path, truth_path: Path):
    clock = ManualClock(SESSION_START)
    journal = Journal(journal_path, clock=clock)
    broker = DurableBroker(truth_path, clock)
    risk = RiskEngine(RiskConfig(strategy_whitelist=("manual_test",)), clock)
    ctl = Controller(journal, broker, risk, clock, TradingCalendar(), ExecutionPolicy())
    journal.commit(EventType.PROCESS_STARTED, {})
    ctl.on_connected(1)
    ctl.on_quote(
        Quote("SPY", Decimal("599.98"), Decimal("600.02"), 100, 100, clock.now())
    )
    assert ctl.reconcile()
    return clock, journal, broker, ctl


def run(scenario: str, journal_path: Path, truth_path: Path, checkpoint_path: Path) -> None:
    clock, journal, broker, ctl = build(journal_path, truth_path)
    target = TargetPosition(
        "manual_test", "SPY", 2, "crash-d1", clock.now() + timedelta(minutes=10)
    )

    if scenario in {"before_wal", "after_wal_before_send"}:
        original = journal.commit

        def intercepted(event_type, *args, **kwargs):
            if scenario == "before_wal" and event_type is EventType.ORDER_INTENT_COMMITTED:
                checkpoint(checkpoint_path, "before_wal_commit")
            seq = original(event_type, *args, **kwargs)
            if scenario == "after_wal_before_send" and event_type is EventType.SEND_ATTEMPT_STARTED:
                checkpoint(checkpoint_path, "after_wal_before_send")
            return seq

        journal.commit = intercepted
        ctl.submit_target(target)
    elif scenario == "after_send_before_ack":
        broker.block_place_at = checkpoint_path
        ctl.submit_target(target)
    elif scenario == "partial_fill":
        ctl.submit_target(target)
        leg = ctl.leg("manual_test", "SPY")
        assert leg.live_intent is not None
        order_ref = leg.live_intent.order_ref
        state = broker._read()
        order = next(o for o in state["orders"] if o["order_ref"] == order_ref)
        ctl.on_ack(order_ref, order["broker_order_id"], order["perm_id"])
        ctl.on_working(order_ref, order["perm_id"])
        ctl.on_execution(broker.apply_partial(order_ref))
        checkpoint(checkpoint_path, "after_partial_fill")
    elif scenario == "cancel_request":
        ctl.submit_target(target)
        leg = ctl.leg("manual_test", "SPY")
        assert leg.live_intent is not None
        state = broker._read()
        order = state["orders"][0]
        ctl.on_ack(order["order_ref"], order["broker_order_id"], order["perm_id"])
        ctl.on_working(order["order_ref"], order["perm_id"])
        broker.block_cancel_at = checkpoint_path
        ctl.submit_target(
            TargetPosition(
                "manual_test", "SPY", -1, "crash-d2", clock.now() + timedelta(minutes=10)
            )
        )
    elif scenario == "stable_snapshot":
        broker.block_snapshot_at = checkpoint_path
        ctl.reconcile()
    elif scenario == "halt_cause":
        ctl.halt("crash-window durable halt")
        checkpoint(checkpoint_path, "after_halt_cause")
    else:
        raise ValueError(scenario)


if __name__ == "__main__":
    run(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
