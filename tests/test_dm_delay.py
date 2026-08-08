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

import os

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
    volume.destroy()
    volume.destroy()
    assert not volume.image.exists()


def test_the_device_path_is_derived_from_the_namespaced_name(tmp_path):
    volume = _volume(tmp_path)
    assert volume.device == f"/dev/mapper/{dm_delay.MAPPER_PREFIX}test"


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
        volume.destroy()
    assert not os.path.ismount(volume.mount_point)
    assert not volume.image.exists()
