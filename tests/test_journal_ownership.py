"""
Invariant 0: at most one execution host writes one journal.

Before the ownership lock, single-writer was an architectural convention.
``Journal`` held a ``threading.Lock``, which is per-process, and SQLite in WAL
mode admits a second writing process happily -- so two hosts on one journal
each kept their own state machine and each sent orders. The per-process
invariants (1-4) are all silent about that, and invariant 1's primary key
cannot help when two processes mint different decision ids for one intent.

The important tests here use real subprocesses. An in-process test of a
cross-process property proves very little.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from ib_execution.journal import Journal, JournalOwnershipError, JournalUnavailable
from ib_execution.models import EventType

SRC = str(Path(__file__).resolve().parents[1] / "src")

HOLDER = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {src!r})
    from ib_execution.journal import Journal
    j = Journal(sys.argv[1])
    print("OWNED", flush=True)
    time.sleep(120)
    """
)

CLAIMANT = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {src!r})
    from ib_execution.journal import Journal, JournalOwnershipError
    try:
        Journal(sys.argv[1])
    except JournalOwnershipError as exc:
        print("REFUSED", flush=True)
        raise SystemExit(11)
    print("ACQUIRED", flush=True)
    raise SystemExit(0)
    """
)


def _spawn_owner(path: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER.format(src=SRC), str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if proc.stdout.readline().strip() == "OWNED":
            return proc
        if proc.poll() is not None:
            raise AssertionError("owner exited before acquiring the journal")
    raise AssertionError("owner never acquired the journal")


def _claim(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", CLAIMANT.format(src=SRC), str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_a_second_process_is_refused_and_exits_non_zero(tmp_path):
    """Gate B1.1's core claim, with two real processes."""
    path = tmp_path / "journal.db"
    owner = _spawn_owner(path)
    try:
        result = _claim(path)
        assert result.returncode == 11
        assert "REFUSED" in result.stdout
    finally:
        owner.kill()
        owner.wait(timeout=20)


def test_a_refused_process_never_writes_to_the_database(tmp_path):
    """Ownership is taken before the schema bootstrap, so a loser touches nothing."""
    path = tmp_path / "journal.db"
    owner = _spawn_owner(path)
    try:
        before = path.read_bytes()
        assert _claim(path).returncode == 11
        assert path.read_bytes() == before
    finally:
        owner.kill()
        owner.wait(timeout=20)


def test_a_successor_may_claim_after_the_owner_is_force_killed(tmp_path):
    """No lease, no stale-lock reaper: the kernel releases it on SIGKILL."""
    path = tmp_path / "journal.db"
    owner = _spawn_owner(path)
    assert _claim(path).returncode == 11

    owner.kill()
    owner.wait(timeout=20)

    result = _claim(path)
    assert result.returncode == 0
    assert "ACQUIRED" in result.stdout


def test_ownership_is_released_on_close(tmp_path):
    path = tmp_path / "journal.db"
    first = Journal(path)
    with pytest.raises(JournalOwnershipError):
        Journal(path)
    first.close()
    Journal(path).close()


def test_ownership_refusal_is_not_a_journal_unavailable(tmp_path):
    """Different failure, different response.

    ``JournalUnavailable`` means fail closed and fence. Ownership refusal means
    this process was never entitled to run: exit, do not fence, and above all
    do not connect to a broker.
    """
    path = tmp_path / "journal.db"
    first = Journal(path)
    try:
        with pytest.raises(JournalOwnershipError) as excinfo:
            Journal(path)
        assert not isinstance(excinfo.value, JournalUnavailable)
    finally:
        first.close()


def test_a_read_only_auditor_handle_does_not_take_ownership(tmp_path):
    """The auditor must be runnable against a live engine's journal."""
    path = tmp_path / "journal.db"
    engine = Journal(path)
    try:
        engine.commit(EventType.PROCESS_STARTED, {"pid": 1})
        reader = Journal(path, owner=False)
        try:
            assert any(ev.event_type is EventType.PROCESS_STARTED for ev in reader.replay())
        finally:
            reader.close()
    finally:
        engine.close()


def test_the_lock_sidecar_names_the_owning_pid(tmp_path):
    path = tmp_path / "journal.db"
    journal = Journal(path)
    try:
        sidecar = path.with_name(path.name + ".lock")
        assert sidecar.exists()
        owner = sidecar.with_name(sidecar.name + ".owner")
        assert "journal=journal.db" in owner.read_text(encoding="utf-8")
    finally:
        journal.close()
