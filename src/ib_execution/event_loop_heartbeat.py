"""Heartbeat publication that cannot be refreshed by a wedged event loop."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .watchdog import Watchdog


class HeartbeatPublishFailed(RuntimeError):
    """The external watchdog can no longer observe the recorder safely."""


class EventLoopHeartbeat:
    """Publish the last pulse supplied by the event loop from another thread.

    The publisher thread never invents a fresh ``heartbeat_mono``. If an IB
    request blocks the only event loop, this thread keeps publishing the same
    old pulse and an external watchdog can detect the increasing age.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        component: str,
        publish_seconds: float = 1.0,
    ) -> None:
        if publish_seconds <= 0:
            raise ValueError("heartbeat publish interval must be positive")
        self.path = Path(path)
        self.component = component
        self.publish_seconds = float(publish_seconds)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._pulse_mono = time.monotonic()
        self._pulse_wall = time.time()
        self._state: dict[str, Any] = {"phase": "STARTING"}
        self._failure: BaseException | None = None
        self._started = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"{component}-heartbeat-publisher",
            daemon=True,
        )

    def pulse(self, **state: Any) -> None:
        self.raise_if_failed()
        with self._lock:
            self._pulse_mono = time.monotonic()
            self._pulse_wall = time.time()
            self._state.update(state)

    def _payload(self) -> dict[str, Any]:
        with self._lock:
            payload = dict(self._state)
            payload.update(
                {
                    "component": self.component,
                    "pid": os.getpid(),
                    "pid_start_ticks": Watchdog._pid_start_ticks(os.getpid()),
                    "heartbeat_mono": self._pulse_mono,
                    "heartbeat_wall": self._pulse_wall,
                    "publisher_wall": time.time(),
                    "operating_mode": "READ_ONLY",
                    "net_position": 0,
                }
            )
            return payload

    def _publish(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._payload(), indent=2, sort_keys=True), encoding="utf-8")
        # A Windows reader opens the current file without FILE_SHARE_DELETE,
        # so even an atomic replace can briefly return ERROR_ACCESS_DENIED.
        # The watchdog read is short; retry for one publish interval instead
        # of declaring the observation channel dead on that benign collision.
        deadline = time.monotonic() + max(1.0, self.publish_seconds)
        while True:
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._publish()
            except BaseException as exc:
                with self._lock:
                    self._failure = exc
                return
            self._stop.wait(self.publish_seconds)

    def start(self) -> None:
        if self._started:
            return
        self._publish()
        self._started = True
        self._thread.start()

    def raise_if_failed(self) -> None:
        with self._lock:
            failure = self._failure
        if failure is not None:
            raise HeartbeatPublishFailed(
                f"event-loop heartbeat publication failed: {failure}"
            ) from failure

    def close(self, *, phase: str = "STOPPED", timeout: float = 5.0) -> None:
        if not self._started:
            return
        with self._lock:
            self._state["phase"] = phase
        self._stop.set()
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise HeartbeatPublishFailed("heartbeat publisher did not stop")
        self._publish()
        self.raise_if_failed()

    def __enter__(self) -> "EventLoopHeartbeat":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close(phase="FAILED" if exc_type else "STOPPED")
