"""Create immutable health-v4 and manifest-amendment-v4 from raw Recorder evidence."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

from ib_execution.recorder_health_v4 import write_reanalysis_v4


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--session-open", required=True, type=_aware_datetime)
    parser.add_argument("--session-close", required=True, type=_aware_datetime)
    parser.add_argument("--original-health", required=True, type=Path)
    parser.add_argument("--original-manifest", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        health_path, amendment_path = write_reanalysis_v4(
            args.raw_dir,
            args.output_dir,
            session_open=args.session_open,
            session_close=args.session_close,
            original_health=args.original_health,
            original_manifest=args.original_manifest,
        )
    except (FileExistsError, FileNotFoundError, OSError, ValueError) as exc:
        print(f"reanalysis refused: {exc}", file=sys.stderr)
        return 2
    print(health_path)
    print(amendment_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
