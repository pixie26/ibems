from __future__ import annotations

import importlib.util
from pathlib import Path

from ib_execution.gateway_detection import (
    GatewayState,
    ListenerObservation,
    PathStatus,
    ProcessObservation,
    classify_gateway,
)


def _load_detector_script():
    script = Path(__file__).resolve().parents[1] / "scripts" / "detect_ib_gateway.py"
    spec = importlib.util.spec_from_file_location("detect_ib_gateway", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load detector script: {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


detect_ib_gateway = _load_detector_script()

EXPECTED = r"D:\tws\ibgateway\ibgateway.exe"


def _process(**kwargs) -> ProcessObservation:
    return ProcessObservation(pid=19060, **kwargs)


def _listener(pid: int = 19060) -> ListenerObservation:
    return ListenerObservation(local_address="0.0.0.0", local_port=4002, owning_pid=pid)


def test_cim_access_denied_continues_to_process_path_and_listener():
    result = classify_gateway(
        expected_path=EXPECTED,
        processes=(
            _process(
                get_process_path=EXPECTED,
                get_process_path_status="OK",
                cim_status="ACCESS_DENIED",
                cim_error="Access is denied",
            ),
        ),
        listeners=(_listener(),),
        process_query_ok=True,
        listener_query_ok=True,
    )

    assert result.state is GatewayState.RUNNING_LISTENER_VERIFIED
    assert result.is_running is True
    assert result.path_status is PathStatus.MATCH
    assert any("continued with other evidence" in item for item in result.diagnostics)


def test_api_handshake_proves_running_even_when_both_path_lookups_are_denied():
    result = classify_gateway(
        expected_path=EXPECTED,
        processes=(
            _process(
                get_process_path_status="ACCESS_DENIED",
                cim_status="ACCESS_DENIED",
                cim_error="Access is denied",
            ),
        ),
        listeners=(_listener(),),
        process_query_ok=True,
        listener_query_ok=True,
        api_status="VERIFIED",
        api_server_version=178,
    )

    assert result.state is GatewayState.RUNNING_API_VERIFIED
    assert result.is_running is True
    assert result.path_status is PathStatus.UNKNOWN_ACCESS_DENIED
    assert result.api_server_version == 178


def test_process_without_readable_path_is_running_not_absent():
    result = classify_gateway(
        expected_path=EXPECTED,
        processes=(_process(cim_status="ACCESS_DENIED"),),
        listeners=(),
        process_query_ok=True,
        listener_query_ok=True,
    )

    assert result.state is GatewayState.RUNNING_PROCESS_DETECTED
    assert result.is_running is True
    assert result.path_status is PathStatus.UNKNOWN_ACCESS_DENIED


def test_failed_queries_can_never_be_reported_as_not_running():
    result = classify_gateway(
        expected_path=EXPECTED,
        processes=(),
        listeners=(),
        process_query_ok=False,
        listener_query_ok=False,
        collection_errors=("Access is denied",),
    )

    assert result.state is GatewayState.INDETERMINATE
    assert result.is_running is False


def test_only_complete_negative_evidence_reports_not_running():
    result = classify_gateway(
        expected_path=EXPECTED,
        processes=(),
        listeners=(),
        process_query_ok=True,
        listener_query_ok=True,
        api_status="FAILED",
    )

    assert result.state is GatewayState.NOT_RUNNING
    assert result.is_running is False
    assert result.path_status is PathStatus.NO_PROCESS


def test_listener_owned_by_another_process_is_indeterminate():
    result = classify_gateway(
        expected_path=EXPECTED,
        processes=(),
        listeners=(_listener(pid=99999),),
        process_query_ok=True,
        listener_query_ok=True,
        api_status="FAILED",
    )

    assert result.state is GatewayState.INDETERMINATE
    assert result.is_running is False


def test_path_mismatch_is_visible_even_when_gateway_is_running():
    result = classify_gateway(
        expected_path=EXPECTED,
        processes=(
            _process(
                get_process_path=r"C:\unexpected\ibgateway.exe",
                get_process_path_status="OK",
                cim_status="ACCESS_DENIED",
            ),
        ),
        listeners=(_listener(),),
        process_query_ok=True,
        listener_query_ok=True,
    )

    assert result.is_running is True
    assert result.path_status is PathStatus.MISMATCH


def test_netstat_fallback_parses_ipv4_and_ipv6_listeners_for_target_port():
    output = """
      TCP    0.0.0.0:4002           0.0.0.0:0              LISTENING       19060
      TCP    [::]:4002              [::]:0                 LISTENING       19060
      TCP    127.0.0.1:9999         0.0.0.0:0              LISTENING       99
    """

    assert detect_ib_gateway.parse_netstat_listeners(output, 4002) == [
        {"local_address": "0.0.0.0", "local_port": 4002, "owning_pid": 19060},
        {"local_address": "[::]", "local_port": 4002, "owning_pid": 19060},
    ]


def test_detector_integration_keeps_cim_denial_nonfatal(monkeypatch):
    monkeypatch.setattr(
        detect_ib_gateway,
        "collect_windows_observations",
        lambda port: {
            "process_query_ok": True,
            "listener_query_ok": True,
            "processes": [
                {
                    "pid": 19060,
                    "get_process_path": EXPECTED,
                    "get_process_path_status": "OK",
                    "cim_status": "ACCESS_DENIED",
                    "cim_error": "Access is denied",
                }
            ],
            "listeners": [
                {"local_address": "0.0.0.0", "local_port": port, "owning_pid": 19060}
            ],
            "errors": [],
        },
    )
    monkeypatch.setattr(
        detect_ib_gateway,
        "probe_ib_api",
        lambda host, port, client_id, timeout: {
            "status": "VERIFIED",
            "server_version": 178,
        },
    )

    result = detect_ib_gateway.detect(
        expected_path=EXPECTED,
        host="127.0.0.1",
        port=4002,
        api_client_id=962,
        api_timeout=5.0,
        skip_api=False,
    )

    assert result["state"] == "RUNNING_API_VERIFIED"
    assert result["path_status"] == "MATCH"


def test_fault_script_checks_detection_and_exact_path_before_firewall_mutation():
    source = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_ib_gateway_outbound_fault.ps1"
    ).read_text(encoding="utf-8")

    detector_at = source.index("detect_ib_gateway.py")
    path_check_at = source.index("path_status")
    firewall_at = source.index("New-NetFirewallRule")
    assert detector_at < path_check_at < firewall_at
    assert "Get-CimInstance" not in source
