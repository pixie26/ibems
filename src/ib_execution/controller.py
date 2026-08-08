"""
The controller: the only normal broker writer in the system.

Everything here is synchronous and clock-injected. That is deliberate. In
Phase 0 it is called directly by deterministic tests. In Gate B2 the IB adapter
must register AsyncControllerBridge, which serializes work onto one dedicated
controller thread; calling Controller directly from the IB event loop is
forbidden because durable journal commits wait for fsync.

Reading order:
  submit_target        -- the strategy-facing entrance
  _evaluate            -- the single place that decides to act
  _send                -- durable-before-send, at-most-once
  reconcile            -- broker is the authority, journal is the explanation
  on_*                 -- broker callbacks, all fail-closed
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Optional

from .broker_protocol import Broker, BrokerRejected, BrokerSendUncertain
from .calendar import ET, TradingCalendar
from .clock import Clock
from .fatal_fence import FatalFence
from .journal import Journal, JournalUnavailable
from .journal_witness import JournalWitness
from .models import (
    BLOCKS_NEW_ORDER,
    UNTRUSTWORTHY_ORDER_STATES,
    DuplicateDecision,
    EventType,
    Execution,
    FlattenReason,
    InvariantViolation,
    LinkState,
    MissReason,
    OperatingMode,
    OrderIntent,
    OrderState,
    OrderType,
    Quote,
    RiskRejection,
    Side,
    SyncState,
    TargetPosition,
    stable_hash,
)
from .risk import RiskEngine, run_self_test


@dataclass
class ExecutionPolicy:
    """
    Frozen before Phase 1. Reprice behaviour is a spec item, not a tuning knob.

    Two independent triggers must never be conflated:
      - target change  -> cancel, wait terminal, reconcile, recompute
      - reprice timeout -> cancel, wait terminal, reconcile, SAME target, next rung

    An unbounded reprice ladder is chasing, and this strategy's P&L lives in
    exactly the fast markets where chasing is most expensive.
    """

    initial_collar_bps: float = 2.0
    collar_step_bps: float = 3.0
    max_attempts: int = 3
    ack_timeout_seconds: float = 10.0
    order_timeout_seconds: float = 20.0
    cancel_timeout_seconds: float = 10.0
    closing_collar_multiplier: float = 4.0

    def collar_bps(self, attempt: int, is_closing: bool = False) -> float:
        base = self.initial_collar_bps + self.collar_step_bps * attempt
        return base * (self.closing_collar_multiplier if is_closing else 1.0)

    def policy_hash(self) -> str:
        return stable_hash(self.__dict__)


@dataclass
class SymbolState:
    """Per (strategy, symbol) leg of the state vector."""

    strategy_id: str
    symbol: str
    order_state: OrderState = OrderState.IDLE
    desired_target: Optional[TargetPosition] = None
    live_intent: Optional[OrderIntent] = None
    attempt: int = 0
    sent_at: Optional[datetime] = None
    cancel_requested_at: Optional[datetime] = None
    cancel_reason: Optional[str] = None
    position: int = 0                 # our belief, reconciled against broker
    working_signed: int = 0
    flatten_reason: Optional[FlattenReason] = None
    deferred_reason: Optional[str] = None


class Controller:
    def __init__(
        self,
        journal: Journal,
        broker: Broker,
        risk: RiskEngine,
        clock: Clock,
        calendar: Optional[TradingCalendar] = None,
        policy: Optional[ExecutionPolicy] = None,
        alert: Optional[Callable[[str, str], None]] = None,
        fence: Optional[FatalFence] = None,
        witness: Optional[JournalWitness] = None,
    ):
        self.journal = journal
        self.broker = broker
        self.risk = risk
        self.clock = clock
        self.calendar = calendar or TradingCalendar()
        self.policy = policy or ExecutionPolicy()
        self.alert = alert or (lambda level, msg: None)
        # Out-of-band durable fence. Optional here so the core stays testable
        # without a second volume; execution_host always supplies one.
        self.fence = fence
        self.fence_write_failed: Optional[str] = None
        # Gate B1.6. Pins the durable evidence behind each broker write so a
        # later WAL rollback that removes it is detectable at startup.
        self.witness = witness

        self.link_state = LinkState.DISCONNECTED
        self.sync_state = SyncState.UNVERIFIED
        self.operating_mode = OperatingMode.NORMAL
        self.legs: dict[tuple[str, str], SymbolState] = {}

        self.quotes: dict[str, Quote] = {}
        self.connection_epoch = 0
        self._booked_execs: set[str] = set()
        self._fees_pending: set[str] = set()
        self.violations: list[InvariantViolation] = []
        self.journal_failure: Optional[str] = None
        self.fatal_shutdown_requested = False
        # (session_date, strategy_id, symbol) already recorded as an EOD residual
        self._eod_residual_recorded: set[tuple] = set()
        self._journal_restored = False
        self._restored_positions: dict[str, int] = {}

        # Invariant 21 is enforced in the actual controller construction path,
        # not only by a separate preflight command that an operator could skip.
        proven = run_self_test(self.risk.config, self.clock)
        self.journal.commit(
            EventType.RISK_CONFIG_LOADED,
            {
                "config_hash": self.risk.config_hash,
                "limits": self.risk.snapshot(),
            },
        )
        self.journal.commit(
            EventType.RISK_SELF_TEST_PASSED,
            {"config_hash": self.risk.config_hash, "proven": proven},
        )
        broker.register(self)

    # ------------------------------------------------------------------
    # state vector helpers
    # ------------------------------------------------------------------

    def leg(self, strategy_id: str, symbol: str) -> SymbolState:
        key = (strategy_id, symbol)
        if key not in self.legs:
            self.legs[key] = SymbolState(strategy_id, symbol)
        return self.legs[key]

    def _set_order_state(self, leg: SymbolState, new: OrderState, why: str = "") -> None:
        old = leg.order_state
        if old is new:
            return
        leg.order_state = new
        self.journal.commit(
            EventType.ORDER_STATE_CHANGED,
            {"from": old.value, "to": new.value, "why": why},
            strategy_id=leg.strategy_id,
            symbol=leg.symbol,
        )
        # SPEC coupling rule C1. Without this, an untrustworthy order state can
        # coexist with sync_state=SYNCED and a write gets waved through.
        if new in UNTRUSTWORTHY_ORDER_STATES:
            self._set_sync(SyncState.UNVERIFIED, f"order_state={new.value}")

    def _set_sync(self, new: SyncState, why: str = "") -> None:
        if self.sync_state is new:
            return
        old, self.sync_state = self.sync_state, new
        self.journal.commit(
            EventType.SYNC_STATE_CHANGED, {"from": old.value, "to": new.value, "why": why}
        )

    def _set_link(self, new: LinkState, why: str = "") -> None:
        if self.link_state is new:
            return
        old, self.link_state = self.link_state, new
        self.journal.commit(
            EventType.LINK_STATE_CHANGED, {"from": old.value, "to": new.value, "why": why}
        )

    def set_mode(self, new: OperatingMode, why: str = "") -> None:
        if self.operating_mode is new:
            return
        old, self.operating_mode = self.operating_mode, new
        seq = self.journal.commit(
            EventType.OPERATING_MODE_CHANGED,
            {"from": old.value, "to": new.value, "why": why},
        )
        if new is OperatingMode.HALTED:
            self._witness_safety_critical(seq, f"HALT: {why}")
            self.alert("CRITICAL", f"HALTED: {why}")

    def halt(self, why: str) -> None:
        if self.operating_mode is OperatingMode.HALTED:
            # A second unexplained fact is not erased merely because the mode
            # is already HALTED. It advances the durable acknowledgement token,
            # so an operator looking at an older screen cannot clear a newer
            # unresolved cause.
            seq = self.journal.commit(EventType.HALT_CAUSE_ADDED, {"why": why})
            self._witness_safety_critical(seq, f"HALT cause: {why}")
            self.alert("CRITICAL", f"HALT cause added: {why}")
            return
        self.set_mode(OperatingMode.HALTED, why)

    # ------------------------------------------------------------------
    # invariant enforcement (runtime assertions, SPEC 20b)
    # ------------------------------------------------------------------

    def _require(self, cond: bool, invariant: int, detail: str) -> None:
        if not cond:
            v = InvariantViolation(invariant, detail)
            self.violations.append(v)
            try:
                self.journal.commit(
                    EventType.INVARIANT_VIOLATION,
                    {"invariant": invariant, "detail": detail},
                )
            except JournalUnavailable:
                pass
            self.halt(str(v))
            raise v

    def _can_write(self, closing: bool) -> tuple[bool, Optional[MissReason]]:
        """The single gate every broker write passes through."""
        if self.journal_failure is not None or not self.journal.is_healthy():
            self._fail_closed_journal(
                "broker write gate",
                JournalUnavailable(str(self.journal.failure or "writer unavailable")),
            )
            return False, MissReason.MODE_BLOCKED
        if self.link_state is not LinkState.CONNECTED:
            return False, MissReason.DISCONNECTED
        if self.sync_state is not SyncState.SYNCED:
            return False, MissReason.NOT_SYNCED
        if self.operating_mode is OperatingMode.HALTED:
            return False, MissReason.MODE_BLOCKED
        if not closing and self.operating_mode is not OperatingMode.NORMAL:
            # Opening requires NORMAL. Closing is permitted in STOP_NEW and
            # FLATTEN_ONLY -- this asymmetry is the whole reason the state
            # vector is orthogonal rather than a flat enum.
            return False, MissReason.MODE_BLOCKED
        return True, None

    # ------------------------------------------------------------------
    # strategy-facing entrance
    # ------------------------------------------------------------------

    def submit_target(self, target: TargetPosition) -> bool:
        try:
            return self._submit_target_impl(target)
        except JournalUnavailable as exc:
            self._fail_closed_journal("submit_target", exc)
            raise

    def _submit_target_impl(self, target: TargetPosition) -> bool:
        """
        The only thing a strategy may call. Returns True if we acted.

        Idempotency is enforced by a database PRIMARY KEY, not by this method.
        """
        accepted = self.journal.accept_decision(
            target.decision_id,
            {
                "target_quantity": target.target_quantity,
                "valid_until": target.valid_until.isoformat(),
                "metadata": target.metadata,
            },
            strategy_id=target.strategy_id,
            symbol=target.symbol,
        )
        if not accepted:
            self.journal.commit(
                EventType.TARGET_REJECTED,
                {"reason": "duplicate_decision_id"},
                strategy_id=target.strategy_id,
                symbol=target.symbol,
                decision_id=target.decision_id,
            )
            return False

        now = self.clock.now()
        if target.is_expired(now):
            self._miss(target, MissReason.EXPIRED)
            return False

        leg = self.leg(target.strategy_id, target.symbol)
        if leg.desired_target is not None and leg.desired_target.decision_id != target.decision_id:
            self.journal.commit(
                EventType.TARGET_SUPERSEDED,
                {"superseded": leg.desired_target.decision_id},
                strategy_id=target.strategy_id,
                symbol=target.symbol,
                decision_id=target.decision_id,
            )

        # No queue. One latest desired target. Old targets are replaced, but
        # every decision_id remains permanently recorded.
        leg.desired_target = target
        leg.deferred_reason = None
        if leg.order_state is OrderState.IDLE:
            leg.attempt = 0
        return self._evaluate(leg)

    def _miss(self, target: TargetPosition, reason: MissReason, detail: str = "") -> None:
        """
        Record a decision that produced no order.

        This is the availability term of the cost model. A backtest trades every
        decision; production does not. For straddle-shaped P&L, misses on tail
        days can dominate every slippage assumption in the model -- and tail days
        are exactly when infrastructure is most likely to be unwell.
        """
        self.journal.commit(
            EventType.DECISION_MISSED,
            {"reason": reason.value, "detail": detail,
             "target_quantity": target.target_quantity},
            strategy_id=target.strategy_id,
            symbol=target.symbol,
            decision_id=target.decision_id,
        )

    def _defer(self, leg: SymbolState, reason: MissReason, detail: str = "") -> None:
        """Record a temporary block without falsely counting a missed decision."""
        target = leg.desired_target
        if target is None:
            return
        signature = f"{reason.value}:{detail}"
        if leg.deferred_reason == signature:
            return
        leg.deferred_reason = signature
        self.journal.commit(
            EventType.TARGET_DEFERRED,
            {
                "reason": reason.value,
                "detail": detail,
                "target_quantity": target.target_quantity,
            },
            strategy_id=target.strategy_id,
            symbol=target.symbol,
            decision_id=target.decision_id,
        )

    # ------------------------------------------------------------------
    # the single decision point
    # ------------------------------------------------------------------

    def _evaluate(self, leg: SymbolState) -> bool:
        target = leg.desired_target
        if target is None:
            return False

        now = self.clock.now()
        if target.is_expired(now):
            # Invariant 11: an expired target is never sent, not even after a
            # reconnect. A 10:00 signal filled at 10:20 is a different trade.
            self._miss(target, MissReason.EXPIRED)
            leg.desired_target = None
            leg.deferred_reason = None
            return False

        desired = target.target_quantity
        closing = abs(desired) <= abs(leg.position) and (
            desired == 0 or (desired * leg.position) >= 0
        )

        ok, miss = self._can_write(closing)
        if not ok:
            self._defer(leg, miss or MissReason.MODE_BLOCKED)
            return False

        if self.operating_mode is OperatingMode.FLATTEN_ONLY and desired != 0:
            # Invariant 8.
            self._miss(target, MissReason.MODE_BLOCKED, "FLATTEN_ONLY permits target=0 only")
            leg.desired_target = None
            leg.deferred_reason = None
            return False

        # Invariants 3, 4, 5: one unterminated intent per leg; never a second
        # order while a send or a cancel is outstanding.
        if leg.order_state in BLOCKS_NEW_ORDER:
            if leg.order_state is OrderState.WORKING:
                return self._maybe_cancel_for_new_target(leg)
            self._defer(leg, MissReason.ORDER_STATE_BLOCKED, leg.order_state.value)
            return False

        delta = desired - leg.position - leg.working_signed
        if delta == 0:
            leg.deferred_reason = None
            return False

        if leg.attempt >= self.policy.max_attempts:
            self._miss(target, MissReason.REPRICE_EXHAUSTED, f"attempt={leg.attempt}")
            leg.desired_target = None
            leg.deferred_reason = None
            leg.attempt = 0
            return False

        return self._send(leg, delta, closing)

    def _maybe_cancel_for_new_target(self, leg: SymbolState) -> bool:
        """WORKING + target changed -> cancel first. Never a simultaneous opposite order."""
        intent = leg.live_intent
        target = leg.desired_target
        if intent is None or target is None:
            return False
        if intent.target_quantity == target.target_quantity:
            return False
        return self._request_cancel(leg, "target_changed")

    # ------------------------------------------------------------------
    # sending: durable-before-send, at-most-once
    # ------------------------------------------------------------------

    def _limit_price(self, symbol: str, side: Side, attempt: int, closing: bool) -> Optional[Decimal]:
        q = self.quotes.get(symbol)
        if q is None:
            return None
        collar = Decimal(str(self.policy.collar_bps(attempt, closing) / 10_000))
        if side is Side.BUY:
            return (q.ask * (Decimal(1) + collar)).quantize(Decimal("0.01"))
        return (q.bid * (Decimal(1) - collar)).quantize(Decimal("0.01"))

    def _send(self, leg: SymbolState, delta: int, closing: bool) -> bool:
        target = leg.desired_target
        assert target is not None
        side = Side.BUY if delta > 0 else Side.SELL
        qty = abs(delta)
        intent_id = OrderIntent.new_intent_id()

        intent = OrderIntent(
            intent_id=intent_id,
            decision_id=target.decision_id,
            strategy_id=leg.strategy_id,
            symbol=leg.symbol,
            side=side,
            quantity=qty,
            order_type=OrderType.MARKETABLE_LIMIT,
            limit_price=self._limit_price(leg.symbol, side, leg.attempt, closing),
            target_quantity=target.target_quantity,
            position_snapshot=leg.position,
            working_snapshot=leg.working_signed,
            valid_until=target.valid_until,
            risk_config_hash=self.risk.config_hash,
            execution_policy_hash=self.policy.policy_hash(),
            order_ref=OrderIntent.build_order_ref(leg.strategy_id, target.decision_id, intent_id),
            attempt=leg.attempt,
            flatten_reason=leg.flatten_reason,
        )

        try:
            risk_evidence = self.risk.check(
                intent,
                current_position=leg.position,
                quote=self.quotes.get(leg.symbol),
                is_closing=closing,
            )
        except RiskRejection as exc:
            self.journal.commit(
                EventType.RISK_REJECTED,
                {"check": exc.check, "detail": exc.detail, "quantity": qty, "side": side.value},
                strategy_id=leg.strategy_id,
                symbol=leg.symbol,
                decision_id=target.decision_id,
                intent_id=intent_id,
            )
            self._miss(target, MissReason.RISK_BLOCKED, exc.check)
            leg.desired_target = None
            leg.deferred_reason = None
            return False

        accounting_price = intent.limit_price
        if accounting_price is None and self.quotes.get(leg.symbol) is not None:
            accounting_price = self.quotes[leg.symbol].mid

        # ---- ORDER MATTERS FROM HERE ----------------------------------
        # 1. intent durable
        intent_payload = intent.to_payload()
        intent_payload["risk_evidence"] = risk_evidence
        self.journal.commit(
            EventType.ORDER_INTENT_COMMITTED,
            intent_payload,
            strategy_id=leg.strategy_id,
            symbol=leg.symbol,
            decision_id=target.decision_id,
            intent_id=intent_id,
            order_ref=intent.order_ref,
        )
        leg.live_intent = intent
        self._set_order_state(leg, OrderState.INTENT_COMMITTED, "intent durable")

        # 2. mark the send attempt durable BEFORE the call (invariant 2).
        # The returned sequence is what the out-of-band witness pins: it is the
        # specific piece of evidence that will later have to prove a send may
        # have happened, so it is that event's continued existence that matters.
        send_seq = self.journal.commit(
            EventType.SEND_ATTEMPT_STARTED,
            {
                "attempt": leg.attempt,
                "quantity": intent.quantity,
                "price": str(accounting_price) if accounting_price is not None else None,
            },
            strategy_id=leg.strategy_id,
            symbol=leg.symbol,
            decision_id=target.decision_id,
            intent_id=intent_id,
            order_ref=intent.order_ref,
        )
        self._set_order_state(leg, OrderState.PENDING_ACK, "send attempt started")

        # 3. the actual broker write. Re-check the gate at the write boundary;
        # a helper higher in the stack is not an invariant.
        self._require(
            self.link_state is LinkState.CONNECTED and self.sync_state is SyncState.SYNCED,
            6,
            f"place_order while link={self.link_state.value} sync={self.sync_state.value}",
        )
        # Count every broker submission attempt, including clean rejects. A
        # rejection loop is still a runaway loop and must hit the daily/minute cap.
        self.risk.record_sent(intent, accounting_price or Decimal(0))
        if not self._witness_or_fence(send_seq, "place_order"):
            self._set_order_state(leg, OrderState.IDLE, "witness unavailable")
            leg.live_intent = None
            self._defer(leg, MissReason.MODE_BLOCKED)
            return False
        try:
            broker_order_id = self.broker.place_order(intent)
        except BrokerRejected as exc:
            # Clean refusal: no order exists. Safe to return to IDLE.
            self.journal.commit(
                EventType.SEND_CALL_FAILED,
                {"kind": "rejected", "detail": str(exc)},
                intent_id=intent_id,
                order_ref=intent.order_ref,
            )
            leg.live_intent = None
            self._set_order_state(leg, OrderState.IDLE, "clean rejection")
            self._miss(target, MissReason.BROKER_REJECTED, str(exc))
            leg.desired_target = None
            return False
        except BrokerSendUncertain as exc:
            # We do NOT know whether the order exists. Never retried.
            self.journal.commit(
                EventType.SEND_CALL_FAILED,
                {"kind": "uncertain", "detail": str(exc)},
                intent_id=intent_id,
                order_ref=intent.order_ref,
            )
            self._set_order_state(leg, OrderState.SUBMISSION_UNCERTAIN, "send uncertain")
            self.alert("WARN", f"send uncertain for {intent.order_ref}: {exc}")
            return False

        # 4. call returned
        intent.broker_order_id = broker_order_id
        self.journal.commit(
            EventType.SEND_CALL_RETURNED,
            {
                "broker_order_id": broker_order_id,
                "quantity": intent.quantity,
                "price": str(accounting_price) if accounting_price is not None else None,
            },
            intent_id=intent_id,
            order_ref=intent.order_ref,
        )
        self._set_order_state(leg, OrderState.PENDING_ACK, "awaiting broker ack")
        leg.sent_at = self.clock.now()
        leg.working_signed = intent.signed_quantity
        leg.deferred_reason = None
        return True

    def _request_cancel(self, leg: SymbolState, why: str) -> bool:
        intent = leg.live_intent
        if intent is None:
            return False

        # Cancellation is also a broker write. C2 applies to it just as strongly
        # as to placeOrder; attempting a cancel while disconnected creates a
        # false belief that risk is being reduced.
        ok, miss = self._can_write(closing=True)
        if not ok:
            self.alert(
                "WARN",
                f"cancel blocked for {leg.symbol}: {miss.value if miss else 'state'}; "
                "broker state must be reconciled before another write",
            )
            return False
        self._require(
            self.link_state is LinkState.CONNECTED and self.sync_state is SyncState.SYNCED,
            6,
            f"cancel_order while link={self.link_state.value} sync={self.sync_state.value}",
        )

        cancel_seq = self.journal.commit(
            EventType.CANCEL_REQUESTED,
            {"why": why},
            strategy_id=leg.strategy_id,
            symbol=leg.symbol,
            intent_id=intent.intent_id,
            order_ref=intent.order_ref,
        )
        self._set_order_state(leg, OrderState.PENDING_CANCEL, why)
        leg.cancel_requested_at = self.clock.now()
        leg.cancel_reason = why
        if not self._witness_or_fence(cancel_seq, "cancel_order"):
            self._set_order_state(leg, OrderState.TERMINAL_UNRECONCILED, "witness unavailable")
            return False
        try:
            self.broker.cancel_order(intent.order_ref)
        except Exception as exc:  # noqa: BLE001
            self.journal.commit(
                EventType.CALLBACK_FAILURE,
                {"where": "cancel_order", "detail": str(exc)},
                order_ref=intent.order_ref,
            )
            self._set_order_state(leg, OrderState.TERMINAL_UNRECONCILED, "cancel call failed")
            return False
        return True

    # ------------------------------------------------------------------
    # timers
    # ------------------------------------------------------------------

    def tick(self) -> None:
        try:
            self._tick_impl()
        except JournalUnavailable as exc:
            self._fail_closed_journal("tick", exc)
            raise

    def _tick_impl(self) -> None:
        """
        Called periodically. Two independent triggers live here.

        Reprice timeout is the COMMON path (order didn't fill), target change is
        the rare one. Conflating them is how unbounded chasing gets written.
        """
        now = self.clock.now()
        for leg in list(self.legs.values()):
            if leg.order_state is OrderState.IDLE and leg.desired_target is not None:
                self._evaluate(leg)

            if leg.order_state is OrderState.PENDING_ACK and leg.sent_at is not None:
                if (now - leg.sent_at).total_seconds() >= self.policy.ack_timeout_seconds:
                    self._set_order_state(
                        leg, OrderState.SUBMISSION_UNCERTAIN, "broker ack timeout"
                    )
                    self.alert(
                        "WARN",
                        f"ack timeout on {leg.symbol}; broker state must be reconciled",
                    )

            if leg.order_state is OrderState.WORKING and leg.live_intent is not None:
                if now >= leg.live_intent.valid_until:
                    self._request_cancel(leg, "target_expired")
                    continue

            if leg.order_state is OrderState.WORKING and leg.sent_at is not None:
                if (now - leg.sent_at).total_seconds() >= self.policy.order_timeout_seconds:
                    self.journal.commit(
                        EventType.REPRICE_TRIGGERED,
                        {"attempt": leg.attempt},
                        strategy_id=leg.strategy_id,
                        symbol=leg.symbol,
                    )
                    self._request_cancel(leg, "reprice_timeout")

            if leg.order_state is OrderState.PENDING_CANCEL and leg.cancel_requested_at:
                elapsed = (now - leg.cancel_requested_at).total_seconds()
                if elapsed >= self.policy.cancel_timeout_seconds:
                    # We do not re-send the cancel. The danger is not that the
                    # order is still live; it is that we do not know.
                    self._set_order_state(
                        leg, OrderState.TERMINAL_UNRECONCILED, "cancel timeout"
                    )
                    self.alert("WARN", f"cancel timeout on {leg.symbol}; reconciling")

        self._check_eod(now)

    def _check_eod(self, now: datetime) -> None:
        # Residual recording runs FIRST and unconditionally.
        #
        # It must fire when disconnected, when HALTED, and after the close --
        # exactly the situations where the earlier guards would have returned.
        # Without it, an unflattened position produces no durable explanation,
        # and tomorrow's reconciliation sees an UNKNOWN position and false-HALTs
        # on a residual we actually understand. Invariant 15 is only meaningful
        # if the explanation is written at the time.
        #
        # A silent overnight position is as dangerous as an unknown one, and
        # unlike an unknown one it is entirely preventable.
        self._record_eod_residual(now)

        if not self.calendar.should_start_flatten(now):
            return
        if self.operating_mode in (OperatingMode.HALTED,):
            return
        for leg in list(self.legs.values()):
            if leg.position == 0 and leg.order_state is OrderState.IDLE:
                continue
            if leg.flatten_reason is FlattenReason.EOD_FLATTEN:
                continue
            self.journal.commit(
                EventType.EOD_FLATTEN_STARTED,
                {"position": leg.position},
                strategy_id=leg.strategy_id,
                symbol=leg.symbol,
            )
            leg.flatten_reason = FlattenReason.EOD_FLATTEN
            self.set_mode(OperatingMode.FLATTEN_ONLY, "eod window")
            self.flatten(leg.strategy_id, leg.symbol, FlattenReason.EOD_FLATTEN)

        if self.calendar.past_escalation(now):
            for leg in self.legs.values():
                if (
                    leg.order_state in UNTRUSTWORTHY_ORDER_STATES
                    or self.sync_state is not SyncState.SYNCED
                ):
                    self.alert(
                        "CRITICAL",
                        f"past escalation deadline with unresolved state on {leg.symbol}: "
                        f"order_state={leg.order_state.value} sync={self.sync_state.value}. "
                        f"Human must verify broker state and decide on manual flatten.",
                    )

    def _record_eod_residual(self, now: datetime) -> None:
        """
        At the hard deadline, write down anything we failed to flatten.

        Once per session per leg. Fires regardless of link, sync or mode: the
        record is a local durable fact about our own intent and last known
        position, not something that requires the broker to be reachable.

        Tomorrow's boot reads this and comes up SYNCED + FLATTEN_ONLY: the
        system knows what it holds and does not pretend the incident did not
        happen.
        """
        plan = self.calendar.plan_for(now)
        if not plan.is_trading_day or plan.flatten_hard_utc is None:
            return
        if now < plan.flatten_hard_utc:
            return

        session = plan.session_date
        for (sid, sym), leg in list(self.legs.items()):
            if leg.position == 0 and leg.order_state is OrderState.IDLE:
                continue
            if (session, sid, sym) in self._eod_residual_recorded:
                continue
            self._eod_residual_recorded.add((session, sid, sym))
            reason = self._residual_reason()
            self.journal.commit(
                EventType.EOD_FLATTEN_FAILED,
                {
                    "symbol": sym,
                    "residual_quantity": leg.position,
                    "working_signed": leg.working_signed,
                    "potential_quantity": leg.position + leg.working_signed,
                    "order_state": leg.order_state.value,
                    "last_known_order": (
                        leg.live_intent.order_ref if leg.live_intent else None
                    ),
                    "failure_reason": reason,
                    "detected_at": now.isoformat(),
                    "session": session.isoformat(),
                },
                strategy_id=sid,
                symbol=sym,
            )
            # Recording a residual must never weaken a stronger safety mode.
            # HALTED remains HALTED; a human may use the separate emergency
            # tool after inspecting broker state.
            if self.operating_mode is not OperatingMode.HALTED:
                self.set_mode(OperatingMode.FLATTEN_ONLY, "eod residual recorded")
            self.alert(
                "CRITICAL",
                f"EOD flatten did not complete on {sym}: position {leg.position:+d}, "
                f"working {leg.working_signed:+d} "
                f"({reason}). Exposure may remain overnight. Verify in TWS.",
            )

    def _residual_reason(self) -> str:
        if self.link_state is not LinkState.CONNECTED:
            return f"link_{self.link_state.value.lower()}"
        if self.sync_state is not SyncState.SYNCED:
            return f"sync_{self.sync_state.value.lower()}"
        if self.operating_mode is OperatingMode.HALTED:
            return "halted"
        return "not_filled_by_deadline"

    def _maybe_complete_flatten(self, leg: SymbolState, source: str) -> bool:
        """Close the durable flatten lifecycle once broker exposure is flat."""
        reason = leg.flatten_reason
        if reason is None:
            return False
        if (
            leg.position != 0
            or leg.working_signed != 0
            or leg.order_state is not OrderState.IDLE
        ):
            return False
        if reason is FlattenReason.EOD_FLATTEN:
            self.journal.commit(
                EventType.EOD_FLATTEN_COMPLETED,
                {"symbol": leg.symbol, "source": source},
                strategy_id=leg.strategy_id,
                symbol=leg.symbol,
            )
        leg.flatten_reason = None
        return True

    def flatten(
        self, strategy_id: str, symbol: str, reason: FlattenReason = FlattenReason.MANUAL_FLATTEN
    ) -> bool:
        leg = self.leg(strategy_id, symbol)
        leg.flatten_reason = reason
        now = self.clock.now()
        target = TargetPosition(
            strategy_id=strategy_id,
            symbol=symbol,
            target_quantity=0,
            decision_id=f"flatten-{reason.value}-{now.isoformat()}",
            valid_until=now + timedelta(minutes=10),
            metadata={"flatten_reason": reason.value},
        )
        # Store the zero target BEFORE asking an existing order to cancel.  The
        # cancel callback must converge to zero, not accidentally re-evaluate
        # the stale pre-flatten target.  This also covers PENDING_ACK: once the
        # order becomes WORKING, on_working() sees the changed target and
        # requests the cancel.
        return self.submit_target(target)

    # ------------------------------------------------------------------
    # reconciliation: broker is the authority, journal is the explanation
    # ------------------------------------------------------------------

    def reconcile(self, *, evaluate_targets: bool = True) -> bool:
        try:
            return self._reconcile_impl(evaluate_targets=evaluate_targets)
        except JournalUnavailable as exc:
            self._fail_closed_journal("reconcile", exc)
            raise

    def _reconcile_impl(self, *, evaluate_targets: bool = True) -> bool:
        self.restore_from_journal()
        self.journal.commit(EventType.RECONCILIATION_STARTED, {})
        self._set_sync(SyncState.SYNCING, "reconcile begin")
        try:
            snap = self.broker.snapshot()
        except Exception as exc:  # noqa: BLE001
            self.journal.commit(EventType.RECONCILIATION_FAILED, {"detail": str(exc)})
            self._set_sync(SyncState.UNVERIFIED, "snapshot failed")
            return False

        if not snap.is_stable:
            self.journal.commit(
                EventType.RECONCILIATION_FAILED,
                {
                    "reason": "snapshot_not_stable",
                    "server_time": snap.server_time.isoformat(),
                },
            )
            self._set_sync(SyncState.UNVERIFIED, "broker snapshot barrier incomplete")
            return False

        all_intents = self._journal_intents_by_ref()
        open_intents = self._journal_open_intents_by_ref()
        known_perm_ids = {
            ev.perm_id
            for ev in self.journal.replay()
            if ev.perm_id is not None
            and ev.event_type in {EventType.BROKER_ACK_RECEIVED, EventType.ORDER_WORKING}
        }

        # Exact attribution only. A whitelisted prefix is not ownership: orderRef
        # is user controlled and may be forged or truncated.
        def order_owned(order) -> bool:
            intent = open_intents.get(order.order_ref)
            if intent is None:
                return False
            if intent.perm_id is not None and order.perm_id is not None:
                return intent.perm_id == order.perm_id
            return True

        def execution_owned(execution) -> bool:
            intent = all_intents.get(execution.order_ref)
            if intent is not None:
                if intent.perm_id is not None and execution.perm_id is not None:
                    return intent.perm_id == execution.perm_id
                return True
            return execution.perm_id is not None and execution.perm_id in known_perm_ids

        external_orders = [o for o in snap.open_orders if not order_owned(o)]
        external_execs = [
            ex
            for ex in snap.executions
            if ex.exec_id not in self._booked_execs and not execution_owned(ex)
        ]
        if external_orders or external_execs:
            self.journal.commit(
                EventType.EXTERNAL_ORDER_DETECTED,
                {
                    "orders": [o.order_ref for o in external_orders],
                    "executions": [e.exec_id for e in external_execs],
                },
            )
            self.journal.commit(
                EventType.RECONCILIATION_FAILED, {"reason": "external_broker_fact"}
            )
            self._set_sync(SyncState.UNVERIFIED, "external broker fact")
            self.halt(
                f"external order/execution present: orders={[o.order_ref for o in external_orders]} "
                f"executions={[e.exec_id for e in external_execs]}"
            )
            return False

        # Adopt broker-proven facts our journal is missing. A crash between the
        # broker filling and us writing it down is normal, not an anomaly.
        adopted = 0
        for ex in snap.executions:
            if ex.exec_id in self._booked_execs:
                continue
            if self._book_execution(ex, source="reconcile"):
                adopted += 1
        if adopted:
            self.journal.commit(EventType.BROKER_FACT_ADOPTED, {"executions": adopted})

        expected = self._expected_positions()
        actual = snap.positions
        symbols = set(expected) | set(actual)
        mismatch = {
            symbol: {"expected": expected.get(symbol, 0), "actual": actual.get(symbol, 0)}
            for symbol in symbols
            if expected.get(symbol, 0) != actual.get(symbol, 0)
        }
        if mismatch:
            self.journal.commit(
                EventType.RECONCILIATION_FAILED,
                {"reason": "position_mismatch", "detail": mismatch},
            )
            self._set_sync(SyncState.UNVERIFIED, "position mismatch")
            self.halt(f"unexplained position mismatch: {mismatch}")
            return False

        broker_open_refs = {o.order_ref for o in snap.open_orders}

        # Broker-proven absence is also a durable terminal fact. Without this,
        # an uncertain intent remains "open" in the journal forever and can be
        # accidentally re-attributed on every future reconciliation.
        for order_ref, intent in open_intents.items():
            if order_ref in broker_open_refs:
                continue
            filled = self._filled_for_intent(intent)
            if filled >= intent.quantity:
                self.journal.commit(
                    EventType.ORDER_FILLED,
                    {"quantity": intent.quantity, "source": "reconcile"},
                    order_ref=order_ref,
                    intent_id=intent.intent_id,
                )
            else:
                self.journal.commit(
                    EventType.ORDER_ABSENT_CONFIRMED,
                    {"filled": filled, "ordered": intent.quantity, "source": "reconcile"},
                    order_ref=order_ref,
                    intent_id=intent.intent_id,
                )

        # Rehydrate every broker-open order from its durable intent. Merely
        # restoring working_signed while leaving order_state=IDLE allows a new
        # opposite target to create a second live order after restart.
        for order in snap.open_orders:
            intent = open_intents[order.order_ref]
            if intent.perm_id is None or intent.broker_order_id is None:
                self.journal.commit(
                    EventType.BROKER_ACK_RECEIVED,
                    {"broker_order_id": order.broker_order_id, "source": "reconcile"},
                    order_ref=order.order_ref,
                    perm_id=order.perm_id,
                    intent_id=intent.intent_id,
                )
                self.journal.commit(
                    EventType.ORDER_WORKING,
                    {"source": "reconcile"},
                    order_ref=order.order_ref,
                    perm_id=order.perm_id,
                    intent_id=intent.intent_id,
                )
            intent.broker_order_id = order.broker_order_id
            intent.perm_id = order.perm_id
            leg = self.leg(intent.strategy_id, intent.symbol)
            leg.live_intent = intent
            leg.position = actual.get(intent.symbol, 0)
            leg.working_signed = order.signed_remaining
            leg.attempt = intent.attempt
            # Snapshot does not expose original submission time reliably. Start
            # a fresh bounded management window rather than leave it immortal.
            leg.sent_at = self.clock.now()
            self._set_order_state(leg, OrderState.WORKING, "rehydrated from broker snapshot")

        # Create legs for explained non-zero positions even when no order is open.
        for symbol, quantity in actual.items():
            if quantity != 0 and not any(sym == symbol for (_, sym) in self.legs):
                self.leg(self._default_strategy(), symbol)

        # Resolve remembered intents that are no longer open after the snapshot.
        for (_, symbol), leg in list(self.legs.items()):
            leg.position = actual.get(symbol, 0)
            if leg.live_intent is not None and leg.live_intent.order_ref not in broker_open_refs:
                leg.live_intent = None
                leg.working_signed = 0
                self._set_order_state(leg, OrderState.IDLE, "resolved by reconciliation")
            elif leg.live_intent is None:
                leg.working_signed = 0
                if leg.order_state in UNTRUSTWORTHY_ORDER_STATES:
                    self._set_order_state(leg, OrderState.IDLE, "resolved by reconciliation")
            self._maybe_complete_flatten(leg, "reconcile")

        self.journal.commit(
            EventType.RECONCILIATION_COMPLETED,
            {"positions": actual, "open_orders": len(snap.open_orders)},
        )
        self._set_sync(SyncState.SYNCED, "reconciled against broker")
        # A target may have changed while the link was unavailable. Reconcile
        # restores trust; only now may that latest desired target cancel or
        # replace the broker-open order. Expired targets are discarded by
        # _evaluate rather than replayed late.
        if evaluate_targets:
            for leg in list(self.legs.values()):
                if leg.desired_target is not None:
                    self._evaluate(leg)
        return True

    def _journal_intents_by_ref(self) -> dict[str, OrderIntent]:
        intents: dict[str, OrderIntent] = {}
        for event in self.journal.replay():
            if event.event_type is EventType.ORDER_INTENT_COMMITTED and event.order_ref:
                intents[event.order_ref] = OrderIntent.from_payload(event.payload)
            elif event.event_type in {
                EventType.BROKER_ACK_RECEIVED,
                EventType.ORDER_WORKING,
            } and event.order_ref:
                intent = intents.get(event.order_ref)
                if intent is not None:
                    if event.perm_id is not None:
                        intent.perm_id = event.perm_id
                    value = event.payload.get("broker_order_id")
                    if value is not None:
                        intent.broker_order_id = int(value)
        return intents

    def _journal_open_intents_by_ref(self) -> dict[str, OrderIntent]:
        intents = self._journal_intents_by_ref()
        # An intent becomes broker-relevant only after SEND_ATTEMPT_STARTED.
        # A crash after intent commit but before that event is provably unsent.
        open_refs: set[str] = set()
        for event in self.journal.replay():
            if not event.order_ref:
                continue
            if event.event_type is EventType.SEND_ATTEMPT_STARTED:
                open_refs.add(event.order_ref)
                continue
            terminal = event.event_type in {
                EventType.ORDER_FILLED,
                EventType.ORDER_CANCELLED,
                EventType.ORDER_REJECTED,
                EventType.ORDER_ABSENT_CONFIRMED,
            }
            if event.event_type is EventType.SEND_CALL_FAILED:
                terminal = event.payload.get("kind") == "rejected"
            if terminal:
                open_refs.discard(event.order_ref)
        return {ref: intents[ref] for ref in open_refs if ref in intents}

    def _is_ours(self, order_ref: str) -> bool:
        return bool(order_ref) and order_ref in self._journal_open_intents_by_ref()

    def _expected_positions(self) -> dict[str, int]:
        """
        Rebuilt from the event log, NOT from memory.

        The baseline for reconciliation is journal-expected, not zero. A legal
        overnight residual from a failed EOD flatten must reconcile as EXPLAINED,
        or every morning after an incident starts with a false HALT.
        """
        pos: dict[str, int] = {}
        for ev in self.journal.replay():
            if ev.event_type is EventType.EXECUTION_RECEIVED:
                p = ev.payload
                sym = p["symbol"]
                pos[sym] = pos.get(sym, 0) + int(p["signed_quantity"])
        return pos

    def _unacknowledged_halt(self, events: list) -> Optional[dict]:
        """
        The last HALT, unless a human has since acknowledged it.

        A HALT is a durable statement: *we found something we cannot explain,
        stop*. If a restart silently clears it, then restarting becomes a way to
        launder a HALT -- and the most likely restarter is a watchdog kill
        followed by an operator who has not read the log yet.

        The RUNBOOK already says "HALT is correct behaviour, do not restart to
        clear it", and ADR-004 makes restart manual for exactly this reason.
        Until now nothing enforced either. Documentation is not a control.
        """
        halt: Optional[dict] = None
        for ev in events:
            if (
                ev.event_type is EventType.OPERATING_MODE_CHANGED
                and ev.payload.get("to") == OperatingMode.HALTED.value
            ):
                halt = {
                    "seq": ev.seq,
                    "started_seq": ev.seq,
                    "why": ev.payload.get("why", ""),
                }
            elif ev.event_type is EventType.HALT_CAUSE_ADDED:
                started = halt["started_seq"] if halt is not None else ev.seq
                halt = {
                    "seq": ev.seq,
                    "started_seq": started,
                    "why": ev.payload.get("why", ""),
                }
            elif ev.event_type is EventType.HALT_ACKNOWLEDGED:
                acked = ev.payload.get("acknowledged_halt_seq")
                if halt is not None and acked is not None and int(acked) == halt["seq"]:
                    halt = None
        return halt

    def acknowledge_halt(self, operator: str, resolution: str) -> int:
        """
        Record an acknowledgement for the exact active HALT.

        This deliberately does *not* resume the running controller. Clearing a
        HALT changes only the durable boot decision; a human must then restart
        and the new process must reconcile broker truth before any write. That
        prevents an acknowledgement from becoming an in-process bypass around
        the recovery gate.
        """
        if not operator or not resolution:
            raise ValueError("acknowledging a HALT requires an operator and a resolution")
        halt = self._unacknowledged_halt(list(self.journal.replay()))
        if halt is None:
            raise ValueError("there is no unacknowledged HALT")
        seq = self.journal.acknowledge_halt(halt["seq"], operator, resolution)
        self.alert(
            "WARN",
            f"HALT seq {halt['seq']} acknowledged by {operator}; controller remains "
            "HALTED until a manual restart and successful reconciliation",
        )
        return seq

    def restore_from_journal(self) -> dict[str, int]:
        """Boot step 1: replay before touching the broker (invariant 10)."""
        if self._journal_restored:
            return dict(self._restored_positions)
        events = list(self.journal.replay())
        self.risk.restore_from_events(events)
        pos = self._expected_positions()
        residual = self._last_eod_residual()
        for sym, qty in pos.items():
            leg = self.leg(self._default_strategy(), sym)
            leg.position = qty
        for ev in events:
            if ev.event_type is EventType.EXECUTION_RECEIVED:
                self._booked_execs.add(ev.exec_id or ev.payload.get("exec_id", ""))
        overnight = self._overnight_expected_positions(pos)
        if residual or overnight:
            # Invariant 15: restore the folded durable mode in memory. Do not
            # append a fresh mode-change event on every restart; doing so would
            # manufacture a new HALT/residual cause and bury the original one.
            self.operating_mode = OperatingMode.FLATTEN_ONLY
            self.alert(
                "WARN",
                f"starting with known residual exposure {residual or overnight}",
            )

        # Invariant 22. Checked LAST so it outranks the residual path: a HALTED
        # system does not get downgraded to FLATTEN_ONLY by a restart either.
        halt = self._unacknowledged_halt(events)
        if halt is not None:
            self.operating_mode = OperatingMode.HALTED
            self.alert(
                "CRITICAL",
                f"starting HALTED: a prior HALT at seq {halt['seq']} was never "
                f"acknowledged ({halt['why']}). Diagnose the cause, then run "
                f"`python -m ib_execution.ack_halt`. Restarting does not clear it.",
            )
        self.journal.commit(
            EventType.PROCESS_STATE_RESTORED,
            {
                "operating_mode": self.operating_mode.value,
                "expected_positions": pos,
                "residual": residual or overnight,
                "active_halt_seq": halt["seq"] if halt is not None else None,
            },
        )
        self._restored_positions = dict(pos)
        self._journal_restored = True
        return pos

    def _last_eod_residual(self) -> dict[str, int]:
        residual: dict[str, int] = {}
        for ev in self.journal.replay():
            if ev.event_type is EventType.EOD_FLATTEN_FAILED:
                qty = int(ev.payload.get("residual_quantity", 0))
                working = int(ev.payload.get("working_signed", 0))
                # A flat last-known position with a live order is still an
                # overnight exposure. Preserve the potential fill direction.
                residual[ev.payload["symbol"]] = qty if qty != 0 else working
            elif ev.event_type is EventType.EOD_FLATTEN_COMPLETED:
                residual.pop(ev.payload.get("symbol", ""), None)
        return {k: v for k, v in residual.items() if v != 0}

    def _overnight_expected_positions(self, pos: dict[str, int]) -> dict[str, int]:
        """Infer an unrecorded carry if the last fill predates today's ET session."""
        nonzero = {sym: qty for sym, qty in pos.items() if qty != 0}
        if not nonzero:
            return {}
        current_session = self.clock.now().astimezone(ET).date()
        last_exec_date: dict[str, object] = {}
        for ev in self.journal.replay():
            if ev.event_type is EventType.EXECUTION_RECEIVED:
                sym = str(ev.payload.get("symbol", ev.symbol or ""))
                last_exec_date[sym] = ev.ts_utc.astimezone(ET).date()
        return {
            sym: qty
            for sym, qty in nonzero.items()
            if last_exec_date.get(sym) is None or last_exec_date[sym] < current_session
        }

    def _default_strategy(self) -> str:
        wl = self.risk.config.strategy_whitelist
        return wl[0] if wl else "unknown"

    def _known_broker_intent(
        self, order_ref: str, perm_id: Optional[int], fact: str
    ) -> Optional[OrderIntent]:
        intent = self._journal_intents_by_ref().get(order_ref)
        mismatch = (
            intent is not None
            and intent.perm_id is not None
            and perm_id is not None
            and intent.perm_id != perm_id
        )
        if intent is None or mismatch:
            self.journal.commit(
                EventType.EXTERNAL_ORDER_DETECTED,
                {"fact": fact, "order_ref": order_ref, "perm_id": perm_id},
                order_ref=order_ref or None,
                perm_id=perm_id,
            )
            self._set_sync(SyncState.UNVERIFIED, f"unknown broker callback: {fact}")
            self.halt(f"unknown broker callback {fact}: ref={order_ref!r} permId={perm_id}")
            return None
        return intent

    def _known_execution_id(self, exec_id: str) -> bool:
        if exec_id in self._booked_execs:
            return True
        return any(
            ev.event_type is EventType.EXECUTION_RECEIVED and ev.exec_id == exec_id
            for ev in self.journal.replay()
        )

    # ------------------------------------------------------------------
    # broker callbacks -- all fail-closed
    # ------------------------------------------------------------------

    def _guarded(self, name: str, fn: Callable[[], None]) -> None:
        """
        Every callback goes through here.

        An exception swallowed inside an event loop means a state transition was
        silently lost: the system looks alive and is not. Stopping is always
        better than continuing on a state we no longer believe.
        """
        try:
            fn()
        except InvariantViolation:
            raise
        except JournalUnavailable as exc:
            self._fail_closed_journal(name, exc)
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc(limit=6)
            try:
                self.journal.commit(
                    EventType.CALLBACK_FAILURE, {"callback": name, "detail": str(exc), "tb": tb}
                )
            except JournalUnavailable:
                pass
            self.halt(f"callback {name} raised: {exc}")

    def _fail_closed_journal(self, context: str, exc: BaseException) -> None:
        """
        Fence all future broker writes without trying to journal the failure.

        The journal is the unavailable component, so attempting another event
        here would either block again or create false confidence.  The in-memory
        state and out-of-band alert are the last reliable controls; the process
        supervisor/watchdog must then terminate the engine.
        """
        detail = f"{context}: {exc}"
        already_reported = self.journal_failure is not None
        self.journal_failure = detail
        self._fail_closed_runtime(
            f"journal unavailable; no further broker writes: {detail}",
            already_reported=already_reported,
        )

    def _fail_closed_runtime(self, detail: str, *, already_reported: bool = False) -> None:
        """Out-of-band fatal fence for components whose durable path is unavailable.

        The in-memory state below fences *this* process. It cannot fence the
        next one: the journal is the failed component, so no HALT reaches the
        disk, and a restart against repaired storage would replay a journal
        with no HALT in it and come back NORMAL. The durable fence is what
        carries the decision across the restart -- see fatal_fence.py.
        """
        self.fatal_shutdown_requested = True
        self.link_state = LinkState.DEGRADED
        self.sync_state = SyncState.UNVERIFIED
        self.operating_mode = OperatingMode.HALTED
        self._raise_durable_fence(detail)
        if not already_reported:
            self.alert(
                "CRITICAL",
                f"engine fenced HALTED; process shutdown required: {detail}",
            )

    def _witness_or_fence(self, seq: int, where: str) -> bool:
        """Pin the authorising event out of band, or refuse to write to the broker.

        Gate B1.6. ``commit()`` returning success is not the same as the event
        still being there after a crash -- WAL recovery discards frames whose
        checksums do not verify and reports nothing, leaving a shorter but
        internally consistent database. Without a witness the restarted engine
        can believe it never sent an order that is live at the broker.

        A witness that cannot be written is a hard stop, not a warning. Sending
        anyway would create precisely the untracked broker state this exists to
        prevent, and it is the one moment where "do nothing" is unambiguously
        safe: no order has been placed yet.
        """
        if self.witness is None:
            return True
        try:
            self.witness.record(self.journal, seq)
            return True
        except Exception as exc:  # noqa: BLE001 -- any failure here stops the send
            self._fail_closed_runtime(
                f"journal witness unavailable before {where}; refusing the broker "
                f"write: {exc}"
            )
            return False

    def _witness_safety_critical(self, seq: int, detail: str) -> None:
        """Pin a HALT out of band, and fence if that cannot be done.

        Asymmetric with the broker-write case, in two different ways.

        *Never undo the HALT.* A witness failure before a broker write means
        "do not send", which is unambiguously safe because nothing has been
        sent. After a HALT the opposite holds: the HALT already happened and is
        already durable, and reversing it to satisfy a bookkeeping file would
        be absurd. The engine stays HALTED.

        *But do fence.* Alerting alone left invariant 22 reachable again:

            HALT at seq 120 commits, witness update fails
            the witness still points at the last send, seq 100
            crash, WAL rollback to seq 110
            110 >= 100, so startup verification passes
            the HALT is gone -> NORMAL

        The fence is the thing that stops the *next* process, and it lives on a
        different volume from the journal, so a witness-specific failure very
        often leaves it writable. If even the fence cannot be written, both
        alerts fire and the engine is still HALTED -- that is the floor, and it
        is reported rather than papered over.
        """
        if self.witness is None:
            return
        try:
            self.witness.record(self.journal, seq)
        except Exception as exc:  # noqa: BLE001
            self.alert(
                "CRITICAL",
                f"could not witness {detail}; a WAL rollback could hide it from the "
                f"next start, so do not restart without reconciling: {exc}",
            )
            self._raise_durable_fence(
                f"safety-critical witness update failed after a durable {detail}: "
                f"{exc}. The next start must not be allowed to replay a journal "
                "that may no longer contain it."
            )

    def _raise_durable_fence(self, detail: str) -> None:
        """Persist the fence, or say plainly that it could not be persisted.

        A fence that failed to write is never reported as one. The process
        still exits non-zero with a CRITICAL alert, which is where this stood
        before the fence existed -- strictly no worse, and honest about it.
        """
        if self.fence is None:
            return
        try:
            self.fence.raise_fence(detail)
        except Exception as exc:  # noqa: BLE001 -- must not mask the original fault
            self.fence_write_failed = str(exc)
            self.alert(
                "CRITICAL",
                "engine fenced HALTED and the DURABLE FENCE COULD NOT BE WRITTEN "
                f"({exc}). A restart will NOT be blocked automatically. Do not "
                f"restart this engine until the account is reconciled by hand: {detail}",
            )

    def on_connected(self, connection_epoch: int) -> None:
        def _do() -> None:
            # Enforce the boot contract even if an application forgot to call
            # restore_from_journal explicitly. Broker connectivity may not
            # launder a durable HALT or residual.
            self.restore_from_journal()
            self.connection_epoch = connection_epoch
            self._set_link(LinkState.CONNECTED, f"epoch={connection_epoch}")
            # A socket coming back is not the account being trustworthy.
            self._set_sync(SyncState.UNVERIFIED, "reconnected; must reconcile")
        self._guarded("on_connected", _do)

    def on_disconnected(self, reason: str) -> None:
        def _do() -> None:
            self._set_link(LinkState.DISCONNECTED, reason)
            self._set_sync(SyncState.UNVERIFIED, "disconnected")
        self._guarded("on_disconnected", _do)

    def on_market_data_lost(self) -> None:
        def _do() -> None:
            self.quotes.clear()
            self._set_link(LinkState.DEGRADED, "market data subscriptions lost (IB 1101)")
            self._set_sync(SyncState.UNVERIFIED, "IB 1101 requires resubscription and reconcile")
        self._guarded("on_market_data_lost", _do)

    def on_market_data_restored(self) -> None:
        """Called only after every required subscription has been re-established."""
        def _do() -> None:
            self._set_link(LinkState.CONNECTED, "required market data subscriptions restored")
            self._set_sync(SyncState.UNVERIFIED, "market data restored; reconcile still required")
        self._guarded("on_market_data_restored", _do)

    def on_ack(self, order_ref: str, broker_order_id: int, perm_id: Optional[int]) -> None:
        def _do() -> None:
            if self._known_broker_intent(order_ref, perm_id, "ack") is None:
                return
            leg = self._leg_for_ref(order_ref)
            if leg is None or leg.live_intent is None:
                return  # delayed callback for a known, already-terminal intent
            leg.live_intent.perm_id = perm_id
            leg.live_intent.broker_order_id = broker_order_id
            self.journal.commit(
                EventType.BROKER_ACK_RECEIVED,
                {"broker_order_id": broker_order_id},
                order_ref=order_ref,
                perm_id=perm_id,
                intent_id=leg.live_intent.intent_id,
            )
            if leg.order_state in (OrderState.PENDING_ACK, OrderState.SUBMISSION_UNCERTAIN):
                self._set_order_state(leg, OrderState.PENDING_ACK, "acked")
        self._guarded("on_ack", _do)

    def on_working(self, order_ref: str, perm_id: Optional[int]) -> None:
        def _do() -> None:
            if self._known_broker_intent(order_ref, perm_id, "working") is None:
                return
            leg = self._leg_for_ref(order_ref)
            if leg is None:
                return
            if leg.live_intent is not None and perm_id is not None:
                leg.live_intent.perm_id = perm_id
            self.journal.commit(
                EventType.ORDER_WORKING, {}, order_ref=order_ref, perm_id=perm_id
            )
            if leg.order_state in (OrderState.PENDING_ACK, OrderState.SUBMISSION_UNCERTAIN):
                self._set_order_state(leg, OrderState.WORKING, "broker confirmed")
                # A single order callback is not an account-wide reconciliation.
                # In the normal path sync was already SYNCED and remains so. If
                # it was UNVERIFIED because of 1101/disconnect/send uncertainty,
                # only reconcile() may promote it back to SYNCED.
                if self.sync_state is SyncState.SYNCED:
                    self._maybe_cancel_for_new_target(leg)
        self._guarded("on_working", _do)

    def on_execution(self, execution: Execution) -> None:
        def _do() -> None:
            intent_record = self._known_broker_intent(
                execution.order_ref, execution.perm_id, "execution"
            )
            if intent_record is None:
                return
            live_leg = self._leg_for_ref(execution.order_ref)
            # Invariant 12 and the primary ledger event are one SQLite
            # transaction. A crash cannot consume execId without booking it.
            if not self._book_execution(execution, source="callback"):
                return
            leg = live_leg or self.leg(intent_record.strategy_id, intent_record.symbol)
            leg.position += execution.signed_quantity

            if live_leg is None:
                # The fill belongs to a durable intent but arrived after local
                # lifecycle state had already gone terminal (the classic
                # Cancelled-before-execDetails callback ordering). The journal
                # now knows the fill and memory reflects its quantity, but the
                # account view is not trusted until a broker snapshot confirms
                # all related order facts.
                self._set_order_state(
                    leg, OrderState.TERMINAL_UNRECONCILED, "execution after local terminal"
                )
                self.alert(
                    "WARN",
                    f"late execution for terminal intent {execution.order_ref}; reconciling",
                )
                return
            intent = leg.live_intent
            if intent is not None:
                filled = self._filled_for_intent(intent)
                remaining = intent.quantity - filled
                leg.working_signed = (
                    remaining if intent.side is Side.BUY else -remaining
                ) if remaining > 0 else 0
                if remaining <= 0:
                    self.journal.commit(
                        EventType.ORDER_FILLED,
                        {"quantity": intent.quantity},
                        order_ref=intent.order_ref,
                        intent_id=intent.intent_id,
                    )
                    if leg.order_state is OrderState.PENDING_CANCEL:
                        # The fill raced our cancel. We do not know whether the
                        # cancel also took effect, and we must not compute a
                        # reversal from a position we are guessing at. Reconcile.
                        leg.live_intent = None
                        leg.working_signed = 0
                        self._set_order_state(
                            leg, OrderState.TERMINAL_UNRECONCILED, "filled during cancel"
                        )
                        self.alert(
                            "WARN",
                            f"{leg.symbol} filled while cancel outstanding; reconciling",
                        )
                        return
                    leg.live_intent = None
                    leg.attempt = 0
                    self._set_order_state(leg, OrderState.IDLE, "fully filled")
                    self._after_terminal(leg)
        self._guarded("on_execution", _do)

    def _book_execution(self, ex: Execution, source: str) -> bool:
        etype = (
            EventType.EXECUTION_REVERSAL
            if ex.is_reversal
            else (EventType.EXECUTION_CORRECTED if ex.corrects_exec_id else EventType.EXECUTION_RECEIVED)
        )
        # Claiming execId and writing the primary position event are atomic.
        booked = self.journal.commit_execution_once(
            ex.exec_id,
            {
                "exec_id": ex.exec_id,
                "symbol": ex.symbol,
                "side": ex.side.value,
                "quantity": ex.quantity,
                "signed_quantity": ex.signed_quantity,
                "price": str(ex.price),
                "is_reversal": ex.is_reversal,
                "corrects_exec_id": ex.corrects_exec_id,
                "source": source,
            },
            symbol=ex.symbol,
            order_ref=ex.order_ref,
            perm_id=ex.perm_id,
        )
        if not booked:
            return False
        self._booked_execs.add(ex.exec_id)
        if etype is not EventType.EXECUTION_RECEIVED:
            self.journal.commit(
                etype,
                {"exec_id": ex.exec_id, "corrects": ex.corrects_exec_id},
                exec_id=ex.exec_id,
                order_ref=ex.order_ref,
            )
        # Fee is expected later, and that is a legal intermediate state.
        self._fees_pending.add(ex.exec_id)
        return True

    def _filled_for_intent(self, intent: OrderIntent) -> int:
        total = 0
        for ev in self.journal.replay():
            if ev.event_type is EventType.EXECUTION_RECEIVED and ev.order_ref == intent.order_ref:
                q = int(ev.payload["signed_quantity"])
                total += abs(q) if not ev.payload.get("is_reversal") else -abs(q)
        return total

    def on_fee(self, exec_id: str, commission: float, currency: str) -> None:
        def _do() -> None:
            if not self._known_execution_id(exec_id):
                self.journal.commit(
                    EventType.EXTERNAL_ORDER_DETECTED,
                    {"fact": "fee", "exec_id": exec_id},
                    exec_id=exec_id,
                )
                self._set_sync(SyncState.UNVERIFIED, "fee for unknown execution")
                self.halt(f"fee received for unknown execId {exec_id}")
                return
            self._fees_pending.discard(exec_id)
            self.journal.commit(
                EventType.FEE_RECEIVED,
                {"commission": commission, "currency": currency},
                exec_id=exec_id,
            )
        self._guarded("on_fee", _do)

    def on_cancelled(self, order_ref: str) -> None:
        def _do() -> None:
            if self._known_broker_intent(order_ref, None, "cancelled") is None:
                return
            leg = self._leg_for_ref(order_ref)
            if leg is None:
                return
            self.journal.commit(EventType.ORDER_CANCELLED, {}, order_ref=order_ref)
            intent = leg.live_intent
            if intent is not None:
                filled = self._filled_for_intent(intent)
                if 0 < filled < intent.quantity and self.clock.now() >= intent.valid_until:
                    self.journal.commit(
                        EventType.PARTIAL_EXPIRED,
                        {"filled": filled, "ordered": intent.quantity},
                        order_ref=order_ref,
                        intent_id=intent.intent_id,
                    )
            leg.live_intent = None
            leg.working_signed = 0
            cancel_reason = leg.cancel_reason or "unknown"
            leg.cancel_requested_at = None
            leg.cancel_reason = None
            # Repricing is the only cancellation that consumes a ladder rung.
            # A target change or flatten starts a fresh execution decision.
            if cancel_reason == "reprice_timeout":
                leg.attempt += 1
            else:
                leg.attempt = 0
            # A cancel callback is not enough evidence to size a replacement.
            # The broker may have filled in the cancel race window. Force a fresh
            # positions/orders/executions snapshot before evaluating the target.
            self._set_order_state(
                leg, OrderState.TERMINAL_UNRECONCILED, "cancel confirmed; snapshot required"
            )
            if not self.reconcile():
                self.alert(
                    "CRITICAL",
                    f"cancel confirmed for {order_ref} but reconciliation failed; no replacement sent",
                )
        self._guarded("on_cancelled", _do)

    def on_cancel_rejected(self, order_ref: str, reason: str) -> None:
        def _do() -> None:
            if self._known_broker_intent(order_ref, None, "cancel_rejected") is None:
                return
            leg = self._leg_for_ref(order_ref)
            if leg is None:
                return
            self.journal.commit(
                EventType.CANCEL_REJECTED, {"reason": reason},
                order_ref=order_ref,
            )
            # A cancel rejection does NOT terminate the order. It may still be
            # working or may already have filled. Keep the durable intent open
            # and re-pull the broker state; otherwise reconciliation would
            # misclassify a still-working order as external.
            leg.cancel_requested_at = None
            leg.cancel_reason = None
            self._set_order_state(leg, OrderState.TERMINAL_UNRECONCILED, "cancel rejected")
            # Re-read broker truth, but never auto-retry a rejected cancel. If
            # the order disappeared because it filled, the latest target may be
            # evaluated safely. If it remains working, human intervention is
            # required; a retry loop is itself a runaway failure mode.
            if not self.reconcile(evaluate_targets=False):
                return
            if leg.order_state is OrderState.IDLE:
                self._after_terminal(leg)
            else:
                self.halt(
                    f"cancel rejected and order remains live: {order_ref}; verify in TWS"
                )
        self._guarded("on_cancel_rejected", _do)

    def on_rejected(self, order_ref: str, reason: str) -> None:
        def _do() -> None:
            if self._known_broker_intent(order_ref, None, "rejected") is None:
                return
            leg = self._leg_for_ref(order_ref)
            if leg is None:
                return
            self.journal.commit(
                EventType.ORDER_REJECTED, {"reason": reason}, order_ref=order_ref
            )
            leg.live_intent = None
            leg.working_signed = 0
            leg.attempt += 1
            self._set_order_state(leg, OrderState.IDLE, "order rejected")
            if leg.desired_target is not None:
                self._miss(leg.desired_target, MissReason.BROKER_REJECTED, reason)
                leg.desired_target = None
        self._guarded("on_rejected", _do)

    def _after_terminal(self, leg: SymbolState) -> None:
        """One order finished. Re-evaluate against the latest desired target."""
        if leg.desired_target is None:
            self._maybe_complete_flatten(leg, "terminal")
            return
        if leg.desired_target.is_expired(self.clock.now()):
            self._miss(leg.desired_target, MissReason.EXPIRED)
            leg.desired_target = None
            self._maybe_complete_flatten(leg, "terminal_expired")
            return
        self._evaluate(leg)
        self._maybe_complete_flatten(leg, "terminal")

    def _leg_for_ref(self, order_ref: str) -> Optional[SymbolState]:
        # Exact durable identity only. A strategy prefix is user-controlled and
        # must never attribute an external callback to our live order.
        for leg in self.legs.values():
            if leg.live_intent is not None and leg.live_intent.order_ref == order_ref:
                return leg
        return None

    # ------------------------------------------------------------------
    # market data
    # ------------------------------------------------------------------

    def on_quote(self, quote: Quote) -> None:
        self.quotes[quote.symbol] = quote
        # One quote is not proof that all subscriptions are restored after 1101.
        # The adapter must explicitly call on_market_data_restored(), followed by
        # reconciliation, before trading is re-enabled.

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        return {
            "link_state": self.link_state.value,
            "sync_state": self.sync_state.value,
            "operating_mode": self.operating_mode.value,
            "connection_epoch": self.connection_epoch,
            "legs": {
                f"{sid}/{sym}": {
                    "order_state": leg.order_state.value,
                    "position": leg.position,
                    "working_signed": leg.working_signed,
                    "attempt": leg.attempt,
                    "desired_target": (
                        leg.desired_target.target_quantity if leg.desired_target else None
                    ),
                }
                for (sid, sym), leg in self.legs.items()
            },
            "fees_pending": len(self._fees_pending),
            "violations": [str(v) for v in self.violations],
            "fatal_shutdown_requested": self.fatal_shutdown_requested,
            "journal_failure": self.journal_failure,
            "risk": self.risk.snapshot(),
            "fsync": self.journal.fsync_stats(),
        }
