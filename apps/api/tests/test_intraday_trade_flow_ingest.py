import asyncio
from datetime import UTC, date, datetime

import pytest

from app.services.intraday_trade_flow_ingest import (
    FEED_SOURCES,
    MAX_SESSIONS_PER_RUN,
    ingest_trade_flow,
    session_window,
)


class FakeConn:
    def __init__(self, candidates, completed):
        self.candidates = candidates
        self.completed = completed

    def execute(self, sql, params=None):
        if "intraday_trade_ingest_checkpoints" in sql:
            return FakeResult(
                [
                    {"symbol": symbol, "session_date": session}
                    for symbol, session in self.completed
                ]
            )
        return FakeResult(
            [
                {"symbol": symbol, "session_date": session}
                for symbol, session in self.candidates
            ]
        )

    def commit(self):
        pass


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


def test_the_window_stops_at_the_regular_close():
    start, end = session_window(date(2025, 3, 3))

    assert start == datetime(2025, 3, 3, 14, 30, tzinfo=UTC)
    assert end == datetime(2025, 3, 3, 21, 0, tzinfo=UTC)


def test_the_window_follows_daylight_saving_rather_than_a_fixed_offset():
    winter_start, _ = session_window(date(2025, 1, 15))
    summer_start, _ = session_window(date(2025, 7, 15))

    assert winter_start.hour == 14
    assert summer_start.hour == 13


def test_both_feeds_map_to_distinct_sources():
    assert FEED_SOURCES["sip"] != FEED_SOURCES["iex"]


def test_an_oversized_window_is_refused_rather_than_started():
    candidates = [("AAPL", date(2025, 3, day)) for day in range(1, 29)] + [
        ("MSFT", date(2025, 3, day)) for day in range(1, 29)
    ]
    conn = FakeConn(candidates, completed=[])

    with pytest.raises(ValueError, match="per-run ceiling"):
        asyncio.run(
            ingest_trade_flow(
                conn,
                symbols=["AAPL", "MSFT"],
                start=date(2025, 3, 1),
                end=date(2025, 3, 28),
                max_sessions=MAX_SESSIONS_PER_RUN,
            )
        )


def test_already_ingested_sessions_are_not_refetched():
    candidates = [("AAPL", date(2025, 3, 3)), ("AAPL", date(2025, 3, 4))]
    conn = FakeConn(candidates, completed=candidates)

    report = asyncio.run(
        ingest_trade_flow(
            conn, symbols=["AAPL"], start=date(2025, 3, 3), end=date(2025, 3, 4)
        )
    )

    assert report["symbol_sessions_available"] == 2
    assert report["symbol_sessions_already_ingested"] == 2
    assert report["symbol_sessions_attempted"] == 0
