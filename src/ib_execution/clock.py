"""
Clock abstraction.

Two reasons this exists:

1. Determinism. Timeouts, valid_until and EOD deadlines are state transitions.
   If they depend on wall time, the state machine cannot be property-tested.

2. Clock authority. Every valid_until check and every EOD flatten deadline
   depends on the local clock being right. SPEC requires we verify local clock
   against IB server time at startup and periodically, and refuse to start if
   the skew exceeds threshold.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...
    def monotonic_ns(self) -> int: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


class ManualClock:
    """Test clock. Time only moves when the test says so."""

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("ManualClock requires tz-aware start")
        self._now = start.astimezone(timezone.utc)
        self._mono = 0

    def now(self) -> datetime:
        return self._now

    def monotonic_ns(self) -> int:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)
        self._mono += int(seconds * 1e9)

    def set(self, dt: datetime) -> None:
        self._now = dt.astimezone(timezone.utc)


class ClockSkewError(RuntimeError):
    pass


def assert_clock_sane(
    local_now: datetime, broker_now: datetime, max_skew_seconds: float = 2.0
) -> float:
    """
    Compare local clock to broker server time.

    Called at startup (refuse to start on failure) and periodically (STOP_NEW
    on failure). A silently drifting local clock produces stale-target rejects
    and missed EOD flattens, and neither failure announces itself.
    """
    skew = abs((local_now - broker_now).total_seconds())
    if skew > max_skew_seconds:
        raise ClockSkewError(
            f"local clock differs from broker by {skew:.3f}s "
            f"(max {max_skew_seconds}s). Check NTP."
        )
    return skew
