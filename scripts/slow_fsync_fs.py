"""
A passthrough filesystem whose ``fsync`` really blocks.

    ####################################################################
    #  Used by the Gate B1.4 fsync-stall drill.  The point is that the #
    #  kernel blocks SQLite inside a real filesystem call -- patching  #
    #  os.fsync in Python would only re-test the mock.                 #
    ####################################################################

Every operation is forwarded to a backing directory unchanged except ``fsync``
and ``fdatasync``, which sleep first. That is enough to hold the journal's
writer thread inside a commit for longer than its write timeout, which is the
condition the drill needs.

    python scripts/slow_fsync_fs.py --backing /srv/real --mount /mnt/slow --delay 45

Requires libfuse2 and fusepy (``pip install '.[drill]'``). Linux only: this is
a drill harness, not part of the platform, and the production runtime never
loads it. On Windows, use a comparable block-layer delay instead and record
which mechanism was used in the Gate B1 sign-off.
"""

from __future__ import annotations

import argparse
import errno
import os
import sys
import time
from typing import Any

try:
    from fuse import FUSE, FuseOSError, Operations
except ImportError:  # pragma: no cover - the drill reports this itself
    print(
        "fusepy is required: pip install '.[drill]' (and the libfuse2 system package)",
        file=sys.stderr,
    )
    raise


class SlowFsync(Operations):
    """Passthrough, except that durability takes as long as we say it does."""

    def __init__(self, backing: str, delay_seconds: float):
        self.backing = backing
        self.delay_seconds = delay_seconds

    def _real(self, partial: str) -> str:
        return os.path.join(self.backing, partial.lstrip("/"))

    # -- the whole point ---------------------------------------------------

    def fsync(self, path, fdatasync, fh):
        time.sleep(self.delay_seconds)
        return os.fsync(fh) if not fdatasync else os.fdatasync(fh)

    def flush(self, path, fh):
        return os.fsync(fh)

    # -- ordinary passthrough ---------------------------------------------

    def access(self, path, mode):
        if not os.access(self._real(path), mode):
            raise FuseOSError(errno.EACCES)

    def getattr(self, path, fh=None) -> dict[str, Any]:
        st = os.lstat(self._real(path))
        return {
            key: getattr(st, key)
            for key in (
                "st_atime", "st_ctime", "st_gid", "st_mode",
                "st_mtime", "st_nlink", "st_size", "st_uid",
            )
        }

    def readdir(self, path, fh):
        yield "."
        yield ".."
        yield from os.listdir(self._real(path))

    def statfs(self, path):
        st = os.statvfs(self._real(path))
        return {
            key: getattr(st, key)
            for key in (
                "f_bavail", "f_bfree", "f_blocks", "f_bsize", "f_favail",
                "f_ffree", "f_files", "f_flag", "f_frsize", "f_namemax",
            )
        }

    def unlink(self, path):
        return os.unlink(self._real(path))

    def rename(self, old, new):
        return os.rename(self._real(old), self._real(new))

    def mkdir(self, path, mode):
        return os.mkdir(self._real(path), mode)

    def rmdir(self, path):
        return os.rmdir(self._real(path))

    def chmod(self, path, mode):
        return os.chmod(self._real(path), mode)

    def chown(self, path, uid, gid):
        return os.chown(self._real(path), uid, gid)

    def utimens(self, path, times=None):
        return os.utime(self._real(path), times)

    def truncate(self, path, length, fh=None):
        with open(self._real(path), "r+b") as fh_:
            fh_.truncate(length)

    def open(self, path, flags):
        return os.open(self._real(path), flags)

    def create(self, path, mode, fi=None):
        return os.open(self._real(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)

    def read(self, path, length, offset, fh):
        os.lseek(fh, offset, os.SEEK_SET)
        return os.read(fh, length)

    def write(self, path, buf, offset, fh):
        os.lseek(fh, offset, os.SEEK_SET)
        return os.write(fh, buf)

    def release(self, path, fh):
        return os.close(fh)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Passthrough FS with a slow fsync")
    ap.add_argument("--backing", required=True)
    ap.add_argument("--mount", required=True)
    ap.add_argument("--delay", type=float, default=45.0)
    ns = ap.parse_args(argv)

    os.makedirs(ns.backing, exist_ok=True)
    os.makedirs(ns.mount, exist_ok=True)
    FUSE(
        SlowFsync(ns.backing, ns.delay),
        ns.mount,
        nothreads=False,
        foreground=True,
        allow_other=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
