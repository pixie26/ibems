"""Fail-closed deployment capability gate for any future real broker adapter."""

from __future__ import annotations

import json
import os
import re
from enum import Enum
from pathlib import Path
from typing import Any


class BrokerCapability(str, Enum):
    SIMULATION = "simulation"
    ORDER_CAPABLE = "order_capable"


class PlatformCapabilityRefused(RuntimeError):
    pass


WINDOWS_REQUIRED_DRILLS = frozenset(
    {
        "ntfs_disk_full",
        "ntfs_flush_stall",
        "publication_force_kill",
        "wal_damage_witness_crossing",
        "service_force_kill_ownership",
        "startup_refusal",
        "volume_failure_domain",
    }
)
LINUX_REQUIRED_DRILLS = frozenset(
    {
        "disk_full",
        "fsync_stall",
        "wal_corruption",
        "process_force_kill",
        "startup_refusal",
        "volume_failure_domain",
    }
)


def current_platform_name(os_name: str | None = None) -> str:
    value = os.name if os_name is None else os_name
    return "windows" if value == "nt" else "linux"


def validate_broker_capability(
    capability: BrokerCapability | str,
    evidence_path: str | Path | None,
    *,
    platform_name: str | None = None,
) -> dict[str, Any] | None:
    """Require exact-freeze, owner and real-fault evidence before broker writes."""

    selected = BrokerCapability(capability)
    if selected is BrokerCapability.SIMULATION:
        return None
    platform_name = platform_name or current_platform_name()
    if evidence_path is None:
        raise PlatformCapabilityRefused(
            f"{platform_name} order-capable startup requires a capability evidence file"
        )
    path = Path(evidence_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PlatformCapabilityRefused(f"capability evidence is unreadable: {exc}") from exc
    if payload.get("schema_version") != 1:
        raise PlatformCapabilityRefused("capability evidence schema_version must be 1")
    if payload.get("platform") != platform_name:
        raise PlatformCapabilityRefused(
            f"capability evidence is for {payload.get('platform')!r}, not {platform_name!r}"
        )
    if payload.get("order_authorization") not in {"PAPER", "LIVE"}:
        raise PlatformCapabilityRefused("owner order_authorization must be PAPER or LIVE")
    if not re.fullmatch(r"[0-9a-f]{40}", str(payload.get("exact_freeze_commit", ""))):
        raise PlatformCapabilityRefused("exact_freeze_commit must be a 40-character git hash")
    if not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("source_tree_sha256", ""))):
        raise PlatformCapabilityRefused("source_tree_sha256 must be a SHA-256 digest")
    required = WINDOWS_REQUIRED_DRILLS if platform_name == "windows" else LINUX_REQUIRED_DRILLS
    drills = payload.get("fault_drills")
    if not isinstance(drills, dict):
        raise PlatformCapabilityRefused("fault_drills must be an object")
    missing = sorted(name for name in required if drills.get(name) != "PASS")
    if missing:
        raise PlatformCapabilityRefused(
            "order-capable fault evidence is incomplete: " + ", ".join(missing)
        )
    artifacts = payload.get("artifact_sha256")
    if not isinstance(artifacts, dict) or not artifacts:
        raise PlatformCapabilityRefused("at least one hashed evidence artifact is required")
    invalid_hashes = sorted(
        name
        for name, digest in artifacts.items()
        if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{64}", str(digest))
    )
    if invalid_hashes:
        raise PlatformCapabilityRefused(
            "invalid evidence artifact hashes: " + ", ".join(invalid_hashes)
        )
    return payload
