"""Run and preserve the reproducible Gate B1 test campaign."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ib_execution import provenance  # noqa: E402


def run_stage(name: str, args: list[str], root: Path, output: Path) -> dict:
    command = [sys.executable, "-m", "pytest", *args]
    completed = subprocess.run(command, cwd=root, text=True, capture_output=True)
    transcript = output / f"{name}.txt"
    transcript.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    return {
        "name": name,
        "command": command,
        "returncode": completed.returncode,
        "transcript": transcript.name,
        "sha256": sha256(transcript),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int)
    ap.add_argument("--output-root", default="artifacts/gate_b1")
    ap.add_argument("--skip-default-property", action="store_true")
    ns = ap.parse_args(argv)
    if sys.version_info[:2] != (3, 12):
        raise SystemExit(f"Gate B1 requires Python 3.12, got {platform.python_version()}")

    root = Path(__file__).resolve().parents[1]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = root / ns.output_root / stamp
    output.mkdir(parents=True, exist_ok=False)
    seed = ns.seed if ns.seed is not None else secrets.randbits(64)
    source_hash, source_files = provenance.source_tree_sha256(root)
    config_hash, config_files = provenance.config_tree_sha256(root)

    stages = [
        run_stage(
            "deterministic",
            ["-q", "-m", "not property", "--junitxml", str(output / "deterministic.xml")],
            root,
            output,
        )
    ]
    if not ns.skip_default_property:
        stages.append(
            run_stage(
                "property_default",
                ["-q", "-m", "property", "--junitxml", str(output / "property_default.xml")],
                root,
                output,
            )
        )
    stages.append(
        run_stage(
            "property_gate",
            [
                "-q", "-m", "property", "--hypothesis-profile=gate",
                f"--hypothesis-seed={seed}", "--hypothesis-show-statistics",
                "--junitxml", str(output / "property_gate.xml"),
            ],
            root,
            output,
        )
    )

    artifact_hashes = {
        path.name: sha256(path)
        for path in sorted(output.iterdir())
        if path.is_file()
    }
    # Four hashes, four different questions. Do not merge them: a lockfile is
    # an intention, resolved_environment_sha256 is an observation, and Gate B1
    # is an argument about observations.
    manifest = {
        "gate": "B1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "pytest": importlib.metadata.version("pytest"),
        "hypothesis": importlib.metadata.version("hypothesis"),
        "seed": seed,
        "commit_sha": provenance.commit_sha(root),
        "worktree_clean": provenance.worktree_clean(root),
        "source_tree_sha256": source_hash,
        "source_files": source_files,
        "config_tree_sha256": config_hash,
        "config_files": config_files,
        "dependency_lock_sha256": provenance.dependency_lock_sha256(root),
        "resolved_environment_sha256": provenance.resolved_environment_sha256(),
        "resolved_environment": provenance.resolved_environment(),
        "stages": stages,
        "artifact_hashes": artifact_hashes,
        "passed": all(stage["returncode"] == 0 for stage in stages),
    }
    if not manifest["worktree_clean"]:
        # A campaign run against a dirty worktree cannot be tied to a commit,
        # so it can never become sign-off evidence. Produce it, label it.
        manifest["passed"] = False
        manifest["invalid_reason"] = (
            "worktree is dirty; Gate B1 evidence must bind to an exact commit"
        )
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(manifest_path)
    return 0 if manifest["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
