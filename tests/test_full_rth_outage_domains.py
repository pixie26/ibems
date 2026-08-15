from __future__ import annotations

from ib_execution.market_liveness import (
    LivenessAction,
    LivenessIncidentKind,
    LivenessIncidentTracker,
    MarketLiveness,
)


def _live() -> MarketLiveness:
    live = MarketLiveness()
    live.subscription_started(0.0)
    return live


def test_20260814_secdef_2157_2158_does_not_create_realtime_outage() -> None:
    """Regression for the observed 2.8s 2157 -> 2158 interval with live bars."""
    live = _live()
    incidents = LivenessIncidentTracker()

    live.note_event("BAR_5S", 1.0)
    live.note_status(2157, "Sec-def data farm connection is broken:secdefil")
    first = live.assess(1.1)
    markers = incidents.observe(first, 1.1)

    live.note_event("BAR_5S", 2.8)
    live.note_status(2158, "Sec-def data farm connection is OK:secdefil")
    second = live.assess(2.9)
    markers.extend(incidents.observe(second, 2.9))

    assert first.action is LivenessAction.CONTINUE
    assert second.action is LivenessAction.CONTINUE
    assert markers == []
    assert incidents.manifest(2.9)["incident_count"] == 0
    assert "security_definition" in live.manifest()["farm_advisories"]


def test_historical_farm_status_is_advisory_only() -> None:
    live = _live()
    live.note_event("BAR_5S", 10.0)
    live.note_status(2105, "HMDS data farm connection is broken:ushmds")

    state = live.assess(10.1)

    assert state.action is LivenessAction.CONTINUE
    assert state.incident_kind is None
    assert "historical" in live.manifest()["farm_advisories"]


def test_realtime_farm_warning_with_live_bars_is_degradation_not_outage() -> None:
    live = _live()
    live.note_status(2103, "Market data farm connection is broken:usfarm")
    live.note_event("BAR_5S", 10.0)

    state = live.assess(10.1)

    assert state.action is LivenessAction.CONTINUE
    assert state.incident_kind is None
    assert live.manifest()["realtime_market_data_farm_degraded"] is not None


def test_realtime_farm_warning_plus_bar_loss_is_feed_outage() -> None:
    live = _live()
    live.note_event("BAR_5S", 5.0)
    live.note_status(2103, "Market data farm connection is broken:usfarm")

    state = live.assess(20.0)

    assert state.action is LivenessAction.WAIT
    assert state.heartbeat_lost is True
    assert state.incident_kind is LivenessIncidentKind.FEED_OUTAGE


def test_bar_loss_without_realtime_farm_evidence_is_gap_suspected() -> None:
    live = _live()
    live.note_event("BAR_5S", 5.0)

    state = live.assess(20.0)

    assert state.action is LivenessAction.RECOVER_SUBSCRIPTION
    assert state.incident_kind is LivenessIncidentKind.GAP_SUSPECTED


def test_1100_is_immediate_feed_outage_even_before_bar_timeout() -> None:
    live = _live()
    live.note_event("BAR_5S", 10.0)
    live.note_status(1100, "Connectivity between IB and TWS has been lost")

    state = live.assess(10.1)

    assert state.action is LivenessAction.WAIT
    assert state.heartbeat_lost is False
    assert state.incident_kind is LivenessIncidentKind.FEED_OUTAGE
