"""F2 material verification for Gate B2 read-only evidence manifests.

The F1 schema proves shape and cross-field semantics.  This module proves the
parts that require observations: exact Git-object bytes, controlled external
files, and GitHub Actions run identity.  It never connects to IB and never
copies evidence files into the repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, MutableMapping, Sequence

from . import b2_evidence

CHUNK_BYTES = 1024 * 1024
DEFAULT_MANIFEST_BUDGET_BYTES = 256 * 1024
ROOT_ALIAS = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SENSITIVE_TEXT = (
    re.compile(r"(?i)authorization\s*[:=]\s*(?:bearer|basic)\s+\S+"),
    re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token)\s*[:=]\s*\S+"),
    re.compile(r"\bDU\d{5,}\b", re.IGNORECASE),
)


class B2MaterialVerificationError(ValueError):
    """Observed material does not support the candidate manifest."""


@dataclass(frozen=True)
class MaterialReport:
    candidate_commit: str
    candidate_tree: str
    git_file_count: int
    evidence_files_verified: int
    evidence_bytes_streamed: int
    ci_runs_verified: int
    ci_jobs_verified: int
    manifest_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "verification_kind": "B2_READ_ONLY_F2_MATERIAL",
            "candidate_commit": self.candidate_commit,
            "candidate_tree": self.candidate_tree,
            "git_file_count": self.git_file_count,
            "evidence_files_verified": self.evidence_files_verified,
            "evidence_bytes_streamed": self.evidence_bytes_streamed,
            "ci_runs_verified": self.ci_runs_verified,
            "ci_jobs_verified": self.ci_jobs_verified,
            "manifest_bytes": self.manifest_bytes,
            "gate_b2": "READ_ONLY_IN_PROGRESS",
            "order_authorization": "NONE",
            "result": "PASS",
            "limits": (
                "Material identity only; evidence verdict semantics remain scope-bound. "
                "This is not Gate B2 PASS or order authorization."
            ),
        }


GitRunner = Callable[[Sequence[str]], bytes]
GithubLookup = Callable[[str, int, int], Mapping[str, Any]]


def _run_git(repo_root: Path, arguments: Sequence[str]) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *arguments],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise B2MaterialVerificationError(f"cannot execute Git: {exc}") from exc
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise B2MaterialVerificationError(f"Git {' '.join(arguments)} failed: {message}")
    return completed.stdout


def _git_text(run_git: GitRunner, arguments: Sequence[str]) -> str:
    try:
        return run_git(arguments).decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise B2MaterialVerificationError("Git returned non-UTF-8 metadata") from exc


def _commit_identity(run_git: GitRunner, commit: str) -> tuple[str, datetime]:
    object_type = _git_text(run_git, ["cat-file", "-t", commit])
    if object_type != "commit":
        raise B2MaterialVerificationError(f"candidate {commit} is not a commit object")
    tree = _git_text(run_git, ["show", "-s", "--format=%T", commit])
    raw_time = _git_text(run_git, ["show", "-s", "--format=%cI", commit])
    try:
        commit_time = datetime.fromisoformat(raw_time)
    except ValueError as exc:
        raise B2MaterialVerificationError("Git commit has invalid committer timestamp") from exc
    return tree, commit_time.astimezone(timezone.utc)


def _git_paths(run_git: GitRunner, commit: str) -> list[str]:
    raw = run_git(["ls-tree", "-r", "-z", "--name-only", commit])
    try:
        paths = [part.decode("utf-8") for part in raw.split(b"\0") if part]
    except UnicodeDecodeError as exc:
        raise B2MaterialVerificationError("candidate tree contains a non-UTF-8 path") from exc
    if len(paths) != len(set(paths)):
        raise B2MaterialVerificationError("candidate tree returned duplicate paths")
    return paths


def _git_blob(run_git: GitRunner, commit: str, path: str) -> bytes:
    return run_git(["show", f"{commit}:{path}"])


def _digest_named_git_blobs(run_git: GitRunner, commit: str, paths: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        digest.update(path.encode("utf-8") + b"\0")
        digest.update(_git_blob(run_git, commit, path))
        digest.update(b"\0")
    return digest.hexdigest()


def derive_candidate_from_git(repo_root: Path, commit: str) -> tuple[dict[str, str], int]:
    """Derive candidate identity without reading source bytes from the worktree."""
    run_git: GitRunner = lambda arguments: _run_git(repo_root, arguments)
    tree, _ = _commit_identity(run_git, commit)
    paths = _git_paths(run_git, commit)
    source_paths = [
        path
        for path in paths
        if path == "pyproject.toml"
        or (path.endswith(".py") and path.split("/", 1)[0] in {"src", "tests", "scripts"})
    ]
    config_paths = [
        path
        for path in paths
        if path.startswith("config/")
        and "/" not in path[len("config/") :]
        and path.endswith(".example.yml")
    ]
    if "uv.lock" not in paths:
        raise B2MaterialVerificationError("candidate tree is missing uv.lock")
    if "STATE.json" not in paths:
        raise B2MaterialVerificationError("candidate tree is missing STATE.json")
    state_raw = _git_blob(run_git, commit, "STATE.json")
    try:
        state = json.loads(state_raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise B2MaterialVerificationError("candidate STATE.json is invalid JSON") from exc
    tree_state = state.get("tree", {})
    gate = state.get("gate_status", {})
    observed = {
        "commit_sha": commit,
        "tree_sha": tree,
        "source_tree_sha256": _digest_named_git_blobs(run_git, commit, source_paths),
        "config_tree_sha256": _digest_named_git_blobs(run_git, commit, config_paths),
        "dependency_lock_sha256": hashlib.sha256(
            _git_blob(run_git, commit, "uv.lock")
        ).hexdigest(),
    }
    for key in ("source_tree_sha256", "config_tree_sha256", "dependency_lock_sha256"):
        if tree_state.get(key) != observed[key]:
            raise B2MaterialVerificationError(
                f"candidate STATE.json {key} does not match exact Git-object bytes"
            )
    expected_boundary = {
        "gate_b2": "READ_ONLY_IN_PROGRESS",
        "order_authorization": "NONE",
        "trading_adapter": "NOT_IMPLEMENTED",
    }
    for key, expected in expected_boundary.items():
        if gate.get(key) != expected:
            raise B2MaterialVerificationError(f"candidate STATE.json {key} must remain {expected}")
    return observed, len(paths)


def load_controlled_roots(path: Path) -> dict[str, Path]:
    """Load an explicit alias-to-directory allowlist; no env expansion is performed."""
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise B2MaterialVerificationError(f"cannot load controlled roots: {exc}") from exc
    if not isinstance(payload, dict) or not payload:
        raise B2MaterialVerificationError("controlled roots must be a non-empty JSON object")
    roots: dict[str, Path] = {}
    for alias, raw_root in payload.items():
        if not isinstance(alias, str) or not ROOT_ALIAS.fullmatch(alias):
            raise B2MaterialVerificationError(f"invalid controlled-root alias: {alias!r}")
        if not isinstance(raw_root, str) or not raw_root or raw_root != raw_root.strip():
            raise B2MaterialVerificationError(f"controlled root {alias} must be a literal path")
        if any(marker in raw_root for marker in ("$", "%")) or raw_root.startswith("~"):
            raise B2MaterialVerificationError(f"controlled root {alias} cannot use expansion")
        root = Path(raw_root)
        if not root.is_absolute():
            raise B2MaterialVerificationError(f"controlled root {alias} must be absolute")
        try:
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise B2MaterialVerificationError(
                f"controlled root {alias} is unavailable: {exc}"
            ) from exc
        if not resolved.is_dir():
            raise B2MaterialVerificationError(f"controlled root {alias} is not a directory")
        roots[alias] = resolved
    return roots


def resolve_evidence_path(relative_path: str, roots: Mapping[str, Path]) -> Path:
    parts = PurePosixPath(relative_path).parts
    if len(parts) < 2:
        raise B2MaterialVerificationError(
            f"evidence path {relative_path!r} must begin with a controlled-root alias"
        )
    alias = parts[0]
    if alias not in roots:
        raise B2MaterialVerificationError(f"evidence path uses unapproved root alias {alias!r}")
    try:
        root = roots[alias].resolve(strict=True)
    except OSError as exc:
        raise B2MaterialVerificationError(
            f"controlled root {alias!r} is unavailable: {exc}"
        ) from exc
    if not root.is_dir():
        raise B2MaterialVerificationError(f"controlled root {alias!r} is not a directory")
    try:
        resolved = root.joinpath(*parts[1:]).resolve(strict=True)
    except OSError as exc:
        raise B2MaterialVerificationError(
            f"evidence path {relative_path!r} is unavailable: {exc}"
        ) from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise B2MaterialVerificationError(
            f"evidence path escapes controlled root: {relative_path}"
        ) from exc
    if not resolved.is_file():
        raise B2MaterialVerificationError(f"evidence path is not a regular file: {relative_path}")
    return resolved


def stream_file_identity(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            while chunk := handle.read(CHUNK_BYTES):
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise B2MaterialVerificationError(f"cannot stream evidence file {path}: {exc}") from exc
    if size <= 0:
        raise B2MaterialVerificationError(f"evidence file is empty: {path}")
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise B2MaterialVerificationError(f"evidence file changed while hashing: {path}")
    if size != after.st_size:
        raise B2MaterialVerificationError(f"evidence byte count changed while hashing: {path}")
    return digest.hexdigest(), size


def _parse_manifest_time(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (AttributeError, ValueError) as exc:
        raise B2MaterialVerificationError(f"invalid evidence capture timestamp: {raw!r}") from exc


def _verify_capture_commit_and_time(
    repo_root: Path, capture_commit: str, captured_at_utc: str, now: datetime
) -> None:
    run_git: GitRunner = lambda arguments: _run_git(repo_root, arguments)
    _, commit_time = _commit_identity(run_git, capture_commit)
    captured = _parse_manifest_time(captured_at_utc)
    if captured < commit_time:
        raise B2MaterialVerificationError(
            f"evidence capture time {captured_at_utc} predates capture commit {capture_commit}"
        )
    if captured > now.astimezone(timezone.utc):
        raise B2MaterialVerificationError(
            f"evidence capture time is in the future: {captured_at_utc}"
        )


def github_lookup_with_gh(repository: str, run_id: int, attempt: int) -> Mapping[str, Any]:
    """Query GitHub directly; stderr is not echoed because it may contain environment detail."""
    if not REPOSITORY.fullmatch(repository) or any(
        part in {".", ".."} for part in repository.split("/")
    ):
        raise B2MaterialVerificationError("GitHub repository must be owner/name")
    base = f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}"

    def query(endpoint: str) -> Mapping[str, Any]:
        try:
            completed = subprocess.run(
                ["gh", "api", endpoint], capture_output=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise B2MaterialVerificationError(f"cannot query GitHub Actions: {exc}") from exc
        if completed.returncode != 0:
            raise B2MaterialVerificationError("GitHub Actions query failed")
        try:
            payload = json.loads(completed.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise B2MaterialVerificationError("GitHub Actions returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise B2MaterialVerificationError("GitHub Actions returned a non-object")
        return payload

    jobs_payload = query(base + "/jobs?per_page=100")
    return {
        "run": query(base),
        "jobs": jobs_payload.get("jobs"),
        "jobs_total_count": jobs_payload.get("total_count"),
    }


def _verify_ci_runs(
    runs: Sequence[Mapping[str, Any]], repository: str, lookup: GithubLookup
) -> tuple[int, int]:
    job_count = 0
    for run in runs:
        observation = lookup(repository, run["run_id"], run["run_attempt"])
        observed = observation.get("run")
        jobs = observation.get("jobs")
        if not isinstance(observed, Mapping) or not isinstance(jobs, list) or not jobs:
            raise B2MaterialVerificationError(
                f"GitHub run {run['run_id']} must include the run and at least one observed job"
            )
        if observation.get("jobs_total_count") != len(jobs):
            raise B2MaterialVerificationError(
                f"GitHub run {run['run_id']} job listing is incomplete"
            )
        expected = {
            "id": run["run_id"],
            "run_attempt": run["run_attempt"],
            "head_sha": run["commit_sha"],
            "name": run["workflow"],
            "status": "completed",
            "conclusion": "success",
        }
        for key, value in expected.items():
            if observed.get(key) != value:
                raise B2MaterialVerificationError(
                    f"GitHub run {run['run_id']} observed {key} does not match manifest"
                )
        job_ids: set[int] = set()
        for job in jobs:
            if not isinstance(job, Mapping):
                raise B2MaterialVerificationError(
                    f"GitHub run {run['run_id']} has invalid job data"
                )
            job_id = job.get("id")
            if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id <= 0:
                raise B2MaterialVerificationError(f"GitHub run {run['run_id']} has invalid job id")
            if job_id in job_ids:
                raise B2MaterialVerificationError(
                    f"GitHub run {run['run_id']} has duplicate job id"
                )
            job_ids.add(job_id)
            expected_job = {
                "run_id": run["run_id"],
                "run_attempt": run["run_attempt"],
                "head_sha": run["commit_sha"],
                "status": "completed",
                "conclusion": "success",
            }
            for key, value in expected_job.items():
                if job.get(key) != value:
                    raise B2MaterialVerificationError(
                        f"GitHub job {job_id} observed {key} does not match manifest"
                    )
            if not isinstance(job.get("name"), str) or not job["name"].strip():
                raise B2MaterialVerificationError(f"GitHub job {job_id} has no name")
            steps = job.get("steps")
            if not isinstance(steps, list) or not any(
                isinstance(step, Mapping) and step.get("conclusion") == "success" for step in steps
            ):
                raise B2MaterialVerificationError(
                    f"GitHub job {job_id} has no successful executed step"
                )
            job_count += 1
    return len(runs), job_count


def _scan_manifest(payload: Mapping[str, Any]) -> bytes:
    raw = b2_evidence.dumps_manifest(payload).encode("utf-8")
    text = raw.decode("utf-8")
    for pattern in SENSITIVE_TEXT:
        if pattern.search(text):
            raise B2MaterialVerificationError("candidate manifest contains sensitive-looking text")
    return raw


def verify_materials(
    payload: MutableMapping[str, Any],
    *,
    repo_root: Path,
    controlled_roots: Mapping[str, Path],
    github_repository: str,
    github_lookup: GithubLookup = github_lookup_with_gh,
    populate_observed: bool = False,
    manifest_budget_bytes: int = DEFAULT_MANIFEST_BUDGET_BYTES,
    now: datetime | None = None,
) -> MaterialReport:
    """Populate or verify observed fields, then apply F1 and F2 fail-closed checks."""
    if manifest_budget_bytes <= 0:
        raise B2MaterialVerificationError("manifest byte budget must be positive")
    # Build mode permits placeholder digests, but the template must already be
    # a valid F1 packet.  This prevents material resolution from operating on
    # unknown keys, malformed paths, invalid timestamps, or unsafe semantics.
    b2_evidence.validate_manifest(payload)
    candidate = payload.get("candidate")
    if not isinstance(candidate, dict) or not b2_evidence.HEX40.fullmatch(
        str(candidate.get("commit_sha", ""))
    ):
        raise B2MaterialVerificationError("manifest must declare a full candidate commit")
    observed_candidate, git_count = derive_candidate_from_git(
        repo_root, candidate["commit_sha"]
    )
    if populate_observed:
        candidate.update(observed_candidate)
    elif candidate != observed_candidate:
        raise B2MaterialVerificationError(
            "manifest candidate does not match exact Git-object identity"
        )

    current_time = now or datetime.now(timezone.utc)
    verified_files = 0
    streamed_bytes = 0
    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        raise B2MaterialVerificationError("manifest evidence must be an array")
    for item in evidence:
        if not isinstance(item, dict):
            raise B2MaterialVerificationError("manifest evidence entry must be an object")
        binding = item.get("binding")
        if binding == "UNBOUND":
            continue
        if binding == "REFERENCE_ONLY" and all(
            item.get(key) is None for key in ("sha256", "bytes", "capture_commit")
        ):
            continue
        path = resolve_evidence_path(str(item.get("relative_path", "")), controlled_roots)
        digest, size = stream_file_identity(path)
        if populate_observed:
            item["sha256"] = digest
            item["bytes"] = size
        elif item.get("sha256") != digest or item.get("bytes") != size:
            raise B2MaterialVerificationError(
                f"evidence bytes do not match manifest: {item.get('id', '<unknown>')}"
            )
        capture_commit = item.get("capture_commit")
        if capture_commit is None:
            raise B2MaterialVerificationError(
                f"material evidence must declare capture_commit: {item.get('id', '<unknown>')}"
            )
        _verify_capture_commit_and_time(
            repo_root, str(capture_commit), str(item.get("captured_at_utc", "")), current_time
        )
        verified_files += 1
        streamed_bytes += size

    b2_evidence.validate_manifest(payload)
    ci_runs = payload["ci_runs"]
    ci_count, ci_job_count = _verify_ci_runs(ci_runs, github_repository, github_lookup)
    raw = _scan_manifest(payload)
    if len(raw) > manifest_budget_bytes:
        raise B2MaterialVerificationError(
            f"candidate manifest is {len(raw)} bytes; budget is {manifest_budget_bytes}"
        )
    return MaterialReport(
        candidate_commit=observed_candidate["commit_sha"],
        candidate_tree=observed_candidate["tree_sha"],
        git_file_count=git_count,
        evidence_files_verified=verified_files,
        evidence_bytes_streamed=streamed_bytes,
        ci_runs_verified=ci_count,
        ci_jobs_verified=ci_job_count,
        manifest_bytes=len(raw),
    )
