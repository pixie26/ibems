"""
Trading calendar and end-of-day deadlines.

Half days are a classic path to an unintended overnight position: the close
moves to 13:00 ET and a hardcoded 15:50 flatten never fires. Nothing errors;
you simply wake up long.

V1 ships a small explicit holiday table rather than pulling exchange_calendars,
so the dependency surface stays flat and the table is auditable by eye. Swap in
exchange_calendars when the instrument set grows past one.

THE TABLE'S COVERAGE IS ENFORCED, NOT REMEMBERED
------------------------------------------------
"Review each December for the following year" was the entire control, and a
comment is not a control. ``is_trading_day`` asked only "is it a weekday and
not in the 2026 table", so every 2027 NYSE holiday -- New Year's Day, MLK,
Good Friday, the observed Independence Day, Thanksgiving -- was planned as a
full session with a 16:00 close, complete with flatten and escalation
deadlines. Nothing would have errored; the engine would simply have tried to
trade into a closed market, and no invariant covers "the holiday table is
still valid" so no amount of generated testing would have found it.

``plan()`` now refuses any date outside ``SUPPORTED_YEARS``. Adding a year is
an explicit code change that gets re-verified, which is the point: being
unable to trade on the first business day of 2027 is a loud, cheap failure,
and trading through a closed session is neither.
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

# The years the tables above actually describe. Extending this set without
# extending the tables is the bug it exists to prevent, so the constructor
# checks that every listed year contains at least one holiday.
SUPPORTED_YEARS: frozenset[int] = frozenset({2026})


class CalendarCoverageError(RuntimeError):
    """A date outside the holiday table's verified coverage.

    Fail-closed on purpose. The alternative -- treating an unknown year as
    "weekdays are trading days" -- silently produces a full session plan for
    every holiday in that year.
    """

    def __init__(self, d: date, supported: frozenset[int]):
        self.date = d
        self.supported = supported
        years = ", ".join(str(y) for y in sorted(supported))
        super().__init__(
            f"{d} is outside the verified calendar coverage ({years}). Extend "
            f"HOLIDAYS_*/HALF_DAYS_* and SUPPORTED_YEARS in calendar.py, then "
            f"re-verify. Refusing to guess a session plan."
        )


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
        supported_years: frozenset[int] = SUPPORTED_YEARS,
    ):
        self.holidays = holidays
        self.half_days = half_days
        self.flatten_lead_minutes = flatten_lead_minutes
        self.flatten_hard_minutes = flatten_hard_minutes
        self.escalation_minutes = escalation_minutes
        self.supported_years = supported_years
        # A year claimed as covered but with no holidays in the table is the
        # failure this whole mechanism exists to catch -- catch it at
        # construction rather than on the first holiday of that year.
        for year in sorted(supported_years):
            if not any(h.year == year for h in holidays):
                raise CalendarCoverageError(date(year, 1, 1), supported_years)

    def covers(self, d: date) -> bool:
        return d.year in self.supported_years

    def require_coverage(self, d: date) -> None:
        if not self.covers(d):
            raise CalendarCoverageError(d, self.supported_years)

    def is_trading_day(self, d: date) -> bool:
        self.require_coverage(d)
        return d.weekday() < 5 and d not in self.holidays

    def plan(self, d: date) -> SessionPlan:
        self.require_coverage(d)
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

    def next_trading_day(self, after: date, horizon_days: int = 10) -> date:
        """The next covered trading day, or a coverage error at the edge.

        Deliberately raises rather than returning None when the search walks
        off the end of the table: "no trading day in the next ten days" and
        "the table stops here" are different facts and must not look alike.
        """
        for offset in range(1, horizon_days + 1):
            candidate = after + timedelta(days=offset)
            self.require_coverage(candidate)      # walking off the table
            if self.is_trading_day(candidate):
                return candidate
        # Distinct from a coverage error on purpose: the table was adequate and
        # genuinely contains no trading day in the window, which for NYSE means
        # the holiday table is wrong.
        raise RuntimeError(
            f"no trading day within {horizon_days} days after {after}; "
            "the holiday table is almost certainly wrong"
        )

    def self_test(self, now_utc: datetime) -> dict[str, object]:
        """Startup must-reject self-test (invariant 21).

        Checks the four things a session actually needs, at boot, rather than
        discovering any of them missing at 15:50 on a half day:
        today is covered, the next session is covered, this session's deadlines
        exist and are ordered, and the half-day rule for today is known.

        Raises ``CalendarCoverageError`` rather than returning a verdict --
        there is no useful degraded mode for an engine that cannot say when
        the market closes.
        """
        today = now_utc.astimezone(ET).date()
        self.require_coverage(today)
        upcoming = self.next_trading_day(today)

        plan = self.plan(today)
        if plan.is_trading_day:
            deadlines = (
                plan.open_utc, plan.flatten_start_utc,
                plan.escalation_utc, plan.flatten_hard_utc, plan.close_utc,
            )
            if any(value is None for value in deadlines):
                raise CalendarCoverageError(today, self.supported_years)
            ordered = [value for value in deadlines if value is not None]
            if ordered != sorted(ordered):
                raise ValueError(
                    f"{today}: session deadlines are out of order ({plan.describe()}); "
                    "flatten lead/hard/escalation minutes are misconfigured"
                )

        return {
            "today": today.isoformat(),
            "today_is_trading_day": plan.is_trading_day,
            "today_is_half_day": plan.is_half_day,
            "next_trading_day": upcoming.isoformat(),
            "supported_years": sorted(self.supported_years),
            "close_utc": plan.close_utc.isoformat() if plan.close_utc else None,
            "flatten_start_utc": (
                plan.flatten_start_utc.isoformat() if plan.flatten_start_utc else None
            ),
        }
