"""
Cross-process exclusive ownership, proved with real processes.

An in-process test of a cross-process lock proves almost nothing, so the
important cases here spawn actual subprocesses and kill them.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from ib_execution.processlock import ProcessLock, ProcessLockUnavailable

HOLDER = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {src!r})
    from ib_execution.processlock import ProcessLock
    lock = ProcessLock(sys.argv[1])
    lock.acquire(note="holder")
    print("ACQUIRED", flush=True)
    time.sleep(120)
    """
)

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _spawn_holder(path: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER.format(src=SRC), str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line.strip() == "ACQUIRED":
            return proc
        if proc.poll() is not None:
            raise AssertionError("holder exited before acquiring")
    raise AssertionError("holder never acquired the lock")


def test_second_holder_is_refused_while_first_lives(tmp_path):
    path = tmp_path / "owner.lock"
    holder = _spawn_holder(path)
    try:
        with pytest.raises(ProcessLockUnavailable):
            ProcessLock(path).acquire()
    finally:
        holder.kill()
        holder.wait(timeout=20)


def test_kernel_releases_the_lock_when_the_holder_is_force_killed(tmp_path):
    """The property the whole design rests on: no lease, no stale-lock reaper.

    SIGKILL/TerminateProcess gives the holder no chance to clean up, which is
    exactly the case a PID file gets wrong.
    """
    path = tmp_path / "owner.lock"
    holder = _spawn_holder(path)
    with pytest.raises(ProcessLockUnavailable):
        ProcessLock(path).acquire()

    holder.send_signal(signal.SIGKILL if os.name != "nt" else signal.SIGTERM)
    holder.wait(timeout=20)

    successor = ProcessLock(path)
    successor.acquire(note="successor")
    try:
        assert successor.held
    finally:
        successor.release()


def test_release_allows_a_successor(tmp_path):
    path = tmp_path / "owner.lock"
    first = ProcessLock(path)
    first.acquire()
    first.release()
    second = ProcessLock(path)
    second.acquire()
    second.release()


def test_holder_diagnostics_name_the_owning_pid(tmp_path):
    path = tmp_path / "owner.lock"
    with ProcessLock(path).acquire(note="unit-test"):
        assert f"pid={os.getpid()}" in path.read_text(encoding="utf-8")
        assert "unit-test" in path.read_text(encoding="utf-8")


def test_refusal_reports_the_holder(tmp_path):
    path = tmp_path / "owner.lock"
    holder = _spawn_holder(path)
    try:
        with pytest.raises(ProcessLockUnavailable) as excinfo:
            ProcessLock(path).acquire()
        assert str(holder.pid) in excinfo.value.holder
    finally:
        holder.kill()
        holder.wait(timeout=20)


def test_context_manager_releases_on_exception(tmp_path):
    path = tmp_path / "owner.lock"
    with pytest.raises(ValueError):
        with ProcessLock(path):
            raise ValueError("boom")
    ProcessLock(path).acquire().release()
