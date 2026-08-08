"""
Cross-process exclusive ownership.

    ####################################################################
    #  The kernel releases these locks when the holding process dies,  #
    #  including on SIGKILL and TerminateProcess. That is the entire   #
    #  reason to use an OS lock instead of a lockfile with a PID in    #
    #  it: there is no lease to renew and no stale state to reap.      #
    ####################################################################

WHY THIS EXISTS
---------------
Two components in this repository were single-writer by architectural
convention and by nothing else:

``Journal``
    Held a ``threading.Lock``, which is per-process. SQLite in WAL mode
    happily admits a second writing process, so two execution hosts on one
    journal each keep their own in-memory state machine and each send orders.
    Invariants 1-4 are all stated per-process; none of them survive that.

``RawEventLog``
    Renames every ``.partial-*`` segment it finds at construction, which
    means a second recorder started on the same session steals the file the
    first one is still writing.

Both are the same missing primitive. A test that asserts "no second writer
appears" only proves the test did not start one; this makes the property
structural.

A note on scope: this is mutual exclusion between processes on one host
sharing one filesystem. It is not a distributed lock, and the single-account
ownership argument in ADR-001 assumes one host. Network filesystems do not
implement either backend reliably -- keep journals and recordings local.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

if os.name == "nt":  # pragma: no cover - exercised on the Windows runtime
    import msvcrt
else:
    import fcntl


class ProcessLockUnavailable(RuntimeError):
    """Another live process owns this resource. Never retry into this."""

    def __init__(self, path: Path, holder: str = ""):
        self.path = path
        self.holder = holder
        detail = f" (held by {holder})" if holder else ""
        super().__init__(
            f"another process already owns {path}{detail}; refusing to start a "
            "second writer"
        )


class ProcessLock:
    """A non-blocking, kernel-released, exclusive lock on a sidecar file.

    Non-blocking on purpose. Waiting for the other writer to exit is never
    the right behaviour here: if a second execution host is running, the
    correct action is to fail loudly, not to queue up behind it and start
    trading the moment it dies.

    The file's *contents* are diagnostics only -- who to go looking for. The
    lock is the control, and it holds even if the contents are unwritable.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fd: Optional[int] = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def _read_holder(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip()[:200]
        except OSError:
            return ""

    def acquire(self, note: str = "") -> "ProcessLock":
        if self._fd is not None:
            raise RuntimeError(f"{self.path} is already held by this ProcessLock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # O_RDWR, not O_TRUNC: truncating before we know we own the lock would
        # destroy the running holder's diagnostics.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            self._lock_fd(fd)
        except OSError as exc:
            holder = self._read_holder()
            os.close(fd)
            raise ProcessLockUnavailable(self.path, holder) from exc

        self._fd = fd
        try:
            os.truncate(fd, 0)
            payload = f"pid={os.getpid()}"
            if note:
                payload += f" {note}"
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
        except OSError:
            # Diagnostics are best-effort; ownership is already established.
            pass
        return self

    @staticmethod
    def _lock_fd(fd: int) -> None:
        if os.name == "nt":  # pragma: no cover - exercised on the Windows runtime
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_fd(fd: int) -> None:
        if os.name == "nt":  # pragma: no cover - exercised on the Windows runtime
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            self._unlock_fd(fd)
        except OSError:
            pass
        os.close(fd)

    def __enter__(self) -> "ProcessLock":
        if not self.held:
            self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()
