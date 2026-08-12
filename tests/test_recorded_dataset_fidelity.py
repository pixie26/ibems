"""
Has anyone ever *read* the data of record?

    ####################################################################
    #  Row counts and a matching schema prove nothing about values.    #
    ####################################################################

Until now the Parquet was verified two ways: ``parquet_rows_verified ==
rows``, and ``schema == declared schema``. Both would pass on a file whose
prices were rounded, whose timestamps had lost their timezone, or whose
event order had been shuffled across segments. The tick counts reconciled
end to end -- handled, selected, enqueued, persisted, readback -- but a
count is preserved by any transformation that keeps one row per row.

These tests take the other side: they push realistic ticks through the
real write path and assert the bytes that come back out of Parquet are the
same *values*, not the same number of values. Nothing here needs a
Gateway, because the question is about the pipeline, not about IB.

The realism that matters is chosen deliberately:

``600.03`` and friends
    Cent-level equity prices. The classic silent corruption is a float
    round-trip that turns them into ``600.0299999999999994`` -- still a
    valid float64, still one row, still schema-clean, and the source of a
    spread study that is quietly wrong in the third decimal.

nanosecond wall clocks
    ``local_wall_ns`` is around 1.8e18 today, which is comfortably inside
    int64 and comfortably *outside* the 2^53 range a float64 can represent
    exactly. Any accidental trip through a float loses the last hundreds
    of nanoseconds. Arrival-time studies are exactly what this recorder
    exists to support.

``special_conditions``
    Written as ``bidPastLow=...;askPastHigh=...``, and only useful if a
    reader can take it apart again months later.

cross-segment ordering
    A session is many rolled segments folded into one file. The fold must
    preserve ``event_id`` order, or every sequence-dependent study built on
    the day is wrong in a way no count can reveal.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from ib_execution.quote_recorder import (
    RawEventLog,
    RawTick,
    finalize_day,
    parquet_schema,
)

pytest.importorskip("pyarrow")

SESSION = date(2026, 8, 11)
OPEN = datetime(2026, 8, 11, 13, 30, tzinfo=timezone.utc)
CLOSE = OPEN + timedelta(hours=6, minutes=30)

# Chosen so a float64 round trip is detectable: none of these is exactly
# representable, and their spreads are one cent.
PRICES = [600.03, 600.07, 599.99, 601.11, 123.45]


def _bidask(event_id: int, run_id: str, *, price: float, wall_ns: int) -> RawTick:
    return RawTick(
        event_id=event_id,
        recorder_run_id=run_id,
        connection_epoch=1,
        contract_id=756733,
        event_type="BID_ASK",
        broker_timestamp=datetime(
            2026, 8, 11, 13, 30, 0, 123456, tzinfo=timezone.utc
        ).isoformat(),
        local_wall_ns=wall_ns,
        local_monotonic_ns=wall_ns - 1_000,
        market_data_type="LIVE",
        receive_sequence=event_id,
        bid=price,
        ask=round(price + 0.01, 2),
        bid_size=300.0,
        ask_size=1200.0,
        special_conditions="bidPastLow=False;askPastHigh=True",
    )


def _write_session(tmp_path, ticks_per_segment=3, segments=3):
    """Write across several rolled segments, as a real session does."""
    log = RawEventLog(tmp_path, session=SESSION, roll_seconds=1)
    written = []
    event_id = 0
    base_ns = 1_818_000_000_000_000_000
    for segment in range(segments):
        for index in range(ticks_per_segment):
            event_id += 1
            tick = _bidask(
                event_id,
                log.run_id,
                price=PRICES[(event_id - 1) % len(PRICES)],
                wall_ns=base_ns + event_id * 137,
            )
            written.append(tick)
            # now_mono crosses the roll boundary between segments.
            log.append(tick, now_mono=float(segment * 10 + index))
    log.close()
    return written


def _parquet_rows(tmp_path):
    import pyarrow.parquet as pq

    log = RawEventLog(tmp_path, session=SESSION)
    try:
        manifest = finalize_day(
            log, session_open=OPEN, session_close=CLOSE, clock_skew_samples=[0.0]
        )
    finally:
        log.close()
    table = pq.read_table(log.dir / "events.parquet")
    return manifest, table, table.to_pylist()


def test_every_recorded_value_survives_the_round_trip(tmp_path):
    """Value-for-value, not row-count-for-row-count."""
    written = _write_session(tmp_path)
    _manifest, _table, rows = _parquet_rows(tmp_path)

    by_id = {row["event_id"]: row for row in rows}
    assert len(by_id) == len(written)
    for tick in written:
        row = by_id[tick.event_id]
        for name, expected in vars(tick).items():
            assert row[name] == expected, f"{name} changed: {row[name]!r} != {expected!r}"


def test_cent_prices_are_not_quietly_rounded(tmp_path):
    """600.03 must come back as 600.03, and the spread must stay one cent."""
    _write_session(tmp_path)
    _manifest, _table, rows = _parquet_rows(tmp_path)

    for row in rows:
        assert row["bid"] in PRICES, row["bid"]
        assert round(row["ask"] - row["bid"], 10) == 0.01
        # repr equality catches a float64 that drifted in the last bits.
        assert repr(row["bid"]) == repr(PRICES[(row["event_id"] - 1) % len(PRICES)])


def test_nanosecond_clocks_keep_every_digit(tmp_path):
    """1.8e18 exceeds 2^53: any float detour silently truncates it."""
    written = _write_session(tmp_path)
    _manifest, table, rows = _parquet_rows(tmp_path)

    assert table.schema.field("local_wall_ns").type == parquet_schema().field(
        "local_wall_ns"
    ).type
    expected = {tick.event_id: tick.local_wall_ns for tick in written}
    for row in rows:
        assert row["local_wall_ns"] == expected[row["event_id"]]
        assert isinstance(row["local_wall_ns"], int)
        assert row["local_wall_ns"] > 2**53


def test_broker_timestamps_keep_their_offset_and_microseconds(tmp_path):
    """Stored as text on purpose: a naive datetime is an unanswerable question."""
    _write_session(tmp_path)
    _manifest, _table, rows = _parquet_rows(tmp_path)

    for row in rows:
        parsed = datetime.fromisoformat(row["broker_timestamp"])
        assert parsed.tzinfo is not None, "a timestamp without an offset is not a time"
        assert parsed.utcoffset() == timedelta(0)
        assert parsed.microsecond == 123456


def test_special_conditions_can_still_be_taken_apart(tmp_path):
    """It is only worth recording if a reader can decode it later."""
    _write_session(tmp_path)
    _manifest, _table, rows = _parquet_rows(tmp_path)

    for row in rows:
        decoded = dict(
            part.split("=", 1) for part in row["special_conditions"].split(";") if part
        )
        assert decoded == {"bidPastLow": "False", "askPastHigh": "True"}


def test_event_order_survives_folding_many_segments_into_one_file(tmp_path):
    """The fold must not shuffle the day."""
    written = _write_session(tmp_path, ticks_per_segment=4, segments=4)
    log_dir = tmp_path / SESSION.isoformat()
    assert len(list(log_dir.glob("segment-*.jsonl.gz"))) > 1, "test needs several segments"

    _manifest, _table, rows = _parquet_rows(tmp_path)

    assert [row["event_id"] for row in rows] == [tick.event_id for tick in written]
    assert [row["receive_sequence"] for row in rows] == sorted(
        row["receive_sequence"] for row in rows
    )


def test_absent_fields_stay_absent_rather_than_becoming_zero(tmp_path):
    """A BidAsk row has no `last`. Null and 0.0 are different claims."""
    _write_session(tmp_path)
    _manifest, _table, rows = _parquet_rows(tmp_path)

    for row in rows:
        assert row["last"] is None
        assert row["volume"] is None
        assert row["trade_count"] is None
