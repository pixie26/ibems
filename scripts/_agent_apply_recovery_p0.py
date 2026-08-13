from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one match, found {count}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# market_liveness.py: explicit recovery hints + veto-only transport evidence
# ---------------------------------------------------------------------------

replace_once(
    "src/ib_execution/market_liveness.py",
    'CONNECTIVITY_RESTORED_DATA_KEPT = 1102\n',
    'CONNECTIVITY_RESTORED_DATA_KEPT = 1102\nREALTIME_BARS_RESET = 10225\n',
)

replace_once(
    "src/ib_execution/market_liveness.py",
    '''class LivenessIncidentKind(Enum):\n    """Audit classification for a sustained non-normal liveness state."""\n\n    FEED_OUTAGE = "FEED_OUTAGE"\n    EXPECTED_SILENCE = "EXPECTED_SILENCE"\n    GAP_SUSPECTED = "GAP_SUSPECTED"\n\n\n@dataclass(frozen=True)\nclass LivenessState:\n''',
    '''class LivenessIncidentKind(Enum):\n    """Audit classification for a sustained non-normal liveness state."""\n\n    FEED_OUTAGE = "FEED_OUTAGE"\n    EXPECTED_SILENCE = "EXPECTED_SILENCE"\n    GAP_SUSPECTED = "GAP_SUSPECTED"\n\n\nclass RecoveryHint(Enum):\n    """An explicit IB instruction about the smallest subscription repair."""\n\n    BARS_ONLY = "bars_only"\n    ALL_MARKET_STREAMS = "all_market_streams"\n\n\n@dataclass(frozen=True)\nclass LivenessState:\n''',
)

replace_once(
    "src/ib_execution/market_liveness.py",
    '''    incident_kind: LivenessIncidentKind | None = None\n    heartbeat_last_mono: float | None = None\n\n    def as_marker(self) -> str:\n''',
    '''    incident_kind: LivenessIncidentKind | None = None\n    heartbeat_last_mono: float | None = None\n    recovery_hint: RecoveryHint | None = None\n\n    def as_marker(self) -> str:\n''',
)

replace_once(
    "src/ib_execution/market_liveness.py",
    '''        if self.expected_silence:\n            parts.append(f"expected_silence={self.expected_silence}")\n        for stream, age in sorted(self.advisory_ages.items()):\n''',
    '''        if self.expected_silence:\n            parts.append(f"expected_silence={self.expected_silence}")\n        if self.recovery_hint is not None:\n            parts.append(f"recovery_hint={self.recovery_hint.value}")\n        for stream, age in sorted(self.advisory_ages.items()):\n''',
)

replace_once(
    "src/ib_execution/market_liveness.py",
    '''        self._calendar_silence: Optional[str] = None\n        self._pending_recover: tuple[LivenessIncidentKind, str] | None = None\n''',
    '''        self._calendar_silence: Optional[str] = None\n        self._pending_recover: tuple[\n            LivenessIncidentKind, str, RecoveryHint | None\n        ] | None = None\n        # Veto-only transport evidence.  True means the current connection has\n        # not crossed ib_async's transport-idle boundary since the last inbound\n        # activity.  It can prevent a destructive socket reconnect, but it can\n        # never initiate recovery by itself.\n        self._transport_evidence = False\n''',
)

replace_once(
    "src/ib_execution/market_liveness.py",
    '''    def subscription_started(self, now_mono: Optional[float] = None) -> None:\n        """Reset the clock origin. Before this there is nothing to judge."""\n        self._started_mono = self._now(now_mono)\n        self._last_event_mono.clear()\n        self._pending_recover = None\n\n    def note_event(self, stream: str, now_mono: Optional[float] = None) -> None:\n        self._last_event_mono[stream] = self._now(now_mono)\n''',
    '''    def subscription_started(self, now_mono: Optional[float] = None) -> None:\n        """Reset the clock origin. Before this there is nothing to judge."""\n        self._started_mono = self._now(now_mono)\n        self._last_event_mono.clear()\n        self._pending_recover = None\n        # A successful subscribe handshake is positive evidence that the peer\n        # is reachable.  If it subsequently goes completely silent, the\n        # transport watchdog revokes this after its own bounded timeout.\n        self._transport_evidence = True\n\n    def note_event(self, stream: str, now_mono: Optional[float] = None) -> None:\n        self._last_event_mono[stream] = self._now(now_mono)\n        self._transport_evidence = True\n\n    def note_transport_activity(self) -> None:\n        """Record inbound protocol activity without treating it as market cadence."""\n        self._transport_evidence = True\n\n    def transport_evidence(self) -> bool:\n        """Whether transport is known alive; veto-only, never a recovery trigger."""\n        return self._transport_evidence\n''',
)

old_note_status = '''    def note_status(self, code: int, message: str = "") -> None:\n        """Classify an IB error/status code into liveness facts."""\n        if code == CONNECTIVITY_LOST:\n            # Told, not inferred. Reconnecting before IB says it is back\n            # just burns the reconnect budget against a known outage.\n            self._outages[code] = f"connectivity lost ({code})"\n        elif code == CONNECTIVITY_RESTORED_DATA_LOST:\n            self._outages.pop(CONNECTIVITY_LOST, None)\n            self._pending_recover = (\n                LivenessIncidentKind.FEED_OUTAGE,\n                f"connectivity restored, market data lost ({code})",\n            )\n        elif code == CONNECTIVITY_RESTORED_DATA_KEPT:\n            self._outages.pop(CONNECTIVITY_LOST, None)\n        elif code in FARM_BROKEN_CODES:\n            self._outages[code] = f"data farm down ({code}): {message}".strip()\n        elif code in FARM_OK_CODES:\n            # 2104 clears 2103, 2106 clears 2105, 2158 clears 2157.\n            self._outages.pop(code - 1, None)\n        elif code == FARM_INACTIVE_CODE:\n            # Explicitly not an outage. Recorded by the caller, ignored here.\n            return\n'''
new_note_status = '''    def note_status(self, code: int, message: str = "") -> None:\n        """Classify an IB error/status code into liveness facts."""\n        # The callback itself proves TWS is talking.  This is deliberately\n        # weaker than a healthy market stream: it may veto socket destruction,\n        # but explicit outage/recovery semantics below still decide what to do.\n        self._transport_evidence = True\n        if code == CONNECTIVITY_LOST:\n            # Told, not inferred. Reconnecting before IB says it is back\n            # just burns the reconnect budget against a known outage.\n            self._outages[code] = f"connectivity lost ({code})"\n        elif code == CONNECTIVITY_RESTORED_DATA_LOST:\n            self._outages.pop(CONNECTIVITY_LOST, None)\n            self._pending_recover = (\n                LivenessIncidentKind.FEED_OUTAGE,\n                f"connectivity restored, market data lost ({code})",\n                RecoveryHint.ALL_MARKET_STREAMS,\n            )\n        elif code == CONNECTIVITY_RESTORED_DATA_KEPT:\n            self._outages.pop(CONNECTIVITY_LOST, None)\n        elif code == REALTIME_BARS_RESET:\n            self._pending_recover = (\n                LivenessIncidentKind.GAP_SUSPECTED,\n                f"real-time bars reset by IB ({code})",\n                RecoveryHint.BARS_ONLY,\n            )\n        elif code in FARM_BROKEN_CODES:\n            self._outages[code] = f"data farm down ({code}): {message}".strip()\n        elif code in FARM_OK_CODES:\n            # 2104 clears 2103, 2106 clears 2105, 2158 clears 2157.\n            self._outages.pop(code - 1, None)\n        elif code == FARM_INACTIVE_CODE:\n            # Explicitly not an outage. Recorded by the caller, ignored here.\n            return\n'''
replace_once("src/ib_execution/market_liveness.py", old_note_status, new_note_status)

replace_once(
    "src/ib_execution/market_liveness.py",
    '''    def note_transport_idle(self, idle_seconds: float) -> None:\n        """ib_async ``timeoutEvent``: nothing at all arrived from TWS.\n\n        Stronger than any per-stream gap, because it does not depend on\n        market activity -- TWS keeps talking even when the tape does not.\n        """\n        self._pending_recover = (\n            LivenessIncidentKind.GAP_SUSPECTED,\n            f"no data of any kind from TWS for {idle_seconds:.1f}s",\n        )\n''',
    '''    def note_transport_idle(self, idle_seconds: float) -> None:\n        """ib_async ``timeoutEvent``: nothing at all arrived from TWS.\n\n        Stronger than any per-stream gap, because it does not depend on\n        market activity -- TWS keeps talking even when the tape does not.\n        """\n        self._transport_evidence = False\n        self._pending_recover = (\n            LivenessIncidentKind.GAP_SUSPECTED,\n            f"no data of any kind from TWS for {idle_seconds:.1f}s",\n            None,\n        )\n''',
)

replace_once(
    "src/ib_execution/market_liveness.py",
    '''        if self._pending_recover is not None:\n            incident_kind, reason = self._pending_recover\n            self._pending_recover = None\n            return LivenessState(\n                action=LivenessAction.RECOVER_SUBSCRIPTION,\n                reason=reason,\n                heartbeat_age=age,\n                advisory_ages=advisory,\n                incident_kind=incident_kind,\n                heartbeat_last_mono=heartbeat_last_mono,\n            )\n''',
    '''        if self._pending_recover is not None:\n            incident_kind, reason, recovery_hint = self._pending_recover\n            self._pending_recover = None\n            return LivenessState(\n                action=LivenessAction.RECOVER_SUBSCRIPTION,\n                reason=reason,\n                heartbeat_age=age,\n                advisory_ages=advisory,\n                incident_kind=incident_kind,\n                heartbeat_last_mono=heartbeat_last_mono,\n                recovery_hint=recovery_hint,\n            )\n''',
)

replace_once(
    "src/ib_execution/market_liveness.py",
    '''            "heartbeat_losses": self.heartbeat_losses,\n        }\n''',
    '''            "heartbeat_losses": self.heartbeat_losses,\n            "transport_evidence": self._transport_evidence,\n        }\n''',
)


# ---------------------------------------------------------------------------
# quote_recorder.py: one recovery pipeline, positive-bar reset, smallest repair
# ---------------------------------------------------------------------------

replace_once(
    "src/ib_execution/quote_recorder.py",
    '''from .market_liveness import (\n    LivenessAction,\n    LivenessIncidentTracker,\n    MarketLiveness,\n)\n''',
    '''from .market_liveness import (\n    LivenessAction,\n    LivenessIncidentTracker,\n    MarketLiveness,\n    RecoveryHint,\n)\n''',
)

replace_once(
    "src/ib_execution/quote_recorder.py",
    '''class RecoveryPlan(Enum):\n    """The smallest repair that could fix what is actually broken."""\n\n    NONE = "none"\n    #: Re-request only the bar stream. The socket and both tick-by-tick\n    #: subscriptions are left untouched, so this costs no quote data.\n    BARS_ONLY = "bars_only"\n    FULL_RECONNECT = "full_reconnect"\n''',
    '''class RecoveryPlan(Enum):\n    """The smallest repair that could fix what is actually broken."""\n\n    NONE = "none"\n    #: Re-request only the bar stream. The socket and both tick-by-tick\n    #: subscriptions are left untouched, so this costs no quote data.\n    BARS_ONLY = "bars_only"\n    #: Rebuild the three capture subscriptions while preserving the proven-live\n    #: socket and ordinary L1 probe.\n    ALL_MARKET_STREAMS = "all_market_streams"\n    FULL_RECONNECT = "full_reconnect"\n''',
)

replace_once(
    "src/ib_execution/quote_recorder.py",
    '''        self.fast_used = 0\n        self.bars_only_attempts = 0\n        self.slow_full_attempts = 0\n''',
    '''        self.fast_used = 0\n        self.fast_full_attempts = 0\n        self.bars_only_attempts = 0\n        self.all_market_stream_attempts = 0\n        self.slow_full_attempts = 0\n''',
)

old_plan = '''    def plan(self, *, evidence_of_life: bool, now_mono: float) -> RecoveryPlan:\n        if self.fast_used < self.fast_attempts:\n            self.fast_used += 1\n            self._next_full = now_mono + self._slow_delay\n            self._next_bars = now_mono + self.bars_only_seconds\n            return RecoveryPlan.FULL_RECONNECT\n\n        if evidence_of_life:\n            if self._next_bars is None or now_mono >= self._next_bars:\n                self._next_bars = now_mono + self.bars_only_seconds\n                self.bars_only_attempts += 1\n                return RecoveryPlan.BARS_ONLY\n            return RecoveryPlan.NONE\n\n        if self._next_full is None or now_mono >= self._next_full:\n            self.slow_full_attempts += 1\n            self._next_full = now_mono + self._slow_delay\n            self._slow_delay = min(self.slow_max_seconds, self._slow_delay * 2)\n            return RecoveryPlan.FULL_RECONNECT\n        return RecoveryPlan.NONE\n'''
new_plan = '''    def _targeted(self, plan: RecoveryPlan, now_mono: float) -> RecoveryPlan:\n        self._next_bars = now_mono + self.bars_only_seconds\n        if plan is RecoveryPlan.BARS_ONLY:\n            self.bars_only_attempts += 1\n        elif plan is RecoveryPlan.ALL_MARKET_STREAMS:\n            self.all_market_stream_attempts += 1\n        else:  # pragma: no cover - internal contract\n            raise ValueError(f"not a targeted recovery plan: {plan}")\n        return plan\n\n    def plan(\n        self,\n        *,\n        evidence_of_life: bool,\n        now_mono: float,\n        transport_evidence: bool = False,\n        requested_plan: RecoveryPlan | None = None,\n    ) -> RecoveryPlan:\n        # Explicit IB semantics outrank inference.  1101 says all market-data\n        # requests were lost; 10225 says the real-time bars alone were reset.\n        if requested_plan is not None:\n            return self._targeted(requested_plan, now_mono)\n\n        # Any capture event inside the heartbeat window proves at least one of\n        # the three subscriptions is alive.  Never tear down a working socket\n        # before trying the smallest repair.  This check must precede the fast\n        # full-reconnect allowance -- the 2026-08-12 incident proved why.\n        if evidence_of_life:\n            if self._next_bars is None or now_mono >= self._next_bars:\n                return self._targeted(RecoveryPlan.BARS_ONLY, now_mono)\n            return RecoveryPlan.NONE\n\n        # The three capture streams may all be stale while TWS is demonstrably\n        # still talking (the incident's L1 request 3 did exactly this).  That\n        # evidence is veto-only: rebuild the capture subscriptions, not the\n        # socket.  It can never initiate recovery without a liveness fault.\n        if transport_evidence:\n            if self._next_bars is None or now_mono >= self._next_bars:\n                return self._targeted(RecoveryPlan.ALL_MARKET_STREAMS, now_mono)\n            return RecoveryPlan.NONE\n\n        if self.fast_used < self.fast_attempts:\n            self.fast_used += 1\n            self.fast_full_attempts += 1\n            self._next_full = now_mono + self._slow_delay\n            return RecoveryPlan.FULL_RECONNECT\n\n        if self._next_full is None or now_mono >= self._next_full:\n            self.slow_full_attempts += 1\n            self._next_full = now_mono + self._slow_delay\n            self._slow_delay = min(self.slow_max_seconds, self._slow_delay * 2)\n            return RecoveryPlan.FULL_RECONNECT\n        return RecoveryPlan.NONE\n'''
replace_once("src/ib_execution/quote_recorder.py", old_plan, new_plan)

replace_once(
    "src/ib_execution/quote_recorder.py",
    '''            "bars_only_attempts": self.bars_only_attempts,\n            "slow_full_reconnect_attempts": self.slow_full_attempts,\n''',
    '''            "fast_full_reconnect_attempts": self.fast_full_attempts,\n            "bars_only_attempts": self.bars_only_attempts,\n            "all_market_stream_attempts": self.all_market_stream_attempts,\n            "slow_full_reconnect_attempts": self.slow_full_attempts,\n''',
)

replace_once(
    "src/ib_execution/quote_recorder.py",
    '        self._resubscribe = False\n',
    '',
)

replace_once(
    "src/ib_execution/quote_recorder.py",
    '''    def _note_handled(self, event_type: str) -> None:\n        self.handled_events[event_type] = self.handled_events.get(event_type, 0) + 1\n        now = time.monotonic()\n        self._last_handled_mono[event_type] = now\n        self.liveness.note_event(event_type, now)\n''',
    '''    def _note_handled(self, event_type: str) -> None:\n        self.handled_events[event_type] = self.handled_events.get(event_type, 0) + 1\n        now = time.monotonic()\n        self._last_handled_mono[event_type] = now\n        self.liveness.note_event(event_type, now)\n        # Only a real time-driven heartbeat is positive recovery evidence.\n        # A grace-period CONTINUE, a quote, or an L1 update must never reset\n        # reconnect/backoff state.\n        if event_type == HEARTBEAT_STREAM:\n            self.recovery.note_recovered()\n''',
)

replace_once(
    "src/ib_execution/quote_recorder.py",
    '''        probe = ib.reqMktData(contract, generic_ticks, False, False)\n        probe.marketDataType = 0  # distinguish an actual callback from ib_async's default\n''',
    '''        probe = ib.reqMktData(contract, generic_ticks, False, False)\n\n        def on_probe_update(_updated) -> None:\n            # Ordinary L1 is event-driven, so silence here proves nothing.\n            # Arrival proves only transport life and is therefore veto-only.\n            now = time.monotonic()\n            self.liveness.note_transport_activity()\n            self.recovery.note_activity(now)\n\n        probe.updateEvent += on_probe_update\n        probe.marketDataType = 0  # distinguish an actual callback from ib_async's default\n''',
)

old_recover = '''    def _recover_market_data(self, ib, contract, bars, state):\n        """Attempt the smallest repair that could fix what is broken.\n\n        Returns the bar handle to keep using. Raises\n        :class:`SlowRecoveryReconnect` when a full reconnect is due; the\n        session loop reconnects and does *not* charge it to the reconnect\n        budget, because this path is paced by its own backoff rather than by\n        a crash-loop cap. Nothing here ever ends the session.\n        """\n        now = time.monotonic()\n        last_any = self.liveness.last_market_event_age(now)\n        # Anything arriving inside one heartbeat window proves the socket and\n        # at least one subscription are alive, whatever the bar clock says.\n        evidence_of_life = (\n            last_any is not None and last_any <= self.config.bar_heartbeat_timeout_seconds\n        )\n        plan = self.recovery.plan(evidence_of_life=evidence_of_life, now_mono=now)\n        if plan is RecoveryPlan.NONE:\n            return bars\n        self._append(\n            "SYSTEM", datetime.now(timezone.utc), contract_id=contract.conId,\n            special_conditions=(\n                f"RECOVERY_ATTEMPT:plan={plan.value};evidence_of_life={evidence_of_life};"\n                f"last_event_age={'unknown' if last_any is None else f'{last_any:.1f}s'};"\n                f"reason={state.reason}"\n            ),\n        )\n        if plan is RecoveryPlan.FULL_RECONNECT:\n            raise SlowRecoveryReconnect(f"market data not live: {state.reason}")\n\n        # Quotes are still flowing, so tearing down the socket would destroy\n        # working data to repair one stream. Re-request just the bars.\n        try:\n            ib.cancelRealTimeBars(bars)\n        except Exception as exc:  # an already-dead subscription is fine to lose\n            self._append(\n                "SYSTEM", datetime.now(timezone.utc), contract_id=contract.conId,\n                special_conditions=f"RECOVERY_CANCEL_BARS_FAILED:{type(exc).__name__}:{exc}",\n            )\n        self._limiter.wait(ib.sleep)\n        refreshed = ib.reqRealTimeBars(contract, 5, "TRADES", True)\n        self._wire_bars(refreshed)\n        return refreshed\n'''
new_recover = '''    def _recover_market_data(self, ib, contract, tickers, bars, state):\n        """Attempt the smallest repair that could fix what is broken.\n\n        Returns ``(tickers, bars)`` handles to keep using. Raises\n        :class:`SlowRecoveryReconnect` only when both capture and transport\n        evidence are absent; targeted repairs preserve the socket. Nothing\n        here ever ends the session.\n        """\n        now = time.monotonic()\n        last_any = self.liveness.last_market_event_age(now)\n        # Event-driven streams are allowed to veto destruction but never to\n        # create a fault.  The time-driven bar heartbeat remains the trigger.\n        evidence_of_life = (\n            last_any is not None and last_any <= self.config.bar_heartbeat_timeout_seconds\n        )\n        transport_evidence = self.liveness.transport_evidence()\n        requested_plan = (\n            RecoveryPlan(state.recovery_hint.value)\n            if state.recovery_hint is not None\n            else None\n        )\n        plan = self.recovery.plan(\n            evidence_of_life=evidence_of_life,\n            transport_evidence=transport_evidence,\n            requested_plan=requested_plan,\n            now_mono=now,\n        )\n        if plan is RecoveryPlan.NONE:\n            return tickers, bars\n        self._append(\n            "SYSTEM", datetime.now(timezone.utc), contract_id=contract.conId,\n            special_conditions=(\n                f"RECOVERY_ATTEMPT:plan={plan.value};evidence_of_life={evidence_of_life};"\n                f"transport_evidence={transport_evidence};"\n                f"requested_plan={'none' if requested_plan is None else requested_plan.value};"\n                f"last_event_age={'unknown' if last_any is None else f'{last_any:.1f}s'};"\n                f"reason={state.reason}"\n            ),\n        )\n        if plan is RecoveryPlan.FULL_RECONNECT:\n            raise SlowRecoveryReconnect(f"market data not live: {state.reason}")\n\n        if plan is RecoveryPlan.ALL_MARKET_STREAMS:\n            for tick_type in ("BidAsk", "AllLast"):\n                try:\n                    ib.cancelTickByTickData(contract, tick_type)\n                except Exception as exc:\n                    self._append(\n                        "SYSTEM", datetime.now(timezone.utc), contract_id=contract.conId,\n                        special_conditions=(\n                            f"RECOVERY_CANCEL_{tick_type.upper()}_FAILED:"\n                            f"{type(exc).__name__}:{exc}"\n                        ),\n                    )\n            try:\n                ib.cancelRealTimeBars(bars)\n            except Exception as exc:\n                self._append(\n                    "SYSTEM", datetime.now(timezone.utc), contract_id=contract.conId,\n                    special_conditions=f"RECOVERY_CANCEL_BARS_FAILED:{type(exc).__name__}:{exc}",\n                )\n\n            self._limiter.wait(ib.sleep)\n            bidask = ib.reqTickByTickData(contract, "BidAsk", 0, False)\n            self._limiter.wait(ib.sleep)\n            alllast = ib.reqTickByTickData(contract, "AllLast", 0, False)\n            self._limiter.wait(ib.sleep)\n            refreshed_bars = ib.reqRealTimeBars(contract, 5, "TRADES", True)\n            self._wire_ticker(bidask)\n            self._wire_ticker(alllast)\n            self._wire_bars(refreshed_bars)\n            return (bidask, alllast), refreshed_bars\n\n        # At least one capture stream is still fresh, or IB explicitly told us\n        # that only real-time bars were reset. Re-request just the bars.\n        try:\n            ib.cancelRealTimeBars(bars)\n        except Exception as exc:  # an already-dead subscription is fine to lose\n            self._append(\n                "SYSTEM", datetime.now(timezone.utc), contract_id=contract.conId,\n                special_conditions=f"RECOVERY_CANCEL_BARS_FAILED:{type(exc).__name__}:{exc}",\n            )\n        self._limiter.wait(ib.sleep)\n        refreshed = ib.reqRealTimeBars(contract, 5, "TRADES", True)\n        self._wire_bars(refreshed)\n        return tickers, refreshed\n'''
replace_once("src/ib_execution/quote_recorder.py", old_recover, new_recover)

replace_once(
    "src/ib_execution/quote_recorder.py",
    '''                    if code in {1101, 10225}:\n                        self._resubscribe = True\n''',
    '',
)

replace_once(
    "src/ib_execution/quote_recorder.py",
    '''                probe, _tickers, bars = self._subscribe(ib, contract)\n''',
    '''                probe, tickers, bars = self._subscribe(ib, contract)\n''',
)

replace_once(
    "src/ib_execution/quote_recorder.py",
    '''                    if state.action is LivenessAction.RECOVER_SUBSCRIPTION:\n                        bars = self._recover_market_data(ib, contract, bars, state)\n                    elif state.action is LivenessAction.CONTINUE:\n                        self.recovery.note_recovered()\n''',
    '''                    if state.action is LivenessAction.RECOVER_SUBSCRIPTION:\n                        tickers, bars = self._recover_market_data(\n                            ib, contract, tickers, bars, state\n                        )\n''',
)

replace_once(
    "src/ib_execution/quote_recorder.py",
    '''                    if self._resubscribe:\n                        self._append(\n                            "SYSTEM", datetime.now(timezone.utc), contract_id=contract.conId,\n                            special_conditions="RESUBSCRIBE_REQUIRED",\n                        )\n                        raise ConnectionError("subscription reset required")\n''',
    '',
)

# There is one reconnect-loop reset of the removed shortcut near the bottom.
replace_once(
    "src/ib_execution/quote_recorder.py",
    '                self._resubscribe = False\n',
    '',
)


# ---------------------------------------------------------------------------
# New regression file: production defaults and the exact incident failure modes
# ---------------------------------------------------------------------------

test_path = ROOT / "tests/test_recovery_p0_regressions.py"
if test_path.exists():
    raise RuntimeError(f"unexpected existing file: {test_path}")
test_path.write_text(
    '''from __future__ import annotations\n\nimport inspect\nfrom types import SimpleNamespace\n\nimport pytest\n\nfrom ib_execution.market_liveness import (\n    LivenessAction,\n    LivenessState,\n    MarketLiveness,\n    RecoveryHint,\n)\nfrom ib_execution.quote_recorder import QuoteRecorder, RecoveryPlan, RecoveryScheduler\n\n\ndef _plan(*, capture: bool, transport: bool) -> RecoveryPlan:\n    scheduler = RecoveryScheduler()  # production defaults: fast_attempts=2\n    return scheduler.plan(\n        evidence_of_life=capture,\n        transport_evidence=transport,\n        now_mono=0.0,\n    )\n\n\n@pytest.mark.parametrize(\n    ("capture", "transport", "expected"),\n    [\n        (True, False, RecoveryPlan.BARS_ONLY),\n        (True, True, RecoveryPlan.BARS_ONLY),\n        (False, True, RecoveryPlan.ALL_MARKET_STREAMS),\n        (False, False, RecoveryPlan.FULL_RECONNECT),\n    ],\n)\ndef test_production_default_recovery_matrix(capture, transport, expected):\n    assert _plan(capture=capture, transport=transport) is expected\n\n\ndef test_capture_life_vetoes_fast_full_reconnect_with_production_defaults():\n    scheduler = RecoveryScheduler()\n\n    first = scheduler.plan(\n        evidence_of_life=True, transport_evidence=True, now_mono=0.0\n    )\n\n    assert first is RecoveryPlan.BARS_ONLY\n    assert scheduler.fast_full_attempts == 0\n\n\ndef test_transport_only_evidence_repairs_all_capture_streams_without_socket_reset():\n    scheduler = RecoveryScheduler()\n\n    assert scheduler.plan(\n        evidence_of_life=False, transport_evidence=True, now_mono=0.0\n    ) is RecoveryPlan.ALL_MARKET_STREAMS\n    assert scheduler.plan(\n        evidence_of_life=False, transport_evidence=True, now_mono=1.0\n    ) is RecoveryPlan.NONE\n    assert scheduler.plan(\n        evidence_of_life=False, transport_evidence=True, now_mono=121.0\n    ) is RecoveryPlan.ALL_MARKET_STREAMS\n    assert scheduler.fast_full_attempts == 0\n\n\ndef test_no_positive_bar_means_fast_attempts_are_not_rearmed_forever():\n    scheduler = RecoveryScheduler()\n\n    assert scheduler.plan(\n        evidence_of_life=False, transport_evidence=False, now_mono=0.0\n    ) is RecoveryPlan.FULL_RECONNECT\n    # A reconnect creates a 12s liveness grace period, but no real BAR_5S has\n    # arrived here.  The second genuine timeout consumes the second fast slot.\n    assert scheduler.plan(\n        evidence_of_life=False, transport_evidence=False, now_mono=12.1\n    ) is RecoveryPlan.FULL_RECONNECT\n    assert scheduler.plan(\n        evidence_of_life=False, transport_evidence=False, now_mono=24.2\n    ) is RecoveryPlan.NONE\n    assert scheduler.plan(\n        evidence_of_life=False, transport_evidence=False, now_mono=312.2\n    ) is RecoveryPlan.FULL_RECONNECT\n\n\ndef test_only_a_real_bar_handler_resets_recovery_state(tmp_path):\n    recorder = QuoteRecorder(tmp_path)\n    scheduler = recorder.recovery\n    scheduler.plan(evidence_of_life=False, transport_evidence=False, now_mono=0.0)\n    assert scheduler.fast_used == 1\n\n    recorder._note_handled("BID_ASK")\n    assert scheduler.fast_used == 1\n\n    recorder._note_handled("BAR_5S")\n    assert scheduler.fast_used == 0\n\n\ndef test_production_loop_cannot_reset_recovery_from_continue_state():\n    source = inspect.getsource(QuoteRecorder._run_session_loop)\n    assert "self.recovery.note_recovered()" not in source\n\n\ndef test_transport_evidence_is_veto_only_and_revoked_by_timeout():\n    liveness = MarketLiveness()\n    liveness.subscription_started(0.0)\n    assert liveness.transport_evidence() is True\n\n    liveness.note_transport_idle(60.0)\n    assert liveness.transport_evidence() is False\n    assert liveness.assess(0.1).action is LivenessAction.RECOVER_SUBSCRIPTION\n\n    liveness.note_transport_activity()\n    assert liveness.transport_evidence() is True\n\n\ndef test_1101_10225_and_1102_map_to_one_recovery_pipeline():\n    lost = MarketLiveness()\n    lost.subscription_started(0.0)\n    lost.note_status(1101, "Connectivity restored - data lost")\n    state_1101 = lost.assess(0.1)\n    assert state_1101.action is LivenessAction.RECOVER_SUBSCRIPTION\n    assert state_1101.recovery_hint is RecoveryHint.ALL_MARKET_STREAMS\n\n    bars = MarketLiveness()\n    bars.subscription_started(0.0)\n    bars.note_status(10225, "Bust event occurred, current subscription deactivated")\n    state_10225 = bars.assess(0.1)\n    assert state_10225.action is LivenessAction.RECOVER_SUBSCRIPTION\n    assert state_10225.recovery_hint is RecoveryHint.BARS_ONLY\n\n    kept = MarketLiveness()\n    kept.subscription_started(0.0)\n    kept.note_status(1100, "Connectivity lost")\n    kept.note_status(1102, "Connectivity restored - data maintained")\n    state_1102 = kept.assess(0.1)\n    assert state_1102.action is LivenessAction.CONTINUE\n    assert state_1102.recovery_hint is None\n\n\nclass _FakeIB:\n    def __init__(self):\n        self.cancelled = []\n        self.requested = []\n        self.disconnected = False\n\n    def sleep(self, _seconds):\n        return None\n\n    def cancelTickByTickData(self, _contract, tick_type):\n        self.cancelled.append(("tbt", tick_type))\n\n    def cancelRealTimeBars(self, _bars):\n        self.cancelled.append(("bars", None))\n\n    def reqTickByTickData(self, _contract, tick_type, _number, _ignore_size):\n        handle = object()\n        self.requested.append(("tbt", tick_type, handle))\n        return handle\n\n    def reqRealTimeBars(self, _contract, _size, _what, _rth):\n        handle = object()\n        self.requested.append(("bars", "TRADES", handle))\n        return handle\n\n    def disconnect(self):\n        self.disconnected = True\n\n\ndef test_explicit_1101_style_repair_rebuilds_three_streams_not_socket(tmp_path):\n    recorder = QuoteRecorder(tmp_path)\n    recorder._wire_ticker = lambda _ticker: None\n    recorder._wire_bars = lambda _bars: None\n    recorder.liveness.subscription_started(0.0)\n    ib = _FakeIB()\n    contract = SimpleNamespace(conId=756733)\n    old_tickers = (object(), object())\n    old_bars = object()\n    state = LivenessState(\n        action=LivenessAction.RECOVER_SUBSCRIPTION,\n        reason="1101",\n        recovery_hint=RecoveryHint.ALL_MARKET_STREAMS,\n    )\n\n    tickers, bars = recorder._recover_market_data(\n        ib, contract, old_tickers, old_bars, state\n    )\n\n    assert ib.disconnected is False\n    assert ib.cancelled == [("tbt", "BidAsk"), ("tbt", "AllLast"), ("bars", None)]\n    assert [item[:2] for item in ib.requested] == [\n        ("tbt", "BidAsk"),\n        ("tbt", "AllLast"),\n        ("bars", "TRADES"),\n    ]\n    assert tickers[0] is ib.requested[0][2]\n    assert tickers[1] is ib.requested[1][2]\n    assert bars is ib.requested[2][2]\n\n\ndef test_10197_remains_fatal():\n    assert QuoteRecorder._is_fatal_market_data_error(10197, "") is True\n''',
    encoding="utf-8",
    newline="\n",
)


# ---------------------------------------------------------------------------
# Documentation: amend failed evidence, never rewrite it as a pass
# ---------------------------------------------------------------------------

incident = ROOT / "docs/INCIDENT_FULL_RTH_20260812_ZH.md"
incident_text = incident.read_text(encoding="utf-8")
amendment = '''\n\n## 8. Amendment：P0 recovery 修复实现（2026-08-13）\n\n后续代码复核确认事故报告 §5.1 之外还有两条独立生产缺陷：\n\n1. 主循环把任何 `LivenessAction.CONTINUE` 都当成恢复并调用 `note_recovered()`。重连后的 12 秒 grace\n   period 即使一条新 `BAR_5S` 都没收到也会返回 `CONTINUE`，因此 `fast_used` 会被反复清零，持续故障下\n   slow backoff 可能永远无法启动。修复后只有真实 `BAR_5S` handler 才能重置 recovery state。\n2. `1101` 与 `10225` 原先通过 `_resubscribe -> ConnectionError` 的平行 shortcut 无条件做 socket 级重连，\n   完全绕过 `RecoveryScheduler`。修复后删除该 shortcut：`1101 -> ALL_MARKET_STREAMS`、\n   `10225 -> BARS_ONLY`、`1102 -> NONE`，统一进入一条 recovery pipeline。\n\n同时增加 veto-only `transport_evidence`：普通 L1/其他 inbound activity 只能证明当前 transport 尚活，不能因\n自身沉默触发任何动作；当三路 capture 都 stale 但 transport 尚有证据时，新增 `ALL_MARKET_STREAMS` 仅取消并\n重建 BidAsk / AllLast / BAR_5S 三路订阅，不动 socket 或 L1 probe。只有 capture 与 transport evidence 都缺失\n时才允许 full reconnect。`10197` 继续保持 fatal prerequisite，不改成自动无限重试。\n\n本 amendment 只说明 P0 代码与离线回归的整改范围；**不改变本次事故 FAIL，不构成 Full-RTH PASS，也不替代\n修复后的 10–20 分钟真实只读 smoke。**\n'''
if "## 8. Amendment：P0 recovery 修复实现（2026-08-13）" in incident_text:
    raise RuntimeError("incident amendment already exists")
incident.write_text(incident_text.rstrip() + amendment + "\n", encoding="utf-8", newline="\n")

replace_once(
    "docs/GATE_B2_STATUS_20260810_ZH.md",
    '| 未解释静默的恢复策略 | **发现生产默认组合缺陷，待修复** | 设计要求有生命迹象时只做最小修复，但 `plan()` 先消耗 fast full-reconnect；真实 run 已记录 `evidence_of_life=True` 仍 full reconnect | 测试使用 `fast_attempts=0` 隐藏了默认值缺陷；修复和真实 smoke 前不得再次投入 Full-RTH |',
    '| 未解释静默的恢复策略 | **P0 代码修复完成，待真实 smoke** | 默认 `fast_attempts=2` 下 capture 生命迹象先 veto full reconnect；真实 BAR 才重置 backoff；transport-only 时只重建三路 capture；1101/10225 已统一进入同一 recovery pipeline | 新回归覆盖生产默认矩阵；仍必须先做 10–20 分钟真实只读 smoke，Full-RTH 继续未完成 |',
)

replace_once(
    "docs/GATE_B2_STATUS_20260810_ZH.md",
    '''3. 保留并索引 2026-08-12 Full-RTH 失败轮；详细见\n   [`INCIDENT_FULL_RTH_20260812_ZH.md`](INCIDENT_FULL_RTH_20260812_ZH.md)。先修复 scheduler 在生产默认\n   `fast_attempts=2` + `evidence_of_life=True` 时错误 full reconnect 的分支，并补足回归测试。\n4. 修复后做一次 **10–20 分钟只读冒烟跑**：确认 LIVE、三路持续、`halt_state_available=false`，且有生命\n''',
    '''3. 保留并索引 2026-08-12 Full-RTH 失败轮；详细见\n   [`INCIDENT_FULL_RTH_20260812_ZH.md`](INCIDENT_FULL_RTH_20260812_ZH.md)。P0 已修复 scheduler 默认排序、\n   grace-period 假恢复重置、transport-only 三路重订以及 1101/10225 平行 shortcut，并补生产默认矩阵回归。\n4. 修复后做一次 **10–20 分钟只读冒烟跑**：确认 LIVE、三路持续、`halt_state_available=false`，且有生命\n''',
)

print("Recovery P0 patch applied")
