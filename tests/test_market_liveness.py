"""
The property under test is a separation, not a threshold.

A quiet tape and a dead subscription must produce *different* verdicts.
Most of these tests are therefore pairs: the same silence, once with an
explanation from IB and once without.
"""

from __future__ import annotations

import math

import pytest

from ib_execution.market_liveness import (
    BAR_PERIOD_SECONDS,
    DEFAULT_BAR_TIMEOUT_SECONDS,
    MIN_BAR_TIMEOUT_SECONDS,
    LivenessAction,
    MarketLiveness,
)


def _live(**kwargs) -> MarketLiveness:
    liveness = MarketLiveness(**kwargs)
    liveness.subscription_started(0.0)
    return liveness


def _bars_through(liveness: MarketLiveness, until: float) -> None:
    """Feed a perfectly regular bar cadence, as IB actually delivers it."""
    tick = BAR_PERIOD_SECONDS
    while tick <= until:
        liveness.note_event("BAR_5S", tick)
        tick += BAR_PERIOD_SECONDS


# -- the central claim ------------------------------------------------


def test_a_silent_tape_with_a_live_bar_cadence_is_not_a_fault():
    """The OVERNIGHT case: 13 prints in two minutes, bars never missed.

    Reproduces the shape of docs/GATE_B2_OVERNIGHT_20260810.md section 3.
    The old per-stream thresholds aborted here; that made a normal illiquid
    window indistinguishable from a dead subscription.
    """
    liveness = _live()
    _bars_through(liveness, 120.0)
    liveness.note_event("ALL_LAST", 5.4)  # next print does not come until 107.5
    for tenth in range(1, 1200):
        liveness.note_event("BID_ASK", tenth / 10.0)

    # Sampled at the widest point of the print gap: 101.6s without a trade.
    state = liveness.assess(107.0)

    assert state.action is LivenessAction.CONTINUE
    assert state.heartbeat_lost is False
    # The quiet is still *reported*, just not acted on.
    assert state.advisory_ages["ALL_LAST"] == pytest.approx(101.6)
    assert "BID_ASK" not in state.advisory_ages


def test_a_missing_bar_with_no_explanation_demands_recovery():
    """Same silence, no explanation: this one must not be tolerated."""
    liveness = _live()
    _bars_through(liveness, 60.0)

    state = liveness.assess(60.0 + DEFAULT_BAR_TIMEOUT_SECONDS + 0.1)

    assert state.action is LivenessAction.RECOVER_SUBSCRIPTION
    assert state.heartbeat_lost is True
    assert "no explanation from IB" in state.reason


def test_advisory_streams_can_never_stop_the_run():
    """No age on an event-driven stream may produce a non-CONTINUE action."""
    liveness = _live()
    for elapsed in range(1, 200):
        liveness.note_event("BAR_5S", float(elapsed))
        state = liveness.assess(float(elapsed))
        assert state.action is LivenessAction.CONTINUE
    # ...while both event-driven streams have been silent the entire time.
    assert set(liveness.assess(199.0).advisory_ages) == {"BID_ASK", "ALL_LAST"}


# -- expected silence -------------------------------------------------


@pytest.mark.parametrize("halted", [1, 2])
def test_a_halt_explains_the_missing_bars(halted):
    liveness = _live()
    _bars_through(liveness, 30.0)
    liveness.note_halted(halted)

    state = liveness.assess(300.0)

    assert state.action is LivenessAction.WAIT
    assert state.expected_silence is not None
    # The gap is still visible; it is simply attributed rather than alarmed.
    assert state.heartbeat_lost is True


def test_an_unknown_halt_state_is_not_an_excuse_for_silence():
    """nan means IB has not told us. That is not permission to stay quiet."""
    liveness = _live()
    liveness.note_halted(float("nan"))
    liveness.note_halted(None)

    state = liveness.assess(300.0)

    assert state.expected_silence is None
    assert state.action is LivenessAction.RECOVER_SUBSCRIPTION


def test_a_resumed_instrument_stops_excusing_silence():
    liveness = _live()
    liveness.note_halted(1)
    assert liveness.assess(300.0).action is LivenessAction.WAIT

    liveness.note_halted(0)

    assert liveness.assess(300.0).action is LivenessAction.RECOVER_SUBSCRIPTION


def test_a_broken_data_farm_explains_silence_until_it_is_declared_ok():
    liveness = _live()
    liveness.note_status(2103, "Market data farm connection is broken:usfarm")
    assert liveness.assess(300.0).action is LivenessAction.WAIT

    liveness.note_status(2104, "Market data farm connection is OK:usfarm")

    assert liveness.assess(300.0).action is LivenessAction.RECOVER_SUBSCRIPTION


def test_the_noisiest_status_code_in_the_api_is_not_an_outage():
    """2108 is 'inactive but available upon demand' -- IB's words, not an error.

    Treating it as an outage is how a liveness detector becomes something
    the operator mutes, taking the real signals with it.
    """
    liveness = _live()
    liveness.note_status(2108, "Market data farm connection is inactive")

    assert liveness.expected_silence() is None


def test_calendar_silence_suppresses_and_then_releases():
    liveness = _live()
    liveness.enter_calendar_silence("closing auction")
    assert liveness.assess(300.0).action is LivenessAction.WAIT

    liveness.leave_calendar_silence()

    assert liveness.assess(300.0).action is LivenessAction.RECOVER_SUBSCRIPTION


# -- explicit signals outrank waiting for a timeout -------------------


def test_1101_demands_resubscription_without_waiting_for_the_bar_timeout():
    """After 1101 the subscriptions are gone. Waiting 12s to infer that is worse."""
    liveness = _live()
    _bars_through(liveness, 30.0)
    liveness.note_status(1101, "Connectivity restored - data lost")

    state = liveness.assess(30.1)

    assert state.action is LivenessAction.RECOVER_SUBSCRIPTION
    assert state.heartbeat_lost is False  # the bar clock has not even expired


def test_1102_keeps_market_data_and_needs_no_resubscription():
    """1102 says subscriptions survived. Resubscribing would be a self-inflicted gap."""
    liveness = _live()
    _bars_through(liveness, 30.0)
    liveness.note_status(1100, "Connectivity lost")
    assert liveness.assess(30.1).action is LivenessAction.WAIT

    liveness.note_status(1102, "Connectivity restored - data maintained")

    assert liveness.assess(30.1).action is LivenessAction.CONTINUE


def test_a_known_outage_does_not_burn_the_reconnect_budget():
    """1100 means IB already told us. Reconnecting before 1101/1102 is churn."""
    liveness = _live()
    liveness.note_status(1100, "Connectivity between IB and TWS has been lost")

    assert liveness.assess(600.0).action is LivenessAction.WAIT


def test_transport_idle_is_reported_once_per_observation():
    """A pending recovery must not latch and re-fire forever after one event."""
    liveness = _live()
    _bars_through(liveness, 30.0)
    liveness.note_transport_idle(60.0)

    assert liveness.assess(30.1).action is LivenessAction.RECOVER_SUBSCRIPTION
    assert liveness.assess(30.2).action is LivenessAction.CONTINUE


def test_transport_idle_beats_a_healthy_bar_clock():
    """Nothing at all from TWS is stronger evidence than any per-stream age."""
    liveness = _live()
    _bars_through(liveness, 30.0)
    liveness.note_transport_idle(60.0)

    state = liveness.assess(30.1)

    assert state.heartbeat_age == pytest.approx(0.1)
    assert state.action is LivenessAction.RECOVER_SUBSCRIPTION


# -- configuration and bookkeeping ------------------------------------


def test_a_timeout_shorter_than_two_bar_periods_is_refused():
    """One period plus jitter is an alarm generator, not a detector."""
    with pytest.raises(ValueError, match="two bar periods"):
        MarketLiveness(bar_timeout_seconds=MIN_BAR_TIMEOUT_SECONDS - 0.1)


def test_the_default_timeout_tolerates_one_missed_delivery():
    assert DEFAULT_BAR_TIMEOUT_SECONDS >= 2 * BAR_PERIOD_SECONDS


def test_nothing_is_judged_before_the_subscription_starts():
    liveness = MarketLiveness()

    state = liveness.assess(10_000.0)

    assert state.action is LivenessAction.CONTINUE
    assert state.heartbeat_age is None
    assert liveness.advisory_ages(10_000.0) == {}


def test_resubscribing_resets_the_bar_clock():
    """Otherwise every recovery would immediately re-trigger itself."""
    liveness = _live()
    assert liveness.assess(300.0).action is LivenessAction.RECOVER_SUBSCRIPTION

    liveness.subscription_started(300.0)

    assert liveness.assess(300.1).action is LivenessAction.CONTINUE


def test_the_manifest_records_what_was_suppressed_and_what_was_lost():
    """A detector that never reports its own suppressions is not measurable."""
    liveness = _live()
    liveness.note_halted(1)
    liveness.assess(300.0)
    liveness.note_halted(0)
    liveness.assess(300.0)

    manifest = liveness.manifest()

    assert manifest["heartbeat_stream"] == "BAR_5S"
    assert manifest["advisory_streams_never_stop_the_run"] is True
    assert manifest["suppressed_assessments"] == 1
    assert manifest["heartbeat_losses"] == 1


def test_the_marker_carries_the_reason_into_the_raw_log():
    liveness = _live()
    liveness.note_halted(2)

    marker = liveness.assess(300.0).as_marker()

    assert "action=wait" in marker
    assert "expected_silence=" in marker
    assert "bar_age=300.0s" in marker


def test_ages_never_go_negative_on_a_clock_that_appears_to_move_backwards():
    liveness = _live()
    liveness.note_event("BAR_5S", 100.0)

    assert liveness.heartbeat_age(99.0) == 0.0
    assert not math.isnan(liveness.heartbeat_age(99.0))


# -- the production loop must actually use all of this ----------------


def _loop_source() -> str:
    import inspect
    import textwrap

    from ib_execution import quote_recorder

    return textwrap.dedent(
        inspect.getsource(quote_recorder.QuoteRecorder._run_session_loop)
    )


def test_the_production_loop_arms_the_transport_watchdog_and_rearms_it():
    """ib_async calls setTimeout(0) on itself after firing (wrapper.py:466).

    Arming it once and never re-arming yields exactly one warning per
    connection, which is indistinguishable from a watchdog that works.
    """
    import ast

    from ib_execution import quote_recorder

    tree = ast.parse(_loop_source())
    set_timeout_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "setTimeout"
    ]
    # One to arm at connect, one to re-arm inside the timeout handler.
    assert len(set_timeout_calls) >= 2
    assert "timeoutEvent" in _loop_source()
    assert quote_recorder.RecorderConfig(root=".").transport_idle_timeout_seconds > 0


def test_the_production_loop_marks_the_data_before_it_reacts():
    """A gap the log does not admit to is the failure this layer prevents.

    An unlabelled gap silently claims continuity, and a backtest cannot tell
    it from a quiet market. The marker must therefore be written for every
    non-CONTINUE verdict, not only the ones that end the session.
    """
    source = _loop_source()
    assert "GAP_SUSPECTED" in source
    marker_at = source.index("GAP_SUSPECTED")
    raise_at = source.index("market data not live")
    assert marker_at < raise_at, "the raw log must be marked before the run reacts"


def test_the_production_loop_no_longer_stops_on_event_driven_gaps():
    """The regression this replaces: aborting on a quiet BID_ASK/ALL_LAST tape."""
    source = _loop_source()
    assert "stream_staleness" not in source
    assert "STREAM_STALE" not in source


def test_the_production_loop_asks_for_the_halt_state():
    assert "note_halted" in _loop_source()
