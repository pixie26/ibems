"""
A disposable, delayed block device for the Gate B1.4 fsync-stall drill.

    ####################################################################
    #  dm-delay sits under the filesystem, so SQLite runs in its       #
    #  production configuration and does not know it is being tested.  #
    #  That is the whole reason to prefer it over a FUSE shim, which   #
    #  cannot back the -shm mmap WAL mode needs.                       #
    ####################################################################

Lifecycle, all of it owned here so the operator runs one command:

    sparse image -> loop device -> dm-delay target -> mkfs -> mount
        ... the drill runs ...
    umount -> dmsetup remove -> losetup -d -> delete the image

``delay_ms`` is applied to writes, which is what an fsync waits on. It can be
changed while mounted (``set_delay``), so a drill can prove the engine is
healthy at a delay below its write timeout and only fails closed above it.

SAFETY
------
This creates and destroys block devices, so every destructive operation is
restricted to devices this module made:

* the backing image must not already exist;
* the mapper name is namespaced and must not already be in use;
* the image is size-capped;
* it never touches a caller-supplied device, and there is no flag to make it;
* teardown runs from ``finally`` and is idempotent, including after Ctrl-C.

Linux only, and root only. It is drill scaffolding, never imported by the
platform at runtime.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

MAPPER_PREFIX = "ibems-drill-"
MAX_IMAGE_MB = 512


class DmDelayUnavailable(RuntimeError):
    """Device-mapper cannot be driven here. The drill is inconclusive, not failed."""


class DmDelayUnsafe(RuntimeError):
    """A precondition that protects real devices was not met."""


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(args)} failed ({result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result


def availability() -> tuple[bool, str]:
    """Whether a delayed device can actually be built here, and why not."""
    if os.name != "posix":
        return False, "dm-delay is Linux-only"
    if os.geteuid() != 0:
        return False, "creating a device-mapper target requires root"
    for tool in ("dmsetup", "losetup", "mkfs.ext4", "mount", "umount", "blockdev"):
        if shutil.which(tool) is None:
            return False, f"{tool} is not installed"
    if not Path("/dev/mapper/control").exists():
        return False, "/dev/mapper/control is absent; device-mapper is not available"
    probe = _run("dmsetup", "targets", check=False)
    if probe.returncode != 0:
        return False, f"dmsetup targets failed: {(probe.stderr or probe.stdout).strip()}"
    if "delay" not in probe.stdout:
        return False, "the dm-delay target is not loaded (modprobe dm-delay)"
    return True, ""


class DelayedVolume:
    """An ext4 filesystem on a dm-delay device, created and destroyed by this object."""

    def __init__(
        self,
        image: Path,
        mount_point: Path,
        *,
        size_mb: int = 128,
        delay_ms: int = 0,
        name: Optional[str] = None,
    ):
        if size_mb > MAX_IMAGE_MB:
            raise DmDelayUnsafe(f"{size_mb}MB exceeds the {MAX_IMAGE_MB}MB drill cap")
        self.image = Path(image)
        self.mount_point = Path(mount_point)
        self.size_mb = size_mb
        self.delay_ms = delay_ms
        self.name = name or f"{MAPPER_PREFIX}{os.getpid()}"
        if not self.name.startswith(MAPPER_PREFIX):
            raise DmDelayUnsafe(f"mapper name {self.name!r} is not namespaced for drills")
        self.loop: Optional[str] = None
        self._mounted = False
        self._created = False

    @property
    def device(self) -> str:
        return f"/dev/mapper/{self.name}"

    # -- provisioning -----------------------------------------------------

    def _table(self, delay_ms: int) -> str:
        sectors = _run("blockdev", "--getsz", str(self.loop)).stdout.strip()
        # Read and write legs both delayed: an fsync waits on the write leg,
        # and delaying reads too keeps recovery honest.
        return f"0 {sectors} delay {self.loop} 0 {delay_ms} {self.loop} 0 {delay_ms}"

    def create(self) -> "DelayedVolume":
        # Refusals first, availability second. These protect devices this
        # module did not create, and that obligation does not depend on
        # whether the kernel happens to support dm today. It also means the
        # refusals are testable on hosts where provisioning is not.
        if self.image.exists():
            raise DmDelayUnsafe(
                f"{self.image} already exists; this drill only destroys images it created"
            )
        if Path(self.device).exists():
            raise DmDelayUnsafe(f"{self.device} already exists; refusing to reuse it")
        if os.path.ismount(self.mount_point):
            raise DmDelayUnsafe(f"{self.mount_point} is already a mount point")

        ok, why = availability()
        if not ok:
            raise DmDelayUnavailable(why)

        self.image.parent.mkdir(parents=True, exist_ok=True)
        self.mount_point.mkdir(parents=True, exist_ok=True)
        try:
            with self.image.open("wb") as fh:
                fh.truncate(self.size_mb * 1024 * 1024)
            self.loop = _run("losetup", "--find", "--show", str(self.image)).stdout.strip()
            _run("dmsetup", "create", self.name, "--table", self._table(self.delay_ms))
            self._created = True
            # -m 0: ext4 otherwise reserves 5% for uid 0, and a root drill would
            # never reach the condition it is trying to create.
            _run("mkfs.ext4", "-q", "-F", "-m", "0", self.device)
            _run("mount", self.device, str(self.mount_point))
            self._mounted = True
        except BaseException:
            self.destroy()
            raise
        return self

    # -- fault control ----------------------------------------------------

    def set_delay(self, delay_ms: int) -> None:
        """Change the delay on a live device, without unmounting.

        Reloading the table lets one run establish that the engine is healthy
        below its write timeout and only fails closed above it. A drill that
        only ever tests the failing delay cannot tell "correctly fails closed"
        apart from "dies whenever storage is slow".
        """
        _run("dmsetup", "suspend", self.name)
        try:
            _run("dmsetup", "reload", self.name, "--table", self._table(delay_ms))
        finally:
            _run("dmsetup", "resume", self.name)
        self.delay_ms = delay_ms

    # -- teardown ---------------------------------------------------------

    def destroy(self) -> None:
        """Idempotent, best-effort, and safe to call twice or after a crash."""
        if self._mounted or os.path.ismount(self.mount_point):
            for attempt in range(5):
                if _run("umount", str(self.mount_point), check=False).returncode == 0:
                    break
                time.sleep(1 + attempt)      # a delayed device unmounts slowly
            else:
                _run("umount", "-l", str(self.mount_point), check=False)
            self._mounted = False
        if self._created or Path(self.device).exists():
            for attempt in range(5):
                if _run("dmsetup", "remove", self.name, check=False).returncode == 0:
                    break
                time.sleep(1 + attempt)
            self._created = False
        if self.loop:
            _run("losetup", "-d", self.loop, check=False)
            self.loop = None
        self.image.unlink(missing_ok=True)

    def __enter__(self) -> "DelayedVolume":
        return self.create()

    def __exit__(self, *exc_info) -> None:
        self.destroy()
