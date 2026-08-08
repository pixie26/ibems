"""
The dm-delay drill volume: the parts testable without a device-mapper kernel.

Provisioning itself needs ``dm`` in the kernel and root, and is skipped where
either is missing -- the drill reports that as INCONCLUSIVE rather than
inventing a result. What is always testable is the half that protects real
devices, and that is the half worth having tests for: this module creates and
destroys block devices, so every refusal below exists to keep it away from
anything it did not make itself.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import subprocess
from pathlib import Path

import pytest

from ib_execution import dm_delay


def _volume(tmp_path, **over):
    kwargs = dict(
        image=tmp_path / "drill.img",
        mount_point=tmp_path / "mnt",
        size_mb=64,
        name=f"{dm_delay.MAPPER_PREFIX}test",
    )
    kwargs.update(over)
    return dm_delay.DelayedVolume(**kwargs)


def _storage_drill_module():
    """Load the script without requiring ``scripts`` to be a package."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_storage_fault_drill.py"
    spec = importlib.util.spec_from_file_location("ibems_storage_fault_drill", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_availability_gives_a_reason_rather_than_a_bare_false():
    """An inconclusive drill has to say why, or it reads as a silent skip."""
    available, why = dm_delay.availability()
    assert isinstance(available, bool)
    if not available:
        assert why, "unavailability must carry a diagnosis"


def test_the_mapper_name_must_be_namespaced(tmp_path):
    """`dmsetup remove` on an arbitrary name would destroy a real target."""
    with pytest.raises(dm_delay.DmDelayUnsafe, match="namespaced"):
        _volume(tmp_path, name="root-vg-data")


def test_the_image_is_size_capped(tmp_path):
    with pytest.raises(dm_delay.DmDelayUnsafe, match="cap"):
        _volume(tmp_path, size_mb=dm_delay.MAX_IMAGE_MB + 1)


def test_an_existing_image_is_never_reused(tmp_path):
    """Teardown deletes the image, so it must only ever delete its own."""
    image = tmp_path / "drill.img"
    image.write_bytes(b"someone else's data")
    volume = _volume(tmp_path, image=image)
    with pytest.raises(dm_delay.DmDelayUnsafe, match="already exists"):
        volume.create()
    assert image.read_bytes() == b"someone else's data", "left untouched"


def test_destroy_is_idempotent_and_safe_before_create(tmp_path):
    """Teardown runs from `finally`, including on paths that never provisioned."""
    volume = _volume(tmp_path)
    assert volume.destroy() == []
    assert volume.destroy() == []
    assert not volume.image.exists()


def test_destroy_resets_a_slow_table_before_unmount(monkeypatch, tmp_path):
    """The first real dm-delay run hung because it unmounted at the 45s delay."""
    calls: list[tuple[str, ...]] = []

    def fake_run(*args, check=True, timeout=120.0):
        calls.append(tuple(args))
        stdout = "4096\n" if args and args[0] == "blockdev" else ""
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(dm_delay, "_run", fake_run)
    monkeypatch.setattr(dm_delay.os.path, "ismount", lambda _p: False)

    volume = _volume(tmp_path, delay_ms=45_000)
    volume.image.touch()
    volume.loop = "/dev/loop99"
    volume._created = True
    volume._mounted = True

    assert volume.destroy() == []
    assert volume.delay_ms == 0

    resume = calls.index(("dmsetup", "resume", volume.name))
    unmount = calls.index(("umount", str(volume.mount_point)))
    assert resume < unmount, "zero-delay table must be active before unmount"


def test_destroy_reports_a_timeout_instead_of_masking_the_drill(monkeypatch, tmp_path):
    """Cleanup failures are evidence too; they must not escape from finally."""
    def fake_run(*args, check=True, timeout=120.0):
        if args[:1] == ("umount",) and "-l" not in args:
            raise subprocess.TimeoutExpired(args, timeout)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(dm_delay, "_run", fake_run)
    monkeypatch.setattr(dm_delay.os.path, "ismount", lambda _p: False)

    volume = _volume(tmp_path)
    volume.image.touch()
    volume._mounted = True
    errors = volume.destroy()

    assert errors
    assert "TimeoutExpired" in errors[0]
    assert not volume.image.exists()


def test_the_device_path_is_derived_from_the_namespaced_name(tmp_path):
    volume = _volume(tmp_path)
    assert volume.device == f"/dev/mapper/{dm_delay.MAPPER_PREFIX}test"


def test_fsync_stall_is_injected_after_a_healthy_start_and_before_the_trigger():
    """Protect the causal ordering that makes B1.4 a runtime-failure drill."""
    drill = _storage_drill_module()
    source = inspect.getsource(drill._run_fsync_stalling_case)
    started = source.index("_wait_started")
    fault = source.index("volume.set_delay(stalling_delay_ms)")
    trigger = source.index('paths["trigger"].write_text')
    assert started < fault < trigger


def test_post_fault_broker_writes_are_read_from_the_child_log(tmp_path):
    """The manifest value must be an observation, never a literal zero."""
    drill = _storage_drill_module()
    path = tmp_path / "broker-calls.jsonl"
    records = [
        {"ts_monotonic_ns": 10, "operation": "place_order", "order_ref": "a"},
        {"ts_monotonic_ns": 20, "operation": "cancel_order", "order_ref": "a"},
    ]
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
    assert drill._read_broker_calls(path) == records
    source = inspect.getsource(drill._run_fsync_stalling_case)
    assert "post_fault_broker_writes=len(post_fault)" in source


def test_healthy_control_requires_a_live_unfenced_process_and_a_real_broker_write():
    """Any self-exit is a failed healthy control, not merely exit-code != 10."""
    drill = _storage_drill_module()
    source = inspect.getsource(drill._run_fsync_healthy_control)
    assert "still_running_after_probe=alive" in source
    assert "len(calls) == 1" in source
    assert 'calls[0].get("operation") == "place_order"' in source
    assert "and not fence_present" in source


@pytest.mark.skipif(
    not dm_delay.availability()[0],
    reason=f"device-mapper unusable here: {dm_delay.availability()[1]}",
)
def test_a_delayed_volume_can_be_created_and_destroyed(tmp_path):
    """The provisioning path, where the kernel supports it."""
    volume = _volume(tmp_path, delay_ms=10)
    try:
        volume.create()
        assert os.path.ismount(volume.mount_point)
        probe = volume.mount_point / "probe"
        probe.write_text("x", encoding="utf-8")
        assert probe.read_text(encoding="utf-8") == "x"

        volume.set_delay(50)
        assert volume.delay_ms == 50
    finally:
        errors = volume.destroy()
    assert errors == []
    assert not os.path.ismount(volume.mount_point)
    assert not volume.image.exists()
