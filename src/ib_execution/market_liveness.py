"""
Is the feed dead, or is the market merely quiet?

    ####################################################################
    #  For an event-driven stream, those two are indistinguishable by  #
    #  timing alone.  No amount of threshold tuning fixes that; the    #
    #  information is not in the data.                                 #
    ####################################################################

WHY A TIMEOUT IS THE WRONG PRIMITIVE
------------------------------------
"No BidAsk for 5 seconds" and "no quote update happened in 5 seconds" are
the same observation. Real market data protocols do not try to separate
them by waiting: ITCH, OPRA and CME MDP 3.0 all carry a per-channel
sequence number, so a gap is a *protocol fact* -- receiving 1001 then 1003
proves 1002 was lost, with no threshold and no statistics. Feeds that go
quiet also send heartbeats, so "the message that should have arrived did
not" stays decidable when there is no news.

IB's API exposes no sequence numbers. It does expose the other half:
``reqRealTimeBars`` is *time*-driven. A bar is a message we know should
arrive, so a missing bar is decidable in exactly the way a missing quote
is not.

THE OBSERVATION THIS RESTS ON
-----------------------------
``docs/GATE_B2_OVERNIGHT_20260810.md`` section 3, one 120.047s OVERNIGHT
window on a real Gateway:

    ALL_LAST   13 events   largest gap 29.641s
    BAR_5S     25 events   largest gap  5.235s

The tape was nearly dead -- thirteen prints in two minutes, once going
half a minute without one -- and the bar cadence did not move. 25 bars is
the full expected count for the window. IB emitted a bar for five-second
windows containing no trade at all, even with ``whatToShow="TRADES"``.

That asymmetry is the whole design:

    BAR_5S              time-driven    heartbeat; may stop the run
    BID_ASK, ALL_LAST   event-driven   recorded; never acted on

and it costs nothing, because the recorder already subscribes to all three.

WHAT ANSWERS "SHOULD THIS SILENCE ALARM?"
-----------------------------------------
Not a duration. IB says so explicitly, and this module listens to what it
says rather than inferring: generic tick 49 for halt state, the market
data farm status codes, and the 1100/1101/1102 connectivity triple. A
halted instrument and a closed data farm are *expected* silence -- alarming
on them is how a detector trains its operator to ignore it.

The two open questions this cannot answer offline -- whether bars keep
flowing through a halt, and whether tick 49 arrives without being asked
for in ``genericTickList`` -- need a real Gateway. Until they are measured,
an unobserved halt state is simply unknown, never assumed.

    NOTE ON SCOPE: this module decides *what is true*. It does not decide
    what to do about it -- the recovery machinery (reconnect budget,
    resubscribe, finalize) already exists in ``quote_recorder`` and is not
    duplicated here. This keeps the interesting logic testable without a
    Gateway, which is the only reason it is a separate module.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

# IB emits one real-time bar per five seconds per subscription.
BAR_PERIOD_SECONDS = 5.0
HEARTBEAT_STREAM = "BAR_5S"

# Two full periods plus room for delivery jitter. Never derived from how
# busy the tape is, because the bar cadence is not either.
DEFAULT_BAR_TIMEOUT_SECONDS = 12.0
MIN_BAR_TIMEOUT_SECONDS = 2 * BAR_PERIOD_SECONDS

# Recorded, never acted on. These exist so a human reading the report can
# see how quiet it was, not so the process can decide anything.
DEFAULT_ADVISORY_THRESHOLDS = {"BID_ASK": 5.0, "ALL_LAST": 30.0}

# ib_async's own transport watchdog fires when nothing at all -- of any
# kind -- has arrived from TWS. It runs on the event loop, so it detects a
# silent *peer*, not a stuck loop; EventLoopHeartbeat covers the latter.
DEFAULT_TRANSPORT_IDLE_SECONDS = 60.0

# 1100/1101/1102 are the connectivity triple. The distinction between 1101
# and 1102 is the entire reason this is not one code: after 1101 the
# subscriptions are gone and must be re-requested, after 1102 they are not.
CONNECTIVITY_LOST = 1100
CONNECTIVITY_RESTORED_DATA_LOST = 1101
CONNECTIVITY_RESTORED_DATA_KEPT = 1102

# Data farm went away / came back. 2157/2158 are the sec-def farm, which
# does not carry market data, but a broken one still means the session is
# degraded and is worth recording.
FARM_BROKEN_CODES = frozenset({2103, 2105, 2157})
FARM_OK_CODES = frozenset({2104, 2106, 2158})

# "inactive but should be available upon demand" -- IB's own words. This is
# the single noisiest code in the API and it is not an outage. Treating it
# as one is how a liveness detector becomes something operators mute.
FARM_INACTIVE_CODE = 2108


class LivenessAction(Enum):
    """What the recorder loop should do, decided in one place."""

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

    def as_marker(self) -> str:
        """Compact, greppable form for the raw log."""
        parts = [f"action={self.action.value}", f"reason={self.reason}"]
        if self.incident_kind is not None:
            parts.append(f"incident_kind={self.incident_kind.value}")
        if self.heartbeat_age is not None:
            parts.append(f"bar_age={self.heartbeat_age:.1f}s")
        if self.expected_silence:
            parts.append(f"expected_silence={self.expected_silence}")
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

    The recorder assesses liveness four times per second. That cadence is an
    implementation detail, not an incident count. This tracker emits on state
    edges, material state changes, an optional durable checkpoint, and
    recovery. A crash leaves a START without an END, which is intentionally an
    unambiguous open incident rather than a fabricated recovery.
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
        # Unexplained heartbeat reasons also embed the current age, so reason
        # text itself cannot be part of the stable identity.
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
        self,
        state: LivenessState,
        now_mono: float | None = None,
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
            # A reconnect resets the grace-period clock. It is not positive
            # evidence that the subscription is producing data again. Keep
            # the incident open until a post-incident BAR_5S arrives.
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
                incident_id=incident_id,
                kind=kind,
                started_mono=now,
                last_emitted_mono=now,
                signature=signature,
                max_heartbeat_age=state.heartbeat_age,
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

    def close(
        self,
        reason: str,
        now_mono: float | None = None,
    ) -> list[str]:
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
        self._max_seconds_by_kind[kind] = max(
            self._max_seconds_by_kind.get(kind, 0.0), duration
        )
        marker = self._marker(
            "END", incident, state, now, recovery_reason=recovery_reason
        )
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
    """Track feed liveness from the bar cadence plus IB's explicit signals.

    Pure state: no IO, no IB objects, an injectable clock. The recorder
    feeds it observations and asks it one question.
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
        self._outages: dict[int, str] = {}
        self._calendar_silence: Optional[str] = None
        self._pending_recover: tuple[LivenessIncidentKind, str] | None = None
        #: Counted for the report: how often silence was explained rather
        #: than alarmed. A detector that never suppresses is not measuring.
        self.suppressed_assessments = 0
        self.heartbeat_losses = 0
        #: Whether tick 49 is actually reaching us. A reader interpreting a
        #: GAP_SUSPECTED needs to know whether the halt suppressor was even
        #: connected: with no halt input, a genuine halt is indistinguishable
        #: from a dead subscription, and this detector will call it the
        #: latter. Unknown until the recorder says which it is.
        self._halt_state_available: Optional[bool] = None
        self._halt_state_note: Optional[str] = None

    # -- observations -------------------------------------------------

    def subscription_started(self, now_mono: Optional[float] = None) -> None:
        """Reset the clock origin. Before this there is nothing to judge."""
        self._started_mono = self._now(now_mono)
        self._last_event_mono.clear()
        self._pending_recover = None

    def note_event(self, stream: str, now_mono: Optional[float] = None) -> None:
        self._last_event_mono[stream] = self._now(now_mono)

    def note_halted(self, value: float) -> None:
        """Generic tick 49. ``nan`` means IB has not told us, not "trading".

        0 = not halted, 1 = halted, 2 = halted for volatility (LULD pause).
        An unknown halt state stays unknown: it must not be read as either
        confirmation of trading or an excuse for silence.
        """
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return
        self._halted = int(value)
        self._halt_state_available = True
        self._halt_state_note = "tick 49 observed"

    def note_halt_state_source(self, requested: bool, detail: str = "") -> None:
        """Record whether tick 49 was even asked for.

        Called at subscribe time. If it was not requested, the suppressor is
        blind by construction and the report must say so rather than let a
        reader assume an absent halt marker means "not halted".
        """
        if requested:
            if self._halt_state_available is None:
                self._halt_state_available = None  # decided by arrival or by 321
                self._halt_state_note = detail or "tick 49 requested; awaiting first value"
            return
        self._halt_state_available = False
        self._halt_state_note = detail or "tick 49 not requested"

    def note_halt_state_unavailable(self, detail: str) -> None:
        """The Gateway refused to serve the halt tick (e.g. error 321)."""
        self._halt_state_available = False
        self._halt_state_note = detail

    def note_status(self, code: int, message: str = "") -> None:
        """Classify an IB error/status code into liveness facts."""
        if code == CONNECTIVITY_LOST:
            # Told, not inferred. Reconnecting before IB says it is back
            # just burns the reconnect budget against a known outage.
            self._outages[code] = f"connectivity lost ({code})"
        elif code == CONNECTIVITY_RESTORED_DATA_LOST:
            self._outages.pop(CONNECTIVITY_LOST, None)
            self._pending_recover = (
                LivenessIncidentKind.FEED_OUTAGE,
                f"connectivity restored, market data lost ({code})",
            )
        elif code == CONNECTIVITY_RESTORED_DATA_KEPT:
            self._outages.pop(CONNECTIVITY_LOST, None)
        elif code in FARM_BROKEN_CODES:
            self._outages[code] = f"data farm down ({code}): {message}".strip()
        elif code in FARM_OK_CODES:
            # 2104 clears 2103, 2106 clears 2105, 2158 clears 2157.
            self._outages.pop(code - 1, None)
        elif code == FARM_INACTIVE_CODE:
            # Explicitly not an outage. Recorded by the caller, ignored here.
            return

    def note_transport_idle(self, idle_seconds: float) -> None:
        """ib_async ``timeoutEvent``: nothing at all arrived from TWS.

        Stronger than any per-stream gap, because it does not depend on
        market activity -- TWS keeps talking even when the tape does not.
        """
        self._pending_recover = (
            LivenessIncidentKind.GAP_SUSPECTED,
            f"no data of any kind from TWS for {idle_seconds:.1f}s",
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
        if self._outages:
            return (
                LivenessIncidentKind.FEED_OUTAGE,
                "; ".join(self._outages[code] for code in sorted(self._outages)),
            )
        if self._calendar_silence:
            return (LivenessIncidentKind.EXPECTED_SILENCE, self._calendar_silence)
        return None

    def heartbeat_age(self, now_mono: Optional[float] = None) -> Optional[float]:
        """Seconds since the last 5-second bar, measured from subscribe."""
        if self._started_mono is None:
            return None
        last = self._last_event_mono.get(HEARTBEAT_STREAM, self._started_mono)
        return max(0.0, self._now(now_mono) - last)

    def last_market_event_age(self, now_mono: Optional[float] = None) -> Optional[float]:
        """Seconds since *any* stream last delivered. None before subscribe.

        Evidence that the pipe is carrying something, whatever the bar clock
        says. Used only to veto a destructive repair, never to trigger one:
        the asymmetry is what keeps the "event-driven streams decide nothing"
        rule intact while still letting a live BidAsk save itself from being
        cut off by a reconnect aimed at the bar stream.
        """
        if self._started_mono is None:
            return None
        now = self._now(now_mono)
        last = max(self._last_event_mono.values(), default=self._started_mono)
        return max(0.0, now - last)

    def advisory_ages(self, now_mono: Optional[float] = None) -> dict[str, float]:
        """Over-threshold ages of the event-driven streams. Report only."""
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
        silence_details = self._expected_silence_details()
        silence = None if silence_details is None else silence_details[1]

        if self._started_mono is None:
            return LivenessState(
                action=LivenessAction.CONTINUE,
                reason="not subscribed",
                advisory_ages=advisory,
            )

        # Expected silence outranks everything, including a lost heartbeat.
        # During a halt or a known farm outage the absence of bars is the
        # correct observation, not a fault -- and acting on it would reset a
        # healthy subscription for no reason.
        if silence is not None:
            self.suppressed_assessments += 1
            return LivenessState(
                action=LivenessAction.WAIT,
                reason="silence explained by an explicit IB signal",
                heartbeat_age=age,
                heartbeat_lost=age is not None and age > self.bar_timeout_seconds,
                expected_silence=silence,
                advisory_ages=advisory,
                incident_kind=silence_details[0],
                heartbeat_last_mono=heartbeat_last_mono,
            )

        # Explicit "your subscription is gone" beats waiting for the bar
        # timeout to notice the same thing several seconds later.
        if self._pending_recover is not None:
            incident_kind, reason = self._pending_recover
            self._pending_recover = None
            return LivenessState(
                action=LivenessAction.RECOVER_SUBSCRIPTION,
                reason=reason,
                heartbeat_age=age,
                advisory_ages=advisory,
                incident_kind=incident_kind,
                heartbeat_last_mono=heartbeat_last_mono,
            )

        if age is not None and age > self.bar_timeout_seconds:
            self.heartbeat_losses += 1
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
        """What this detector was configured to do, for the report."""
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
            "suppressed_assessments": self.suppressed_assessments,
            "suppressed_assessments_are_poll_count": True,
            "heartbeat_losses": self.heartbeat_losses,
        }

    def _now(self, now_mono: Optional[float]) -> float:
        return self._clock() if now_mono is None else float(now_mono)
