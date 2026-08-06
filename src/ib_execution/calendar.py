"""
Trading calendar and end-of-day deadlines.

Half days are a classic path to an unintended overnight position: the close
moves to 13:00 ET and a hardcoded 15:50 flatten never fires. Nothing errors;
you simply wake up long.

V1 ships a small explicit holiday table rather than pulling exchange_calendars,
so the dependency surface stays flat and the table is auditable by eye. Swap in
exchange_calendars when the instrument set grows past one.

TABLE MUST BE REVIEWED ANNUALLY -- see docs/RUNBOOK.md.
"""

from __future__ import annotations

import zoneinfo
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

ET = zoneinfo.ZoneInfo("America/New_York")

# NYSE full holidays. Review each December for the following year.
HOLIDAYS_2026: frozenset[date] = frozenset(
    {
        date(2026, 1, 1),    # New Year's Day
        date(2026, 1, 19),   # MLK
        date(2026, 2, 16),   # Presidents' Day
        date(2026, 4, 3),    # Good Friday
        date(2026, 5, 25),   # Memorial Day
        date(2026, 6, 19),   # Juneteenth
        date(2026, 7, 3),    # Independence Day (observed)
        date(2026, 9, 7),    # Labor Day
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 12, 25),  # Christmas
    }
)

# Early closes: 13:00 ET.
HALF_DAYS_2026: frozenset[date] = frozenset(
    {
        date(2026, 11, 27),  # day after Thanksgiving
        date(2026, 12, 24),  # Christmas Eve
    }
)

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
HALF_DAY_CLOSE = time(13, 0)


@dataclass(frozen=True)
class SessionPlan:
    """Every time-driven decision for one session, computed once at boot."""

    session_date: date
    is_trading_day: bool
    is_half_day: bool
    open_utc: datetime | None
    close_utc: datetime | None
    flatten_start_utc: datetime | None       # begin working out of the position
    flatten_hard_utc: datetime | None        # last acceptable moment
    escalation_utc: datetime | None          # page a human past this point

    def describe(self) -> str:
        if not self.is_trading_day:
            return f"{self.session_date}: not a trading day"
        kind = "half day" if self.is_half_day else "regular"
        return (
            f"{self.session_date} ({kind}) close={self.close_utc:%H:%M}Z "
            f"flatten_start={self.flatten_start_utc:%H:%M}Z "
            f"escalate={self.escalation_utc:%H:%M}Z"
        )


class TradingCalendar:
    def __init__(
        self,
        holidays: frozenset[date] = HOLIDAYS_2026,
        half_days: frozenset[date] = HALF_DAYS_2026,
        flatten_lead_minutes: int = 20,
        flatten_hard_minutes: int = 3,
        escalation_minutes: int = 15,
    ):
        self.holidays = holidays
        self.half_days = half_days
        self.flatten_lead_minutes = flatten_lead_minutes
        self.flatten_hard_minutes = flatten_hard_minutes
        self.escalation_minutes = escalation_minutes

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def plan(self, d: date) -> SessionPlan:
        if not self.is_trading_day(d):
            return SessionPlan(d, False, False, None, None, None, None, None)

        half = d in self.half_days
        close_t = HALF_DAY_CLOSE if half else REGULAR_CLOSE
        open_et = datetime.combine(d, REGULAR_OPEN, tzinfo=ET)
        close_et = datetime.combine(d, close_t, tzinfo=ET)

        flatten_start = close_et - timedelta(minutes=self.flatten_lead_minutes)
        flatten_hard = close_et - timedelta(minutes=self.flatten_hard_minutes)
        escalation = close_et - timedelta(minutes=self.escalation_minutes)

        return SessionPlan(
            session_date=d,
            is_trading_day=True,
            is_half_day=half,
            open_utc=open_et.astimezone(timezone.utc),
            close_utc=close_et.astimezone(timezone.utc),
            flatten_start_utc=flatten_start.astimezone(timezone.utc),
            flatten_hard_utc=flatten_hard.astimezone(timezone.utc),
            escalation_utc=escalation.astimezone(timezone.utc),
        )

    def plan_for(self, now_utc: datetime) -> SessionPlan:
        return self.plan(now_utc.astimezone(ET).date())

    def in_session(self, now_utc: datetime) -> bool:
        p = self.plan_for(now_utc)
        if not p.is_trading_day or p.open_utc is None or p.close_utc is None:
            return False
        return p.open_utc <= now_utc < p.close_utc

    def should_start_flatten(self, now_utc: datetime) -> bool:
        p = self.plan_for(now_utc)
        if not p.is_trading_day or p.flatten_start_utc is None or p.close_utc is None:
            return False
        return p.flatten_start_utc <= now_utc < p.close_utc

    def past_escalation(self, now_utc: datetime) -> bool:
        """
        Past this point, an unresolved sync/order state is a human page,
        not a log line. See SPEC invariant 19 rationale.
        """
        p = self.plan_for(now_utc)
        if not p.is_trading_day or p.escalation_utc is None:
            return False
        return now_utc >= p.escalation_utc
