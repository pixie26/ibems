"""Detect an IB Gateway without treating Windows metadata denial as absence."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from typing import Any

from ib_execution.gateway_detection import (
    GatewayState,
    ListenerObservation,
    ProcessObservation,
    classify_gateway,
)


def _powershell_executable() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("pwsh")
    if executable is None:
        raise RuntimeError("PowerShell is unavailable; Windows Gateway state is indeterminate")
    return executable


def collect_windows_observations(port: int) -> dict[str, Any]:
    command = rf"""
$ErrorActionPreference = 'Stop'
$processQueryOk = $true
$listenerQueryOk = $true
$errors = @()
$processRows = @()
$listenerRows = @()
try {{
    $gatewayProcesses = @(Get-Process -Name 'ibgateway' -ErrorAction SilentlyContinue)
    foreach ($process in $gatewayProcesses) {{
        $getProcessPath = $null
        $getProcessPathStatus = 'UNKNOWN'
        try {{
            $getProcessPath = $process.Path
            $getProcessPathStatus = if ($getProcessPath) {{ 'OK' }} else {{ 'UNKNOWN' }}
        }} catch [System.UnauthorizedAccessException] {{
            $getProcessPathStatus = 'ACCESS_DENIED'
        }} catch {{
            $getProcessPathStatus = 'ERROR'
        }}

        $cimPath = $null
        $cimStatus = 'UNKNOWN'
        $cimError = $null
        try {{
            $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.Id)" -ErrorAction Stop
            $cimPath = $cim.ExecutablePath
            $cimStatus = if ($cimPath) {{ 'OK' }} else {{ 'UNKNOWN' }}
        }} catch [System.UnauthorizedAccessException] {{
            $cimStatus = 'ACCESS_DENIED'
            $cimError = $_.Exception.Message
        }} catch {{
            $cimError = $_.Exception.Message
            $cimStatus = if ($cimError -match 'Access.*denied|拒绝访问') {{
                'ACCESS_DENIED'
            }} else {{
                'ERROR'
            }}
        }}
        $processRows += [pscustomobject]@{{
            pid = [int]$process.Id
            get_process_path = $getProcessPath
            get_process_path_status = $getProcessPathStatus
            cim_path = $cimPath
            cim_status = $cimStatus
            cim_error = $cimError
        }}
    }}
}} catch {{
    $processQueryOk = $false
    $errors += "Get-Process failed: $($_.Exception.Message)"
}}

try {{
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort {int(port)} -ErrorAction Stop)
    foreach ($listener in $listeners) {{
        $listenerRows += [pscustomobject]@{{
            local_address = [string]$listener.LocalAddress
            local_port = [int]$listener.LocalPort
            owning_pid = [int]$listener.OwningProcess
        }}
    }}
}} catch [Microsoft.PowerShell.Cmdletization.Cim.CimJobException] {{
    if ($_.Exception.Message -match 'No matching|找不到') {{
        $listenerRows = @()
    }} else {{
        $listenerQueryOk = $false
        $errors += "Get-NetTCPConnection failed: $($_.Exception.Message)"
    }}
}} catch {{
    $listenerQueryOk = $false
    $errors += "Get-NetTCPConnection failed: $($_.Exception.Message)"
}}

[pscustomobject]@{{
    process_query_ok = $processQueryOk
    listener_query_ok = $listenerQueryOk
    processes = @($processRows)
    listeners = @($listenerRows)
    errors = @($errors)
}} | ConvertTo-Json -Depth 5 -Compress
"""
    completed = subprocess.run(
        [_powershell_executable(), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        return {
            "process_query_ok": False,
            "listener_query_ok": False,
            "processes": [],
            "listeners": [],
            "errors": [f"PowerShell collection failed: {completed.stderr.strip()}"],
        }
    observed = json.loads(completed.stdout)
    if not observed.get("listener_query_ok"):
        try:
            listeners = collect_netstat_listeners(port)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            observed.setdefault("errors", []).append(
                f"netstat listener fallback failed: {type(exc).__name__}: {exc}"
            )
        else:
            observed["listeners"] = listeners
            observed["listener_query_ok"] = True
            observed["errors"] = [
                error
                for error in observed.get("errors", [])
                if not str(error).startswith("Get-NetTCPConnection failed:")
            ]
            observed["errors"].append(
                "Get-NetTCPConnection unavailable; netstat listener fallback succeeded"
            )
    return observed


NETSTAT_LISTENER = re.compile(
    r"^\s*TCP\s+(?P<address>\S+):(?P<port>\d+)\s+\S+\s+LISTENING\s+(?P<pid>\d+)\s*$",
    re.IGNORECASE,
)


def parse_netstat_listeners(output: str, port: int) -> list[dict[str, Any]]:
    listeners: list[dict[str, Any]] = []
    for line in output.splitlines():
        match = NETSTAT_LISTENER.match(line)
        if match is None or int(match.group("port")) != port:
            continue
        listeners.append(
            {
                "local_address": match.group("address"),
                "local_port": port,
                "owning_pid": int(match.group("pid")),
            }
        )
    return listeners


def collect_netstat_listeners(port: int) -> list[dict[str, Any]]:
    executable = shutil.which("netstat.exe") or shutil.which("netstat")
    if executable is None:
        raise RuntimeError("netstat is unavailable")
    completed = subprocess.run(
        [executable, "-ano", "-p", "TCP"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"netstat exited {completed.returncode}: {completed.stderr.strip()}")
    return parse_netstat_listeners(completed.stdout, port)


def probe_ib_api(host: str, port: int, client_id: int, timeout: float) -> dict[str, Any]:
    ib = None
    try:
        from ib_async import IB, StartupFetchNONE

        ib = IB()
        ib.RequestTimeout = timeout
        ib.connect(
            host,
            port,
            clientId=client_id,
            timeout=timeout,
            readonly=True,
            fetchFields=StartupFetchNONE,
        )
        if not ib.isConnected():
            raise ConnectionError("IB.connect returned without a connected API session")
        return {"status": "VERIFIED", "server_version": int(ib.client.serverVersion())}
    except Exception as exc:  # The error is evidence; classification continues.
        return {"status": "FAILED", "server_version": None, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if ib is not None and ib.isConnected():
            ib.disconnect()


def detect(
    *,
    expected_path: str,
    host: str,
    port: int,
    api_client_id: int,
    api_timeout: float,
    skip_api: bool,
) -> dict[str, Any]:
    observed = collect_windows_observations(port)
    processes = tuple(
        ProcessObservation.from_mapping(value) for value in observed.get("processes", [])
    )
    listeners = tuple(
        ListenerObservation.from_mapping(value) for value in observed.get("listeners", [])
    )
    if skip_api:
        api = {"status": "NOT_REQUESTED", "server_version": None}
    else:
        api = probe_ib_api(host, port, api_client_id, api_timeout)
    errors = list(observed.get("errors", []))
    if api.get("error"):
        errors.append(f"IB API probe failed: {api['error']}")
    detection = classify_gateway(
        expected_path=expected_path,
        processes=processes,
        listeners=listeners,
        process_query_ok=bool(observed.get("process_query_ok")),
        listener_query_ok=bool(observed.get("listener_query_ok")),
        api_status=str(api["status"]),
        api_server_version=api.get("server_version"),
        collection_errors=tuple(errors),
    )
    return detection.as_dict()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-path", default=r"D:\tws\ibgateway\ibgateway.exe")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--api-client-id", type=int, default=962)
    parser.add_argument("--api-timeout", type=float, default=5.0)
    parser.add_argument("--skip-api", action="store_true")
    args = parser.parse_args(argv)
    result = detect(
        expected_path=args.expected_path,
        host=args.host,
        port=args.port,
        api_client_id=args.api_client_id,
        api_timeout=args.api_timeout,
        skip_api=args.skip_api,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    state = GatewayState(result["state"])
    if state is GatewayState.NOT_RUNNING:
        return 1
    if state is GatewayState.INDETERMINATE:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
