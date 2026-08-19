"""
Is the feed dead, or is the market merely quiet?

    ####################################################################
    #  For an event-driven stream, those two are indistinguishable by  #
    #  timing alone. No amount of threshold tuning fixes that; the     #
    #  information is not in the data.                                #
    ####################################################################

WHY A TIMEOUT IS THE WRONG PRIMITIVE
------------------------------------
For an event-driven stream, "no BidAsk for 30 seconds" and "no quote update
happened in 30 seconds" are the same observation. Real market-data protocols
solve this with sequence numbers and/or protocol heartbeats. IB's API exposes
neither per-stream sequence numbers nor an event-driven heartbeat, so silence
on ``BID_ASK`` or ``ALL_LAST`` remains a quality observation, not proof of
feed loss.

IB does expose the other half: ``reqRealTimeBars`` is time-driven. A bar is a
message we know should arrive, so a missing ``BAR_5S`` is decidable in exactly
the way a missing quote is not.

THE OBSERVATION THIS RESTS ON
-----------------------------
``docs/GATE_B2_OVERNIGHT_20260810.md`` section 3 records a 120.047s OVERNIGHT
window on a real Gateway:

    ALL_LAST   13 events   largest gap 29.641s
    BAR_5S     25 events   largest gap  5.235s

The tape was nearly dead and the bar cadence did not move. That asymmetry is
the whole design:

    BAR_5S              time-driven    heartbeat; may stop the run
    BID_ASK, ALL_LAST   event-driven   recorded/advisory; never act alone

WHAT ANSWERS "SHOULD THIS SILENCE ALARM?"
-----------------------------------------
Explicit evidence, not a quote timeout. ``1100`` is a connection-wide outage.
``2103/2104`` describe the real-time market-data farm, but a 2103 warning alone
is only degradation evidence: it becomes a hard ``FEED_OUTAGE`` only when the
independent BAR heartbeat is also missing. ``2105/2106`` are historical-data
farm status and ``2157/2158`` are security-definition farm status; both are
useful diagnostics but cannot manufacture or suppress SPY real-time liveness.
A known halt/session boundary remains legitimate expected silence.

The two open questions this cannot answer offline -- whether bars keep flowing
through a halt, and whether tick 49 arrives without being asked for in
``genericTickList`` -- still need a real Gateway. Until measured, an unobserved
halt state is unknown, never assumed.

    NOTE ON SCOPE: this module decides what the evidence means. Recovery
    machinery (reconnect budget, resubscribe, finalize) stays in
    ``quote_recorder`` and is not duplicated here.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

# IB emits one real-time bar per five seconds per subscription.
BAR_PERIOD_SECONDS = 5.0
HEARTBEAT_STREAM = "BAR_5S"

# Two full periods plus room for delivery jitter. Never derived from how busy
# the tape is, because the bar cadence is not either.
DEFAULT_BAR_TIMEOUT_SECONDS = 12.0
MIN_BAR_TIMEOUT_SECONDS = 2 * BAR_PERIOD_SECONDS

# Event-driven silence is report-only. Use one common observation threshold so
# BID_ASK and ALL_LAST have one health vocabulary without implying a hard SLA.
DEFAULT_ADVISORY_THRESHOLDS = {"BID_ASK": 30.0, "ALL_LAST": 30.0}

# ib_async's transport watchdog is separate: it detects a silent peer, not a
# quiet market stream. EventLoopHeartbeat covers a stuck event loop.
DEFAULT_TRANSPORT_IDLE_SECONDS = 60.0

# 1100/1101/1102 are connection-wide. After 1101 subscriptions are lost and
# must be re-requested; after 1102 they are maintained.
CONNECTIVITY_LOST = 1100
CONNECTIVITY_RESTORED_DATA_LOST = 1101
CONNECTIVITY_RESTORED_DATA_KEPT = 1102
REALTIME_BARS_RESET = 10225

# Farm ownership matters. Only 2103/2104 describe the real-time market-data
# farm. Historical and security-definition farms are advisory diagnostics.
REALTIME_FARM_BROKEN = 2103
REALTIME_FARM_OK = 2104
HISTORICAL_FARM_CODES = frozenset({2105, 2106})
SECDEF_FARM_CODES = frozenset({2157, 2158})
FARM_ADVISORY_CODES = HISTORICAL_FARM_CODES | SECDEF_FARM_CODES

# "inactive but should be available upon demand" -- never an outage.
FARM_INACTIVE_CODE = 2108
_FARM_ID_RE = re.compile(r":(?P<farm>[A-Za-z0-9_.-]+)\s*$")


def realtime_farm_identity(message: str) -> str:
    """Return the IB farm suffix, or one fail-closed unspecified bucket."""
    match = _FARM_ID_RE.search(message.strip())
    return match.group("farm").lower() if match is not None else "__unspecified__"


class LivenessAction(Enum):
    """What the Recorder loop should do, decided in one place."""

    CONTINUE = "continue"
    #: Silence is explained. Record it, do not act, do not call it a gap.
    WAIT = "wait"
    #: The subscription is gone or unresponsive; re-request market data.
    RECOVER_SUBSCRIPTION = "recover_subscription"


class LivenessIncidentKind(Enum):
    """Audit classification for a sustained non-normal liveness state."""

    FEED_OUTAGE = "FEED_OUTAGE"
    EXPECTED_SILENCE = "EXPECTED_SILENCE"
    GAP_SUSPECTED = "GAP_SUSPECTED"


class RecoveryHint(Enum):
    """An explicit IB instruction about the smallest subscription repair."""

    BARS_ONLY = "bars_only"
    ALL_MARKET_STREAMS = "all_market_streams"


@dataclass(frozen=True)
class LivenessState:
    """One assessment. Everything a SYSTEM marker or report needs."""

    action: LivenessAction
    reason: str
    heartbeat_age: Optional[float] = None
    heartbeat_lost: bool = False
    expected_silence: Optional[str] = None
    advisory_ages: dict[str, float] = field(default_factory=dict)
    incident_kind: LivenessIncidentKind | None = None
    heartbeat_last_mono: float | None = None
    recovery_hint: RecoveryHint | None = None

    def as_marker(self) -> str:
        """Compact, greppable form for the raw log."""
        parts = [f"action={self.action.value}", f"reason={self.reason}"]
        if self.incident_kind is not None:
            parts.append(f"incident_kind={self.incident_kind.value}")
        if self.heartbeat_age is not None:
            parts.append(f"bar_age={self.heartbeat_age:.1f}s")
        if self.expected_silence:
            parts.append(f"expected_silence={self.expected_silence}")
        if self.recovery_hint is not None:
            parts.append(f"recovery_hint={self.recovery_hint.value}")
        for stream, age in sorted(self.advisory_ages.items()):
            parts.append(f"{stream}_age={age:.1f}s")
        return ";".join(parts)


@dataclass
class _OpenIncident:
    incident_id: str
    kind: LivenessIncidentKind
    started_mono: float
    last_emitted_mono: float
    signature: tuple[str, str | None]
    max_heartbeat_age: float | None


class LivenessIncidentTracker:
    """Turn poll-level assessments into bounded incident lifecycle records.

    The Recorder assesses liveness repeatedly. That polling cadence is an
    implementation detail, not an incident count. Emit only state edges,
    material updates, optional durable checkpoints, and recovery.
    """

    def __init__(
        self,
        *,
        checkpoint_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if checkpoint_seconds <= 0:
            raise ValueError("checkpoint_seconds must be positive")
        self.checkpoint_seconds = float(checkpoint_seconds)
        self._clock = clock
        self._open: _OpenIncident | None = None
        self._next_id = 0
        self._incident_by_kind: dict[str, int] = {}
        self._completed_by_kind: dict[str, int] = {}
        self._total_seconds_by_kind: dict[str, float] = {}
        self._max_seconds_by_kind: dict[str, float] = {}

    @staticmethod
    def _signature(state: LivenessState) -> tuple[str, str | None]:
        # Ages move on every poll and are deliberately excluded. Only a
        # semantic action or explicit-silence change merits an UPDATE row.
        return (state.action.value, state.expected_silence)

    @staticmethod
    def _max_age(current: float | None, candidate: float | None) -> float | None:
        if candidate is None:
            return current
        return candidate if current is None else max(current, candidate)

    @staticmethod
    def _marker(
        phase: str,
        incident: _OpenIncident,
        state: LivenessState,
        now: float,
        *,
        recovery_reason: str | None = None,
    ) -> str:
        parts = [
            f"{incident.kind.value}_{phase}",
            f"incident_id={incident.incident_id}",
            f"duration={max(0.0, now - incident.started_mono):.1f}s",
        ]
        if incident.max_heartbeat_age is not None:
            parts.append(f"max_bar_age={incident.max_heartbeat_age:.1f}s")
        if recovery_reason is not None:
            parts.append(f"recovery_reason={recovery_reason}")
        parts.append(state.as_marker())
        return ":".join((parts[0], ";".join(parts[1:])))

    def observe(
        self, state: LivenessState, now_mono: float | None = None
    ) -> list[str]:
        """Return zero or more lifecycle markers for one assessment."""
        now = self._clock() if now_mono is None else float(now_mono)
        markers: list[str] = []
        kind = state.incident_kind
        if (
            self._open is not None
            and kind is None
            and (
                state.heartbeat_last_mono is None
                or state.heartbeat_last_mono < self._open.started_mono
            )
        ):
            # A reconnect/grace reset is not recovery evidence. Keep the
            # incident open until a post-incident BAR_5S arrives.
            self._open.max_heartbeat_age = self._max_age(
                self._open.max_heartbeat_age, state.heartbeat_age
            )
            return markers
        if self._open is not None and self._open.kind is not kind:
            markers.append(self._end(state, now, recovery_reason=state.reason))
        if kind is None:
            return markers
        signature = self._signature(state)
        if self._open is None:
            self._next_id += 1
            incident_id = f"{kind.value.lower()}-{self._next_id:04d}"
            self._open = _OpenIncident(
                incident_id,
                kind,
                now,
                now,
                signature,
                state.heartbeat_age,
            )
            self._incident_by_kind[kind.value] = self._incident_by_kind.get(kind.value, 0) + 1
            markers.append(self._marker("START", self._open, state, now))
            return markers
        incident = self._open
        incident.max_heartbeat_age = self._max_age(
            incident.max_heartbeat_age, state.heartbeat_age
        )
        if signature != incident.signature:
            incident.signature = signature
            incident.last_emitted_mono = now
            markers.append(self._marker("UPDATE", incident, state, now))
        elif now - incident.last_emitted_mono >= self.checkpoint_seconds:
            incident.last_emitted_mono = now
            markers.append(self._marker("CHECKPOINT", incident, state, now))
        return markers

    def close(self, reason: str, now_mono: float | None = None) -> list[str]:
        """Close an incident at a clean boundary such as normal session end."""
        if self._open is None:
            return []
        now = self._clock() if now_mono is None else float(now_mono)
        state = LivenessState(action=LivenessAction.CONTINUE, reason=reason)
        return [self._end(state, now, recovery_reason=reason)]

    def _end(self, state: LivenessState, now: float, *, recovery_reason: str) -> str:
        assert self._open is not None
        incident = self._open
        incident.max_heartbeat_age = self._max_age(
            incident.max_heartbeat_age, state.heartbeat_age
        )
        duration = max(0.0, now - incident.started_mono)
        kind = incident.kind.value
        self._completed_by_kind[kind] = self._completed_by_kind.get(kind, 0) + 1
        self._total_seconds_by_kind[kind] = self._total_seconds_by_kind.get(kind, 0.0) + duration
        self._max_seconds_by_kind[kind] = max(self._max_seconds_by_kind.get(kind, 0.0), duration)
        marker = self._marker("END", incident, state, now, recovery_reason=recovery_reason)
        self._open = None
        return marker

    def manifest(self, now_mono: float | None = None) -> dict[str, object]:
        """Bounded incident statistics for the final manifest."""
        now = self._clock() if now_mono is None else float(now_mono)
        open_incident = None
        if self._open is not None:
            open_incident = {
                "incident_id": self._open.incident_id,
                "kind": self._open.kind.value,
                "duration_seconds": max(0.0, now - self._open.started_mono),
                "max_heartbeat_age_seconds": self._open.max_heartbeat_age,
            }
        return {
            "checkpoint_seconds": self.checkpoint_seconds,
            "incident_count": sum(self._incident_by_kind.values()),
            "incident_by_kind": dict(sorted(self._incident_by_kind.items())),
            "completed_incident_count": sum(self._completed_by_kind.values()),
            "completed_by_kind": dict(sorted(self._completed_by_kind.items())),
            "total_seconds_by_kind": dict(sorted(self._total_seconds_by_kind.items())),
            "max_seconds_by_kind": dict(sorted(self._max_seconds_by_kind.items())),
            "open_incident_count": int(self._open is not None),
            "open_incident": open_incident,
        }


class MarketLiveness:
    """Track feed liveness from BAR cadence plus scoped IB evidence.

    Pure state: no IO, no IB objects, injectable clock. The Recorder feeds it
    observations and asks one question. Recovery actions remain elsewhere.
    """

    def __init__(
        self,
        *,
        bar_timeout_seconds: float = DEFAULT_BAR_TIMEOUT_SECONDS,
        advisory_thresholds: Optional[dict[str, float]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if bar_timeout_seconds < MIN_BAR_TIMEOUT_SECONDS:
            raise ValueError(
                f"bar_timeout_seconds must be at least {MIN_BAR_TIMEOUT_SECONDS} "
                f"(two bar periods); got {bar_timeout_seconds}"
            )
        self.bar_timeout_seconds = float(bar_timeout_seconds)
        self.advisory_thresholds = dict(
            DEFAULT_ADVISORY_THRESHOLDS if advisory_thresholds is None else advisory_thresholds
        )
        self._clock = clock
        self._started_mono: Optional[float] = None
        self._last_event_mono: dict[str, float] = {}
        self._halted: Optional[int] = None

        # Only 1100 belongs in unconditional outage state. A real-time farm
        # warning is held separately until a missing BAR independently confirms
        # that the market-data path is not producing its time-driven heartbeat.
        self._outages: dict[int, str] = {}
        self._realtime_farms_degraded: dict[str, str] = {}
        self._farm_advisories: dict[str, str] = {}
        self._calendar_silence: Optional[str] = None
        self._pending_recover: tuple[
            LivenessIncidentKind, str, RecoveryHint | None
        ] | None = None

        # Veto-only transport evidence. Positive TWS activity can prevent a
        # destructive socket reconnect, but never initiates recovery itself.
        self._transport_evidence = False
        self.suppressed_assessments = 0
        self.heartbeat_losses = 0
        self._halt_state_available: Optional[bool] = None
        self._halt_state_note: Optional[str] = None

    # -- observations -------------------------------------------------

    def subscription_started(self, now_mono: Optional[float] = None) -> None:
        """Reset the clock origin. Before this there is nothing to judge."""
        self._started_mono = self._now(now_mono)
        self._last_event_mono.clear()
        self._pending_recover = None
        self._transport_evidence = True

    def note_event(self, stream: str, now_mono: Optional[float] = None) -> None:
        self._last_event_mono[stream] = self._now(now_mono)
        self._transport_evidence = True

    def note_transport_activity(self) -> None:
        """Record inbound protocol activity without treating it as cadence."""
        self._transport_evidence = True

    def transport_evidence(self) -> bool:
        """Whether transport is known alive; veto-only, never a trigger."""
        return self._transport_evidence

    def note_halted(self, value: float) -> None:
        """Generic tick 49: 0 trading, 1 halted, 2 volatility pause."""
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return
        self._halted = int(value)
        self._halt_state_available = True
        self._halt_state_note = "tick 49 observed"

    def note_halt_state_source(self, requested: bool, detail: str = "") -> None:
        """Record whether tick 49 was actually requested."""
        if requested:
            if self._halt_state_available is None:
                self._halt_state_note = detail or "tick 49 requested; awaiting first value"
            return
        self._halt_state_available = False
        self._halt_state_note = detail or "tick 49 not requested"

    def note_halt_state_unavailable(self, detail: str) -> None:
        """The Gateway refused to serve the halt tick (for example error 321)."""
        self._halt_state_available = False
        self._halt_state_note = detail

    def note_status(self, code: int, message: str = "") -> None:
        """Classify one IB status by the data domain it actually describes."""
        self._transport_evidence = True
        detail = message.strip()
        if code == CONNECTIVITY_LOST:
            self._outages[code] = f"connectivity lost ({code})"
        elif code == CONNECTIVITY_RESTORED_DATA_LOST:
            self._outages.pop(CONNECTIVITY_LOST, None)
            self._pending_recover = (
                LivenessIncidentKind.FEED_OUTAGE,
                f"connectivity restored, market data lost ({code})",
                RecoveryHint.ALL_MARKET_STREAMS,
            )
        elif code == CONNECTIVITY_RESTORED_DATA_KEPT:
            self._outages.pop(CONNECTIVITY_LOST, None)
            self._pending_recover = None
        elif code == REALTIME_BARS_RESET:
            self._pending_recover = (
                LivenessIncidentKind.GAP_SUSPECTED,
                f"real-time bars reset by IB ({code})",
                RecoveryHint.BARS_ONLY,
            )
        elif code == REALTIME_FARM_BROKEN:
            self._realtime_farms_degraded[realtime_farm_identity(detail)] = (
                f"real-time market data farm down ({code}): {detail}".strip()
            )
        elif code == REALTIME_FARM_OK:
            self._realtime_farms_degraded.pop(realtime_farm_identity(detail), None)
        elif code in HISTORICAL_FARM_CODES:
            self._farm_advisories["historical"] = (
                f"historical data farm status ({code}): {detail}".strip()
            )
        elif code in SECDEF_FARM_CODES:
            self._farm_advisories["security_definition"] = (
                f"security definition farm status ({code}): {detail}".strip()
            )
        elif code == FARM_INACTIVE_CODE:
            self._farm_advisories["inactive"] = (
                f"farm inactive/on-demand ({code}): {detail}".strip()
            )

    def note_transport_idle(self, idle_seconds: float) -> None:
        """ib_async timeout: nothing at all arrived from TWS."""
        self._transport_evidence = False
        self._pending_recover = (
            LivenessIncidentKind.GAP_SUSPECTED,
            f"no data of any kind from TWS for {idle_seconds:.1f}s",
            None,
        )

    def enter_calendar_silence(self, reason: str) -> None:
        """A known auction/session boundary. Silence here is scheduled."""
        self._calendar_silence = reason

    def leave_calendar_silence(self) -> None:
        self._calendar_silence = None

    # -- the question -------------------------------------------------

    def expected_silence(self) -> Optional[str]:
        """Why silence is legitimate right now, or None."""
        details = self._expected_silence_details()
        return None if details is None else details[1]

    def _expected_silence_details(
        self,
    ) -> Optional[tuple[LivenessIncidentKind, str]]:
        if self._halted in (1, 2):
            kind = "volatility pause" if self._halted == 2 else "halted"
            return (
                LivenessIncidentKind.EXPECTED_SILENCE,
                f"instrument {kind} (tick 49 = {self._halted})",
            )
        if CONNECTIVITY_LOST in self._outages:
            return LivenessIncidentKind.FEED_OUTAGE, self._outages[CONNECTIVITY_LOST]
        if self._calendar_silence:
            return LivenessIncidentKind.EXPECTED_SILENCE, self._calendar_silence
        return None

    def heartbeat_age(self, now_mono: Optional[float] = None) -> Optional[float]:
        """Seconds since the last 5-second bar, measured from subscribe."""
        if self._started_mono is None:
            return None
        last = self._last_event_mono.get(HEARTBEAT_STREAM, self._started_mono)
        return max(0.0, self._now(now_mono) - last)

    def last_market_event_age(self, now_mono: Optional[float] = None) -> Optional[float]:
        """Seconds since any market stream last delivered."""
        if self._started_mono is None:
            return None
        now = self._now(now_mono)
        last = max(self._last_event_mono.values(), default=self._started_mono)
        return max(0.0, now - last)

    def advisory_ages(self, now_mono: Optional[float] = None) -> dict[str, float]:
        """Over-threshold ages of event-driven streams. Report only."""
        if self._started_mono is None:
            return {}
        now = self._now(now_mono)
        ages: dict[str, float] = {}
        for stream, threshold in self.advisory_thresholds.items():
            last = self._last_event_mono.get(stream, self._started_mono)
            age = max(0.0, now - last)
            if age > threshold:
                ages[stream] = age
        return ages

    def assess(self, now_mono: Optional[float] = None) -> LivenessState:
        """Combine connection, BAR cadence, and scoped farm evidence."""
        now = self._now(now_mono)
        advisory = self.advisory_ages(now)
        age = self.heartbeat_age(now)
        heartbeat_last_mono = self._last_event_mono.get(HEARTBEAT_STREAM)
        if self._started_mono is None:
            return LivenessState(
                action=LivenessAction.CONTINUE,
                reason="not subscribed",
                advisory_ages=advisory,
            )

        # Only truly explicit silence -- halt, 1100, calendar boundary -- may
        # suppress a missing heartbeat without corroboration.
        silence_details = self._expected_silence_details()
        if silence_details is not None:
            self.suppressed_assessments += 1
            return LivenessState(
                action=LivenessAction.WAIT,
                reason="silence explained by an explicit IB signal",
                heartbeat_age=age,
                heartbeat_lost=age is not None and age > self.bar_timeout_seconds,
                expected_silence=silence_details[1],
                advisory_ages=advisory,
                incident_kind=silence_details[0],
                heartbeat_last_mono=heartbeat_last_mono,
            )

        # Explicit subscription loss/reset beats waiting for the bar timer.
        if self._pending_recover is not None:
            incident_kind, reason, recovery_hint = self._pending_recover
            self._pending_recover = None
            return LivenessState(
                action=LivenessAction.RECOVER_SUBSCRIPTION,
                reason=reason,
                heartbeat_age=age,
                advisory_ages=advisory,
                incident_kind=incident_kind,
                heartbeat_last_mono=heartbeat_last_mono,
                recovery_hint=recovery_hint,
            )

        if age is not None and age > self.bar_timeout_seconds:
            self.heartbeat_losses += 1
            if self._realtime_farms_degraded:
                # 2103 alone is not an outage. Missing BAR is the independent
                # corroboration that upgrades the interval to FEED_OUTAGE.
                self.suppressed_assessments += 1
                return LivenessState(
                    action=LivenessAction.WAIT,
                    reason="BAR heartbeat lost while real-time market data farm is degraded",
                    heartbeat_age=age,
                    heartbeat_lost=True,
                    expected_silence=" | ".join(
                        self._realtime_farms_degraded[name]
                        for name in sorted(self._realtime_farms_degraded)
                    ),
                    advisory_ages=advisory,
                    incident_kind=LivenessIncidentKind.FEED_OUTAGE,
                    heartbeat_last_mono=heartbeat_last_mono,
                )
            return LivenessState(
                action=LivenessAction.RECOVER_SUBSCRIPTION,
                reason=(
                    f"no 5-second bar for {age:.1f}s "
                    f"(> {self.bar_timeout_seconds:.1f}s) with no explanation from IB"
                ),
                heartbeat_age=age,
                heartbeat_lost=True,
                advisory_ages=advisory,
                incident_kind=LivenessIncidentKind.GAP_SUSPECTED,
                heartbeat_last_mono=heartbeat_last_mono,
            )

        return LivenessState(
            action=LivenessAction.CONTINUE,
            reason="bar cadence intact",
            heartbeat_age=age,
            advisory_ages=advisory,
            heartbeat_last_mono=heartbeat_last_mono,
        )

    def manifest(self) -> dict[str, object]:
        """Configuration and liveness evidence needed by the report."""
        realtime_degraded = (
            None
            if not self._realtime_farms_degraded
            else " | ".join(
                self._realtime_farms_degraded[name]
                for name in sorted(self._realtime_farms_degraded)
            )
        )
        return {
            "heartbeat_stream": HEARTBEAT_STREAM,
            "bar_period_seconds": BAR_PERIOD_SECONDS,
            "bar_timeout_seconds": self.bar_timeout_seconds,
            "advisory_thresholds": dict(self.advisory_thresholds),
            "advisory_streams_never_stop_the_run": True,
            "halt_state": self._halted,
            "halt_state_available": self._halt_state_available,
            "halt_state_note": self._halt_state_note,
            "open_outages": [self._outages[code] for code in sorted(self._outages)],
            "realtime_market_data_farm_degraded": realtime_degraded,
            "realtime_market_data_farms_degraded": dict(
                sorted(self._realtime_farms_degraded.items())
            ),
            "farm_advisories": dict(sorted(self._farm_advisories.items())),
            "suppressed_assessments": self.suppressed_assessments,
            "suppressed_assessments_are_poll_count": True,
            "heartbeat_losses": self.heartbeat_losses,
            "transport_evidence": self._transport_evidence,
        }

    def _now(self, now_mono: Optional[float]) -> float:
        return self._clock() if now_mono is None else float(now_mono)
