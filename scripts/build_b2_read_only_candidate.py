"""Populate or verify an F2 B2 read-only candidate manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ib_execution import b2_evidence, b2_evidence_material

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build or verify an F2 B2 read-only evidence candidate"
    )
    parser.add_argument("manifest", type=Path, help="candidate template or completed manifest")
    parser.add_argument("--controlled-roots", required=True, type=Path)
    parser.add_argument("--github-repository", required=True, help="owner/repository")
    parser.add_argument("--output", type=Path, help="write populated manifest here")
    parser.add_argument("--report", type=Path, help="write a small replay report here")
    parser.add_argument("--verify-only", action="store_true", help="do not populate hashes")
    parser.add_argument(
        "--manifest-budget-bytes",
        type=int,
        default=b2_evidence_material.DEFAULT_MANIFEST_BUDGET_BYTES,
    )
    args = parser.parse_args(argv)
    if args.verify_only and args.output:
        parser.error("--verify-only cannot be combined with --output")
    if not args.verify_only and args.output is None:
        parser.error("build mode requires --output")
    try:
        try:
            input_bytes = args.manifest.stat().st_size
        except OSError as exc:
            raise b2_evidence_material.B2MaterialVerificationError(
                f"cannot stat candidate manifest: {exc}"
            ) from exc
        if input_bytes > args.manifest_budget_bytes:
            raise b2_evidence_material.B2MaterialVerificationError(
                f"input manifest is {input_bytes} bytes; budget is {args.manifest_budget_bytes}"
            )
        payload = b2_evidence.load_manifest(args.manifest)
        roots = b2_evidence_material.load_controlled_roots(args.controlled_roots)
        report = b2_evidence_material.verify_materials(
            payload,
            repo_root=ROOT,
            controlled_roots=roots,
            github_repository=args.github_repository,
            populate_observed=not args.verify_only,
            manifest_budget_bytes=args.manifest_budget_bytes,
        )
        manifest_raw = b2_evidence.dumps_manifest(payload).encode("utf-8")
        if args.output:
            b2_evidence_material.publish_create_only(args.output, manifest_raw)
        if args.report:
            report_raw = (json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            )
            b2_evidence_material.publish_create_only(args.report, report_raw)
    except (
        b2_evidence.B2EvidenceValidationError,
        b2_evidence_material.B2MaterialVerificationError,
    ) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        "PASS: exact Git objects, controlled evidence bytes, and GitHub CI identity verified; "
        "Gate B2 remains READ_ONLY_IN_PROGRESS and orders remain unauthorized"
    )
    print(json.dumps(report.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
