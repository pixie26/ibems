from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, TypeVar

from ib_execution.processlock import ProcessLock, ProcessLockUnavailable

T = TypeVar("T")


def call_after_known_exit(
    factory: Callable[[], T],
    *,
    retry_exceptions: tuple[type[BaseException], ...],
    timeout: float = 2.0,
    retry_seconds: float = 0.01,
) -> tuple[T, dict[str, int]]:
    """Call a successor factory after the test proved the holder exited."""

    started = time.monotonic()
    deadline = started + timeout
    refused_retries = 0
    retry_wait_seconds = 0.0
    while True:
        try:
            value = factory()
        except retry_exceptions:
            refused_retries += 1
            now = time.monotonic()
            if now >= deadline:
                raise
            delay = min(retry_seconds, deadline - now)
            time.sleep(delay)
            retry_wait_seconds += delay
            continue
        elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
        return value, {
            "lock_refused_retries": refused_retries,
            "retry_wait_ms": max(0, round(retry_wait_seconds * 1000)),
            "acquire_elapsed_ms": elapsed_ms,
        }


def acquire_process_lock_after_known_exit(
    path: str | Path,
    *,
    note: str,
    timeout: float = 2.0,
    retry_seconds: float = 0.01,
) -> tuple[ProcessLock, dict[str, int]]:
    """Acquire only after the test has proved the prior holder exited.

    Windows can briefly retain a byte-range lock after ``Popen.wait()`` has
    returned.  The production primitive deliberately remains non-blocking;
    this bounded test helper measures that post-exit kernel-release boundary.
    """

    return call_after_known_exit(
        lambda: ProcessLock(path).acquire(note=note),
        retry_exceptions=(ProcessLockUnavailable,),
        timeout=timeout,
        retry_seconds=retry_seconds,
    )
