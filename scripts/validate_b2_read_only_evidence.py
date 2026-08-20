"""Validate a Gate B2 read-only evidence manifest without touching IB."""

from __future__ import annotations

import argparse
from pathlib import Path

from ib_execution.b2_evidence import B2EvidenceValidationError, load_manifest, validate_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--require-owner-acceptance",
        action="store_true",
        help="require the final metadata owner acceptance rather than a candidate packet",
    )
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        validate_manifest(
            manifest,
            require_owner_acceptance=args.require_owner_acceptance,
        )
    except B2EvidenceValidationError as exc:
        parser.exit(1, f"FAIL: {exc}\n")
    print(
        "PASS: valid B2_READ_ONLY_EVIDENCE structure; "
        "this is not Gate B2 PASS or order authorization"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
