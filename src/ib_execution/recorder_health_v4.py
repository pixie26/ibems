"""Immutable Full-RTH health schema v4 and offline reanalysis.

v3 artifacts are historical evidence and are never rewritten. This module
re-reads immutable raw Recorder segments and emits a sidecar health verdict
whose semantics match the live liveness contract:

* ``BAR_5S`` is time-driven and may create a hard problem.
* ``BID_ASK`` and ``ALL_LAST`` are event-driven; silence is quality evidence.
* 1100 is a direct ``FEED_OUTAGE``.
* 2103/2104 describe the real-time market-data farm. Only the temporal overlap
  between a 2103 degradation and an independently missing BAR is classified as
  ``FEED_OUTAGE``; unexplained remainder stays ``GAP_SUSPECTED``.
* 2105/2106 (historical) and 2157/2158 (security definition) are structured
  advisories. They can neither create nor hide a SPY real-time outage.

The implementation is disk-backed and single-pass over gzip JSONL for semantic
analysis. It never materializes a Full-RTH day in Python memory.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .market_liveness import realtime_farm_identity

SCHEMA_VERSION = 4
BAR_STREAM = "BAR_5S"
EVENT_DRIVEN_STREAMS = ("BID_ASK", "ALL_LAST")
MARKET_STREAMS = (*EVENT_DRIVEN_STREAMS, BAR_STREAM)
BAR_GAP_SECONDS = 15.0
EVENT_OBSERVATION_SECONDS = 30.0
LEGACY_GAP_SECONDS = {"BID_ASK": 5.0, "ALL_LAST": 30.0, "BAR_5S": 15.0}

CONNECTIVITY_LOST = 1100
CONNECTIVITY_RESTORED_DATA_LOST = 1101
CONNECTIVITY_RESTORED_DATA_KEPT = 1102
REALTIME_FARM_BROKEN = 2103
REALTIME_FARM_OK = 2104
HISTORICAL_FARM_BROKEN = 2105
HISTORICAL_FARM_OK = 2106
FARM_INACTIVE = 2108
SECDEF_FARM_BROKEN = 2157
SECDEF_FARM_OK = 2158
AUXILIARY_FARM_CODES = frozenset(
    {
        HISTORICAL_FARM_BROKEN,
        HISTORICAL_FARM_OK,
        FARM_INACTIVE,
        SECDEF_FARM_BROKEN,
        SECDEF_FARM_OK,
    }
)
FATAL_MARKET_DATA_CODES = frozenset({354, 10089, 10189, 10197})
REALTIME_BARS_RESET = 10225

_ERROR_RE = re.compile(r"^IB_ERROR:(?P<code>-?\d+):(?P<req>[^:]*):(?P<message>.*)$")
_HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

RAW_EVENT_KEYS = frozenset(
    {
        "event_id",
        "recorder_run_id",
        "connection_epoch",
        "contract_id",
        "event_type",
        "broker_timestamp",
        "local_wall_ns",
        "local_monotonic_ns",
        "market_data_type",
        "receive_sequence",
        "bid",
        "ask",
        "bid_size",
        "ask_size",
        "last",
        "last_size",
        "exchange",
        "special_conditions",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "wap",
        "trade_count",
    }
)
_RAW_INTEGER_FIELDS = {
    "event_id": 1,
    "connection_epoch": 0,
    "contract_id": 0,
    "local_wall_ns": 1,
    "local_monotonic_ns": 0,
    "receive_sequence": 1,
}
_RAW_OPTIONAL_NUMBER_FIELDS = frozenset(
    {
        "bid",
        "ask",
        "bid_size",
        "ask_size",
        "last",
        "last_size",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "wap",
    }
)


class RawRowValidationError(ValueError):
    """One decoded raw row does not satisfy the frozen Recorder schema."""


@dataclass(frozen=True)
class RawFileState:
    size: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int
    sha256: str


@dataclass(frozen=True)
class HealthFinding:
    stream: str
    classification: str
    start_utc: str
    end_utc: str
    duration_seconds: float
    evidence: list[str]


@dataclass(frozen=True)
class StreamMetrics:
    stream: str
    rows: int
    first_utc: str | None
    last_utc: str | None
    max_gap_seconds: float
    observation_threshold_seconds: float
    gaps_over_observation_threshold: int
    legacy_v3_threshold_seconds: float
    legacy_v3_gaps_over_threshold: int


@dataclass(frozen=True)
class StatusEvent:
    wall_ns: int
    code: int
    message: str


class _Staging:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=OFF")
        self.connection.execute("PRAGMA synchronous=OFF")
        self.connection.execute("PRAGMA temp_store=FILE")
        self.connection.execute("PRAGMA cache_size=-65536")
        self.connection.executescript(
            """
            CREATE TABLE stream_times (
                stream TEXT NOT NULL,
                wall_ns INTEGER NOT NULL
            );
            CREATE TABLE statuses (
                wall_ns INTEGER NOT NULL,
                code INTEGER NOT NULL,
                message TEXT NOT NULL
            );
            """
        )
        self.events = 0
        self.market_data_types: set[str] = set()
        self.recorder_errors: list[tuple[int, str]] = []
        self.auxiliary_farm_statuses: list[StatusEvent] = []
        self._prepared = False

    def add_rows(self, rows: Sequence[dict[str, Any]]) -> None:
        stream_times: list[tuple[str, int]] = []
        statuses: list[tuple[int, int, str]] = []
        for row in rows:
            self.events += 1
            wall_ns = int(row.get("local_wall_ns") or 0)
            event_type = str(row.get("event_type") or "")
            if event_type in MARKET_STREAMS:
                stream_times.append((event_type, wall_ns))
                self.market_data_types.add(str(row.get("market_data_type") or "UNKNOWN"))
                continue
            if event_type != "SYSTEM":
                continue
            condition = str(row.get("special_conditions") or "")
            if condition.startswith("RECORDER_ERROR:"):
                self.recorder_errors.append((wall_ns, condition))
            match = _ERROR_RE.match(condition)
            if match is None:
                continue
            code = int(match.group("code"))
            message = match.group("message")
            statuses.append((wall_ns, code, message))
            if code in AUXILIARY_FARM_CODES:
                self.auxiliary_farm_statuses.append(StatusEvent(wall_ns, code, message))
        with self.connection:
            self.connection.executemany(
                "INSERT INTO stream_times(stream, wall_ns) VALUES (?, ?)", stream_times
            )
            self.connection.executemany(
                "INSERT INTO statuses(wall_ns, code, message) VALUES (?, ?, ?)", statuses
            )

    def prepare(self) -> None:
        if self._prepared:
            return
        self.connection.commit()
        self.connection.execute(
            "CREATE INDEX stream_times_order ON stream_times(stream, wall_ns)"
        )
        self.connection.execute("CREATE INDEX statuses_order ON statuses(wall_ns, code)")
        self._prepared = True

    def stream_stamps(self, stream: str) -> Iterator[int]:
        self.prepare()
        cursor = self.connection.execute(
            "SELECT wall_ns FROM stream_times WHERE stream=? ORDER BY wall_ns", (stream,)
        )
        for (wall_ns,) in cursor:
            yield int(wall_ns)

    def statuses(self) -> list[StatusEvent]:
        self.prepare()
        return [
            StatusEvent(int(wall_ns), int(code), str(message))
            for wall_ns, code, message in self.connection.execute(
                "SELECT wall_ns, code, message FROM statuses ORDER BY wall_ns, rowid"
            )
        ]

    def close(self) -> None:
        self.connection.close()


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, timezone.utc).isoformat()


def _duration_seconds(start_ns: int, end_ns: int) -> float:
    return max(0.0, (end_ns - start_ns) / 1e9)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _combined_input_digest(file_hashes: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name, value in sorted(file_hashes.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _validate_raw_row(row: Any, *, segment: str, line_number: int) -> dict[str, Any]:
    location = f"{segment}:{line_number}"
    if not isinstance(row, dict):
        raise RawRowValidationError(f"{location}: raw row must be a JSON object")
    keys = frozenset(row)
    missing = sorted(RAW_EVENT_KEYS - keys)
    unknown = sorted(keys - RAW_EVENT_KEYS)
    if missing or unknown:
        raise RawRowValidationError(
            f"{location}: raw schema mismatch; missing={missing}; unknown={unknown}"
        )

    event_type = row["event_type"]
    if not isinstance(event_type, str) or event_type not in {*MARKET_STREAMS, "SYSTEM"}:
        raise RawRowValidationError(f"{location}: unknown event_type {event_type!r}")
    run_id = row["recorder_run_id"]
    if not isinstance(run_id, str) or not run_id.strip():
        raise RawRowValidationError(f"{location}: recorder_run_id must be non-empty")
    market_data_type = row["market_data_type"]
    if not isinstance(market_data_type, str) or not market_data_type.strip():
        raise RawRowValidationError(f"{location}: market_data_type must be non-empty")

    for name, minimum in _RAW_INTEGER_FIELDS.items():
        value = row[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise RawRowValidationError(
                f"{location}: {name} must be an integer >= {minimum}; got {value!r}"
            )

    broker_timestamp = row["broker_timestamp"]
    if not isinstance(broker_timestamp, str):
        raise RawRowValidationError(f"{location}: broker_timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(broker_timestamp)
    except ValueError as exc:
        raise RawRowValidationError(
            f"{location}: broker_timestamp is not ISO-8601: {broker_timestamp!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise RawRowValidationError(f"{location}: broker_timestamp must include UTC offset")

    for name in _RAW_OPTIONAL_NUMBER_FIELDS:
        value = row[name]
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RawRowValidationError(f"{location}: {name} must be numeric or null")
        if not math.isfinite(float(value)):
            raise RawRowValidationError(f"{location}: {name} must be finite")
    trade_count = row["trade_count"]
    if trade_count is not None and (
        isinstance(trade_count, bool) or not isinstance(trade_count, int) or trade_count < 0
    ):
        raise RawRowValidationError(f"{location}: trade_count must be a nonnegative integer or null")
    for name in ("exchange", "special_conditions"):
        value = row[name]
        if value is not None and not isinstance(value, str):
            raise RawRowValidationError(f"{location}: {name} must be a string or null")
    return row


def _raw_segments(raw_dir: Path) -> list[Path]:
    segments = sorted(raw_dir.glob("segment-*.jsonl.gz"))
    segments.extend(sorted(raw_dir.glob("crashed-*.jsonl.gz")))
    return sorted(set(segments), key=lambda path: path.name)


def _capture_raw_state(raw_dir: Path) -> tuple[list[Path], dict[str, RawFileState]]:
    segments = _raw_segments(raw_dir)
    if not segments:
        raise FileNotFoundError(f"no raw Recorder segments found under {raw_dir}")
    states: dict[str, RawFileState] = {}
    for path in segments:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"raw segment must be a regular non-symlink file: {path}")
        before = path.stat()
        digest = _sha256(path)
        after = path.stat()
        before_identity = (
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_dev,
            before.st_ino,
        )
        after_identity = (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_dev,
            after.st_ino,
        )
        if before_identity != after_identity:
            raise ValueError(f"raw segment changed while hashing: {path.name}")
        states[path.name] = RawFileState(
            size=int(after.st_size),
            mtime_ns=int(after.st_mtime_ns),
            ctime_ns=int(after.st_ctime_ns),
            device=int(after.st_dev),
            inode=int(after.st_ino),
            sha256=digest,
        )
    return segments, states


def _manifest_raw_hashes(payload: dict[str, Any]) -> dict[str, str]:
    files = payload.get("files")
    if not isinstance(files, dict):
        raise ValueError("original v3 manifest is missing files mapping")
    hashes = {
        str(name): str(value)
        for name, value in files.items()
        if str(name).startswith(("segment-", "crashed-"))
        and str(name).endswith(".jsonl.gz")
    }
    if not hashes:
        raise ValueError("original v3 manifest has no raw segment hashes")
    invalid = sorted(name for name, value in hashes.items() if not _HEX_SHA256_RE.fullmatch(value))
    if invalid:
        raise ValueError(f"original v3 manifest has invalid raw SHA-256 values: {invalid}")
    return dict(sorted(hashes.items()))


def _describe_state_change(
    before: dict[str, RawFileState], after: dict[str, RawFileState]
) -> str:
    before_names = set(before)
    after_names = set(after)
    added = sorted(after_names - before_names)
    removed = sorted(before_names - after_names)
    changed = sorted(name for name in before_names & after_names if before[name] != after[name])
    return f"added={added}; removed={removed}; changed={changed}"


def iter_raw_rows(
    raw_dir: Path,
    *,
    segments: Sequence[Path] | None = None,
    integrity: list[dict[str, Any]] | None = None,
) -> Iterator[dict[str, Any]]:
    """Read each immutable raw segment exactly once for semantic analysis."""
    for segment in _raw_segments(raw_dir) if segments is None else segments:
        rows = 0
        error: str | None = None
        try:
            with gzip.open(segment, "rb") as handle:
                for line in handle:
                    rows += 1
                    decoded = json.loads(line)
                    yield _validate_raw_row(
                        decoded,
                        segment=segment.name,
                        line_number=rows,
                    )
        except (
            EOFError,
            OSError,
            gzip.BadGzipFile,
            json.JSONDecodeError,
            RawRowValidationError,
        ) as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if integrity is not None:
                integrity.append(
                    {
                        "segment": segment.name,
                        "rows_read": rows,
                        "compressed_bytes": segment.stat().st_size,
                        "read_error": error,
                        "salvaged": segment.name.startswith("crashed-"),
                    }
                )


def _batched(
    rows: Iterable[dict[str, Any]], size: int = 50_000
) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _gaps(
    stamps: Iterable[int],
    session_open_ns: int,
    session_close_ns: int,
) -> tuple[int, str | None, str | None, float, list[tuple[int, int]]]:
    first: int | None = None
    previous: int | None = None
    last: int | None = None
    rows = 0
    max_gap = 0.0
    gaps: list[tuple[int, int]] = []

    for stamp in stamps:
        clipped = min(max(int(stamp), session_open_ns), session_close_ns)
        if first is None:
            first = clipped
            if clipped > session_open_ns:
                gaps.append((session_open_ns, clipped))
        if previous is not None and clipped > previous:
            gaps.append((previous, clipped))
        previous = clipped
        last = clipped
        rows += 1

    if last is None:
        if session_close_ns > session_open_ns:
            gaps.append((session_open_ns, session_close_ns))
    elif last < session_close_ns:
        gaps.append((last, session_close_ns))

    for start_ns, end_ns in gaps:
        max_gap = max(max_gap, _duration_seconds(start_ns, end_ns))
    return (
        rows,
        _iso(first) if first is not None else None,
        _iso(last) if last is not None else None,
        max_gap,
        gaps,
    )


def _status_state_at(
    statuses: Sequence[StatusEvent], target_ns: int
) -> tuple[bool, dict[str, StatusEvent]]:
    connectivity_lost = False
    realtime_farms_broken: dict[str, StatusEvent] = {}
    for status in statuses:
        if status.wall_ns > target_ns:
            break
        if status.code == CONNECTIVITY_LOST:
            connectivity_lost = True
        elif status.code in (
            CONNECTIVITY_RESTORED_DATA_LOST,
            CONNECTIVITY_RESTORED_DATA_KEPT,
        ):
            connectivity_lost = False
        elif status.code == REALTIME_FARM_BROKEN:
            realtime_farms_broken[realtime_farm_identity(status.message)] = status
        elif status.code == REALTIME_FARM_OK:
            realtime_farms_broken.pop(realtime_farm_identity(status.message), None)
    return connectivity_lost, realtime_farms_broken


def _connectivity_outages(
    statuses: Sequence[StatusEvent], session_open_ns: int, session_close_ns: int
) -> list[HealthFinding]:
    findings: list[HealthFinding] = []
    open_status: StatusEvent | None = None
    for status in statuses:
        if status.wall_ns > session_close_ns:
            break
        if status.code == CONNECTIVITY_LOST:
            if open_status is None:
                open_status = status
            continue
        if status.code not in (
            CONNECTIVITY_RESTORED_DATA_LOST,
            CONNECTIVITY_RESTORED_DATA_KEPT,
        ) or open_status is None:
            continue
        end_ns = min(status.wall_ns, session_close_ns)
        start_ns = max(open_status.wall_ns, session_open_ns)
        if end_ns > start_ns:
            findings.append(
                HealthFinding(
                    stream=BAR_STREAM,
                    classification="FEED_OUTAGE",
                    start_utc=_iso(start_ns),
                    end_utc=_iso(end_ns),
                    duration_seconds=_duration_seconds(start_ns, end_ns),
                    evidence=[
                        f"IB {open_status.code}: {open_status.message}",
                        f"IB {status.code}: {status.message}",
                    ],
                )
            )
        open_status = None
    if open_status is not None and open_status.wall_ns < session_close_ns:
        start_ns = max(open_status.wall_ns, session_open_ns)
        if session_close_ns > start_ns:
            findings.append(
                HealthFinding(
                    stream=BAR_STREAM,
                    classification="FEED_OUTAGE",
                    start_utc=_iso(start_ns),
                    end_utc=_iso(session_close_ns),
                    duration_seconds=_duration_seconds(start_ns, session_close_ns),
                    evidence=[
                        f"IB {open_status.code}: {open_status.message}",
                        "no connectivity restoration before session close",
                    ],
                )
            )
    return findings


def _bar_gap_findings(
    statuses: Sequence[StatusEvent], start_ns: int, end_ns: int
) -> list[HealthFinding]:
    """Partition one missing-BAR interval by actual IB state transitions."""
    boundaries = {start_ns, end_ns}
    relevant = frozenset(
        {
            CONNECTIVITY_LOST,
            CONNECTIVITY_RESTORED_DATA_LOST,
            CONNECTIVITY_RESTORED_DATA_KEPT,
            REALTIME_FARM_BROKEN,
            REALTIME_FARM_OK,
        }
    )
    for status in statuses:
        if start_ns < status.wall_ns < end_ns and status.code in relevant:
            boundaries.add(status.wall_ns)
    ordered = sorted(boundaries)
    findings: list[HealthFinding] = []
    total_gap = _duration_seconds(start_ns, end_ns)

    for sub_start, sub_end in zip(ordered, ordered[1:]):
        if sub_end <= sub_start:
            continue
        connectivity_lost, realtime_farms_broken = _status_state_at(statuses, sub_start)
        if connectivity_lost:
            # 1100 produces its own direct exact-duration hard finding.
            continue
        if realtime_farms_broken:
            evidence = [
                f"BAR_5S gap {total_gap:.3f}s > {BAR_GAP_SECONDS:.0f}s",
                "BAR heartbeat is absent during a real-time market-data farm degradation",
            ]
            evidence.extend(
                f"IB {status.code}: {status.message}"
                for _, status in sorted(realtime_farms_broken.items())
            )
            classification = "FEED_OUTAGE"
        else:
            evidence = [
                f"BAR_5S gap {total_gap:.3f}s > {BAR_GAP_SECONDS:.0f}s",
                "no IB connectivity or real-time farm outage explains this subinterval",
            ]
            classification = "GAP_SUSPECTED"
        findings.append(
            HealthFinding(
                stream=BAR_STREAM,
                classification=classification,
                start_utc=_iso(sub_start),
                end_utc=_iso(sub_end),
                duration_seconds=_duration_seconds(sub_start, sub_end),
                evidence=evidence,
            )
        )
    return findings


def _paired_auxiliary_advisories(
    statuses: Sequence[StatusEvent],
    *,
    broken_code: int,
    ok_code: int,
    stream: str,
    session_open_ns: int,
    session_close_ns: int,
) -> list[HealthFinding]:
    advisories: list[HealthFinding] = []
    opened: StatusEvent | None = None
    for status in statuses:
        if status.wall_ns > session_close_ns:
            break
        if status.code == broken_code:
            if opened is None:
                opened = status
            continue
        if status.code != ok_code or opened is None:
            continue
        start_ns = max(opened.wall_ns, session_open_ns)
        end_ns = min(status.wall_ns, session_close_ns)
        if end_ns >= start_ns:
            advisories.append(
                HealthFinding(
                    stream=stream,
                    classification="AUXILIARY_FARM_DEGRADED",
                    start_utc=_iso(start_ns),
                    end_utc=_iso(end_ns),
                    duration_seconds=_duration_seconds(start_ns, end_ns),
                    evidence=[
                        f"IB {opened.code}: {opened.message}",
                        f"IB {status.code}: {status.message}",
                        "auxiliary farm state is not evidence about SPY real-time feed liveness",
                    ],
                )
            )
        opened = None
    if opened is not None and opened.wall_ns < session_close_ns:
        start_ns = max(opened.wall_ns, session_open_ns)
        advisories.append(
            HealthFinding(
                stream=stream,
                classification="AUXILIARY_FARM_DEGRADED",
                start_utc=_iso(start_ns),
                end_utc=_iso(session_close_ns),
                duration_seconds=_duration_seconds(start_ns, session_close_ns),
                evidence=[
                    f"IB {opened.code}: {opened.message}",
                    "no auxiliary-farm restoration observed before session close",
                    "auxiliary farm state is not evidence about SPY real-time feed liveness",
                ],
            )
        )
    return advisories


def _auxiliary_advisories(
    statuses: Sequence[StatusEvent], session_open_ns: int, session_close_ns: int
) -> list[HealthFinding]:
    advisories = _paired_auxiliary_advisories(
        statuses,
        broken_code=HISTORICAL_FARM_BROKEN,
        ok_code=HISTORICAL_FARM_OK,
        stream="HISTORICAL_DATA_FARM",
        session_open_ns=session_open_ns,
        session_close_ns=session_close_ns,
    )
    advisories.extend(
        _paired_auxiliary_advisories(
            statuses,
            broken_code=SECDEF_FARM_BROKEN,
            ok_code=SECDEF_FARM_OK,
            stream="SECURITY_DEFINITION_FARM",
            session_open_ns=session_open_ns,
            session_close_ns=session_close_ns,
        )
    )
    for status in statuses:
        if status.code != FARM_INACTIVE:
            continue
        if not session_open_ns <= status.wall_ns <= session_close_ns:
            continue
        advisories.append(
            HealthFinding(
                stream="AUXILIARY_FARM",
                classification="AUXILIARY_FARM_INACTIVE",
                start_utc=_iso(status.wall_ns),
                end_utc=_iso(status.wall_ns),
                duration_seconds=0.0,
                evidence=[
                    f"IB {status.code}: {status.message}",
                    "inactive/on-demand auxiliary farm status is advisory only",
                ],
            )
        )
    return sorted(advisories, key=lambda item: (item.start_utc, item.stream))


def _fatal_market_data_status(status: StatusEvent) -> bool:
    if status.code in FATAL_MARKET_DATA_CODES:
        return True
    return status.code == 420 and "market data permissions" in status.message.lower()


def analyze_rows_v4(
    rows: Iterable[dict[str, Any]],
    *,
    session_open: datetime,
    session_close: datetime,
    input_metadata: dict[str, Any] | None = None,
    integrity: list[dict[str, Any]] | None = None,
    work_parent: Path | None = None,
) -> dict[str, Any]:
    if session_open.tzinfo is None or session_close.tzinfo is None:
        raise ValueError("session_open and session_close must be timezone-aware")
    if session_close <= session_open:
        raise ValueError("session_close must be after session_open")

    session_open_ns = int(session_open.timestamp() * 1e9)
    session_close_ns = int(session_close.timestamp() * 1e9)
    with tempfile.TemporaryDirectory(prefix=".health-v4-", dir=work_parent) as temporary:
        staging = _Staging(Path(temporary) / "health-v4.sqlite")
        try:
            for batch in _batched(rows):
                staging.add_rows(batch)
            statuses = staging.statuses()
            problems: list[HealthFinding] = []
            advisories: list[HealthFinding] = _auxiliary_advisories(
                statuses, session_open_ns, session_close_ns
            )
            metrics: dict[str, StreamMetrics] = {}

            problems.extend(
                _connectivity_outages(statuses, session_open_ns, session_close_ns)
            )

            for stream in MARKET_STREAMS:
                row_count, first_utc, last_utc, max_gap, gaps = _gaps(
                    staging.stream_stamps(stream), session_open_ns, session_close_ns
                )
                observation_threshold = (
                    BAR_GAP_SECONDS if stream == BAR_STREAM else EVENT_OBSERVATION_SECONDS
                )
                legacy_threshold = LEGACY_GAP_SECONDS[stream]
                metrics[stream] = StreamMetrics(
                    stream=stream,
                    rows=row_count,
                    first_utc=first_utc,
                    last_utc=last_utc,
                    max_gap_seconds=max_gap,
                    observation_threshold_seconds=observation_threshold,
                    gaps_over_observation_threshold=sum(
                        _duration_seconds(start, end) > observation_threshold
                        for start, end in gaps
                    ),
                    legacy_v3_threshold_seconds=legacy_threshold,
                    legacy_v3_gaps_over_threshold=sum(
                        _duration_seconds(start, end) > legacy_threshold
                        for start, end in gaps
                    ),
                )

                for start_ns, end_ns in gaps:
                    duration = _duration_seconds(start_ns, end_ns)
                    if duration <= observation_threshold:
                        continue
                    if stream in EVENT_DRIVEN_STREAMS:
                        advisories.append(
                            HealthFinding(
                                stream=stream,
                                classification="EVENT_DRIVEN_GAP",
                                start_utc=_iso(start_ns),
                                end_utc=_iso(end_ns),
                                duration_seconds=duration,
                                evidence=[
                                    f"{stream} had no event for {duration:.3f}s",
                                    "event-driven silence alone does not prove feed loss",
                                ],
                            )
                        )
                    else:
                        problems.extend(_bar_gap_findings(statuses, start_ns, end_ns))

            for status in statuses:
                if _fatal_market_data_status(status):
                    problems.append(
                        HealthFinding(
                            stream="MARKET_DATA",
                            classification="SUBSCRIPTION_ERROR",
                            start_utc=_iso(status.wall_ns),
                            end_utc=_iso(status.wall_ns),
                            duration_seconds=0.0,
                            evidence=[f"IB {status.code}: {status.message}"],
                        )
                    )
                elif status.code == REALTIME_BARS_RESET:
                    problems.append(
                        HealthFinding(
                            stream=BAR_STREAM,
                            classification="SUBSCRIPTION_RESET",
                            start_utc=_iso(status.wall_ns),
                            end_utc=_iso(status.wall_ns),
                            duration_seconds=0.0,
                            evidence=[f"IB {status.code}: {status.message}"],
                        )
                    )

            for wall_ns, error in staging.recorder_errors:
                problems.append(
                    HealthFinding(
                        stream="RECORDER",
                        classification="RECORDER_ERROR",
                        start_utc=_iso(wall_ns),
                        end_utc=_iso(wall_ns),
                        duration_seconds=0.0,
                        evidence=[error],
                    )
                )

            market_data_type = (
                "LIVE"
                if staging.market_data_types == {"LIVE"}
                else "UNKNOWN"
                if not staging.market_data_types
                else "MIXED:" + ",".join(sorted(staging.market_data_types))
            )
            if market_data_type != "LIVE":
                problems.append(
                    HealthFinding(
                        stream="MARKET_DATA",
                        classification="MARKET_DATA_TYPE",
                        start_utc=session_open.astimezone(timezone.utc).isoformat(),
                        end_utc=session_close.astimezone(timezone.utc).isoformat(),
                        duration_seconds=(session_close - session_open).total_seconds(),
                        evidence=[f"observed market data type is {market_data_type}, not LIVE"],
                    )
                )

            if integrity:
                for item in integrity:
                    if item.get("read_error") or item.get("salvaged"):
                        problems.append(
                            HealthFinding(
                                stream="RAW",
                                classification="RAW_SEGMENT_INCOMPLETE",
                                start_utc=session_open.astimezone(timezone.utc).isoformat(),
                                end_utc=session_close.astimezone(timezone.utc).isoformat(),
                                duration_seconds=(session_close - session_open).total_seconds(),
                                evidence=[json.dumps(item, sort_keys=True)],
                            )
                        )

            auxiliary = [
                {
                    "timestamp_utc": _iso(status.wall_ns),
                    "code": status.code,
                    "message": status.message,
                }
                for status in staging.auxiliary_farm_statuses
            ]
            analyzer_path = Path(__file__)
            return {
                "schema_version": SCHEMA_VERSION,
                "semantics": "FULL_RTH_HEALTH_V4",
                "session_open": session_open.astimezone(timezone.utc).isoformat(),
                "session_close": session_close.astimezone(timezone.utc).isoformat(),
                "events": staging.events,
                "market_data_type": market_data_type,
                "health_ok": not problems,
                "problems": [asdict(item) for item in problems],
                "advisories": [asdict(item) for item in advisories],
                "stream_metrics": {
                    name: asdict(metric) for name, metric in sorted(metrics.items())
                },
                "auxiliary_farm_statuses": auxiliary,
                "analyzer_sha256": _sha256(analyzer_path),
                "input": input_metadata or {},
            }
        finally:
            staging.close()


def reanalyze_raw_v4(
    raw_dir: Path,
    *,
    session_open: datetime,
    session_close: datetime,
    expected_raw_hashes: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_dir = raw_dir.resolve(strict=True)
    segments, state_before = _capture_raw_state(raw_dir)
    file_hashes = {name: state.sha256 for name, state in state_before.items()}
    if expected_raw_hashes is not None and file_hashes != expected_raw_hashes:
        manifest_only = sorted(set(expected_raw_hashes) - set(file_hashes))
        raw_only = sorted(set(file_hashes) - set(expected_raw_hashes))
        hash_mismatch = sorted(
            name
            for name in set(file_hashes) & set(expected_raw_hashes)
            if file_hashes[name] != expected_raw_hashes[name]
        )
        raise ValueError(
            "raw segment inventory/hash does not match original v3 manifest; "
            f"manifest_only={manifest_only}; raw_only={raw_only}; "
            f"hash_mismatch={hash_mismatch}"
        )
    integrity: list[dict[str, Any]] = []
    input_metadata = {
        "raw_dir": str(raw_dir),
        "raw_segment_count": len(segments),
        "raw_file_sha256": file_hashes,
        "raw_input_sha256": _combined_input_digest(file_hashes),
        "original_manifest_inventory_match": expected_raw_hashes is not None,
    }
    health = analyze_rows_v4(
        iter_raw_rows(raw_dir, segments=segments, integrity=integrity),
        session_open=session_open,
        session_close=session_close,
        input_metadata=input_metadata,
        integrity=integrity,
        work_parent=raw_dir,
    )
    _, state_after = _capture_raw_state(raw_dir)
    if state_before != state_after:
        raise ValueError(
            "raw evidence changed between attestation and semantic analysis; "
            + _describe_state_change(state_before, state_after)
        )
    input_metadata["verification"] = {
        "segment_inventory_stable": True,
        "pre_and_post_sha256_match": True,
        "compressed_sha256_scan_passes": 2,
        "semantic_decode_passes": 1,
    }
    health["input"] = input_metadata
    return health, {"integrity": integrity, **input_metadata}


def _create_durable(path: Path, payload: bytes) -> None:
    # Windows defaults ``os.open`` descriptors to text mode.  ``os.write`` then
    # expands LF to CRLF, so a digest computed over ``payload`` no longer binds
    # the bytes actually persisted.  O_BINARY is zero/absent on POSIX and
    # prevents that translation on Windows.
    binary_flag = getattr(os, "O_BINARY", 0)
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | binary_flag,
        0o644,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(f"short write while publishing {path}")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    if os.name != "nt":
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def write_reanalysis_v4(
    raw_dir: Path,
    output_dir: Path,
    *,
    session_open: datetime,
    session_close: datetime,
    original_health: Path,
    original_manifest: Path,
) -> tuple[Path, Path]:
    """Write v4 sidecars once; amendment is the completion marker.

    Existing targets are never replaced. A crash after health publication but
    before amendment publication leaves a visibly incomplete pair and the next
    invocation refuses to overwrite it, preserving fail-closed provenance.
    """
    output_dir = output_dir.resolve(strict=False)
    output_dir.mkdir(parents=True, exist_ok=True)
    health_path = output_dir / "health-v4.json"
    amendment_path = output_dir / "manifest-amendment-v4.json"
    if health_path.exists() or amendment_path.exists():
        raise FileExistsError(
            "v4 reanalysis target already exists (or a prior attempt is incomplete); "
            "refusing to overwrite evidence"
        )

    original_health_resolved = original_health.resolve(strict=True)
    original_manifest_resolved = original_manifest.resolve(strict=True)
    original_health_bytes = original_health_resolved.read_bytes()
    original_manifest_bytes = original_manifest_resolved.read_bytes()
    try:
        original_health_payload = json.loads(original_health_bytes)
        original_manifest_payload = json.loads(original_manifest_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError(f"original v3 health/manifest is not valid JSON: {exc}") from exc
    if not isinstance(original_health_payload, dict) or not isinstance(
        original_manifest_payload, dict
    ):
        raise ValueError("original v3 health and manifest must be JSON objects")
    expected_raw_hashes = _manifest_raw_hashes(original_manifest_payload)

    health, input_metadata = reanalyze_raw_v4(
        raw_dir,
        session_open=session_open,
        session_close=session_close,
        expected_raw_hashes=expected_raw_hashes,
    )
    health_bytes = (json.dumps(health, indent=2, sort_keys=True) + "\n").encode("utf-8")

    originals: dict[str, Any] = {
        "health_v3": {
            "path": str(original_health_resolved),
            "sha256": hashlib.sha256(original_health_bytes).hexdigest(),
        },
        "manifest_v3": {
            "path": str(original_manifest_resolved),
            "sha256": hashlib.sha256(original_manifest_bytes).hexdigest(),
        },
    }

    amendment = {
        "schema_version": SCHEMA_VERSION,
        "amendment_type": "IMMUTABLE_REANALYSIS",
        "original_gate_unchanged": True,
        "originals": originals,
        "input": input_metadata,
        "analyzer_sha256": health["analyzer_sha256"],
        "health_v4_file": health_path.name,
        "health_v4_sha256": hashlib.sha256(health_bytes).hexdigest(),
        "health_ok_v4": health["health_ok"],
        "statement": (
            "This is a v4 semantic reanalysis of immutable raw evidence. "
            "It does not overwrite, upgrade, or retroactively change any v3 Gate verdict."
        ),
    }
    amendment_bytes = (json.dumps(amendment, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )

    _create_durable(health_path, health_bytes)
    _create_durable(amendment_path, amendment_bytes)
    return health_path, amendment_path
