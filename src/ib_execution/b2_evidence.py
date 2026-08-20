"""Strict schema and validator for Gate B2 read-only evidence manifests.

This module deliberately does not collect evidence, connect to IB, derive a
Gate PASS, or authorize orders.  It defines the fail-closed packet that the F2
builder must later populate and that the F3 exact-tree freeze must validate.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = 1
FREEZE_KIND = "B2_READ_ONLY_EVIDENCE"

HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER = re.compile(r"^[A-Z0-9][A-Z0-9_.-]{0,127}$")
UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

REQUIRED_RISK_ASSUMPTIONS = {
    "D1_EVENT_DRIVEN_30S",
    "D2_WRITER_LAG_ROOT_CAUSE",
}

TOP_LEVEL_KEYS = {
    "schema_version",
    "freeze_kind",
    "candidate",
    "safety_boundary",
    "ci_runs",
    "evidence",
    "authority_evidence_ids",
    "required_failures",
    "unknowns",
    "risk_assumptions",
    "owner_acceptance",
}
CANDIDATE_KEYS = {
    "commit_sha",
    "tree_sha",
    "source_tree_sha256",
    "config_tree_sha256",
    "dependency_lock_sha256",
}
SAFETY_BOUNDARY_KEYS = {
    "gate_b2",
    "order_authorization",
    "trading_adapter",
}
CI_KEYS = {
    "provider",
    "workflow",
    "run_id",
    "run_attempt",
    "commit_sha",
    "conclusion",
}
EVIDENCE_KEYS = {
    "id",
    "claim_key",
    "evidence_kind",
    "binding",
    "relative_path",
    "sha256",
    "bytes",
    "captured_at_utc",
    "capture_commit",
    "verdict",
    "scope",
    "sensitivity",
}
UNKNOWN_KEYS = {
    "id",
    "statement",
    "status",
    "blocks_b2_read_only_freeze",
    "review_before",
}
RISK_KEYS = {
    "id",
    "statement",
    "failure_mode",
    "mitigation",
    "status",
    "blocks_b2_read_only_freeze",
    "required_review_before",
}
OWNER_KEYS = {
    "owner",
    "accepted_at_utc",
    "decision",
    "gate_b2_decision",
    "order_capability",
    "accepted_authority_evidence_ids",
    "accepted_unknown_ids",
    "accepted_risk_assumption_ids",
}

EVIDENCE_KINDS = {
    "FULL_RTH_HEALTH",
    "HISTORICAL_FAILURE",
    "INCIDENT",
    "WINDOWS_LIFECYCLE",
    "CI_ARTIFACT",
    "DOCUMENTED_VS_OBSERVED",
    "OTHER",
}
BINDINGS = {"BOUND_AUTHORITY", "REFERENCE_ONLY", "UNBOUND"}
VERDICTS = {
    "PASS",
    "FAIL",
    "PARTIALLY_VERIFIED",
    "NOT_VERIFIED",
    "OPEN",
    "INFORMATIONAL",
}
SENSITIVITIES = {
    "PUBLIC_METADATA",
    "REDACTED",
    "CONTROLLED_EXTERNAL",
    "SENSITIVE_EXTERNAL",
}
UNKNOWN_STATUSES = {"NOT_VERIFIED", "AMBIGUOUS", "NOT_GUARANTEED", "OPEN"}
UNKNOWN_REVIEW_POINTS = {"B2_FREEZE", "PRODUCTION_OR_ORDER_CAPABLE", "NONE"}


class B2EvidenceValidationError(ValueError):
    """The manifest is not safe to use as B2 read-only freeze evidence."""


def _fail(location: str, message: str) -> None:
    raise B2EvidenceValidationError(f"{location}: {message}")


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(location, "must be an object")
    return value


def _sequence(value: Any, location: str) -> Sequence[Any]:
    if not isinstance(value, list):
        _fail(location, "must be an array")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], location: str) -> None:
    actual = set(value)
    if actual != expected:
        unknown = sorted(actual - expected)
        missing = sorted(expected - actual)
        details: list[str] = []
        if unknown:
            details.append("unknown keys=" + ",".join(unknown))
        if missing:
            details.append("missing keys=" + ",".join(missing))
        _fail(location, "; ".join(details))


def _text(value: Any, location: str, *, max_length: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(location, "must be a non-empty string")
    if value != value.strip():
        _fail(location, "must not have leading or trailing whitespace")
    if len(value) > max_length:
        _fail(location, f"must be at most {max_length} characters")
    if any(ord(character) < 32 for character in value):
        _fail(location, "must not contain control characters")
    return value


def _enum(value: Any, allowed: set[str], location: str) -> str:
    text = _text(value, location, max_length=128)
    if text not in allowed:
        _fail(location, "unsupported value")
    return text


def _identifier(value: Any, location: str) -> str:
    text = _text(value, location, max_length=128)
    if not IDENTIFIER.fullmatch(text):
        _fail(location, "must match [A-Z0-9][A-Z0-9_.-]{0,127}")
    return text


def _hex(value: Any, pattern: re.Pattern[str], location: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        _fail(location, "must be a lowercase hexadecimal digest of the required length")
    return value


def _positive_int(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail(location, "must be a positive integer")
    return value


def _utc_timestamp(value: Any, location: str) -> str:
    timestamp = _text(value, location)
    if not UTC_TIMESTAMP.fullmatch(timestamp):
        _fail(location, "must be UTC YYYY-MM-DDTHH:MM:SSZ")
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise B2EvidenceValidationError(f"{location}: invalid calendar timestamp") from exc
    return timestamp


def _bool(value: Any, location: str) -> bool:
    if not isinstance(value, bool):
        _fail(location, "must be a boolean")
    return value


def _relative_path(value: Any, location: str) -> str:
    text = _text(value, location, max_length=1024)
    if "\\" in text or ":" in text or "$" in text or "%" in text or text.startswith("~"):
        _fail(location, "must be a literal relative POSIX path")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        _fail(location, "must not be absolute or contain empty, dot, or parent segments")
    if path.as_posix() != text:
        _fail(location, "must be normalized POSIX form")
    return text


def _unique_identifiers(value: Any, location: str, *, allow_empty: bool = False) -> list[str]:
    items = _sequence(value, location)
    if not items and not allow_empty:
        _fail(location, "must not be empty")
    result = [_identifier(item, f"{location}[{index}]") for index, item in enumerate(items)]
    if len(set(result)) != len(result):
        _fail(location, "contains duplicate ids")
    return result


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise B2EvidenceValidationError(f"JSON object contains duplicate key: {key}")
        result[key] = value
    return result


def loads_manifest(raw: str | bytes) -> dict[str, Any]:
    """Decode JSON without silently accepting duplicate object keys."""
    try:
        data = json.loads(raw, object_pairs_hook=_strict_object)
    except B2EvidenceValidationError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise B2EvidenceValidationError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        _fail("manifest", "must be a JSON object")
    return data


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise B2EvidenceValidationError(f"cannot read manifest: {exc}") from exc
    return loads_manifest(raw)


def _validate_candidate(value: Any) -> Mapping[str, Any]:
    candidate = _mapping(value, "candidate")
    _exact_keys(candidate, CANDIDATE_KEYS, "candidate")
    _hex(candidate["commit_sha"], HEX40, "candidate.commit_sha")
    _hex(candidate["tree_sha"], HEX40, "candidate.tree_sha")
    for key in ("source_tree_sha256", "config_tree_sha256", "dependency_lock_sha256"):
        _hex(candidate[key], HEX64, f"candidate.{key}")
    return candidate


def _validate_safety_boundary(value: Any) -> None:
    boundary = _mapping(value, "safety_boundary")
    _exact_keys(boundary, SAFETY_BOUNDARY_KEYS, "safety_boundary")
    expected = {
        "gate_b2": "READ_ONLY_IN_PROGRESS",
        "order_authorization": "NONE",
        "trading_adapter": "NOT_IMPLEMENTED",
    }
    for key, expected_value in expected.items():
        if boundary[key] != expected_value:
            _fail(f"safety_boundary.{key}", f"must remain {expected_value}")


def _validate_ci_runs(value: Any, candidate_commit: str) -> None:
    runs = _sequence(value, "ci_runs")
    if not runs:
        _fail("ci_runs", "must contain exact-candidate CI")
    identities: set[tuple[str, str, int, int]] = set()
    for index, raw in enumerate(runs):
        location = f"ci_runs[{index}]"
        run = _mapping(raw, location)
        _exact_keys(run, CI_KEYS, location)
        provider = _enum(run["provider"], {"GITHUB_ACTIONS"}, f"{location}.provider")
        workflow = _text(run["workflow"], f"{location}.workflow", max_length=256)
        run_id = _positive_int(run["run_id"], f"{location}.run_id")
        attempt = _positive_int(run["run_attempt"], f"{location}.run_attempt")
        commit = _hex(run["commit_sha"], HEX40, f"{location}.commit_sha")
        if commit != candidate_commit:
            _fail(f"{location}.commit_sha", "must equal candidate.commit_sha")
        if run["conclusion"] != "SUCCESS":
            _fail(f"{location}.conclusion", "must be SUCCESS")
        identity = (provider, workflow, run_id, attempt)
        if identity in identities:
            _fail(location, "duplicate CI run identity")
        identities.add(identity)


def _validate_evidence(value: Any) -> dict[str, Mapping[str, Any]]:
    items = _sequence(value, "evidence")
    if not items:
        _fail("evidence", "must not be empty")
    by_id: dict[str, Mapping[str, Any]] = {}
    claim_verdicts: dict[str, set[str]] = {}
    for index, raw in enumerate(items):
        location = f"evidence[{index}]"
        item = _mapping(raw, location)
        _exact_keys(item, EVIDENCE_KEYS, location)
        evidence_id = _identifier(item["id"], f"{location}.id")
        if evidence_id in by_id:
            _fail(f"{location}.id", "duplicate evidence id")
        claim = _identifier(item["claim_key"], f"{location}.claim_key")
        _enum(item["evidence_kind"], EVIDENCE_KINDS, f"{location}.evidence_kind")
        binding = _enum(item["binding"], BINDINGS, f"{location}.binding")
        _relative_path(item["relative_path"], f"{location}.relative_path")
        _utc_timestamp(item["captured_at_utc"], f"{location}.captured_at_utc")
        verdict = _enum(item["verdict"], VERDICTS, f"{location}.verdict")
        _text(item["scope"], f"{location}.scope")
        _enum(item["sensitivity"], SENSITIVITIES, f"{location}.sensitivity")

        digest = item["sha256"]
        size = item["bytes"]
        capture_commit = item["capture_commit"]
        if binding == "BOUND_AUTHORITY":
            _hex(digest, HEX64, f"{location}.sha256")
            _positive_int(size, f"{location}.bytes")
            _hex(capture_commit, HEX40, f"{location}.capture_commit")
        elif binding == "UNBOUND":
            if digest is not None or size is not None or capture_commit is not None:
                _fail(location, "UNBOUND evidence must use null hash, bytes, and capture_commit")
        else:
            if digest is not None:
                _hex(digest, HEX64, f"{location}.sha256")
            if size is not None:
                _positive_int(size, f"{location}.bytes")
            if capture_commit is not None:
                _hex(capture_commit, HEX40, f"{location}.capture_commit")

        if verdict in {"PASS", "FAIL"}:
            claim_verdicts.setdefault(claim, set()).add(verdict)
        by_id[evidence_id] = item

    conflicts = sorted(claim for claim, verdicts in claim_verdicts.items() if len(verdicts) > 1)
    if conflicts:
        _fail("evidence", "PASS/FAIL conflict for claim_key=" + ",".join(conflicts))
    return by_id


def _validate_authority_and_failures(
    payload: Mapping[str, Any], by_id: Mapping[str, Mapping[str, Any]]
) -> tuple[list[str], list[str]]:
    authority_ids = _unique_identifiers(payload["authority_evidence_ids"], "authority_evidence_ids")
    failure_ids = _unique_identifiers(payload["required_failures"], "required_failures")
    for evidence_id in authority_ids:
        item = by_id.get(evidence_id)
        if item is None:
            _fail("authority_evidence_ids", f"unknown evidence id {evidence_id}")
        if item["binding"] != "BOUND_AUTHORITY":
            _fail("authority_evidence_ids", f"{evidence_id} is not BOUND_AUTHORITY")
    for evidence_id in failure_ids:
        item = by_id.get(evidence_id)
        if item is None:
            _fail("required_failures", f"unknown evidence id {evidence_id}")
        if evidence_id not in authority_ids:
            _fail("required_failures", f"{evidence_id} must also be authority evidence")
        if item["binding"] != "BOUND_AUTHORITY" or item["verdict"] != "FAIL":
            _fail("required_failures", f"{evidence_id} must be a bound FAIL")
    if not any(
        by_id[item_id]["evidence_kind"] == "HISTORICAL_FAILURE" for item_id in failure_ids
    ):
        _fail("required_failures", "must preserve a HISTORICAL_FAILURE")
    if not any(
        by_id[item_id]["evidence_kind"] == "FULL_RTH_HEALTH"
        and by_id[item_id]["verdict"] == "PASS"
        for item_id in authority_ids
    ):
        _fail("authority_evidence_ids", "must bind a FULL_RTH_HEALTH PASS")
    return authority_ids, failure_ids


def _validate_unknowns(value: Any) -> tuple[list[str], list[str]]:
    items = _sequence(value, "unknowns")
    if not items:
        _fail("unknowns", "must preserve at least one unknown or documented ambiguity")
    ids: list[str] = []
    blockers: list[str] = []
    for index, raw in enumerate(items):
        location = f"unknowns[{index}]"
        item = _mapping(raw, location)
        _exact_keys(item, UNKNOWN_KEYS, location)
        ids.append(_identifier(item["id"], f"{location}.id"))
        _text(item["statement"], f"{location}.statement")
        _enum(item["status"], UNKNOWN_STATUSES, f"{location}.status")
        blocks = _bool(
            item["blocks_b2_read_only_freeze"],
            f"{location}.blocks_b2_read_only_freeze",
        )
        review = _enum(
            item["review_before"], UNKNOWN_REVIEW_POINTS, f"{location}.review_before"
        )
        if blocks != (review == "B2_FREEZE"):
            _fail(location, "B2_FREEZE review and blocks_b2_read_only_freeze must agree")
        if blocks:
            blockers.append(ids[-1])
    if len(set(ids)) != len(ids):
        _fail("unknowns", "contains duplicate ids")
    return ids, blockers


def _validate_risk_assumptions(value: Any) -> list[str]:
    items = _sequence(value, "risk_assumptions")
    ids: list[str] = []
    for index, raw in enumerate(items):
        location = f"risk_assumptions[{index}]"
        item = _mapping(raw, location)
        _exact_keys(item, RISK_KEYS, location)
        ids.append(_identifier(item["id"], f"{location}.id"))
        for key in ("statement", "failure_mode", "mitigation"):
            _text(item[key], f"{location}.{key}")
        if item["status"] != "OPEN_REVIEW_REQUIRED":
            _fail(f"{location}.status", "must be OPEN_REVIEW_REQUIRED")
        if item["blocks_b2_read_only_freeze"] is not False:
            _fail(
                f"{location}.blocks_b2_read_only_freeze",
                "must be false; these assumptions do not block the read-only freeze",
            )
        if item["required_review_before"] != "PRODUCTION_OR_ORDER_CAPABLE_PAPER_LIVE":
            _fail(
                f"{location}.required_review_before",
                "must require review before production or order-capable Paper/Live",
            )
    if len(set(ids)) != len(ids):
        _fail("risk_assumptions", "contains duplicate ids")
    missing = REQUIRED_RISK_ASSUMPTIONS - set(ids)
    if missing:
        _fail("risk_assumptions", "missing required ids=" + ",".join(sorted(missing)))
    return ids


def _validate_owner_acceptance(
    value: Any,
    *,
    authority_ids: Iterable[str],
    unknown_ids: Iterable[str],
    risk_ids: Iterable[str],
    required: bool,
) -> None:
    if value is None:
        if required:
            _fail("owner_acceptance", "is required for a final freeze")
        return
    owner = _mapping(value, "owner_acceptance")
    _exact_keys(owner, OWNER_KEYS, "owner_acceptance")
    _text(owner["owner"], "owner_acceptance.owner", max_length=256)
    _utc_timestamp(owner["accepted_at_utc"], "owner_acceptance.accepted_at_utc")
    if owner["decision"] != "ACCEPT_EVIDENCE_SCOPE_AND_RESIDUAL_RISK":
        _fail("owner_acceptance.decision", "cannot accept a Gate PASS or trading capability")
    if owner["gate_b2_decision"] != "REMAIN_READ_ONLY_IN_PROGRESS":
        _fail("owner_acceptance.gate_b2_decision", "must preserve the current Gate B2 state")
    if owner["order_capability"] != "NOT_AUTHORIZED":
        _fail("owner_acceptance.order_capability", "must remain NOT_AUTHORIZED")

    expected_sets = {
        "accepted_authority_evidence_ids": set(authority_ids),
        "accepted_unknown_ids": set(unknown_ids),
        "accepted_risk_assumption_ids": set(risk_ids),
    }
    for key, expected in expected_sets.items():
        actual = set(_unique_identifiers(owner[key], f"owner_acceptance.{key}"))
        if actual != expected:
            _fail(f"owner_acceptance.{key}", "must exactly accept the manifest ids")


def validate_manifest(
    payload: Mapping[str, Any], *, require_owner_acceptance: bool = False
) -> None:
    """Validate structure and cross-field safety semantics.

    This F1 validator intentionally does not prove that referenced bytes exist
    or match Git/disk.  That is the F2 builder's job.  A valid F1 packet is a
    necessary shape for a future freeze, never sufficient evidence by itself.
    """
    manifest = _mapping(payload, "manifest")
    _exact_keys(manifest, TOP_LEVEL_KEYS, "manifest")
    if isinstance(manifest["schema_version"], bool) or manifest["schema_version"] != SCHEMA_VERSION:
        _fail("schema_version", f"must be {SCHEMA_VERSION}")
    if manifest["freeze_kind"] != FREEZE_KIND:
        _fail("freeze_kind", f"must be {FREEZE_KIND}")
    candidate = _validate_candidate(manifest["candidate"])
    _validate_safety_boundary(manifest["safety_boundary"])
    _validate_ci_runs(manifest["ci_runs"], candidate["commit_sha"])
    evidence_by_id = _validate_evidence(manifest["evidence"])
    authority_ids, _ = _validate_authority_and_failures(manifest, evidence_by_id)
    unknown_ids, freeze_blockers = _validate_unknowns(manifest["unknowns"])
    risk_ids = _validate_risk_assumptions(manifest["risk_assumptions"])
    if manifest["owner_acceptance"] is not None and freeze_blockers:
        _fail(
            "owner_acceptance",
            "cannot accept a final freeze with blocking unknowns="
            + ",".join(sorted(freeze_blockers)),
        )
    _validate_owner_acceptance(
        manifest["owner_acceptance"],
        authority_ids=authority_ids,
        unknown_ids=unknown_ids,
        risk_ids=risk_ids,
        required=require_owner_acceptance,
    )


def build_manifest(**fields: Any) -> dict[str, Any]:
    """Assemble and validate caller-supplied records without collecting evidence."""
    payload = dict(fields)
    validate_manifest(payload)
    return payload


def dumps_manifest(payload: Mapping[str, Any]) -> str:
    validate_manifest(payload)
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
