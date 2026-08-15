"""Immutable Full-RTH health schema v4 and offline reanalysis.

v3 artifacts are historical evidence and are never rewritten. This module
re-reads immutable raw Recorder segments and emits a sidecar health verdict
whose semantics match the live liveness contract:

* BAR_5S is time-driven and may create a hard problem.
* BID_ASK and ALL_LAST are event-driven; silence is quality evidence only.
* 1100 is a direct FEED_OUTAGE.
* 2103/2104 describe the real-time market-data farm. A 2103 interval becomes
  a FEED_OUTAGE only when BAR_5S also stops.
* 2105/2106 (historical) and 2157/2158 (security definition) are advisory
  status only and can neither create nor hide a SPY real-time outage.

The implementation is disk-backed and single-pass over gzip JSONL. It never
materializes a Full-RTH day in Python memory.
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
AUXILIARY_FARM_CODES = frozenset({2105, 2106, 2157, 2158, 2108})
FATAL_MARKET_DATA_CODES = frozenset({354, 10089, 10189, 10197})
REALTIME_BARS_RESET = 10225

_ERROR_RE = re.compile(r"^IB_ERROR:(?P<code>-?\d+):(?P<req>[^:]*):(?P<message>.*)$")


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


def _raw_segments(raw_dir: Path) -> list[Path]:
    segments = sorted(raw_dir.glob("segment-*.jsonl.gz"))
    segments.extend(sorted(raw_dir.glob("crashed-*.jsonl.gz")))
    return sorted(set(segments), key=lambda path: path.name)


def iter_raw_rows(
    raw_dir: Path,
    *,
    integrity: list[dict[str, Any]] | None = None,
) -> Iterator[dict[str, Any]]:
    """Read each immutable raw segment exactly once, preserving salvage facts."""
    for segment in _raw_segments(raw_dir):
        rows = 0
        error: str | None = None
        try:
            with gzip.open(segment, "rb") as handle:
                for line in handle:
                    rows += 1
                    yield json.loads(line)
        except (EOFError, OSError, gzip.BadGzipFile, json.JSONDecodeError) as exc:
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


def _batched(rows: Iterable[dict[str, Any]], size: int = 50_000) -> Iterator[list[dict[str, Any]]]:
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
    return rows, (_iso(first) if first is not None else None), (_iso(last) if last is not None else None), max_gap, gaps


def _status_state_at(statuses: Sequence[StatusEvent], target_ns: int) -> tuple[bool, bool]:
    connectivity_lost = False
    realtime_farm_broken = False
    for status in statuses:
        if status.wall_ns > target_ns:
            break
        if status.code == CONNECTIVITY_LOST:
            connectivity_lost = True
        elif status.code in (CONNECTIVITY_RESTORED_DATA_LOST, CONNECTIVITY_RESTORED_DATA_KEPT):
            connectivity_lost = False
        elif status.code == REALTIME_FARM_BROKEN:
            realtime_farm_broken = True
        elif status.code == REALTIME_FARM_OK:
            realtime_farm_broken = False
    return connectivity_lost, realtime_farm_broken


def _statuses_in(
    statuses: Sequence[StatusEvent], start_ns: int, end_ns: int
) -> list[StatusEvent]:
    return [status for status in statuses if start_ns <= status.wall_ns <= end_ns]


def _gap_classification(
    statuses: Sequence[StatusEvent], start_ns: int, end_ns: int
) -> tuple[str, list[str]]:
    connectivity_lost, realtime_farm_broken = _status_state_at(statuses, start_ns)
    evidence = [
        f"BAR_5S gap {_duration_seconds(start_ns, end_ns):.3f}s > {BAR_GAP_SECONDS:.0f}s"
    ]
    for status in _statuses_in(statuses, start_ns, end_ns):
        evidence.append(f"IB {status.code}: {status.message}")
        if status.code == CONNECTIVITY_LOST:
            connectivity_lost = True
        elif status.code in (CONNECTIVITY_RESTORED_DATA_LOST, CONNECTIVITY_RESTORED_DATA_KEPT):
            connectivity_lost = False
        elif status.code == REALTIME_FARM_BROKEN:
            realtime_farm_broken = True
        elif status.code == REALTIME_FARM_OK:
            realtime_farm_broken = False
    if connectivity_lost:
        return "FEED_OUTAGE", evidence
    # A 2103 observed anywhere in the gap is corroborated by the missing BAR.
    farm_seen = realtime_farm_broken or any(
        status.code == REALTIME_FARM_BROKEN
        for status in _statuses_in(statuses, start_ns, end_ns)
    )
    if farm_seen:
        return "FEED_OUTAGE", evidence
    return "GAP_SUSPECTED", evidence


def _connectivity_outages(
    statuses: Sequence[StatusEvent], session_open_ns: int, session_close_ns: int
) -> list[HealthFinding]:
    findings: list[HealthFinding] = []
    open_status: StatusEvent | None = None
    for status in statuses:
        if status.wall_ns < session_open_ns:
            continue
        if status.wall_ns > session_close_ns:
            break
        if status.code == CONNECTIVITY_LOST and open_status is None:
            open_status = status
        elif (
            status.code in (CONNECTIVITY_RESTORED_DATA_LOST, CONNECTIVITY_RESTORED_DATA_KEPT)
            and open_status is not None
        ):
            findings.append(
                HealthFinding(
                    stream=BAR_STREAM,
                    classification="FEED_OUTAGE",
                    start_utc=_iso(open_status.wall_ns),
                    end_utc=_iso(status.wall_ns),
                    duration_seconds=_duration_seconds(open_status.wall_ns, status.wall_ns),
                    evidence=[
                        f"IB {open_status.code}: {open_status.message}",
                        f"IB {status.code}: {status.message}",
                    ],
                )
            )
            open_status = None
    if open_status is not None:
        findings.append(
            HealthFinding(
                stream=BAR_STREAM,
                classification="FEED_OUTAGE",
                start_utc=_iso(open_status.wall_ns),
                end_utc=_iso(session_close_ns),
                duration_seconds=_duration_seconds(open_status.wall_ns, session_close_ns),
                evidence=[f"IB {open_status.code}: {open_status.message}", "no restoration before session close"],
            )
        )
    return findings


def _overlaps(finding: HealthFinding, start_ns: int, end_ns: int) -> bool:
    finding_start = int(datetime.fromisoformat(finding.start_utc).timestamp() * 1e9)
    finding_end = int(datetime.fromisoformat(finding.end_utc).timestamp() * 1e9)
    return finding_start <= end_ns and finding_end >= start_ns


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
            advisories: list[HealthFinding] = []
            metrics: dict[str, StreamMetrics] = {}

            direct_outages = _connectivity_outages(statuses, session_open_ns, session_close_ns)
            problems.extend(direct_outages)

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
                        continue

                    if any(_overlaps(finding, start_ns, end_ns) for finding in direct_outages):
                        # 1100 already created the direct hard finding. Keep the
                        # BAR gap in metrics without double-counting one outage.
                        continue
                    classification, evidence = _gap_classification(statuses, start_ns, end_ns)
                    problems.append(
                        HealthFinding(
                            stream=BAR_STREAM,
                            classification=classification,
                            start_utc=_iso(start_ns),
                            end_utc=_iso(end_ns),
                            duration_seconds=duration,
                            evidence=evidence,
                        )
                    )

            for status in statuses:
                if status.code in FATAL_MARKET_DATA_CODES:
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
            result = {
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
            return result
        finally:
            staging.close()


def reanalyze_raw_v4(
    raw_dir: Path,
    *,
    session_open: datetime,
    session_close: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_dir = raw_dir.resolve(strict=True)
    segments = _raw_segments(raw_dir)
    if not segments:
        raise FileNotFoundError(f"no raw Recorder segments found under {raw_dir}")
    file_hashes = {path.name: _sha256(path) for path in segments}
    integrity: list[dict[str, Any]] = []
    input_metadata = {
        "raw_dir": str(raw_dir),
        "raw_segment_count": len(segments),
        "raw_file_sha256": file_hashes,
        "raw_input_sha256": _combined_input_digest(file_hashes),
    }
    health = analyze_rows_v4(
        iter_raw_rows(raw_dir, integrity=integrity),
        session_open=session_open,
        session_close=session_close,
        input_metadata=input_metadata,
        integrity=integrity,
        work_parent=raw_dir,
    )
    return health, {"integrity": integrity, **input_metadata}


def write_reanalysis_v4(
    raw_dir: Path,
    output_dir: Path,
    *,
    session_open: datetime,
    session_close: datetime,
    original_health: Path | None = None,
    original_manifest: Path | None = None,
) -> tuple[Path, Path]:
    """Write v4 sidecars once. Existing artifacts are never overwritten."""
    output_dir = output_dir.resolve(strict=False)
    output_dir.mkdir(parents=True, exist_ok=True)
    health_path = output_dir / "health-v4.json"
    amendment_path = output_dir / "manifest-amendment-v4.json"
    if health_path.exists() or amendment_path.exists():
        raise FileExistsError("v4 reanalysis target already exists; refusing to overwrite evidence")

    health, input_metadata = reanalyze_raw_v4(
        raw_dir,
        session_open=session_open,
        session_close=session_close,
    )
    health_bytes = (json.dumps(health, indent=2, sort_keys=True) + "\n").encode("utf-8")

    originals: dict[str, Any] = {}
    for name, path in (("health_v3", original_health), ("manifest_v3", original_manifest)):
        if path is None:
            continue
        resolved = path.resolve(strict=True)
        originals[name] = {"path": str(resolved), "sha256": _sha256(resolved)}

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
    amendment_bytes = (json.dumps(amendment, indent=2, sort_keys=True) + "\n").encode("utf-8")

    # Create-only semantics. O_EXCL prevents a race from replacing evidence.
    for path, payload in ((health_path, health_bytes), (amendment_path, amendment_bytes)):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
    return health_path, amendment_path
