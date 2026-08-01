"""Bounded, checkpointed ingestion of signed trade flow.

Trade data is not candle data at a finer resolution; it is three or four
orders of magnitude larger.  A single liquid symbol prints more rows in one
session than the entire 237-symbol candle dataset holds for a month.  So this
never runs "the whole history": it runs a declared window, resumes from a
per-symbol-session checkpoint, and folds each page away instead of holding it.

The module contains no campaign, broker, order-submission, or UI code.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import psycopg

from app.providers.alpaca import (
    fetch_stock_quotes,
    iter_stock_trade_pages,
    normalize_stock_quote,
    normalize_stock_trade,
)
from app.services.intraday_trade_flow import (
    TRADE_FLOW_VERSION,
    TradeFlowAccumulator,
    classifier_agreement_report,
    completed_sessions,
    persist_trade_flow_features,
    record_checkpoint,
)

EXCHANGE = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)

FEED_SOURCES = {"sip": "alpaca_sip", "iex": "alpaca_iex"}

# A window wider than this is refused rather than started: trade ingestion at
# universe scale is a multi-day job and should be declared, not stumbled into.
MAX_SESSIONS_PER_RUN = 40


def session_window(session: date) -> tuple[datetime, datetime]:
    """UTC bounds of one regular session.

    Early closes simply return fewer trades; the window is never wider than
    the regular session, so after-hours prints cannot enter the aggregate.
    """
    start = datetime.combine(session, REGULAR_OPEN, tzinfo=EXCHANGE)
    end = datetime.combine(session, REGULAR_CLOSE, tzinfo=EXCHANGE)
    return start.astimezone(UTC), end.astimezone(UTC)


def sessions_with_candles(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    start: date,
    end: date,
    timeframe: str,
    source: str,
) -> list[tuple[str, date]]:
    """Symbol-sessions the candle dataset already covers.

    Trade flow is only fetched where there is a bar to align it to; a flow
    feature with no candle beside it can never enter a factor.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT symbol,
               (timestamp AT TIME ZONE 'America/New_York')::date AS session_date
        FROM candles
        WHERE symbol = ANY(%s)
          AND timeframe = %s
          AND source = %s
          AND (timestamp AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s
          AND (timestamp AT TIME ZONE 'America/New_York')::time >= TIME '09:30'
          AND (timestamp AT TIME ZONE 'America/New_York')::time < TIME '16:00'
        ORDER BY session_date, symbol
        """,
        ([item.upper() for item in symbols], timeframe, source, start, end),
    ).fetchall()
    return [(str(row["symbol"]), row["session_date"]) for row in rows]


async def ingest_symbol_session(
    conn: psycopg.Connection,
    *,
    symbol: str,
    session: date,
    timeframe: str,
    feed: str,
) -> dict[str, Any]:
    """Fetch, sign and aggregate one symbol-session, page by page."""
    start, end = session_window(session)
    accumulator = TradeFlowAccumulator(symbol=symbol, timeframe=timeframe, feed=feed)
    fetched = 0
    pages = 0
    exhausted = False
    record_checkpoint(
        conn, symbol=symbol, session_date=session, feed=feed, status="running"
    )
    try:
        async for page, meta in iter_stock_trade_pages(
            symbol, start=start, end=end, feed=feed
        ):
            normalized = [
                row
                for row in (normalize_stock_trade(symbol, item, feed=feed) for item in page)
                if row is not None
            ]
            accumulator.add(normalized)
            fetched += len(normalized)
            pages += 1
            exhausted = bool(meta["exhausted"])
        bars = accumulator.bars()
        written = persist_trade_flow_features(conn, bars)
    except Exception as error:  # noqa: BLE001 - failure is recorded, not swallowed
        record_checkpoint(
            conn,
            symbol=symbol,
            session_date=session,
            feed=feed,
            status="failed",
            trades_fetched=fetched,
            pages=pages,
            error=str(error)[:500],
        )
        return {
            "symbol": symbol,
            "session_date": session,
            "status": "failed",
            "error": str(error)[:500],
            "trades_fetched": fetched,
            "pages": pages,
        }

    # A page ceiling hit mid-session means the day is partial. Recording it as
    # completed would let a truncated afternoon look like a real one.
    status = "completed" if exhausted else "failed"
    record_checkpoint(
        conn,
        symbol=symbol,
        session_date=session,
        feed=feed,
        status=status,
        trades_fetched=fetched,
        bars_written=written,
        pages=pages,
        error=None if exhausted else "page_limit_reached_before_session_end",
    )
    return {
        "symbol": symbol,
        "session_date": session,
        "status": status,
        "trades_fetched": fetched,
        "bars_written": written,
        "bars": len(bars),
        "pages": pages,
    }


async def ingest_trade_flow(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    start: date,
    end: date,
    timeframe: str = "30m",
    feed: str = "sip",
    max_sessions: int = MAX_SESSIONS_PER_RUN,
) -> dict[str, Any]:
    """Ingest a bounded window, resuming from checkpoints."""
    source = FEED_SOURCES[feed]
    candidates = sessions_with_candles(
        conn, symbols=symbols, start=start, end=end, timeframe=timeframe, source=source
    )
    already = completed_sessions(conn, feed=feed)
    pending = [item for item in candidates if item not in already]
    if len(pending) > max_sessions:
        raise ValueError(
            f"{len(pending)} symbol-sessions pending exceeds the {max_sessions} "
            "per-run ceiling; narrow the window or raise it deliberately."
        )

    results: list[dict[str, Any]] = []
    for symbol, session in pending:
        results.append(
            await ingest_symbol_session(
                conn, symbol=symbol, session=session, timeframe=timeframe, feed=feed
            )
        )
    completed = [row for row in results if row["status"] == "completed"]
    return {
        "trade_flow_version": TRADE_FLOW_VERSION,
        "feed": feed,
        "source": source,
        "timeframe": timeframe,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "symbol_sessions_available": len(candidates),
        "symbol_sessions_already_ingested": len(candidates) - len(pending),
        "symbol_sessions_attempted": len(results),
        "symbol_sessions_completed": len(completed),
        "trades_fetched": sum(row.get("trades_fetched", 0) for row in results),
        "bars_written": sum(row.get("bars_written", 0) for row in completed),
        "failures": [row for row in results if row["status"] == "failed"][:20],
    }


async def measure_classifier_agreement(
    *,
    symbol: str,
    session: date,
    feed: str = "sip",
    window_minutes: int = 30,
    quote_limit: int = 200_000,
) -> dict[str, Any]:
    """Compare the tick rule against Lee-Ready on one bounded window.

    Quotes arrive at roughly ten times the volume of trades, so this is
    deliberately a subsample rather than a whole session.  Its output is what
    licenses -- or refuses -- the cheap classifier used for bulk ingestion.
    """
    start, _ = session_window(session)
    end = start + timedelta(minutes=window_minutes)

    trades: list[dict[str, Any]] = []
    async for page, _meta in iter_stock_trade_pages(
        symbol, start=start, end=end, feed=feed
    ):
        trades.extend(
            row
            for row in (normalize_stock_trade(symbol, item, feed=feed) for item in page)
            if row is not None
        )

    _status, raw_quotes, _log, _request_id = await fetch_stock_quotes(
        symbol, start=start, end=end, limit=quote_limit, feed=feed
    )
    quotes = [
        row
        for row in (normalize_stock_quote(symbol, item, feed=feed) for item in raw_quotes)
        if row is not None
    ]

    report = classifier_agreement_report(trades, quotes)
    return {
        **report,
        "symbol": symbol.upper(),
        "session_date": session.isoformat(),
        "feed": feed,
        "window_minutes": window_minutes,
        # If the quote page ceiling truncated the stream, later trades are
        # measured against a stale prevailing quote and the rate is a floor.
        "quote_stream_possibly_truncated": len(raw_quotes) >= quote_limit,
    }
