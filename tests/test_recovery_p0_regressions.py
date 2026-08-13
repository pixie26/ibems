from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from ib_execution.market_liveness import (
    LivenessAction,
    LivenessState,
    MarketLiveness,
    RecoveryHint,
)
from ib_execution.quote_recorder import QuoteRecorder, RecoveryPlan, RecoveryScheduler


def _plan(*, capture: bool, transport: bool) -> RecoveryPlan:
    scheduler = RecoveryScheduler()  # production defaults: fast_attempts=2
    return scheduler.plan(
        evidence_of_life=capture,
        transport_evidence=transport,
        now_mono=0.0,
    )


@pytest.mark.parametrize(
    ("capture", "transport", "expected"),
    [
        (True, False, RecoveryPlan.BARS_ONLY),
        (True, True, RecoveryPlan.BARS_ONLY),
        (False, True, RecoveryPlan.ALL_MARKET_STREAMS),
        (False, False, RecoveryPlan.FULL_RECONNECT),
    ],
)
def test_production_default_recovery_matrix(capture, transport, expected):
    assert _plan(capture=capture, transport=transport) is expected


def test_capture_life_vetoes_fast_full_reconnect_with_production_defaults():
    scheduler = RecoveryScheduler()

    first = scheduler.plan(
        evidence_of_life=True, transport_evidence=True, now_mono=0.0
    )

    assert first is RecoveryPlan.BARS_ONLY
    assert scheduler.fast_full_attempts == 0


def test_transport_only_evidence_repairs_all_capture_streams_without_socket_reset():
    scheduler = RecoveryScheduler()

    assert scheduler.plan(
        evidence_of_life=False, transport_evidence=True, now_mono=0.0
    ) is RecoveryPlan.ALL_MARKET_STREAMS
    assert scheduler.plan(
        evidence_of_life=False, transport_evidence=True, now_mono=1.0
    ) is RecoveryPlan.NONE
    assert scheduler.plan(
        evidence_of_life=False, transport_evidence=True, now_mono=121.0
    ) is RecoveryPlan.ALL_MARKET_STREAMS
    assert scheduler.fast_full_attempts == 0


def test_no_positive_bar_means_fast_attempts_are_not_rearmed_forever():
    scheduler = RecoveryScheduler()

    assert scheduler.plan(
        evidence_of_life=False, transport_evidence=False, now_mono=0.0
    ) is RecoveryPlan.FULL_RECONNECT
    # A reconnect creates a 12s liveness grace period, but no real BAR_5S has
    # arrived here.  The second genuine timeout consumes the second fast slot.
    assert scheduler.plan(
        evidence_of_life=False, transport_evidence=False, now_mono=12.1
    ) is RecoveryPlan.FULL_RECONNECT
    assert scheduler.plan(
        evidence_of_life=False, transport_evidence=False, now_mono=24.2
    ) is RecoveryPlan.NONE
    assert scheduler.plan(
        evidence_of_life=False, transport_evidence=False, now_mono=312.2
    ) is RecoveryPlan.FULL_RECONNECT


def test_only_a_real_bar_handler_resets_recovery_state(tmp_path):
    recorder = QuoteRecorder(tmp_path)
    scheduler = recorder.recovery
    scheduler.plan(evidence_of_life=False, transport_evidence=False, now_mono=0.0)
    assert scheduler.fast_used == 1

    recorder._note_handled("BID_ASK")
    assert scheduler.fast_used == 1

    recorder._note_handled("BAR_5S")
    assert scheduler.fast_used == 0


def test_production_loop_cannot_reset_recovery_from_continue_state():
    source = inspect.getsource(QuoteRecorder._run_session_loop)
    assert "self.recovery.note_recovered()" not in source


def test_transport_evidence_is_veto_only_and_revoked_by_timeout():
    liveness = MarketLiveness()
    liveness.subscription_started(0.0)
    assert liveness.transport_evidence() is True

    liveness.note_transport_idle(60.0)
    assert liveness.transport_evidence() is False
    assert liveness.assess(0.1).action is LivenessAction.RECOVER_SUBSCRIPTION

    liveness.note_transport_activity()
    assert liveness.transport_evidence() is True


def test_1101_10225_and_1102_map_to_one_recovery_pipeline():
    lost = MarketLiveness()
    lost.subscription_started(0.0)
    lost.note_status(1101, "Connectivity restored - data lost")
    state_1101 = lost.assess(0.1)
    assert state_1101.action is LivenessAction.RECOVER_SUBSCRIPTION
    assert state_1101.recovery_hint is RecoveryHint.ALL_MARKET_STREAMS

    bars = MarketLiveness()
    bars.subscription_started(0.0)
    bars.note_status(10225, "Bust event occurred, current subscription deactivated")
    state_10225 = bars.assess(0.1)
    assert state_10225.action is LivenessAction.RECOVER_SUBSCRIPTION
    assert state_10225.recovery_hint is RecoveryHint.BARS_ONLY

    kept = MarketLiveness()
    kept.subscription_started(0.0)
    kept.note_status(1100, "Connectivity lost")
    kept.note_status(1102, "Connectivity restored - data maintained")
    state_1102 = kept.assess(0.1)
    assert state_1102.action is LivenessAction.CONTINUE
    assert state_1102.recovery_hint is None


class _FakeIB:
    def __init__(self):
        self.cancelled = []
        self.requested = []
        self.disconnected = False

    def sleep(self, _seconds):
        return None

    def cancelTickByTickData(self, _contract, tick_type):
        self.cancelled.append(("tbt", tick_type))

    def cancelRealTimeBars(self, _bars):
        self.cancelled.append(("bars", None))

    def reqTickByTickData(self, _contract, tick_type, _number, _ignore_size):
        handle = object()
        self.requested.append(("tbt", tick_type, handle))
        return handle

    def reqRealTimeBars(self, _contract, _size, _what, _rth):
        handle = object()
        self.requested.append(("bars", "TRADES", handle))
        return handle

    def disconnect(self):
        self.disconnected = True


def test_explicit_1101_style_repair_rebuilds_three_streams_not_socket(tmp_path):
    recorder = QuoteRecorder(tmp_path)
    recorder._wire_ticker = lambda _ticker: None
    recorder._wire_bars = lambda _bars: None
    recorder.liveness.subscription_started(0.0)
    ib = _FakeIB()
    contract = SimpleNamespace(conId=756733)
    old_tickers = (object(), object())
    old_bars = object()
    state = LivenessState(
        action=LivenessAction.RECOVER_SUBSCRIPTION,
        reason="1101",
        recovery_hint=RecoveryHint.ALL_MARKET_STREAMS,
    )

    tickers, bars = recorder._recover_market_data(
        ib, contract, old_tickers, old_bars, state
    )

    assert ib.disconnected is False
    assert ib.cancelled == [("tbt", "BidAsk"), ("tbt", "AllLast"), ("bars", None)]
    assert [item[:2] for item in ib.requested] == [
        ("tbt", "BidAsk"),
        ("tbt", "AllLast"),
        ("bars", "TRADES"),
    ]
    assert tickers[0] is ib.requested[0][2]
    assert tickers[1] is ib.requested[1][2]
    assert bars is ib.requested[2][2]


def test_10197_remains_fatal():
    assert QuoteRecorder._is_fatal_market_data_error(10197, "") is True
