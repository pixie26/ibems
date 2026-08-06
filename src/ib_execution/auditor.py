"""
Offline journal auditor -- evidence for invariants that are observable in the journal.

IMPORTANT: Phase 0 does not yet audit all 22 invariants. SPEC 20 remains a gate,
not a completed claim. See docs/INVARIANT_COVERAGE.md.

SPEC 20 requires each invariant to exist three times:
  (a) property test      -- the code cannot violate it under generated input
  (b) runtime assertion  -- if it is violated, we fail closed immediately
  (c) journal auditor    -- THIS FILE: replay any real session and prove it held

(c) is the one that gets skipped, and the one that matters most. A passing test
suite proves the code was correct on the inputs someone imagined. Only the
auditor proves the system that actually ran, on the day it actually ran, obeyed
the spec. It is a required end-of-day artifact from Phase 1 onward.

Usage:
    python -m ib_execution.auditor journal.db
"""

from __future__ import annotations

import sys
from pathlib import Path
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from .journal import Journal, JournalEvent
from .models import EventType, TERMINAL_ORDER_EVENTS


@dataclass
class Finding:
    invariant: int
    seq: int
    detail: str

    def __str__(self) -> str:
        return f"[seq {self.seq}] INVARIANT {self.invariant}: {self.detail}"


class JournalAuditor:
    """Replays an event log and reports every invariant breach it can see."""

    def __init__(
        self,
        events: Iterable[JournalEvent],
        max_orders_per_minute: int = 4,
        max_orders_per_day: int = 50,
        max_daily_shares: int = 200,
        max_daily_notional: Decimal = Decimal("100000"),
    ):
        # Caps are passed in rather than read from the log: the auditor must
        # be able to check against the limits that were SUPPOSED to apply,
        # not the ones the running system believed applied.
        self.events = list(events)
        self.max_orders_per_minute = max_orders_per_minute
        self.max_orders_per_day = max_orders_per_day
        self.max_daily_shares = max_daily_shares
        self.max_daily_notional = max_daily_notional

    def audit(self) -> list[Finding]:
        f: list[Finding] = []
        f += self._i1_decision_once()
        f += self._i2_durable_before_send()
        f += self._i3_one_live_intent()
        f += self._i4_no_second_pending_ack()
        f += self._i5_no_second_pending_cancel()
        f += self._i10_reconcile_before_send()
        f += self._i11_no_expired_sends()
        f += self._i12_exec_once()
        f += self._i6_link_sync_gate()
        f += self._i7_i8_mode_gate()
        f += self._i14_external_fact_halts()
        f += self._i17_config_hash()
        f += self._i18_fail_closed()
        f += self._i9_flatten_after_working_resolved()
        f += self._i13_missing_fee_is_benign()
        f += self._i15_residual_boots_flatten_only()
        f += self._i16_runaway_caps_held()
        f += self._i22_halt_survives_restart()
        return sorted(f, key=lambda x: x.seq)

    # -- individual invariants -------------------------------------------

    def _i1_decision_once(self) -> list[Finding]:
        """Each decision_id accepted at most once."""
        seen: dict[str, int] = {}
        out = []
        for e in self.events:
            if e.event_type is EventType.TARGET_RECEIVED and e.decision_id:
                if e.decision_id in seen:
                    out.append(
                        Finding(1, e.seq, f"decision_id {e.decision_id} accepted twice "
                                          f"(first at seq {seen[e.decision_id]})")
                    )
                seen[e.decision_id] = e.seq
        return out

    def _i2_durable_before_send(self) -> list[Finding]:
        """Every send attempt preceded by a committed intent, in that order."""
        committed: set[str] = set()
        out = []
        for e in self.events:
            if e.event_type is EventType.ORDER_INTENT_COMMITTED and e.intent_id:
                committed.add(e.intent_id)
            elif e.event_type is EventType.SEND_ATTEMPT_STARTED:
                if not e.intent_id or e.intent_id not in committed:
                    out.append(
                        Finding(2, e.seq, f"send attempt for intent {e.intent_id} "
                                          f"with no prior committed intent")
                    )
        return out

    def _i3_one_live_intent(self) -> list[Finding]:
        """At most one unterminated intent per (strategy, symbol)."""
        live: dict[tuple, str] = {}
        out = []
        opens = {EventType.SEND_ATTEMPT_STARTED}
        closes = TERMINAL_ORDER_EVENTS
        by_ref_key: dict[str, tuple] = {}
        for e in self.events:
            if e.event_type is EventType.ORDER_INTENT_COMMITTED:
                key = (e.strategy_id, e.symbol)
                if e.order_ref:
                    by_ref_key[e.order_ref] = key
            if e.event_type in opens:
                key = (e.strategy_id, e.symbol)
                if key == (None, None) and e.order_ref:
                    key = by_ref_key.get(e.order_ref, key)
                if key in live and live[key] != e.intent_id:
                    out.append(
                        Finding(3, e.seq, f"second live intent {e.intent_id} for {key} "
                                          f"while {live[key]} still open")
                    )
                live[key] = e.intent_id or ""
            elif e.event_type in closes or (
                e.event_type is EventType.SEND_CALL_FAILED
                and e.payload.get("kind") == "rejected"
            ):
                key = by_ref_key.get(e.order_ref or "", (e.strategy_id, e.symbol))
                live.pop(key, None)
        return out

    def _i4_no_second_pending_ack(self) -> list[Finding]:
        """No second send while an earlier send is awaiting broker identity/state."""
        out: list[Finding] = []
        pending: set[str] = set()
        for e in self.events:
            if e.event_type is EventType.SEND_ATTEMPT_STARTED and e.intent_id:
                if pending:
                    out.append(
                        Finding(4, e.seq, f"send while pending ack exists: {sorted(pending)}")
                    )
                pending.add(e.intent_id)
            elif e.event_type in (
                TERMINAL_ORDER_EVENTS
                | {EventType.BROKER_ACK_RECEIVED, EventType.ORDER_WORKING}
            ) and e.intent_id:
                pending.discard(e.intent_id)
            elif e.event_type is EventType.SEND_CALL_FAILED and e.intent_id:
                # Clean rejection is terminal. Uncertain delivery remains pending
                # until reconciliation proves the broker state.
                if e.payload.get("kind") == "rejected":
                    pending.discard(e.intent_id)
            elif e.event_type is EventType.RECONCILIATION_COMPLETED:
                pending.clear()
        return out

    def _i5_no_second_pending_cancel(self) -> list[Finding]:
        """No new send between CANCEL_REQUESTED and terminal reconciliation."""
        out = []
        pending_cancel: set[str] = set()
        for e in self.events:
            if e.event_type is EventType.CANCEL_REQUESTED and e.order_ref:
                pending_cancel.add(e.order_ref)
            elif e.event_type in TERMINAL_ORDER_EVENTS and e.order_ref:
                pending_cancel.discard(e.order_ref)
            elif e.event_type is EventType.RECONCILIATION_COMPLETED:
                pending_cancel.clear()
            elif e.event_type is EventType.SEND_ATTEMPT_STARTED and pending_cancel:
                out.append(
                    Finding(5, e.seq, f"order sent while cancel outstanding on "
                                      f"{sorted(pending_cancel)}")
                )
        return out

    def _i10_reconcile_before_send(self) -> list[Finding]:
        """After PROCESS_STARTED, no send before a completed reconciliation."""
        out = []
        armed = False
        reconciled = False
        for e in self.events:
            if e.event_type is EventType.PROCESS_STARTED:
                armed, reconciled = True, False
            elif e.event_type is EventType.RECONCILIATION_COMPLETED:
                reconciled = True
            elif e.event_type is EventType.SEND_ATTEMPT_STARTED and armed and not reconciled:
                out.append(
                    Finding(10, e.seq, "send after process start with no completed reconciliation")
                )
        return out

    def _i11_no_expired_sends(self) -> list[Finding]:
        """A send must not occur after the target's valid_until."""
        out = []
        for e in self.events:
            if e.event_type is EventType.ORDER_INTENT_COMMITTED:
                vu = e.payload.get("valid_until")
                if not vu:
                    continue
                try:
                    valid_until = datetime.fromisoformat(vu)
                except ValueError:
                    continue
                if e.ts_utc >= valid_until:
                    out.append(
                        Finding(11, e.seq, f"intent committed at {e.ts_utc.isoformat()} "
                                           f"after valid_until {vu}")
                    )
        return out

    def _i12_exec_once(self) -> list[Finding]:
        """Each raw execId booked at most once; corrections are new events."""
        out = []
        seen: dict[str, int] = {}
        for e in self.events:
            if e.event_type is EventType.EXECUTION_RECEIVED and e.exec_id:
                if e.exec_id in seen:
                    out.append(
                        Finding(12, e.seq, f"execId {e.exec_id} booked twice "
                                           f"(first seq {seen[e.exec_id]})")
                    )
                seen[e.exec_id] = e.seq
        return out

    def _i6_link_sync_gate(self) -> list[Finding]:
        """No broker write while link/sync is untrusted."""
        out = []
        link = "DISCONNECTED"
        sync = "UNVERIFIED"
        for e in self.events:
            if e.event_type is EventType.LINK_STATE_CHANGED:
                link = e.payload.get("to", link)
            elif e.event_type is EventType.SYNC_STATE_CHANGED:
                sync = e.payload.get("to", sync)
            elif e.event_type in {EventType.SEND_ATTEMPT_STARTED, EventType.CANCEL_REQUESTED}:
                if link != "CONNECTED" or sync != "SYNCED":
                    out.append(
                        Finding(6, e.seq, f"broker write with link={link} sync={sync}")
                    )
        return out

    def _i7_i8_mode_gate(self) -> list[Finding]:
        """Opening requires NORMAL; FLATTEN_ONLY permits only target zero."""
        out: list[Finding] = []
        mode = "NORMAL"
        intents: dict[str, dict] = {}
        for e in self.events:
            if e.event_type is EventType.OPERATING_MODE_CHANGED:
                mode = e.payload.get("to", mode)
            elif e.event_type is EventType.PROCESS_STATE_RESTORED:
                mode = e.payload.get("operating_mode", mode)
            elif e.event_type is EventType.ORDER_INTENT_COMMITTED and e.intent_id:
                intents[e.intent_id] = e.payload
            elif e.event_type is EventType.SEND_ATTEMPT_STARTED and e.intent_id:
                p = intents.get(e.intent_id, {})
                target = int(p.get("target_quantity", 0))
                position = int(p.get("position_snapshot", 0))
                closing = abs(target) <= abs(position) and (
                    target == 0 or target * position >= 0
                )
                if not closing and mode != "NORMAL":
                    out.append(Finding(7, e.seq, f"risk-increasing send in mode={mode}"))
                if mode == "FLATTEN_ONLY" and target != 0:
                    out.append(Finding(8, e.seq, f"target={target} sent in FLATTEN_ONLY"))
                if mode == "HALTED":
                    out.append(Finding(7, e.seq, "broker send in HALTED mode"))
        return out

    def _i14_external_fact_halts(self) -> list[Finding]:
        """An external broker fact must arm a halt before any later send."""
        out: list[Finding] = []
        armed: int | None = None
        for e in self.events:
            if e.event_type is EventType.EXTERNAL_ORDER_DETECTED:
                armed = e.seq
            elif armed is not None:
                if (
                    (e.event_type is EventType.OPERATING_MODE_CHANGED
                     and e.payload.get("to") == "HALTED")
                    or e.event_type is EventType.HALT_CAUSE_ADDED
                ):
                    armed = None
                elif e.event_type is EventType.SEND_ATTEMPT_STARTED:
                    out.append(
                        Finding(14, e.seq, f"send after external fact at seq {armed} before HALT")
                    )
                    armed = None
        return out

    def _i17_config_hash(self) -> list[Finding]:
        """Every intent records the risk config it was approved under."""
        out = []
        for e in self.events:
            if e.event_type is EventType.ORDER_INTENT_COMMITTED:
                if not e.payload.get("risk_config_hash"):
                    out.append(Finding(17, e.seq, "intent missing risk_config_hash"))
        return out

    def _i18_fail_closed(self) -> list[Finding]:
        """A callback failure must be followed by HALT, with no send in between."""
        out = []
        armed_seq: int | None = None
        for e in self.events:
            if e.event_type is EventType.CALLBACK_FAILURE:
                armed_seq = e.seq
            elif armed_seq is not None:
                if (
                    (e.event_type is EventType.OPERATING_MODE_CHANGED
                     and e.payload.get("to") == "HALTED")
                    or e.event_type is EventType.HALT_CAUSE_ADDED
                ):
                    armed_seq = None
                elif e.event_type is EventType.SEND_ATTEMPT_STARTED:
                    out.append(
                        Finding(18, e.seq, f"send after callback failure at seq {armed_seq} "
                                           f"with no intervening HALT")
                    )
                    armed_seq = None
        return out

    # -- invariants added in v0.1.2 ---------------------------------------

    def _i9_flatten_after_working_resolved(self) -> list[Finding]:
        """
        A flatten intent must not be committed while an order is still working.

        Flatten is the moment people cut corners: the position is wrong, the
        instinct is to send the closing order immediately. Doing so while the
        old order is live is how you end up short twice.
        """
        out: list[Finding] = []
        working: dict[str, tuple[str | None, str | None]] = {}
        ref_key: dict[str, tuple[str | None, str | None]] = {}
        for e in self.events:
            if e.event_type is EventType.ORDER_INTENT_COMMITTED and e.order_ref:
                ref_key[e.order_ref] = (e.strategy_id, e.symbol)
            if e.event_type is EventType.ORDER_WORKING and e.order_ref:
                working[e.order_ref] = ref_key.get(e.order_ref, (e.strategy_id, e.symbol))
            elif e.event_type in TERMINAL_ORDER_EVENTS and e.order_ref:
                working.pop(e.order_ref, None)
            elif e.event_type is EventType.ORDER_INTENT_COMMITTED:
                if e.payload.get("target_quantity") == 0 and working:
                    key = (e.strategy_id, e.symbol)
                    others = {
                        ref
                        for ref, working_key in working.items()
                        if ref != e.order_ref and working_key == key
                    }
                    if others:
                        out.append(
                            Finding(9, e.seq, f"flatten intent committed while "
                                              f"{sorted(others)} still working")
                        )
        return out

    def _i13_missing_fee_is_benign(self) -> list[Finding]:
        """
        A fee that has not arrived is a legal intermediate state.

        Commission arrives separately from the execution and can be much later.
        Treating that as an error would HALT the system on a routine event, so
        we assert the opposite direction: no HALT may be attributed to it.
        """
        out: list[Finding] = []
        for e in self.events:
            if (
                (e.event_type is EventType.OPERATING_MODE_CHANGED
                 and e.payload.get("to") == "HALTED")
                or e.event_type is EventType.HALT_CAUSE_ADDED
            ):
                why = str(e.payload.get("why", "")).lower()
                if re.search(r"\b(?:fee|commission)\b", why):
                    out.append(
                        Finding(13, e.seq, f"HALT attributed to a fee condition: {why}")
                    )
        return out

    def _i15_residual_boots_flatten_only(self) -> list[Finding]:
        """
        An explained residual must come up FLATTEN_ONLY, never as an opening
        session.

        The failure this prevents is subtle: a residual that is *recorded* but
        not *acted on* looks healthy in the log and quietly resumes trading on
        top of an unintended position.
        """
        out: list[Finding] = []
        residual: dict[str, int] = {}
        armed = False
        mode = "NORMAL"
        for e in self.events:
            if e.event_type is EventType.EOD_FLATTEN_FAILED:
                sym = e.payload.get("symbol", e.symbol or "?")
                quantity = int(e.payload.get("residual_quantity", 0))
                working = int(e.payload.get("working_signed", 0))
                residual[sym] = quantity if quantity != 0 else working
            elif e.event_type is EventType.EOD_FLATTEN_COMPLETED:
                residual.pop(e.payload.get("symbol", e.symbol or "?"), None)
            elif e.event_type is EventType.PROCESS_STARTED:
                armed = any(v != 0 for v in residual.values())
                mode = "NORMAL"
            elif e.event_type is EventType.OPERATING_MODE_CHANGED:
                mode = e.payload.get("to", mode)
            elif e.event_type is EventType.PROCESS_STATE_RESTORED:
                mode = e.payload.get("operating_mode", mode)
            elif e.event_type is EventType.ORDER_INTENT_COMMITTED and armed:
                if mode not in ("FLATTEN_ONLY", "HALTED"):
                    out.append(
                        Finding(15, e.seq, f"order committed in mode {mode} while a "
                                           f"recorded residual {residual} was outstanding")
                    )
                    armed = False
        return out

    def _i16_runaway_caps_held(self) -> list[Finding]:
        """
        The runaway breaker, proven offline.

        This is the single most important control in the system -- the disaster
        case for an automated trader is not one bad order, it is a loop emitting
        ten thousand -- and until now it had no offline proof at all. Recompute
        the counters straight from send attempts and check the caps.

        Counts EVERY submission attempt, including ones the broker cleanly
        rejected, because a reject loop is exactly the runaway we are guarding
        against.
        """
        out: list[Finding] = []
        by_day_orders: dict[str, int] = {}
        by_day_shares: dict[str, int] = {}
        by_day_notional: dict[str, Decimal] = {}
        stamps: list[datetime] = []
        for e in self.events:
            if e.event_type is not EventType.SEND_ATTEMPT_STARTED:
                continue
            day = e.ts_utc.date().isoformat()
            by_day_orders[day] = by_day_orders.get(day, 0) + 1
            qty = abs(int(e.payload.get("quantity", 0)))
            price_raw = e.payload.get("price")
            price = Decimal(str(price_raw)) if price_raw not in (None, "None") else Decimal(0)
            by_day_shares[day] = by_day_shares.get(day, 0) + qty
            by_day_notional[day] = by_day_notional.get(day, Decimal(0)) + price * qty
            if by_day_orders[day] > self.max_orders_per_day:
                out.append(
                    Finding(16, e.seq, f"{by_day_orders[day]} send attempts on {day}, "
                                       f"cap {self.max_orders_per_day}")
                )
            if by_day_shares[day] > self.max_daily_shares:
                out.append(
                    Finding(16, e.seq, f"{by_day_shares[day]} submitted shares on {day}, "
                                       f"cap {self.max_daily_shares}")
                )
            if by_day_notional[day] > self.max_daily_notional:
                out.append(
                    Finding(16, e.seq, f"submitted notional {by_day_notional[day]} on {day}, "
                                       f"cap {self.max_daily_notional}")
                )
            stamps.append(e.ts_utc)
            window = [t for t in stamps if (e.ts_utc - t).total_seconds() < 60]
            if len(window) > self.max_orders_per_minute:
                out.append(
                    Finding(16, e.seq, f"{len(window)} send attempts within 60s, "
                                       f"cap {self.max_orders_per_minute}")
                )
        return out

    def _i22_halt_survives_restart(self) -> list[Finding]:
        """
        A restart must not launder a HALT.

        The mode fold deliberately does NOT reset on PROCESS_STARTED. That is
        the whole point: if the engine comes up NORMAL after an unacknowledged
        HALT, restarting has become a way to bypass a safety stop.

        Only HALT_ACKNOWLEDGED -- which carries a named operator and a written
        resolution -- clears it.
        """
        out: list[Finding] = []
        halt_seq: int | None = None
        for e in self.events:
            if (
                e.event_type is EventType.OPERATING_MODE_CHANGED
                and e.payload.get("to") == "HALTED"
            ):
                halt_seq = e.seq
            elif e.event_type is EventType.HALT_CAUSE_ADDED:
                halt_seq = e.seq
            elif e.event_type is EventType.HALT_ACKNOWLEDGED:
                acked = e.payload.get("acknowledged_halt_seq")
                if halt_seq is not None and acked is not None and int(acked) == halt_seq:
                    halt_seq = None
            elif halt_seq is not None:
                if (
                    e.event_type is EventType.OPERATING_MODE_CHANGED
                    and e.payload.get("to") in ("NORMAL", "STOP_NEW", "FLATTEN_ONLY")
                ):
                    out.append(
                        Finding(22, e.seq, f"mode left HALTED (-> {e.payload.get('to')}) "
                                           f"with the HALT at seq {halt_seq} never acknowledged")
                    )
                    halt_seq = None
        return out

    # -- reporting --------------------------------------------------------

    def summary(self) -> dict:
        counts: dict[str, int] = defaultdict(int)
        for e in self.events:
            counts[e.event_type.value] += 1
        misses: dict[str, int] = defaultdict(int)
        for e in self.events:
            if e.event_type is EventType.DECISION_MISSED:
                misses[e.payload.get("reason", "?")] += 1
        return {
            "events": len(self.events),
            "by_type": dict(sorted(counts.items())),
            "decision_misses": dict(misses),
            "audited_invariants": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 22],
            # 19 (overnight sizing), 20 (three-way coverage) and 21 (self-test)
            # are structural/config properties. They cannot be proven from an
            # event log and are enforced at config-validation and startup instead.
            "not_fully_audited": [19, 20, 21],
        }


def audit_file(path: str) -> int:
    db = Path(path)
    if not db.exists() or not db.is_file():
        print(f"FAIL: journal does not exist: {db}")
        return 2
    j = Journal(db)
    try:
        auditor = JournalAuditor(j.replay())
        findings = auditor.audit()
        summary = auditor.summary()
    finally:
        j.close()

    print(f"events: {summary['events']}")
    print(f"audited invariants: {summary['audited_invariants']}")
    print(f"not fully audited: {summary['not_fully_audited']}")
    if summary["events"] == 0:
        print("FAIL: empty journal is not evidence of a safe session")
        return 2
    if summary["decision_misses"]:
        # This is the availability term of the cost model. It belongs in the
        # daily report, not buried in the log.
        print("decision misses (cost model availability term):")
        for k, v in summary["decision_misses"].items():
            print(f"  {k}: {v}")
    if not findings:
        print("PASS (partial coverage): no violations found in audited invariants")
        return 0
    print(f"FAIL: {len(findings)} invariant violation(s)")
    for f in findings:
        print(f"  {f}")
    return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m ib_execution.auditor <journal.db>")
        raise SystemExit(2)
    raise SystemExit(audit_file(sys.argv[1]))
