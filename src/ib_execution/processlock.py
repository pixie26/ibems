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
import time
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
        self.owner_path = self.path.with_name(self.path.name + ".owner")
        self._fd: Optional[int] = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def _read_holder(self) -> str:
        # Windows byte-range locking may prevent a second handle from reading
        # the lock file at all. Ownership therefore stays in the kernel lock,
        # while diagnostics live in an unlocked sidecar.
        for candidate in (self.owner_path, self.path):
            try:
                text = candidate.read_text(encoding="utf-8").strip()[:500]
            except OSError:
                continue
            if text:
                return text
        return ""

    @staticmethod
    def _process_start_identity() -> str:
        """Return an OS process-start identity when available.

        The value is diagnostic, not a lease: the kernel lock is always the
        control. Including it prevents an operator from confusing a reused PID
        with the process that originally acquired the resource.
        """

        if os.name == "nt":  # pragma: no cover - Windows runtime only
            try:
                import ctypes
                from ctypes import wintypes

                created = wintypes.FILETIME()
                exited = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.GetCurrentProcess.restype = wintypes.HANDLE
                kernel32.GetProcessTimes.argtypes = (
                    wintypes.HANDLE,
                    ctypes.POINTER(wintypes.FILETIME),
                    ctypes.POINTER(wintypes.FILETIME),
                    ctypes.POINTER(wintypes.FILETIME),
                    ctypes.POINTER(wintypes.FILETIME),
                )
                kernel32.GetProcessTimes.restype = wintypes.BOOL
                handle = kernel32.GetCurrentProcess()
                ok = kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(exited),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                )
                if ok:
                    value = (created.dwHighDateTime << 32) | created.dwLowDateTime
                    return f"win_filetime={value}"
            except (AttributeError, OSError):
                pass
        else:
            try:
                # /proc/<pid>/stat field 22 is start time in clock ticks since
                # boot. Split after ')' because the process name may contain spaces.
                tail = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1]
                return f"proc_start_ticks={tail.split()[19]}"
            except (OSError, IndexError):
                pass
        return f"acquired_wall_ns={time.time_ns()}"

    def _write_diagnostics(self, fd: int, note: str) -> None:
        payload = f"pid={os.getpid()} {self._process_start_identity()}"
        if note:
            payload += f" {note}"
        encoded = payload.encode("utf-8")
        os.truncate(fd, 0)
        os.write(fd, encoded)
        os.fsync(fd)

        owner_fd = os.open(
            self.owner_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644
        )
        try:
            os.write(owner_fd, encoded)
            os.fsync(owner_fd)
        finally:
            os.close(owner_fd)

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
            self._write_diagnostics(fd, note)
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
            # Remove diagnostics while ownership is still held. A successor
            # cannot race in and have its freshly-written sidecar deleted.
            self.owner_path.unlink(missing_ok=True)
        except OSError:
            pass
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
