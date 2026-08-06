"""
Core data model for the IB execution platform.

FROZEN per docs/SPEC.md. Changes to the state vector or the event list are
spec changes, not implementation details: they require an ADR.

Design note on the state vector: it is FOUR orthogonal dimensions, not one flat
enum. A flat enum cannot express "WORKING + disconnected + flatten-only" without
combinatorial explosion, and the one combination it most often gets wrong is
"allowed to close but not to open".
"""

from __future__ import annotations

import enum
import hashlib
import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional


# --------------------------------------------------------------------------
# State vector (SPEC §2)
# --------------------------------------------------------------------------


class LinkState(str, enum.Enum):
    """Transport-level connectivity to the broker."""

    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    DISCONNECTED = "DISCONNECTED"


class SyncState(str, enum.Enum):
    """
    Whether our view of the account is believed to match the broker's.

    A TCP socket being up does NOT mean the account state is trustworthy.
    IB error 1101 (subscriptions lost on reconnect) is the canonical case:
    link is CONNECTED, sync is UNVERIFIED.
    """

    UNVERIFIED = "UNVERIFIED"
    SYNCING = "SYNCING"
    SYNCED = "SYNCED"


class OperatingMode(str, enum.Enum):
    """What class of action the platform is permitted to take."""

    NORMAL = "NORMAL"
    STOP_NEW = "STOP_NEW"
    FLATTEN_ONLY = "FLATTEN_ONLY"
    HALTED = "HALTED"


class OrderState(str, enum.Enum):
    """Per (strategy, symbol) order lifecycle."""

    IDLE = "IDLE"
    INTENT_COMMITTED = "INTENT_COMMITTED"
    SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN"
    PENDING_ACK = "PENDING_ACK"
    WORKING = "WORKING"
    PENDING_CANCEL = "PENDING_CANCEL"
    TERMINAL_UNRECONCILED = "TERMINAL_UNRECONCILED"


#: Order states in which our belief about the broker is not trustworthy.
#: SPEC coupling rule C1: entering either of these forces sync_state=UNVERIFIED.
UNTRUSTWORTHY_ORDER_STATES = frozenset(
    {OrderState.SUBMISSION_UNCERTAIN, OrderState.TERMINAL_UNRECONCILED}
)

#: Order states that block issuing a new order (invariants 3, 4, 5).
BLOCKS_NEW_ORDER = frozenset(
    {
        OrderState.INTENT_COMMITTED,
        OrderState.SUBMISSION_UNCERTAIN,
        OrderState.PENDING_ACK,
        OrderState.WORKING,
        OrderState.PENDING_CANCEL,
        OrderState.TERMINAL_UNRECONCILED,
    }
)

#: Order states that are terminal-and-clean.
TERMINAL_CLEAN = frozenset({OrderState.IDLE})


#: Events that resolve an order. Defined ONCE.
#:
#: This set was previously copy-pasted into four places in the auditor, and the
#: fifth copy -- written for the invariant 9 check -- omitted
#: ORDER_ABSENT_CONFIRMED. The result was a false positive on correct behaviour.
#: False positives are corrosive: they teach people to ignore auditor findings,
#: which costs more than the check was ever worth. One constant, referenced
#: everywhere, so the auditor and the controller cannot drift apart.
TERMINAL_ORDER_EVENTS: frozenset = frozenset()   # populated after EventType


class Side(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, enum.Enum):
    MARKETABLE_LIMIT = "MARKETABLE_LIMIT"
    MARKET = "MARKET"
    # MOC deliberately absent in V1. See ADR-003.


class FlattenReason(str, enum.Enum):
    """Flatten cause drives price aggressiveness and post-failure handling."""

    EOD_FLATTEN = "EOD_FLATTEN"
    RISK_FLATTEN = "RISK_FLATTEN"
    MANUAL_FLATTEN = "MANUAL_FLATTEN"
    RECOVERY_FLATTEN = "RECOVERY_FLATTEN"


class MissReason(str, enum.Enum):
    """
    Why a decision produced no order.

    This is the availability term in the cost model. A backtest assumes every
    decision trades; production does not. For a straddle-shaped P&L this term
    can dominate slippage, because the days the system is most likely to fail
    are the tail days that carry the P&L.
    """

    DISCONNECTED = "DISCONNECTED"
    NOT_SYNCED = "NOT_SYNCED"
    RISK_BLOCKED = "RISK_BLOCKED"
    EXPIRED = "EXPIRED"
    DATA_STALE = "DATA_STALE"
    MODE_BLOCKED = "MODE_BLOCKED"
    ORDER_STATE_BLOCKED = "ORDER_STATE_BLOCKED"
    REPRICE_EXHAUSTED = "REPRICE_EXHAUSTED"
    BROKER_REJECTED = "BROKER_REJECTED"


# --------------------------------------------------------------------------
# Journal event types (SPEC §5) -- append-only
# --------------------------------------------------------------------------


class EventType(str, enum.Enum):
    # target lifecycle
    TARGET_RECEIVED = "TARGET_RECEIVED"
    TARGET_REJECTED = "TARGET_REJECTED"
    TARGET_SUPERSEDED = "TARGET_SUPERSEDED"
    TARGET_DEFERRED = "TARGET_DEFERRED"
    DECISION_MISSED = "DECISION_MISSED"

    # order lifecycle
    ORDER_INTENT_COMMITTED = "ORDER_INTENT_COMMITTED"
    SEND_ATTEMPT_STARTED = "SEND_ATTEMPT_STARTED"
    SEND_CALL_RETURNED = "SEND_CALL_RETURNED"
    SEND_CALL_FAILED = "SEND_CALL_FAILED"
    BROKER_ACK_RECEIVED = "BROKER_ACK_RECEIVED"
    ORDER_WORKING = "ORDER_WORKING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCEL_REJECTED = "CANCEL_REJECTED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_ABSENT_CONFIRMED = "ORDER_ABSENT_CONFIRMED"
    REPRICE_TRIGGERED = "REPRICE_TRIGGERED"
    PARTIAL_EXPIRED = "PARTIAL_EXPIRED"

    # execution ledger (correction is a new event, never an in-place update)
    EXECUTION_RECEIVED = "EXECUTION_RECEIVED"
    EXECUTION_REVERSAL = "EXECUTION_REVERSAL"
    EXECUTION_CORRECTED = "EXECUTION_CORRECTED"
    FEE_RECEIVED = "FEE_RECEIVED"

    # eod
    EOD_FLATTEN_STARTED = "EOD_FLATTEN_STARTED"
    EOD_FLATTEN_COMPLETED = "EOD_FLATTEN_COMPLETED"
    EOD_FLATTEN_FAILED = "EOD_FLATTEN_FAILED"

    # sync / lifecycle
    RECONCILIATION_STARTED = "RECONCILIATION_STARTED"
    RECONCILIATION_COMPLETED = "RECONCILIATION_COMPLETED"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
    BROKER_FACT_ADOPTED = "BROKER_FACT_ADOPTED"
    LINK_STATE_CHANGED = "LINK_STATE_CHANGED"
    SYNC_STATE_CHANGED = "SYNC_STATE_CHANGED"
    OPERATING_MODE_CHANGED = "OPERATING_MODE_CHANGED"
    ORDER_STATE_CHANGED = "ORDER_STATE_CHANGED"

    # safety
    CALLBACK_FAILURE = "CALLBACK_FAILURE"
    INVARIANT_VIOLATION = "INVARIANT_VIOLATION"
    HALT_CAUSE_ADDED = "HALT_CAUSE_ADDED"
    HALT_ACKNOWLEDGED = "HALT_ACKNOWLEDGED"
    RISK_REJECTED = "RISK_REJECTED"
    RISK_CONFIG_LOADED = "RISK_CONFIG_LOADED"
    RISK_SELF_TEST_PASSED = "RISK_SELF_TEST_PASSED"
    EXTERNAL_ORDER_DETECTED = "EXTERNAL_ORDER_DETECTED"
    HEARTBEAT = "HEARTBEAT"
    PROCESS_STARTED = "PROCESS_STARTED"
    PROCESS_STATE_RESTORED = "PROCESS_STATE_RESTORED"
    PROCESS_STOPPING = "PROCESS_STOPPING"
    FSYNC_LATENCY_SAMPLE = "FSYNC_LATENCY_SAMPLE"


# --------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime rejected; all timestamps must be tz-aware")
    return dt.astimezone(timezone.utc)


TERMINAL_ORDER_EVENTS = frozenset(
    {
        EventType.ORDER_FILLED,
        EventType.ORDER_CANCELLED,
        EventType.ORDER_REJECTED,
        EventType.ORDER_ABSENT_CONFIRMED,
    }
)


@dataclass(frozen=True)
class TargetPosition:
    """
    The ONLY thing a strategy is allowed to say.

    A strategy never specifies: order id, current position, whether to cancel,
    whether this is a flip, or what to do after a partial fill. Those are
    platform responsibilities.
    """

    strategy_id: str
    symbol: str
    target_quantity: int
    decision_id: str
    valid_until: datetime
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "valid_until", _utc(self.valid_until))
        if not self.decision_id:
            raise ValueError("decision_id is required (idempotency key)")
        if not isinstance(self.target_quantity, int):
            raise TypeError("target_quantity must be int (whole shares)")

    def is_expired(self, now: datetime) -> bool:
        return _utc(now) >= self.valid_until

    def key(self) -> tuple[str, str]:
        return (self.strategy_id, self.symbol)


@dataclass(frozen=True)
class Quote:
    """Top of book snapshot with its own arrival time, for staleness checks."""

    symbol: str
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int
    ts: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", _utc(self.ts))
        if self.bid <= 0 or self.ask <= 0:
            raise ValueError("bid and ask must be positive")
        if self.ask < self.bid:
            raise ValueError("ask must be >= bid")
        if self.bid_size < 0 or self.ask_size < 0:
            raise ValueError("quote sizes must be non-negative")

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal(2)

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    def age_seconds(self, now: datetime) -> float:
        return (_utc(now) - self.ts).total_seconds()


@dataclass
class OrderIntent:
    """
    A durable record of what we are about to ask the broker to do.

    Invariant 2: this must be committed BEFORE any broker write. If we crash
    between commit and the broker call, we may have an order the broker knows
    about and we don't -- but we will KNOW we might, which is the whole point.
    """

    intent_id: str
    decision_id: str
    strategy_id: str
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType
    limit_price: Optional[Decimal]
    target_quantity: int
    position_snapshot: int
    working_snapshot: int
    valid_until: datetime
    risk_config_hash: str
    execution_policy_hash: str
    order_ref: str
    attempt: int = 0
    flatten_reason: Optional[FlattenReason] = None

    # broker-side identity, filled in as it becomes known
    broker_order_id: Optional[int] = None
    perm_id: Optional[int] = None

    @staticmethod
    def new_intent_id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def build_order_ref(strategy_id: str, decision_id: str, intent_id: str) -> str:
        """
        orderRef is our only cross-restart attribution handle that survives into
        the execution record. IB does NOT enforce uniqueness on it, so it is a
        hint, never a primary key. permId is the broker-side stable identity.
        """
        # Do not rely on undocumented broker-side length/truncation behaviour.
        # Keep the value compact and map it back through the durable journal.
        tag = "".join(ch for ch in strategy_id.lower() if ch.isalnum())[:8] or "strategy"
        digest = hashlib.sha256(
            f"{strategy_id}\x1f{decision_id}\x1f{intent_id}".encode("utf-8")
        ).hexdigest()[:20]
        return f"{tag}-{digest}"

    @property
    def signed_quantity(self) -> int:
        return self.quantity if self.side is Side.BUY else -self.quantity

    def to_payload(self) -> dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        d["order_type"] = self.order_type.value
        d["limit_price"] = str(self.limit_price) if self.limit_price is not None else None
        d["valid_until"] = self.valid_until.isoformat()
        d["flatten_reason"] = self.flatten_reason.value if self.flatten_reason else None
        return d

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "OrderIntent":
        """Rehydrate the durable intent during restart reconciliation."""
        return cls(
            intent_id=str(payload["intent_id"]),
            decision_id=str(payload["decision_id"]),
            strategy_id=str(payload["strategy_id"]),
            symbol=str(payload["symbol"]),
            side=Side(payload["side"]),
            quantity=int(payload["quantity"]),
            order_type=OrderType(payload["order_type"]),
            limit_price=(
                Decimal(str(payload["limit_price"]))
                if payload.get("limit_price") is not None
                else None
            ),
            target_quantity=int(payload["target_quantity"]),
            position_snapshot=int(payload["position_snapshot"]),
            working_snapshot=int(payload["working_snapshot"]),
            valid_until=datetime.fromisoformat(str(payload["valid_until"])),
            risk_config_hash=str(payload["risk_config_hash"]),
            execution_policy_hash=str(payload["execution_policy_hash"]),
            order_ref=str(payload["order_ref"]),
            attempt=int(payload.get("attempt", 0)),
            flatten_reason=(
                FlattenReason(payload["flatten_reason"])
                if payload.get("flatten_reason")
                else None
            ),
            broker_order_id=(
                int(payload["broker_order_id"])
                if payload.get("broker_order_id") is not None
                else None
            ),
            perm_id=(int(payload["perm_id"]) if payload.get("perm_id") is not None else None),
        )


@dataclass(frozen=True)
class Execution:
    """
    One fill. Corrections arrive as NEW execIds referencing the original;
    we never mutate a recorded fill (invariant 12).
    """

    exec_id: str
    order_ref: str
    perm_id: Optional[int]
    symbol: str
    side: Side
    quantity: int
    price: Decimal
    ts: datetime
    is_reversal: bool = False
    corrects_exec_id: Optional[str] = None

    @property
    def signed_quantity(self) -> int:
        q = self.quantity if self.side is Side.BUY else -self.quantity
        return -q if self.is_reversal else q


@dataclass(frozen=True)
class BrokerOrder:
    """An order as the broker reports it."""

    order_ref: str
    perm_id: Optional[int]
    broker_order_id: Optional[int]
    symbol: str
    side: Side
    total_quantity: int
    filled_quantity: int
    status: str

    @property
    def remaining(self) -> int:
        return max(0, self.total_quantity - self.filled_quantity)

    @property
    def signed_remaining(self) -> int:
        r = self.remaining
        return r if self.side is Side.BUY else -r


@dataclass(frozen=True)
class BrokerSnapshot:
    """Everything we pulled from the broker in one reconciliation pass."""

    positions: dict[str, int]
    open_orders: list[BrokerOrder]
    executions: list[Execution]
    server_time: datetime
    # True only after the adapter has completed a broker-side barrier across
    # positions, open orders and executions. A non-atomic or callback-backlogged
    # snapshot may be useful for diagnostics but may never restore SYNCED.
    is_stable: bool
    account: str = "UNKNOWN"


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class InvariantViolation(RuntimeError):
    """
    Raised by a runtime assertion (SPEC invariant 20b).

    Always fail-closed: the controller catches this, journals it, and HALTs.
    Never caught-and-continued anywhere else.
    """

    def __init__(self, invariant: int, detail: str):
        self.invariant = invariant
        self.detail = detail
        super().__init__(f"INVARIANT {invariant} VIOLATED: {detail}")


class RiskRejection(Exception):
    def __init__(self, check: str, detail: str):
        self.check = check
        self.detail = detail
        super().__init__(f"{check}: {detail}")


class DuplicateDecision(Exception):
    """decision_id already consumed (invariant 1)."""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def stable_hash(obj: Any) -> str:
    """Deterministic hash for config provenance (invariant 17)."""
    blob = json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
