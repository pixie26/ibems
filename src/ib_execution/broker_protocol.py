"""
Broker seam.

Everything above this line is deterministic and testable without IB.
Everything below it is IB's problem. Phase 0 proves the state machine against
FakeBroker; Phase 1/2 prove that IbAdapter honours this contract.

place_order() may raise BrokerSendUncertain: the call failed in a way that does
NOT tell us whether the broker received it. That is not an error path to be
retried -- it is a state, and the correct response is reconciliation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from .models import BrokerSnapshot, Execution, OrderIntent


class BrokerSendUncertain(Exception):
    """
    We do not know whether the broker received this order.

    Never retried automatically. SPEC §4: at-most-once beats at-least-once when
    the failure mode of the latter is a duplicate position.
    """


class BrokerRejected(Exception):
    """Broker definitively refused. Safe: no order exists."""


@runtime_checkable
class BrokerCallbacks(Protocol):
    """Implemented by the controller. Called from the broker's thread/loop."""

    def on_ack(self, order_ref: str, broker_order_id: int, perm_id: int | None) -> None: ...
    def on_working(self, order_ref: str, perm_id: int | None) -> None: ...
    def on_execution(self, execution: Execution) -> None: ...
    def on_fee(self, exec_id: str, commission: float, currency: str) -> None: ...
    def on_cancelled(self, order_ref: str) -> None: ...
    def on_cancel_rejected(self, order_ref: str, reason: str) -> None: ...
    def on_rejected(self, order_ref: str, reason: str) -> None: ...
    def on_disconnected(self, reason: str) -> None: ...
    def on_connected(self, connection_epoch: int) -> None: ...
    def on_market_data_lost(self) -> None: ...
    def on_market_data_restored(self) -> None: ...


@runtime_checkable
class Broker(Protocol):
    def register(self, callbacks: BrokerCallbacks) -> None: ...

    def place_order(self, intent: OrderIntent) -> int:
        """Returns broker order id. Raises BrokerSendUncertain / BrokerRejected."""
        ...

    def cancel_order(self, order_ref: str) -> None: ...

    def snapshot(self) -> BrokerSnapshot:
        """Authoritative account state. The basis of reconciliation."""
        ...

    def server_time(self) -> datetime: ...

    def is_connected(self) -> bool: ...
