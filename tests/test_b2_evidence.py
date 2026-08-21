from __future__ import annotations

import copy
import json

import pytest

from ib_execution import b2_evidence


COMMIT = "1" * 40
TREE = "2" * 40
DIGEST = "a" * 64


def _evidence(
    evidence_id: str,
    claim_key: str,
    kind: str,
    verdict: str,
    *,
    binding: str = "BOUND_AUTHORITY",
) -> dict[str, object]:
    bound = binding == "BOUND_AUTHORITY"
    return {
        "id": evidence_id,
        "claim_key": claim_key,
        "evidence_kind": kind,
        "binding": binding,
        "relative_path": f"controlled/{evidence_id.lower()}.json",
        "sha256": DIGEST if bound else None,
        "bytes": 123 if bound else None,
        "captured_at_utc": "2026-08-18T20:00:00Z",
        "capture_commit": COMMIT if bound else None,
        "verdict": verdict,
        "scope": "Bound test scope only; no order capability.",
        "sensitivity": "REDACTED",
    }


def _manifest(*, owner: bool = False) -> dict[str, object]:
    authority_ids = ["V4_HEALTH", "V3_FAIL"]
    unknown_ids = ["EXECUTION_WINDOW_AMBIGUITY"]
    risk_ids = ["D1_EVENT_DRIVEN_30S", "D2_WRITER_LAG_ROOT_CAUSE"]
    payload: dict[str, object] = {
        "schema_version": 2,
        "freeze_kind": "B2_READ_ONLY_EVIDENCE",
        "candidate": {
            "commit_sha": COMMIT,
            "tree_sha": TREE,
            "source_tree_sha256": "3" * 64,
            "config_tree_sha256": "4" * 64,
            "dependency_lock_sha256": "5" * 64,
        },
        "safety_boundary": {
            "gate_b2": "READ_ONLY_IN_PROGRESS",
            "order_authorization": "NONE",
            "trading_adapter": "NOT_IMPLEMENTED",
        },
        "ci_runs": [
            {
                "provider": "GITHUB_ACTIONS",
                "workflow": "CI",
                "run_id": 99,
                "run_attempt": 1,
                "commit_sha": COMMIT,
                "conclusion": "SUCCESS",
            }
        ],
        "ci_artifacts": [
            {
                "run_id": 99,
                "job_name": "verify",
                "artifact_id": 456,
                "artifact_name": f"junit-{COMMIT}",
                "archive_sha256": "8" * 64,
                "checkout_identity_member_path": "checkout-identity.json",
                "evidence_id": None,
                "evidence_member_path": None,
            }
        ],
        "evidence": [
            _evidence("V4_HEALTH", "FULL_RTH_20260818_V4", "FULL_RTH_HEALTH", "PASS"),
            _evidence("V3_FAIL", "FULL_RTH_20260818_V3", "HISTORICAL_FAILURE", "FAIL"),
            _evidence(
                "INCIDENT_REFERENCE",
                "INCIDENT_20260813",
                "INCIDENT",
                "INFORMATIONAL",
                binding="REFERENCE_ONLY",
            ),
        ],
        "authority_evidence_ids": authority_ids,
        "required_failures": ["V3_FAIL"],
        "unknowns": [
            {
                "id": unknown_ids[0],
                "statement": "Official IB pages disagree about the executions request window.",
                "status": "AMBIGUOUS",
                "blocks_b2_read_only_freeze": False,
                "review_before": "PRODUCTION_OR_ORDER_CAPABLE",
            }
        ],
        "risk_assumptions": [
            {
                "id": "D1_EVENT_DRIVEN_30S",
                "statement": "The 30 second event-driven threshold is scope-bound.",
                "failure_mode": "A shorter synchronized feed outage may remain advisory.",
                "mitigation": "Review multiple sessions and cross-check bars and farm state.",
                "status": "OPEN_REVIEW_REQUIRED",
                "blocks_b2_read_only_freeze": False,
                "required_review_before": "PRODUCTION_OR_ORDER_CAPABLE_PAPER_LIVE",
            },
            {
                "id": "D2_WRITER_LAG_ROOT_CAUSE",
                "statement": "Writer-lag root cause remains open.",
                "failure_mode": "Lag may recur under storage or resource pressure.",
                "mitigation": "Run a bounded storage probe before production review.",
                "status": "OPEN_REVIEW_REQUIRED",
                "blocks_b2_read_only_freeze": False,
                "required_review_before": "PRODUCTION_OR_ORDER_CAPABLE_PAPER_LIVE",
            },
        ],
        "owner_acceptance": None,
    }
    if owner:
        payload["owner_acceptance"] = {
            "owner": "Project Owner",
            "accepted_at_utc": "2026-08-20T12:34:56Z",
            "decision": "ACCEPT_EVIDENCE_SCOPE_AND_RESIDUAL_RISK",
            "gate_b2_decision": "REMAIN_READ_ONLY_IN_PROGRESS",
            "order_capability": "NOT_AUTHORIZED",
            "accepted_authority_evidence_ids": authority_ids,
            "accepted_unknown_ids": unknown_ids,
            "accepted_risk_assumption_ids": risk_ids,
        }
    return payload


def test_valid_candidate_and_owner_accepted_packets_pass() -> None:
    candidate = _manifest()
    b2_evidence.validate_manifest(candidate)
    assert b2_evidence.build_manifest(**candidate) == candidate
    assert json.loads(b2_evidence.dumps_manifest(candidate)) == candidate

    accepted = _manifest(owner=True)
    b2_evidence.validate_manifest(accepted, require_owner_acceptance=True)


def test_candidate_cannot_be_treated_as_final_owner_acceptance() -> None:
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="required for a final"):
        b2_evidence.validate_manifest(_manifest(), require_owner_acceptance=True)


def test_boolean_cannot_impersonate_schema_version_two() -> None:
    payload = _manifest()
    payload["schema_version"] = True
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="must be 2"):
        b2_evidence.validate_manifest(payload)


def test_schema_dispatch_preserves_v2_and_fails_closed_for_unknown_versions() -> None:
    payload = _manifest(owner=True)
    b2_evidence.validate_manifest_v2(payload, require_owner_acceptance=True)
    payload["schema_version"] = 3
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="no validator registered"):
        b2_evidence.validate_manifest(payload, require_owner_acceptance=True)

    payload["schema_version"] = 1
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="unsupported historical"):
        b2_evidence.validate_manifest(payload)


@pytest.mark.parametrize(
    ("location", "key"),
    [
        ((), "surprise"),
        (("candidate",), "surprise"),
        (("evidence", 0), "surprise"),
        (("unknowns", 0), "surprise"),
        (("risk_assumptions", 0), "surprise"),
    ],
)
def test_unknown_keys_are_rejected(location: tuple[object, ...], key: str) -> None:
    payload = _manifest()
    target: object = payload
    for part in location:
        target = target[part]  # type: ignore[index]
    target[key] = True  # type: ignore[index]
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="unknown keys"):
        b2_evidence.validate_manifest(payload)


def test_duplicate_json_object_keys_are_rejected_before_validation() -> None:
    raw = '{"schema_version":1,"schema_version":1}'
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="duplicate key"):
        b2_evidence.loads_manifest(raw)


def test_duplicate_evidence_ids_are_rejected() -> None:
    payload = _manifest()
    duplicate = copy.deepcopy(payload["evidence"][0])  # type: ignore[index]
    payload["evidence"].append(duplicate)  # type: ignore[union-attr]
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="duplicate evidence id"):
        b2_evidence.validate_manifest(payload)


@pytest.mark.parametrize(
    ("field", "bad_value", "message"),
    [
        ("sha256", "A" * 64, "lowercase hexadecimal"),
        ("bytes", 0, "positive integer"),
        ("bytes", True, "positive integer"),
        ("capture_commit", "1" * 39, "lowercase hexadecimal"),
    ],
)
def test_bound_evidence_requires_valid_hash_size_and_capture_commit(
    field: str, bad_value: object, message: str
) -> None:
    payload = _manifest()
    payload["evidence"][0][field] = bad_value  # type: ignore[index]
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match=message):
        b2_evidence.validate_manifest(payload)


def test_evidence_capture_time_must_be_real_utc_timestamp() -> None:
    payload = _manifest()
    payload["evidence"][0]["captured_at_utc"] = "2026-02-30T00:00:00Z"  # type: ignore[index]
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="invalid calendar"):
        b2_evidence.validate_manifest(payload)


@pytest.mark.parametrize(
    "bad_path",
    [
        "../escape.json",
        "/absolute.json",
        "C:/absolute.json",
        "controlled\\windows.json",
        "$EVIDENCE/file.json",
        "%EVIDENCE%/file.json",
        "controlled/./file.json",
        "controlled//file.json",
    ],
)
def test_path_escape_and_nonliteral_paths_are_rejected(bad_path: str) -> None:
    payload = _manifest()
    payload["evidence"][0]["relative_path"] = bad_path  # type: ignore[index]
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="relative|POSIX"):
        b2_evidence.validate_manifest(payload)


def test_pass_fail_conflict_for_the_same_claim_is_rejected() -> None:
    payload = _manifest()
    payload["evidence"][1]["claim_key"] = "FULL_RTH_20260818_V4"  # type: ignore[index]
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="PASS/FAIL conflict"):
        b2_evidence.validate_manifest(payload)


def test_reference_only_cannot_be_promoted_to_authority() -> None:
    payload = _manifest()
    payload["evidence"][0]["binding"] = "REFERENCE_ONLY"  # type: ignore[index]
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="not BOUND_AUTHORITY"):
        b2_evidence.validate_manifest(payload)


def test_historical_failure_is_mandatory_and_must_stay_failed() -> None:
    payload = _manifest()
    payload["evidence"][1]["evidence_kind"] = "OTHER"  # type: ignore[index]
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="HISTORICAL_FAILURE"):
        b2_evidence.validate_manifest(payload)

    payload = _manifest()
    payload["evidence"][1]["verdict"] = "PASS"  # type: ignore[index]
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="bound FAIL"):
        b2_evidence.validate_manifest(payload)


def test_ci_must_be_success_on_the_exact_candidate() -> None:
    payload = _manifest()
    payload["ci_runs"][0]["commit_sha"] = "9" * 40  # type: ignore[index]
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="equal candidate"):
        b2_evidence.validate_manifest(payload)

    payload = _manifest()
    payload["ci_runs"][0]["conclusion"] = "FAILURE"  # type: ignore[index]
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="must be SUCCESS"):
        b2_evidence.validate_manifest(payload)


def test_ci_artifact_must_reference_a_declared_run() -> None:
    payload = _manifest()
    payload["ci_artifacts"][0]["run_id"] = 1000  # type: ignore[index]
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="must reference ci_runs"):
        b2_evidence.validate_manifest(payload)


def test_bound_ci_evidence_requires_exactly_one_artifact_member() -> None:
    payload = _manifest()
    payload["evidence"][0]["evidence_kind"] = "CI_ARTIFACT"  # type: ignore[index]
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="must bind every"):
        b2_evidence.validate_manifest(payload)

@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("gate_b2", "PASS"),
        ("order_authorization", "PAPER"),
        ("trading_adapter", "IMPLEMENTED"),
    ],
)
def test_manifest_cannot_upgrade_the_safety_boundary(field: str, bad_value: str) -> None:
    payload = _manifest()
    payload["safety_boundary"][field] = bad_value  # type: ignore[index]
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="must remain"):
        b2_evidence.validate_manifest(payload)


@pytest.mark.parametrize(
    "risk_id",
    ["D1_EVENT_DRIVEN_30S", "D2_WRITER_LAG_ROOT_CAUSE"],
)
def test_d1_and_d2_risk_assumptions_are_mandatory(risk_id: str) -> None:
    payload = _manifest()
    payload["risk_assumptions"] = [
        item for item in payload["risk_assumptions"] if item["id"] != risk_id  # type: ignore[union-attr]
    ]
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="missing required ids"):
        b2_evidence.validate_manifest(payload)


def test_d1_d2_do_not_block_freeze_but_require_later_review() -> None:
    payload = _manifest()
    payload["risk_assumptions"][0]["blocks_b2_read_only_freeze"] = True  # type: ignore[index]
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="must be false"):
        b2_evidence.validate_manifest(payload)

    payload = _manifest()
    payload["risk_assumptions"][0]["required_review_before"] = "B2_FREEZE"  # type: ignore[index]
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="must require review"):
        b2_evidence.validate_manifest(payload)


def test_owner_can_accept_only_exact_scope_and_not_order_capability() -> None:
    payload = _manifest(owner=True)
    payload["owner_acceptance"]["order_capability"] = "AUTHORIZED"  # type: ignore[index]
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="NOT_AUTHORIZED"):
        b2_evidence.validate_manifest(payload, require_owner_acceptance=True)

    payload = _manifest(owner=True)
    payload["owner_acceptance"]["accepted_unknown_ids"] = []  # type: ignore[index]
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="must not be empty"):
        b2_evidence.validate_manifest(payload, require_owner_acceptance=True)


def test_unknown_freeze_blocker_semantics_cannot_contradict_review_point() -> None:
    payload = _manifest()
    payload["unknowns"][0]["blocks_b2_read_only_freeze"] = True  # type: ignore[index]
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="must agree"):
        b2_evidence.validate_manifest(payload)


def test_owner_cannot_accept_a_manifest_with_a_freeze_blocker() -> None:
    payload = _manifest(owner=True)
    payload["unknowns"][0]["blocks_b2_read_only_freeze"] = True  # type: ignore[index]
    payload["unknowns"][0]["review_before"] = "B2_FREEZE"  # type: ignore[index]
    with pytest.raises(b2_evidence.B2EvidenceValidationError, match="blocking unknowns"):
        b2_evidence.validate_manifest(payload, require_owner_acceptance=True)
