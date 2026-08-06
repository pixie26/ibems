"""
Deterministic in-process broker with fault injection.

This is the single most important piece of test equipment in the project.
IB paper cannot do most of what we need: it will not reorder callbacks on
demand, will not fill an order in the window between our cancel and its ack,
and will not crash at a chosen instruction. Those are exactly the failures that
break execution systems.

Callbacks are queued, not delivered inline. Tests call pump() to deliver them,
which makes every interleaving reproducible and lets us reorder deliberately.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Callable, Optional

from .broker_protocol import BrokerCallbacks, BrokerRejected, BrokerSendUncertain
from .models import (
    BrokerOrder,
    BrokerSnapshot,
    Execution,
    OrderIntent,
    Side,
)


@dataclass
class Faults:
    """Every flag here corresponds to a Gate B1 acceptance scenario."""

    # submission
    send_raises_uncertain: bool = False   # crash/timeout with unknown delivery
    send_raises_rejected: bool = False    # clean refusal
    delay_ack: bool = False               # ack queued but not auto-delivered

    # callbacks
    duplicate_callbacks: bool = False     # deliver every callback twice
    reorder_callbacks: bool = False       # deliver queue back-to-front

    # fills
    partial_fill_qty: Optional[int] = None
    fill_before_cancel: bool = False      # cancel arrives after a fill
    no_fill: bool = False                 # order just sits

    # cancels
    cancel_rejects: bool = False
    cancel_silent: bool = False           # no response at all -> timeout path

    # corrections / fees
    emit_execution_correction: bool = False
    correction_price_delta: Decimal = Decimal("0.01")
    late_fee: bool = True                 # fee arrives separately (realistic)

    # connectivity
    disconnect_on_next_call: bool = False
    stale_snapshot: bool = False          # snapshot omits latest facts

    # external
    external_position: int = 0            # position we never created
    external_order: bool = False


@dataclass
class _Pending:
    fn: Callable[[], None]
    label: str = ""


class FakeBroker:
    def __init__(self, clock, faults: Optional[Faults] = None, symbol: str = "SPY"):
        self.clock = clock
        self.faults = faults or Faults()
        self.symbol = symbol

        self._cb: Optional[BrokerCallbacks] = None
        self._orders: dict[str, BrokerOrder] = {}
        self._positions: dict[str, int] = {}
        self._executions: list[Execution] = []
        self._emitted_fills: dict[str, Execution] = {}
        self._queue: list[_Pending] = []

        self._order_id = itertools.count(1000)
        self._perm_id = itertools.count(900_000)
        self._exec_seq = itertools.count(1)
        self._epoch = itertools.count(1)

        self.connected = True
        self.connection_epoch = next(self._epoch)

        # observability for tests
        self.place_calls: list[OrderIntent] = []
        self.cancel_calls: list[str] = []

        if self.faults.external_position:
            self._positions[symbol] = self.faults.external_position

    # -- wiring -----------------------------------------------------------

    def register(self, callbacks: BrokerCallbacks) -> None:
        self._cb = callbacks

    def is_connected(self) -> bool:
        return self.connected

    def server_time(self) -> datetime:
        return self.clock.now()

    # -- order entry ------------------------------------------------------

    def place_order(self, intent: OrderIntent) -> int:
        self.place_calls.append(intent)

        if self.faults.disconnect_on_next_call:
            self.faults.disconnect_on_next_call = False
            self.connected = False
            self._enqueue(lambda: self._cb and self._cb.on_disconnected("injected"), "disc")
            raise BrokerSendUncertain("connection dropped during send")

        if self.faults.send_raises_uncertain:
            # The order MAY exist at the broker. This is the dangerous case.
            self._create_order(intent)
            raise BrokerSendUncertain("timeout with unknown delivery")

        if self.faults.send_raises_rejected:
            raise BrokerRejected("injected clean rejection")

        if not self.connected:
            raise BrokerSendUncertain("not connected")

        oid = self._create_order(intent)

        if not self.faults.delay_ack:
            self._enqueue(
                lambda: self._cb
                and self._cb.on_ack(intent.order_ref, oid, self._orders[intent.order_ref].perm_id),
                f"ack:{intent.order_ref}",
            )
            self._enqueue(
                lambda: self._cb
                and self._cb.on_working(intent.order_ref, self._orders[intent.order_ref].perm_id),
                f"working:{intent.order_ref}",
            )
            if not self.faults.no_fill:
                self._schedule_fill(intent)
        return oid

    def _create_order(self, intent: OrderIntent) -> int:
        oid = next(self._order_id)
        pid = next(self._perm_id)
        self._orders[intent.order_ref] = BrokerOrder(
            order_ref=intent.order_ref,
            perm_id=pid,
            broker_order_id=oid,
            symbol=intent.symbol,
            side=intent.side,
            total_quantity=intent.quantity,
            filled_quantity=0,
            status="PreSubmitted",
        )
        return oid

    def _schedule_fill(self, intent: OrderIntent) -> None:
        qty = self.faults.partial_fill_qty or intent.quantity
        qty = min(qty, intent.quantity)
        self._enqueue(lambda: self._fill(intent.order_ref, qty), f"fill:{intent.order_ref}")

    def _fill(
        self, order_ref: str, qty: int, price: Decimal = Decimal("600.00"), _key: str = ""
    ) -> None:
        """
        Create ONE execution and deliver it.

        If this callable is delivered twice (duplicate_callbacks), the broker
        must redeliver the SAME execution, not fill again. Modelling a duplicate
        callback as a second fill would be testing a different -- and much less
        common -- failure.
        """
        key = _key or f"{order_ref}:{qty}"
        prior = self._emitted_fills.get(key)
        if prior is not None:
            if self._cb:
                self._cb.on_execution(prior)
            return

        o = self._orders.get(order_ref)
        if o is None or qty <= 0:
            return
        exec_id = f"E{next(self._exec_seq):08d}.01"
        ex = Execution(
            exec_id=exec_id,
            order_ref=order_ref,
            perm_id=o.perm_id,
            symbol=o.symbol,
            side=o.side,
            quantity=qty,
            price=price,
            ts=self.clock.now(),
        )
        self._executions.append(ex)
        self._emitted_fills[key] = ex
        signed = qty if o.side is Side.BUY else -qty
        self._positions[o.symbol] = self._positions.get(o.symbol, 0) + signed
        self._orders[order_ref] = BrokerOrder(
            order_ref=o.order_ref,
            perm_id=o.perm_id,
            broker_order_id=o.broker_order_id,
            symbol=o.symbol,
            side=o.side,
            total_quantity=o.total_quantity,
            filled_quantity=o.filled_quantity + qty,
            # A fill delivered AFTER a cancel must not resurrect the order.
            # Terminal statuses are terminal. Without this the harness reports
            # phantom live orders and sends people chasing a controller bug
            # that does not exist -- a fake broker that lies is worse than none.
            status=(
                o.status
                if o.status in ("Cancelled", "Rejected")
                else ("Filled" if o.filled_quantity + qty >= o.total_quantity else "Submitted")
            ),
        )
        if self._cb:
            self._cb.on_execution(ex)
        if self.faults.late_fee:
            self._enqueue(
                lambda: self._cb and self._cb.on_fee(exec_id, 0.35, "USD"), f"fee:{exec_id}"
            )
        else:
            if self._cb:
                self._cb.on_fee(exec_id, 0.35, "USD")

        if self.faults.emit_execution_correction:
            self.faults.emit_execution_correction = False
            self._enqueue(lambda: self._correct(ex), f"corr:{exec_id}")

    def _correct(self, original: Execution) -> None:
        """
        IB sends corrections as a new execId whose trailing segment increments.
        We reverse then re-book; the original row is never touched.
        """
        base = original.exec_id.rsplit(".", 1)[0]
        rev = Execution(
            exec_id=f"{base}.02",
            order_ref=original.order_ref,
            perm_id=original.perm_id,
            symbol=original.symbol,
            side=original.side,
            quantity=original.quantity,
            price=original.price,
            ts=self.clock.now(),
            is_reversal=True,
            corrects_exec_id=original.exec_id,
        )
        new = Execution(
            exec_id=f"{base}.03",
            order_ref=original.order_ref,
            perm_id=original.perm_id,
            symbol=original.symbol,
            side=original.side,
            quantity=original.quantity,
            price=original.price + self.faults.correction_price_delta,
            ts=self.clock.now(),
            corrects_exec_id=original.exec_id,
        )
        self._executions.extend([rev, new])
        if self._cb:
            self._cb.on_execution(rev)
            self._cb.on_execution(new)

    # -- cancel -----------------------------------------------------------

    def cancel_order(self, order_ref: str) -> None:
        self.cancel_calls.append(order_ref)

        if self.faults.cancel_silent:
            return  # deliberate black hole -> exercises the timeout path

        if self.faults.fill_before_cancel:
            self.faults.fill_before_cancel = False
            o = self._orders.get(order_ref)
            if o is not None:
                self._enqueue(lambda: self._fill(order_ref, o.remaining), "fill-before-cancel")
                self._enqueue(
                    lambda: self._cb
                    and self._cb.on_cancel_rejected(order_ref, "already filled"),
                    "cancel-rej",
                )
                return

        if self.faults.cancel_rejects:
            self._enqueue(
                lambda: self._cb and self._cb.on_cancel_rejected(order_ref, "injected"),
                "cancel-rej",
            )
            return

        o = self._orders.get(order_ref)
        if o is not None:
            self._orders[order_ref] = BrokerOrder(
                order_ref=o.order_ref,
                perm_id=o.perm_id,
                broker_order_id=o.broker_order_id,
                symbol=o.symbol,
                side=o.side,
                total_quantity=o.total_quantity,
                filled_quantity=o.filled_quantity,
                status="Cancelled",
            )
        self._enqueue(lambda: self._cb and self._cb.on_cancelled(order_ref), "cancelled")

    # -- snapshot ---------------------------------------------------------

    def snapshot(self) -> BrokerSnapshot:
        if not self.connected:
            raise BrokerSendUncertain("cannot snapshot while disconnected")

        execs = list(self._executions)
        orders = [o for o in self._orders.values() if o.status in ("PreSubmitted", "Submitted")]
        positions = dict(self._positions)

        if self.faults.stale_snapshot and execs:
            # Broker hasn't caught up: drop the newest fact.
            last = execs[-1]
            execs = execs[:-1]
            positions[last.symbol] = positions.get(last.symbol, 0) - last.signed_quantity

        if self.faults.external_order:
            orders.append(
                BrokerOrder(
                    order_ref="MANUAL-TWS",
                    perm_id=999_999,
                    broker_order_id=42,
                    symbol=self.symbol,
                    side=Side.BUY,
                    total_quantity=50,
                    filled_quantity=0,
                    status="Submitted",
                )
            )

        position_or_order_callbacks_pending = any(
            not pending.label.startswith("fee:") for pending in self._queue
        )
        return BrokerSnapshot(
            positions=positions,
            open_orders=orders,
            executions=execs,
            server_time=self.clock.now(),
            is_stable=not position_or_order_callbacks_pending,
        )

    # -- callback pump ----------------------------------------------------

    def _enqueue(self, fn: Callable[[], None], label: str = "") -> None:
        self._queue.append(_Pending(fn, label))
        if self.faults.duplicate_callbacks:
            self._queue.append(_Pending(fn, label + "#dup"))

    def pump(self, limit: int = 1000) -> int:
        """Deliver queued callbacks. Returns how many fired."""
        n = 0
        while self._queue and n < limit:
            batch, self._queue = self._queue, []
            if self.faults.reorder_callbacks:
                batch.reverse()
            for p in batch:
                p.fn()
                n += 1
        return n

    def pending_labels(self) -> list[str]:
        return [p.label for p in self._queue]

    # -- connectivity -----------------------------------------------------

    def disconnect(self, reason: str = "test") -> None:
        self.connected = False
        if self._cb:
            self._cb.on_disconnected(reason)

    def reconnect(self, market_data_lost: bool = True) -> None:
        """Mirrors an IB Gateway restart: link returns, subscriptions may not."""
        self.connected = True
        self.connection_epoch = next(self._epoch)
        if self._cb:
            self._cb.on_connected(self.connection_epoch)
            if market_data_lost:
                self._cb.on_market_data_lost()

    # -- test helpers -----------------------------------------------------

    def force_position(self, symbol: str, qty: int) -> None:
        self._positions[symbol] = qty

    def position(self, symbol: str) -> int:
        return self._positions.get(symbol, 0)

    def deliver_ack_now(self, order_ref: str) -> None:
        o = self._orders[order_ref]
        if self._cb:
            self._cb.on_ack(order_ref, o.broker_order_id or 0, o.perm_id)
            self._cb.on_working(order_ref, o.perm_id)
