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
    LivenessIncidentKind,
    LivenessIncidentTracker,
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
    assert manifest["suppressed_assessments_are_poll_count"] is True
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


# -- incident lifecycle, not poll-count logging -----------------------


def test_a_hundred_second_feed_outage_emits_three_rows_not_four_hundred():
    liveness = _live()
    incidents = LivenessIncidentTracker(checkpoint_seconds=60.0)
    liveness.note_status(1100, "Connectivity between IB and TWS has been lost")

    markers = []
    for quarter_second in range(1, 401):
        now = quarter_second / 4.0
        markers.extend(incidents.observe(liveness.assess(now), now))

    liveness.note_status(1102, "Connectivity restored - data maintained")
    # 1102 says the subscription was maintained; the first returned bar is
    # still the positive observation that closes the coverage incident.
    liveness.note_event("BAR_5S", 100.25)
    markers.extend(incidents.observe(liveness.assess(100.25), 100.25))

    assert len(markers) == 3
    assert markers[0].startswith("FEED_OUTAGE_START:")
    assert markers[1].startswith("FEED_OUTAGE_CHECKPOINT:")
    assert markers[2].startswith("FEED_OUTAGE_END:")
    assert all("GAP_SUSPECTED" not in marker for marker in markers)
    assert "duration=100.0s" in markers[2]

    manifest = incidents.manifest(100.25)
    assert manifest["incident_count"] == 1
    assert manifest["completed_incident_count"] == 1
    assert manifest["open_incident_count"] == 0
    assert manifest["incident_by_kind"] == {"FEED_OUTAGE": 1}
    assert manifest["total_seconds_by_kind"]["FEED_OUTAGE"] == pytest.approx(100.0)


def test_a_material_feed_outage_change_emits_one_update():
    liveness = _live()
    incidents = LivenessIncidentTracker()
    liveness.note_status(1100, "Connectivity lost")
    start = incidents.observe(liveness.assess(1.0), 1.0)

    liveness.note_status(1101, "Connectivity restored - data lost")
    update = incidents.observe(liveness.assess(2.0), 2.0)

    assert len(start) == 1
    assert len(update) == 1
    assert update[0].startswith("FEED_OUTAGE_UPDATE:")
    assert "action=recover_subscription" in update[0]
    assert "market data lost (1101)" in update[0]


def test_unexplained_bar_loss_is_one_gap_incident_until_recovery():
    liveness = _live()
    incidents = LivenessIncidentTracker(checkpoint_seconds=60.0)

    start = incidents.observe(liveness.assess(12.1), 12.1)
    duplicate = incidents.observe(liveness.assess(12.35), 12.35)
    liveness.note_event("BAR_5S", 12.5)
    end = incidents.observe(liveness.assess(12.5), 12.5)

    assert len(start) == 1
    assert start[0].startswith("GAP_SUSPECTED_START:")
    assert duplicate == []
    assert len(end) == 1
    assert end[0].startswith("GAP_SUSPECTED_END:")
    assert "max_bar_age=12.3s" in end[0]


def test_reconnect_grace_period_does_not_claim_recovery_before_a_bar():
    liveness = _live()
    incidents = LivenessIncidentTracker()
    incidents.observe(liveness.assess(12.1), 12.1)

    liveness.subscription_started(20.0)
    no_bar_yet = incidents.observe(liveness.assess(20.1), 20.1)
    liveness.note_event("BAR_5S", 20.2)
    first_bar = incidents.observe(liveness.assess(20.2), 20.2)

    assert no_bar_yet == []
    assert len(first_bar) == 1
    assert first_bar[0].startswith("GAP_SUSPECTED_END:")
    assert "recovery_reason=bar cadence intact" in first_bar[0]


@pytest.mark.parametrize(
    ("silence", "configure"),
    [
        ("halt", lambda live: live.note_halted(1)),
        ("calendar", lambda live: live.enter_calendar_silence("closing auction")),
    ],
)
def test_legitimate_silence_is_not_labelled_as_a_data_gap(silence, configure):
    liveness = _live()
    incidents = LivenessIncidentTracker()
    configure(liveness)

    state = liveness.assess(30.0)
    markers = incidents.observe(state, 30.0)

    assert silence in {"halt", "calendar"}
    assert state.incident_kind is LivenessIncidentKind.EXPECTED_SILENCE
    assert markers[0].startswith("EXPECTED_SILENCE_START:")
    assert "GAP_SUSPECTED" not in markers[0]


def test_incident_kind_transition_closes_old_before_starting_new():
    liveness = _live()
    incidents = LivenessIncidentTracker()
    liveness.note_halted(1)
    incidents.observe(liveness.assess(20.0), 20.0)

    liveness.note_halted(0)
    markers = incidents.observe(liveness.assess(21.0), 21.0)

    assert len(markers) == 2
    assert markers[0].startswith("EXPECTED_SILENCE_END:")
    assert markers[1].startswith("GAP_SUSPECTED_START:")


def test_unclosed_incident_is_explicit_in_the_manifest():
    liveness = _live()
    incidents = LivenessIncidentTracker()
    liveness.note_status(1100, "Connectivity lost")
    incidents.observe(liveness.assess(1.0), 1.0)

    manifest = incidents.manifest(11.0)

    assert manifest["open_incident_count"] == 1
    assert manifest["completed_incident_count"] == 0
    assert manifest["open_incident"]["kind"] == "FEED_OUTAGE"
    assert manifest["open_incident"]["duration_seconds"] == pytest.approx(10.0)


def test_clean_boundary_closes_an_open_incident():
    liveness = _live()
    incidents = LivenessIncidentTracker()
    liveness.enter_calendar_silence("closing auction")
    incidents.observe(liveness.assess(1.0), 1.0)

    markers = incidents.close("session ended", 5.0)

    assert len(markers) == 1
    assert markers[0].startswith("EXPECTED_SILENCE_END:")
    assert "recovery_reason=session ended" in markers[0]
    assert incidents.manifest(5.0)["open_incident_count"] == 0


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
    it from a quiet market. The incident edge must therefore be written before
    an actionable verdict ends the connection.
    """
    import inspect
    import textwrap

    from ib_execution import quote_recorder

    source = _loop_source()
    assert "_liveness_incidents.observe" in source
    assert "special_conditions=marker" in source
    marker_at = source.index("_liveness_incidents.observe")
    react_at = source.index("_recover_market_data")
    assert marker_at < react_at, "the raw log must be marked before the run reacts"

    # The repair itself is also on the record, including which repair was
    # chosen and the evidence it was chosen on, before it is carried out.
    repair = textwrap.dedent(
        inspect.getsource(quote_recorder.QuoteRecorder._recover_market_data)
    )
    assert repair.index("RECOVERY_ATTEMPT") < repair.index("SlowRecoveryReconnect(")
    assert repair.index("RECOVERY_ATTEMPT") < repair.index("cancelRealTimeBars")


def test_the_production_loop_no_longer_stops_on_event_driven_gaps():
    """The regression this replaces: aborting on a quiet BID_ASK/ALL_LAST tape."""
    source = _loop_source()
    assert "stream_staleness" not in source
    assert "STREAM_STALE" not in source


def test_the_production_loop_asks_for_the_halt_state():
    assert "note_halted" in _loop_source()


# -- the halt suppressor must admit when it has no input --------------


def test_a_gateway_that_refuses_tick_49_is_recorded_as_a_blind_suppressor():
    """Real Gateway, 2026-08-12: error 321 for tick 49 on a STK contract.

    Requesting it was argued to strictly dominate not requesting it. It does
    not: the rejection took the whole reqMktData probe with it, and the run
    failed its prerequisites with three zero streams. What survives is the
    reporting duty -- with no halt input, a genuine halt looks exactly like a
    dead subscription to this detector, and the manifest has to say so
    instead of letting an absent halt marker read as "not halted".
    """
    liveness = _live()
    liveness.note_halt_state_source(True, detail="market_data_generic_ticks='49'")
    liveness.note_halt_state_unavailable("IB rejected the market-data request (321)")

    manifest = liveness.manifest()

    assert manifest["halt_state_available"] is False
    assert "321" in str(manifest["halt_state_note"])
    # Blind is not the same as "not halted": silence stays unexplained.
    assert liveness.expected_silence() is None
    assert liveness.assess(300.0).action is LivenessAction.RECOVER_SUBSCRIPTION


def test_not_requesting_the_halt_tick_is_disclosed_too():
    """The default path. Blind by construction is still blind."""
    liveness = _live()
    liveness.note_halt_state_source(False, detail="market_data_generic_ticks=''")

    assert liveness.manifest()["halt_state_available"] is False


def test_an_arriving_halt_value_proves_the_suppressor_is_connected():
    liveness = _live()
    liveness.note_halt_state_source(True)
    assert liveness.manifest()["halt_state_available"] is None  # asked, not yet answered

    liveness.note_halted(0.0)

    assert liveness.manifest()["halt_state_available"] is True


# -- recovery policy: repair the smallest broken thing ----------------


def _scheduler(**kwargs):
    from ib_execution.quote_recorder import RecoveryScheduler

    defaults = dict(
        fast_attempts=2, bars_only_seconds=120.0,
        slow_base_seconds=300.0, slow_max_seconds=1800.0,
    )
    return RecoveryScheduler(**{**defaults, **kwargs})


def _plan_names(scheduler, *, evidence_of_life, times):
    return [
        scheduler.plan(evidence_of_life=evidence_of_life, now_mono=t).value for t in times
    ]


def test_live_quotes_veto_a_reconnect_and_get_a_targeted_bar_repair():
    """Tearing down a working socket to fix one stream is the wrong repair.

    At the RTH rate observed on 2026-08-10 (25,665 BidAsk in 120.1s, ~214/s)
    a full reconnect costs thousands of quotes. Re-requesting only the bar
    stream costs none, which is what makes a short retry interval defensible.
    """
    from ib_execution.quote_recorder import RecoveryPlan

    scheduler = _scheduler(fast_attempts=0)

    first = scheduler.plan(evidence_of_life=True, now_mono=0.0)
    immediately_after = scheduler.plan(evidence_of_life=True, now_mono=1.0)
    after_the_interval = scheduler.plan(evidence_of_life=True, now_mono=121.0)

    assert first is RecoveryPlan.BARS_ONLY
    assert immediately_after is RecoveryPlan.NONE  # paced, not every poll
    assert after_the_interval is RecoveryPlan.BARS_ONLY
    assert scheduler.bars_only_attempts == 2
    assert scheduler.slow_full_attempts == 0


def test_total_silence_earns_a_full_reconnect_that_backs_off():
    """Nothing is arriving, so a reconnect destroys nothing and may fix it.

    Rate follows the cost of retrying, not the severity: free here, so start
    short, then stretch out because the hundredth identical failure is
    neither more likely to work nor worth another audit row.
    """
    from ib_execution.quote_recorder import RecoveryPlan

    scheduler = _scheduler(fast_attempts=0, slow_base_seconds=300.0, slow_max_seconds=1200.0)
    fired = []
    now = 0.0
    for _ in range(6):
        for _ in range(20_000):  # four hours of 0.25s polls, compressed
            if scheduler.plan(evidence_of_life=False, now_mono=now) is (
                RecoveryPlan.FULL_RECONNECT
            ):
                fired.append(now)
                break
            now += 0.25
        now += 0.25

    gaps = [round(b - a) for a, b in zip(fired, fired[1:])]
    assert gaps == [300, 600, 1200, 1200, 1200], gaps
    assert scheduler.bars_only_attempts == 0


def test_the_first_attempts_are_immediate():
    """A genuinely dead subscription should not wait five minutes."""
    from ib_execution.quote_recorder import RecoveryPlan

    scheduler = _scheduler(fast_attempts=2)

    assert scheduler.plan(evidence_of_life=False, now_mono=0.0) is RecoveryPlan.FULL_RECONNECT
    assert scheduler.plan(evidence_of_life=False, now_mono=0.25) is RecoveryPlan.FULL_RECONNECT
    assert scheduler.plan(evidence_of_life=False, now_mono=0.5) is RecoveryPlan.NONE


def test_any_inbound_message_resets_the_backoff():
    """A farm coming back at 14:00 must not wait out a 30-minute timer."""
    from ib_execution.quote_recorder import RecoveryPlan

    scheduler = _scheduler(fast_attempts=0, slow_base_seconds=300.0, slow_max_seconds=1800.0)
    scheduler.plan(evidence_of_life=False, now_mono=0.0)
    scheduler.plan(evidence_of_life=False, now_mono=300.0)
    scheduler.plan(evidence_of_life=False, now_mono=900.0)  # delay now 1200s

    # Without this the next attempt would not be due until t=2100.
    scheduler.note_activity(now_mono=900.0)

    assert scheduler.plan(evidence_of_life=False, now_mono=1199.0) is RecoveryPlan.NONE
    assert scheduler.plan(evidence_of_life=False, now_mono=1200.1) is (
        RecoveryPlan.FULL_RECONNECT
    )


def test_a_chatty_peer_cannot_turn_the_backoff_into_a_hammer():
    """IB emits several messages around an outage; each one must not re-arm."""
    from ib_execution.quote_recorder import RecoveryPlan

    scheduler = _scheduler(fast_attempts=0, slow_base_seconds=300.0)
    assert scheduler.plan(evidence_of_life=False, now_mono=0.0) is RecoveryPlan.FULL_RECONNECT

    fired = 0
    for tick in range(1, 1200):  # 300s of 0.25s polls, a message on every one
        now = tick * 0.25
        scheduler.note_activity(now_mono=now)
        if scheduler.plan(evidence_of_life=False, now_mono=now) is RecoveryPlan.FULL_RECONNECT:
            fired += 1

    assert fired == 0, "no second attempt may land inside one base interval"


def test_a_real_bar_puts_everything_back_to_square_one():
    from ib_execution.quote_recorder import RecoveryPlan

    scheduler = _scheduler(fast_attempts=1)
    scheduler.plan(evidence_of_life=False, now_mono=0.0)
    assert scheduler.plan(evidence_of_life=False, now_mono=1.0) is RecoveryPlan.NONE

    scheduler.note_recovered()

    assert scheduler.plan(evidence_of_life=False, now_mono=2.0) is RecoveryPlan.FULL_RECONNECT


def test_the_scheduler_never_returns_an_exit():
    """The owner's decision, made structural: unexplained silence never quits.

    A recorder's only product is data, so ending the session turns a
    labelled gap into no data for the rest of the day. The retry loop is
    bounded in rate and in cost, and its stop condition is the session
    close -- not a counter that can fire at 11:00.
    """
    from ib_execution.quote_recorder import RecoveryPlan

    scheduler = _scheduler()
    seen = set()
    now = 0.0
    for _ in range(200_000):  # well past a full trading day of polls
        seen.add(scheduler.plan(evidence_of_life=now % 2 < 1, now_mono=now))
        now += 0.25

    assert seen <= set(RecoveryPlan)
    assert scheduler.manifest()["exits_on_unexplained_silence"] is False


def test_recovery_intervals_are_validated():
    with pytest.raises(ValueError, match="must not be below"):
        _scheduler(slow_base_seconds=600.0, slow_max_seconds=300.0)
    with pytest.raises(ValueError, match="must be positive"):
        _scheduler(bars_only_seconds=0.0)
