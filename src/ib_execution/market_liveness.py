"""Market-data liveness classification for the read-only Recorder.

``BAR_5S`` is time-driven and can prove a gap. ``BID_ASK`` and ``ALL_LAST``
are event-driven and silence alone is advisory. IB farm status is scoped by
data domain so auxiliary historical/sec-def farms cannot create or hide a
SPY real-time outage.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

BAR_PERIOD_SECONDS = 5.0
HEARTBEAT_STREAM = "BAR_5S"
DEFAULT_BAR_TIMEOUT_SECONDS = 12.0
MIN_BAR_TIMEOUT_SECONDS = 2 * BAR_PERIOD_SECONDS
DEFAULT_ADVISORY_THRESHOLDS = {"BID_ASK": 30.0, "ALL_LAST": 30.0}
DEFAULT_TRANSPORT_IDLE_SECONDS = 60.0

CONNECTIVITY_LOST = 1100
CONNECTIVITY_RESTORED_DATA_LOST = 1101
CONNECTIVITY_RESTORED_DATA_KEPT = 1102
REALTIME_BARS_RESET = 10225
REALTIME_FARM_BROKEN = 2103
REALTIME_FARM_OK = 2104
HISTORICAL_FARM_CODES = frozenset({2105, 2106})
SECDEF_FARM_CODES = frozenset({2157, 2158})
FARM_ADVISORY_CODES = HISTORICAL_FARM_CODES | SECDEF_FARM_CODES
FARM_INACTIVE_CODE = 2108


class LivenessAction(Enum):
    CONTINUE = "continue"
    WAIT = "wait"
    RECOVER_SUBSCRIPTION = "recover_subscription"


class LivenessIncidentKind(Enum):
    FEED_OUTAGE = "FEED_OUTAGE"
    EXPECTED_SILENCE = "EXPECTED_SILENCE"
    GAP_SUSPECTED = "GAP_SUSPECTED"


class RecoveryHint(Enum):
    BARS_ONLY = "bars_only"
    ALL_MARKET_STREAMS = "all_market_streams"


@dataclass(frozen=True)
class LivenessState:
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
        self._outages: dict[int, str] = {}
        self._realtime_farm_degraded: str | None = None
        self._farm_advisories: dict[str, str] = {}
        self._calendar_silence: Optional[str] = None
        self._pending_recover: tuple[
            LivenessIncidentKind, str, RecoveryHint | None
        ] | None = None
        self._transport_evidence = False
        self.suppressed_assessments = 0
        self.heartbeat_losses = 0
        self._halt_state_available: Optional[bool] = None
        self._halt_state_note: Optional[str] = None

    def subscription_started(self, now_mono: Optional[float] = None) -> None:
        self._started_mono = self._now(now_mono)
        self._last_event_mono.clear()
        self._pending_recover = None
        self._transport_evidence = True

    def note_event(self, stream: str, now_mono: Optional[float] = None) -> None:
        self._last_event_mono[stream] = self._now(now_mono)
        self._transport_evidence = True

    def note_transport_activity(self) -> None:
        self._transport_evidence = True

    def transport_evidence(self) -> bool:
        return self._transport_evidence

    def note_halted(self, value: float) -> None:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return
        self._halted = int(value)
        self._halt_state_available = True
        self._halt_state_note = "tick 49 observed"

    def note_halt_state_source(self, requested: bool, detail: str = "") -> None:
        if requested:
            if self._halt_state_available is None:
                self._halt_state_note = detail or "tick 49 requested; awaiting first value"
            return
        self._halt_state_available = False
        self._halt_state_note = detail or "tick 49 not requested"

    def note_halt_state_unavailable(self, detail: str) -> None:
        self._halt_state_available = False
        self._halt_state_note = detail

    def note_status(self, code: int, message: str = "") -> None:
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
            self._realtime_farm_degraded = (
                f"real-time market data farm down ({code}): {detail}".strip()
            )
        elif code == REALTIME_FARM_OK:
            self._realtime_farm_degraded = None
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
        self._transport_evidence = False
        self._pending_recover = (
            LivenessIncidentKind.GAP_SUSPECTED,
            f"no data of any kind from TWS for {idle_seconds:.1f}s",
            None,
        )

    def enter_calendar_silence(self, reason: str) -> None:
        self._calendar_silence = reason

    def leave_calendar_silence(self) -> None:
        self._calendar_silence = None

    def expected_silence(self) -> Optional[str]:
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
        if self._started_mono is None:
            return None
        last = self._last_event_mono.get(HEARTBEAT_STREAM, self._started_mono)
        return max(0.0, self._now(now_mono) - last)

    def last_market_event_age(self, now_mono: Optional[float] = None) -> Optional[float]:
        if self._started_mono is None:
            return None
        now = self._now(now_mono)
        last = max(self._last_event_mono.values(), default=self._started_mono)
        return max(0.0, now - last)

    def advisory_ages(self, now_mono: Optional[float] = None) -> dict[str, float]:
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
            if self._realtime_farm_degraded is not None:
                self.suppressed_assessments += 1
                return LivenessState(
                    action=LivenessAction.WAIT,
                    reason="BAR heartbeat lost while real-time market data farm is degraded",
                    heartbeat_age=age,
                    heartbeat_lost=True,
                    expected_silence=self._realtime_farm_degraded,
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
            "realtime_market_data_farm_degraded": self._realtime_farm_degraded,
            "farm_advisories": dict(sorted(self._farm_advisories.items())),
            "suppressed_assessments": self.suppressed_assessments,
            "suppressed_assessments_are_poll_count": True,
            "heartbeat_losses": self.heartbeat_losses,
            "transport_evidence": self._transport_evidence,
        }

    def _now(self, now_mono: Optional[float]) -> float:
        return self._clock() if now_mono is None else float(now_mono)
