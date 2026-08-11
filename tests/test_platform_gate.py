from __future__ import annotations

import json

import pytest

from ib_execution.execution_host import HostConfig
from ib_execution.platform_gate import (
    BrokerCapability,
    LINUX_REQUIRED_DRILLS,
    WINDOWS_REQUIRED_DRILLS,
    PlatformCapabilityRefused,
    validate_broker_capability,
)
from ib_execution.recorder_modes import DataMode


def _evidence(platform: str, drills: set[str]) -> dict:
    return {
        "schema_version": 1,
        "platform": platform,
        "order_authorization": "PAPER",
        "exact_freeze_commit": "a" * 40,
        "source_tree_sha256": "b" * 64,
        "fault_drills": {name: "PASS" for name in drills},
        "artifact_sha256": {"manifest.json": "c" * 64},
    }


def test_simulation_never_needs_order_capability_evidence():
    assert validate_broker_capability(BrokerCapability.SIMULATION, None) is None


@pytest.mark.parametrize(
    ("platform", "drills"),
    [("windows", WINDOWS_REQUIRED_DRILLS), ("linux", LINUX_REQUIRED_DRILLS)],
)
def test_complete_exact_freeze_evidence_unlocks_only_its_platform(tmp_path, platform, drills):
    path = tmp_path / "capability.json"
    path.write_text(json.dumps(_evidence(platform, set(drills))), encoding="utf-8")
    loaded = validate_broker_capability(
        BrokerCapability.ORDER_CAPABLE,
        path,
        platform_name=platform,
    )
    assert loaded["platform"] == platform
    other = "linux" if platform == "windows" else "windows"
    with pytest.raises(PlatformCapabilityRefused, match="not"):
        validate_broker_capability(BrokerCapability.ORDER_CAPABLE, path, platform_name=other)


def test_missing_real_fault_drill_refuses_order_capable_startup(tmp_path):
    evidence = _evidence("windows", set(WINDOWS_REQUIRED_DRILLS))
    evidence["fault_drills"]["ntfs_disk_full"] = "NOT_RUN"
    path = tmp_path / "capability.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(PlatformCapabilityRefused, match="ntfs_disk_full"):
        validate_broker_capability(BrokerCapability.ORDER_CAPABLE, path, platform_name="windows")


def test_owner_authorization_and_hashes_are_mandatory(tmp_path):
    evidence = _evidence("linux", set(LINUX_REQUIRED_DRILLS))
    evidence["order_authorization"] = "NONE"
    path = tmp_path / "capability.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(PlatformCapabilityRefused, match="PAPER or LIVE"):
        validate_broker_capability(BrokerCapability.ORDER_CAPABLE, path, platform_name="linux")


def test_execution_host_cannot_embed_sampled_or_full_raw_recording(tmp_path):
    with pytest.raises(ValueError, match="execution_minimal"):
        HostConfig(
            journal_path=tmp_path / "journal.db",
            fence_path=tmp_path / "fence.json",
            status_path=tmp_path / "status.json",
            data_mode=DataMode.RESEARCH_FULL,
        )
