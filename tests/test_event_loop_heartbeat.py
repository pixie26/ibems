from __future__ import annotations

import json
import time

import pytest

from ib_execution.event_loop_heartbeat import EventLoopHeartbeat, HeartbeatPublishFailed
from ib_execution.watchdog import Watchdog, WatchdogConfig


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


def test_publisher_thread_does_not_mask_a_wedged_event_loop(tmp_path):
    path = tmp_path / "recorder-status.json"
    heartbeat = EventLoopHeartbeat(path, component="test-recorder", publish_seconds=0.02)
    heartbeat.start()
    heartbeat.pulse(phase="CAPTURING")
    _wait_until(lambda: json.loads(path.read_text())["phase"] == "CAPTURING")
    first = json.loads(path.read_text())
    time.sleep(0.08)
    second = json.loads(path.read_text())
    assert second["publisher_wall"] > first["publisher_wall"]
    assert second["heartbeat_mono"] == first["heartbeat_mono"]

    watchdog = Watchdog(
        WatchdogConfig(status_path=path, heartbeat_timeout_seconds=0.05),
        alert=lambda *_: None,
    )
    verdict = watchdog.evaluate(second["heartbeat_mono"] + 0.06, second)
    assert not verdict.healthy
    assert "heartbeat stale" in verdict.reason
    heartbeat.close()


def test_event_loop_pulse_refreshes_the_observed_timestamp(tmp_path):
    path = tmp_path / "recorder-status.json"
    heartbeat = EventLoopHeartbeat(path, component="test-recorder", publish_seconds=0.02)
    heartbeat.start()
    before = json.loads(path.read_text())["heartbeat_mono"]
    time.sleep(0.02)
    heartbeat.pulse(phase="CAPTURING")
    _wait_until(lambda: json.loads(path.read_text())["heartbeat_mono"] > before)
    heartbeat.close()


def test_finalize_progress_has_its_own_clock_and_does_not_fake_event_loop_liveness(
    tmp_path,
):
    path = tmp_path / "recorder-status.json"
    heartbeat = EventLoopHeartbeat(path, component="test-recorder", publish_seconds=0.02)
    heartbeat.start()
    heartbeat.pulse(phase="CAPTURING")
    _wait_until(lambda: json.loads(path.read_text())["phase"] == "CAPTURING")
    event_loop_pulse = json.loads(path.read_text())["heartbeat_mono"]

    time.sleep(0.02)
    heartbeat.finalize_progress(
        stage="READING_RAW", rows_processed=50_000, segments_total=91
    )
    _wait_until(lambda: json.loads(path.read_text())["phase"] == "FINALIZING")
    status = json.loads(path.read_text())
    assert status["heartbeat_mono"] == event_loop_pulse
    assert status["finalize_progress_mono"] > event_loop_pulse
    assert status["finalize_stage"] == "READING_RAW"
    assert status["finalize_rows_processed"] == 50_000
    assert status["finalize_segments_total"] == 91
    heartbeat.close()


def test_publisher_failure_is_visible_to_the_event_loop(tmp_path, monkeypatch):
    path = tmp_path / "recorder-status.json"
    heartbeat = EventLoopHeartbeat(path, component="test-recorder", publish_seconds=0.01)
    heartbeat.start()

    def fail():
        raise OSError("status disk unavailable")

    monkeypatch.setattr(heartbeat, "_publish", fail)
    _wait_until(lambda: not heartbeat._thread.is_alive())
    with pytest.raises(HeartbeatPublishFailed, match="status disk unavailable"):
        heartbeat.pulse(phase="CAPTURING")
