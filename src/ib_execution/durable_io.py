"""Explicit durable-publication primitives shared by safety sidecars.

POSIX publishes with ``fsync(file) -> replace -> fsync(parent)``. Windows
cannot use the POSIX directory-open shape, so it uses ``MoveFileExW`` with
``MOVEFILE_WRITE_THROUGH`` and flushes the final file handle. Real NTFS fault
drills remain a deployment prerequisite; the platform difference is explicit
instead of being hidden behind an ignored ``PermissionError``.
"""

from __future__ import annotations

import os
from pathlib import Path


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while publishing durable file")
        view = view[written:]


def _replace(tmp: Path, target: Path) -> None:
    if os.name != "nt":
        os.replace(tmp, target)
        return

    import ctypes
    from ctypes import wintypes

    move_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file_ex.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file_ex.restype = wintypes.BOOL
    movefile_replace_existing = 0x1
    movefile_write_through = 0x8
    if not move_file_ex(
        str(tmp), str(target), movefile_replace_existing | movefile_write_through
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _sync_parent(parent: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    dir_fd = os.open(parent, flags)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def durable_atomic_write(path: str | Path, payload: bytes) -> None:
    """Atomically replace *path* and wait for the platform durability boundary."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)

    try:
        durable_replace(tmp, target, source_is_synced=True)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def durable_replace(
    source: str | Path,
    target: str | Path,
    *,
    source_is_synced: bool = False,
) -> None:
    """Publish an existing file with explicit platform durability semantics."""

    src = Path(source)
    dst = Path(target)
    if not source_is_synced:
        src_flags = os.O_RDWR if os.name == "nt" else os.O_RDONLY
        src_fd = os.open(src, src_flags)
        try:
            os.fsync(src_fd)
        finally:
            os.close(src_fd)
    _replace(src, dst)
    final_flags = os.O_RDWR if os.name == "nt" else os.O_RDONLY
    final_fd = os.open(dst, final_flags)
    try:
        os.fsync(final_fd)
    finally:
        os.close(final_fd)
    _sync_parent(dst.parent)
