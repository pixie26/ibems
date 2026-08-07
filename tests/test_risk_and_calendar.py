"""
Risk config, startup self-test, and calendar.

The production failure of a risk engine is "it was configured to allow it",
not "it crashed". These tests target that.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ib_execution.calendar import TradingCalendar
from ib_execution.clock import ManualClock, assert_clock_sane, ClockSkewError
from ib_execution.models import RiskRejection
from ib_execution.risk import (
    HARD_MAX_POSITION_SHARES,
    RiskConfig,
    RiskSelfTestFailed,
    run_self_test,
)


# --------------------------------------------------------------------------
# config validation
# --------------------------------------------------------------------------


def test_config_must_permit_a_full_reversal():
    """
    A flip (+N -> -N) needs 2N shares in ONE order.

    If max_order_shares is below that, every reversal is silently rejected by
    risk: no error, no crash, just a growing pile of RISK_BLOCKED misses while
    the strategy quietly never reverses. Found by scripts/demo.py, not by a
    test -- which is the argument for having a runnable demo at all.
    """
    with pytest.raises(ValueError, match="reversal would be unexecutable"):
        RiskConfig(max_position_shares=5, max_order_shares=5)

    RiskConfig(max_position_shares=5, max_order_shares=10)  # must not raise


def test_config_cannot_exceed_hard_bounds():
    """A fat-fingered YAML must not authorise a position the design cannot survive."""
    with pytest.raises(ValueError, match="hard bound"):
        RiskConfig(max_position_shares=HARD_MAX_POSITION_SHARES + 1)


def test_config_rejects_disabled_overnight_stress():
    """Invariant 19 cannot be reduced to a boolean assertion or disabled."""
    with pytest.raises(ValueError, match="overnight_gap_stress_pct"):
        RiskConfig(overnight_gap_stress_pct=Decimal("0"))


def test_config_hash_is_stable_and_sensitive():
    a = RiskConfig(max_position_shares=5)
    b = RiskConfig(max_position_shares=5)
    c = RiskConfig(max_position_shares=4)
    assert a.config_hash() == b.config_hash()
    assert a.config_hash() != c.config_hash()


# --------------------------------------------------------------------------
# startup self-test (invariant 21)
# --------------------------------------------------------------------------


def test_self_test_proves_checks_are_live():
    clock = ManualClock(datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc))
    proven = run_self_test(RiskConfig(strategy_whitelist=("manual_test",)), clock)
    for expected in (
        "symbol_whitelist",
        "max_order_shares",
        "max_position_shares",
        "quote_stale",
        "max_spread_bps",
        "limit_collar",
        "max_orders_per_minute",
    ):
        assert expected in proven, f"{expected} was not proven live"


def test_self_test_fails_when_a_check_is_disabled():
    """
    The self-test must be able to fail, or it proves nothing.

    We neuter one check and confirm the process would refuse to start.
    """
    import ib_execution.risk as risk_mod

    clock = ManualClock(datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc))
    cfg = RiskConfig(strategy_whitelist=("manual_test",))
    original = risk_mod.RiskEngine.check

    def broken_check(self, intent, *, current_position, quote, is_closing=False):
        if intent.symbol not in self.config.symbol_whitelist:
            return  # bug: silently allow a non-whitelisted symbol
        return original(
            self, intent, current_position=current_position, quote=quote, is_closing=is_closing
        )

    risk_mod.RiskEngine.check = broken_check
    try:
        with pytest.raises(RiskSelfTestFailed, match="symbol_whitelist"):
            run_self_test(cfg, clock)
    finally:
        risk_mod.RiskEngine.check = original


def test_self_test_works_for_a_config_that_is_not_spy_manual_test():
    """The probes used to hardcode SPY/manual_test as the admissible baseline.

    Any configuration that did not happen to whitelist both raised
    ``RiskRejection: symbol_whitelist: SPY`` out of ``run_self_test`` -- and
    therefore out of ``Controller.__init__``. A production config for another
    instrument could not construct a controller at all.
    """
    clock = ManualClock(datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc))
    cfg = RiskConfig(symbol_whitelist=("QQQ",), strategy_whitelist=("momo_v1",))
    proven = run_self_test(cfg, clock)
    for expected in (
        "symbol_whitelist",
        "strategy_whitelist",
        "max_order_shares",
        "max_position_shares",
        "quote_stale",
        "max_spread_bps",
        "limit_collar",
        "no_quote",
        "max_orders_per_minute",
    ):
        assert expected in proven, f"{expected} was not proven live"


def test_a_probe_rejected_by_the_wrong_control_is_not_counted_as_proof():
    """The worse of the two defects: a false pass.

    Every ``RiskRejection`` used to count, whatever fired it. With a config
    whitelisting only QQQ, the SPY-priced ``limit_collar`` probe was rejected
    by ``symbol_whitelist`` and recorded as proof the collar was live. A
    self-test whose purpose is to show that *specific* controls fire cannot
    accept the wrong one firing as proof.
    """
    import ib_execution.risk as risk_mod

    clock = ManualClock(datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc))
    cfg = RiskConfig(strategy_whitelist=("manual_test",))
    original = risk_mod.RiskEngine.check

    def collar_answers_with_the_wrong_reason(self, intent, *, current_position, quote,
                                             is_closing=False):
        # The collar is dead; something unrelated rejects instead.
        if quote is not None and intent.limit_price == Decimal("700"):
            raise RiskRejection("max_spread_bps", "not the collar")
        return original(
            self, intent, current_position=current_position, quote=quote, is_closing=is_closing
        )

    risk_mod.RiskEngine.check = collar_answers_with_the_wrong_reason
    try:
        with pytest.raises(RiskSelfTestFailed, match=r"limit_collar \(rejected by max_spread_bps"):
            run_self_test(cfg, clock)
    finally:
        risk_mod.RiskEngine.check = original


def test_an_empty_whitelist_is_rejected_before_it_reaches_the_self_test():
    """The config layer is the control; the self-test guard is only a backstop.

    ``run_self_test`` indexes ``symbol_whitelist[0]`` for its baseline order, so
    it also refuses an empty one -- but a config that empty never gets built.
    """
    with pytest.raises(ValueError, match="symbol_whitelist empty"):
        RiskConfig(symbol_whitelist=(), strategy_whitelist=("manual_test",))

    clock = ManualClock(datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc))
    hollowed = RiskConfig(strategy_whitelist=("manual_test",))
    object.__setattr__(hollowed, "symbol_whitelist", ())
    with pytest.raises(RiskSelfTestFailed, match="empty symbol or strategy whitelist"):
        run_self_test(hollowed, clock)


# --------------------------------------------------------------------------
# calendar
# --------------------------------------------------------------------------


def test_half_day_moves_the_flatten_deadline():
    """
    Christmas Eve closes at 13:00 ET. A hardcoded 15:50 flatten never fires and
    nothing errors -- you simply wake up long.
    """
    cal = TradingCalendar()
    regular = cal.plan(date(2026, 8, 5))
    half = cal.plan(date(2026, 12, 24))

    from ib_execution.calendar import ET

    assert not regular.is_half_day
    assert half.is_half_day
    # Compare in exchange-local time: UTC offsets differ across DST.
    assert regular.close_utc.astimezone(ET).hour == 16
    assert half.close_utc.astimezone(ET).hour == 13
    assert half.flatten_start_utc < half.close_utc


def test_holiday_is_not_a_trading_day():
    cal = TradingCalendar()
    assert cal.plan(date(2026, 11, 26)).is_trading_day is False  # Thanksgiving
    assert cal.plan(date(2026, 11, 27)).is_trading_day is True   # half day, but open


def test_flatten_window_and_escalation_ordering():
    cal = TradingCalendar()
    p = cal.plan(date(2026, 8, 5))
    assert p.flatten_start_utc <= p.escalation_utc <= p.flatten_hard_utc <= p.close_utc


def test_should_start_flatten_only_inside_window():
    cal = TradingCalendar()
    p = cal.plan(date(2026, 8, 5))
    assert cal.should_start_flatten(p.flatten_start_utc + timedelta(seconds=1))
    assert not cal.should_start_flatten(p.flatten_start_utc - timedelta(minutes=30))
    assert not cal.should_start_flatten(p.close_utc + timedelta(minutes=5))


def test_weekend_is_not_a_trading_day():
    cal = TradingCalendar()
    assert not cal.plan(date(2026, 8, 8)).is_trading_day   # Saturday
    assert not cal.plan(date(2026, 8, 9)).is_trading_day   # Sunday


# --------------------------------------------------------------------------
# clock authority
# --------------------------------------------------------------------------


def test_clock_skew_is_refused():
    """
    Every valid_until check and every EOD deadline rides on the local clock.
    A silently drifting clock produces stale-target rejects and missed flattens,
    and neither announces itself.
    """
    now = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)
    assert assert_clock_sane(now, now + timedelta(seconds=1)) <= 2.0
    with pytest.raises(ClockSkewError):
        assert_clock_sane(now, now + timedelta(seconds=30))
