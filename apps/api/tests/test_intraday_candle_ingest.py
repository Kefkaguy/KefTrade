from datetime import UTC, date, datetime

import pytest

from app.services.intraday_candle_ingest import (
    CANDLE_INGEST_VERSION,
    ChunkResult,
    completed_chunks,
    feed_source,
    month_chunks,
    record_checkpoint,
    reconcile_sessions,
)


class Result:
    def __init__(self, rows=None, row=None):
        self.rows = rows or []
        self.row = row

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.row


class FakeConn:
    def __init__(self, *, completed=(), stored_sessions=()):
        self.completed = list(completed)
        self.stored_sessions = list(stored_sessions)
        self.written: list[tuple] = []
        self.committed = 0

    def execute(self, query, params=None):
        text = " ".join(query.split())
        if text.startswith("SELECT chunk_start"):
            return Result(rows=[{"chunk_start": item} for item in self.completed])
        if text.startswith("INSERT INTO intraday_candle_ingest_checkpoints"):
            self.written.append(params)
            return Result()
        if "SELECT DISTINCT (timestamp AT TIME ZONE" in text:
            return Result(rows=[{"session_date": item} for item in self.stored_sessions])
        return Result()

    def commit(self):
        self.committed += 1


def test_feed_source_never_blends_two_feeds():
    assert feed_source("sip") == "alpaca_sip"
    assert feed_source("iex") == "alpaca_iex"
    assert feed_source("sip") != feed_source("iex")
    with pytest.raises(ValueError):
        feed_source("nasdaq_basic")


def test_month_chunks_cover_the_range_without_gaps_or_overlap():
    chunks = month_chunks(date(2024, 1, 15), date(2024, 4, 10))

    assert chunks[0] == (date(2024, 1, 15), date(2024, 1, 31))
    assert chunks[-1] == (date(2024, 4, 1), date(2024, 4, 10))
    assert len(chunks) == 4
    for (_, previous_end), (next_start, _) in zip(chunks, chunks[1:]):
        assert (next_start - previous_end).days == 1


def test_month_chunks_handle_a_year_boundary():
    chunks = month_chunks(date(2023, 11, 20), date(2024, 2, 5))

    assert [item[0] for item in chunks] == [
        date(2023, 11, 20),
        date(2023, 12, 1),
        date(2024, 1, 1),
        date(2024, 2, 1),
    ]


def test_month_chunks_reject_a_reversed_range():
    with pytest.raises(ValueError):
        month_chunks(date(2024, 5, 1), date(2024, 1, 1))


def test_a_single_day_range_is_one_chunk():
    assert month_chunks(date(2024, 3, 7), date(2024, 3, 7)) == [
        (date(2024, 3, 7), date(2024, 3, 7))
    ]


def test_resume_skips_only_chunks_already_completed():
    conn = FakeConn(completed=[date(2024, 1, 1), date(2024, 2, 1)])

    done = completed_chunks(conn, symbol="AAPL", timeframe="30m", feed="sip")

    assert done == {date(2024, 1, 1), date(2024, 2, 1)}
    assert date(2024, 3, 1) not in done


def test_checkpoints_record_failures_so_a_resume_retries_them():
    conn = FakeConn()

    record_checkpoint(
        conn,
        ChunkResult(
            symbol="AAPL",
            timeframe="30m",
            feed="sip",
            chunk_start=date(2024, 1, 1),
            chunk_end=date(2024, 1, 31),
            status="failed",
            error="HTTPError: boom",
        ),
    )

    assert conn.written[0][5] == "failed"
    assert conn.written[0][-2] == "HTTPError: boom"
    assert conn.written[0][-1] == CANDLE_INGEST_VERSION
    assert conn.committed == 1


def test_reconciliation_reports_sessions_the_calendar_expected():
    # 2024-07-01..05 are all trading days; 4 July is a market holiday.
    conn = FakeConn(
        stored_sessions=[date(2024, 7, 1), date(2024, 7, 2), date(2024, 7, 5)]
    )

    report = reconcile_sessions(
        conn,
        symbol="AAPL",
        timeframe="30m",
        source="alpaca_sip",
        start=date(2024, 7, 1),
        end=date(2024, 7, 5),
    )

    assert report["expected_sessions"] == 4
    assert report["received_sessions"] == 3
    assert report["missing_sessions"] == 1
    assert report["missing_session_dates"] == ["2024-07-03"]
    assert report["coverage"] == 0.75


def test_reconciliation_flags_bars_outside_the_exchange_calendar():
    conn = FakeConn(
        stored_sessions=[date(2024, 7, 1), date(2024, 7, 2), date(2024, 7, 4)]
    )

    report = reconcile_sessions(
        conn,
        symbol="AAPL",
        timeframe="30m",
        source="alpaca_sip",
        start=date(2024, 7, 1),
        end=date(2024, 7, 5),
    )

    assert "2024-07-04" in report["sessions_outside_calendar"]
