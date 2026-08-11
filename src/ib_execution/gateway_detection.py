"""Layered, tri-state IB Gateway detection for Windows operators.

Process metadata is useful but not authoritative: Windows can deny CIM or
executable-path access for a process that is plainly running. Detection must
preserve that uncertainty and continue through process, listener and real IB
API evidence. Only complete negative observations may produce NOT_RUNNING.
"""

from __future__ import annotations

import ntpath
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class GatewayState(str, Enum):
    RUNNING_API_VERIFIED = "RUNNING_API_VERIFIED"
    RUNNING_LISTENER_VERIFIED = "RUNNING_LISTENER_VERIFIED"
    RUNNING_PROCESS_DETECTED = "RUNNING_PROCESS_DETECTED"
    INDETERMINATE = "INDETERMINATE"
    NOT_RUNNING = "NOT_RUNNING"


class PathStatus(str, Enum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNKNOWN_ACCESS_DENIED = "UNKNOWN_ACCESS_DENIED"
    UNKNOWN = "UNKNOWN"
    NO_PROCESS = "NO_PROCESS"


@dataclass(frozen=True)
class ProcessObservation:
    pid: int
    get_process_path: str | None = None
    get_process_path_status: str = "UNKNOWN"
    cim_path: str | None = None
    cim_status: str = "UNKNOWN"
    cim_error: str | None = None

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> ProcessObservation:
        return cls(
            pid=int(value["pid"]),
            get_process_path=value.get("get_process_path"),
            get_process_path_status=str(value.get("get_process_path_status", "UNKNOWN")),
            cim_path=value.get("cim_path"),
            cim_status=str(value.get("cim_status", "UNKNOWN")),
            cim_error=value.get("cim_error"),
        )

    @property
    def best_path(self) -> str | None:
        return self.cim_path or self.get_process_path


@dataclass(frozen=True)
class ListenerObservation:
    local_address: str
    local_port: int
    owning_pid: int

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> ListenerObservation:
        return cls(
            local_address=str(value.get("local_address", "")),
            local_port=int(value["local_port"]),
            owning_pid=int(value["owning_pid"]),
        )


@dataclass(frozen=True)
class GatewayDetection:
    state: GatewayState
    is_running: bool
    expected_path: str
    path_status: PathStatus
    process_query_ok: bool
    listener_query_ok: bool
    process_pids: tuple[int, ...]
    listener_pids: tuple[int, ...]
    api_status: str
    api_server_version: int | None
    diagnostics: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        data["path_status"] = self.path_status.value
        data["process_pids"] = list(self.process_pids)
        data["listener_pids"] = list(self.listener_pids)
        data["diagnostics"] = list(self.diagnostics)
        return data


def _same_windows_path(left: str, right: str) -> bool:
    return ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(ntpath.normpath(right))


def _path_status(
    processes: tuple[ProcessObservation, ...], expected_path: str
) -> PathStatus:
    if not processes:
        return PathStatus.NO_PROCESS
    known_paths = [process.best_path for process in processes if process.best_path]
    if any(_same_windows_path(path, expected_path) for path in known_paths):
        return PathStatus.MATCH
    if known_paths:
        return PathStatus.MISMATCH
    if any(
        process.cim_status == "ACCESS_DENIED"
        or process.get_process_path_status == "ACCESS_DENIED"
        for process in processes
    ):
        return PathStatus.UNKNOWN_ACCESS_DENIED
    return PathStatus.UNKNOWN


def classify_gateway(
    *,
    expected_path: str,
    processes: tuple[ProcessObservation, ...],
    listeners: tuple[ListenerObservation, ...],
    process_query_ok: bool,
    listener_query_ok: bool,
    api_status: str = "NOT_REQUESTED",
    api_server_version: int | None = None,
    collection_errors: tuple[str, ...] = (),
) -> GatewayDetection:
    """Classify evidence without collapsing inspection failure into absence."""

    process_pids = tuple(sorted({process.pid for process in processes}))
    listener_pids = tuple(sorted({listener.owning_pid for listener in listeners}))
    listener_matches_process = bool(set(process_pids) & set(listener_pids))
    path_status = _path_status(processes, expected_path)
    diagnostics = list(collection_errors)

    for process in processes:
        if process.cim_status == "ACCESS_DENIED":
            diagnostics.append(
                f"PID {process.pid}: CIM executable path unavailable (Access Denied); "
                "continued with other evidence"
            )

    if api_status == "VERIFIED":
        state = GatewayState.RUNNING_API_VERIFIED
        is_running = True
    elif listener_matches_process:
        state = GatewayState.RUNNING_LISTENER_VERIFIED
        is_running = True
    elif processes:
        state = GatewayState.RUNNING_PROCESS_DETECTED
        is_running = True
    elif process_query_ok and listener_query_ok and not listeners:
        state = GatewayState.NOT_RUNNING
        is_running = False
    else:
        state = GatewayState.INDETERMINATE
        is_running = False

    if listeners and not listener_matches_process and api_status != "VERIFIED":
        diagnostics.append(
            "target port is listening, but its owning PID is not a detected ibgateway process"
        )
        if not processes:
            state = GatewayState.INDETERMINATE
            is_running = False
    if path_status in {PathStatus.UNKNOWN, PathStatus.UNKNOWN_ACCESS_DENIED} and is_running:
        diagnostics.append("Gateway is running, but the executable path is not confirmed")
    elif path_status is PathStatus.MISMATCH:
        diagnostics.append("detected ibgateway executable path does not match the expected path")

    return GatewayDetection(
        state=state,
        is_running=is_running,
        expected_path=expected_path,
        path_status=path_status,
        process_query_ok=process_query_ok,
        listener_query_ok=listener_query_ok,
        process_pids=process_pids,
        listener_pids=listener_pids,
        api_status=api_status,
        api_server_version=api_server_version,
        diagnostics=tuple(diagnostics),
    )
