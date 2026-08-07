"""
Gate B1.2: the holiday table's coverage is enforced, not remembered.

``is_trading_day`` asked only "weekday, and not in the 2026 table". The table
holds 2026, so every 2027 NYSE holiday was a full session with a 16:00 close
and a complete set of flatten and escalation deadlines. Nothing errored; the
engine would simply have traded into a closed market.

No invariant covers "the holiday table is still valid", so no amount of
generated testing would have found it -- which is the general lesson: a
comment saying "review each December" is not a control.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from ib_execution.calendar import (
    HOLIDAYS_2026,
    SUPPORTED_YEARS,
    CalendarCoverageError,
    TradingCalendar,
)

# Every one of these was planned as a regular full session before the guard.
NYSE_HOLIDAYS_2027 = [
    date(2027, 1, 1),     # New Year's Day, a Friday
    date(2027, 1, 18),    # MLK
    date(2027, 3, 26),    # Good Friday
    date(2027, 5, 31),    # Memorial Day
    date(2027, 7, 5),     # Independence Day (observed)
    date(2027, 9, 6),     # Labor Day
    date(2027, 11, 25),   # Thanksgiving
]


@pytest.mark.parametrize("day", NYSE_HOLIDAYS_2027, ids=lambda d: d.isoformat())
def test_an_uncovered_year_is_refused_rather_than_guessed(day):
    calendar = TradingCalendar()
    with pytest.raises(CalendarCoverageError):
        calendar.plan(day)
    with pytest.raises(CalendarCoverageError):
        calendar.is_trading_day(day)


def test_the_refusal_says_how_to_fix_it():
    with pytest.raises(CalendarCoverageError) as excinfo:
        TradingCalendar().plan(date(2027, 1, 1))
    message = str(excinfo.value)
    assert "2026" in message
    assert "SUPPORTED_YEARS" in message
    assert "Refusing to guess" in message


def test_covered_dates_are_unaffected():
    calendar = TradingCalendar()
    plan = calendar.plan(date(2026, 8, 5))
    assert plan.is_trading_day and not plan.is_half_day
    assert calendar.plan(date(2026, 11, 27)).is_half_day
    assert not calendar.plan(date(2026, 12, 25)).is_trading_day


def test_a_year_claimed_as_covered_must_actually_be_in_the_table():
    """The exact mistake the mechanism exists to prevent, caught at construction."""
    with pytest.raises(CalendarCoverageError):
        TradingCalendar(supported_years=frozenset({2026, 2027}))


def test_supported_years_matches_the_shipped_table():
    assert SUPPORTED_YEARS == frozenset({h.year for h in HOLIDAYS_2026})


def test_time_based_predicates_refuse_uncovered_dates():
    calendar = TradingCalendar()
    moment = datetime(2027, 1, 1, 15, 0, tzinfo=timezone.utc)
    for predicate in (calendar.in_session, calendar.should_start_flatten, calendar.past_escalation):
        with pytest.raises(CalendarCoverageError):
            predicate(moment)


def test_self_test_accepts_a_covered_session():
    state = TradingCalendar().self_test(datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc))
    assert state["today"] == "2026-08-05"
    assert state["today_is_trading_day"] is True
    assert state["next_trading_day"] == "2026-08-06"
    assert state["close_utc"] and state["flatten_start_utc"]


def test_self_test_rejects_a_session_it_cannot_bound():
    with pytest.raises(CalendarCoverageError):
        TradingCalendar().self_test(datetime(2027, 1, 4, 15, 0, tzinfo=timezone.utc))


def test_self_test_rejects_a_year_end_it_cannot_see_past():
    """The last covered days fail because the *next* session is uncovered.

    Deliberate: an engine that cannot say when the next session starts should
    refuse in December, loudly, rather than discover it on 4 January.
    """
    with pytest.raises(CalendarCoverageError):
        TradingCalendar().self_test(datetime(2026, 12, 31, 15, 0, tzinfo=timezone.utc))


def test_self_test_rejects_misordered_deadlines():
    """A flatten lead shorter than the escalation lead inverts the sequence."""
    calendar = TradingCalendar(flatten_lead_minutes=5, escalation_minutes=30)
    with pytest.raises(ValueError, match="out of order"):
        calendar.self_test(datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc))


def test_next_trading_day_skips_weekends_and_holidays():
    calendar = TradingCalendar()
    assert calendar.next_trading_day(date(2026, 8, 7)) == date(2026, 8, 10)   # Fri -> Mon
    assert calendar.next_trading_day(date(2026, 12, 24)) == date(2026, 12, 28)  # Christmas


def test_next_trading_day_refuses_to_walk_off_the_table():
    with pytest.raises(CalendarCoverageError):
        TradingCalendar().next_trading_day(date(2026, 12, 31))


def test_an_exhausted_horizon_is_not_reported_as_a_coverage_problem():
    """"No trading day in the window" and "the table stops here" are different facts.

    Reporting both as a coverage error would send an operator to extend the
    holiday table when the real answer is that the table is already wrong.
    """
    calendar = TradingCalendar()
    with pytest.raises(RuntimeError, match="almost certainly wrong") as excinfo:
        calendar.next_trading_day(date(2026, 12, 24), horizon_days=1)  # 12-25 is Christmas
    assert not isinstance(excinfo.value, CalendarCoverageError)
