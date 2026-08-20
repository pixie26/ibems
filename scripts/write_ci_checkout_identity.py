"""Write a small, auditable identity sidecar for an exact CI checkout."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], capture_output=True, text=True, timeout=30, check=True
    )
    return completed.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    required = {
        "event": "GITHUB_EVENT_NAME",
        "job_name": "GITHUB_JOB",
        "repository": "GITHUB_REPOSITORY",
        "run_id": "GITHUB_RUN_ID",
        "run_attempt": "GITHUB_RUN_ATTEMPT",
        "workflow": "GITHUB_WORKFLOW",
    }
    missing = [name for name in required.values() if not os.environ.get(name)]
    if missing:
        parser.error(f"missing GitHub Actions environment: {', '.join(sorted(missing))}")
    payload = {key: os.environ[name] for key, name in required.items()}
    payload.update(
        {
            "schema_version": 1,
            "commit_sha": _git("rev-parse", "HEAD"),
            "tree_sha": _git("show", "-s", "--format=%T", "HEAD"),
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
