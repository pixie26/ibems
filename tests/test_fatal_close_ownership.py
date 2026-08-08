"""Fatal host shutdown must not release writer ownership before process death."""

from types import SimpleNamespace

from ib_execution.execution_host import ExecutionHost


class _JournalProbe:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


def _bare_host(*, fatal: bool):
    host = object.__new__(ExecutionHost)
    host.journal = _JournalProbe()
    host.controller = SimpleNamespace(fatal_shutdown_requested=fatal)
    return host


def test_graceful_close_closes_journal_and_releases_reference():
    host = _bare_host(fatal=False)
    journal = host.journal
    host.close()
    assert journal.close_calls == 1
    assert host.journal is None


def test_fatal_close_retains_journal_until_process_death():
    """A writer still blocked in fsync must not lose the process ownership lock.

    Journal.close() uses a bounded writer join and then releases its ProcessLock.
    On a fatal storage path the writer may still be inside the kernel after that
    join. ExecutionHost therefore deliberately skips graceful Journal.close():
    the Journal object and its OS lock remain live until the process exits, at
    which point the kernel releases the lock atomically with process death.
    """
    host = _bare_host(fatal=True)
    journal = host.journal
    host.close()
    assert journal.close_calls == 0
    assert host.journal is journal
