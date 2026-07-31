"""Resumable date-range candle ingestion for institutional intraday research.

The existing `sync_alpaca_candles` path asks for "the last N bars" and stops at
a page cap, so a request for ten years of history silently returns whatever
fits.  That is fine for keeping a live table warm and useless for building an
immutable research dataset, where a quietly truncated history looks exactly
like a market that had no data.

This module ingests an explicit ``[start, end)`` range instead: it walks every
page to exhaustion, checkpoints each month so an interrupted run resumes
instead of restarting, retries transport and rate-limit failures, and
reconciles what arrived against the exchange calendar so missing sessions are
reported rather than assumed absent.

Feed choice is recorded in the candle ``source`` (``alpaca_sip`` /
``alpaca_iex``).  The two feeds must never share a source label: IEX is one
venue's share of the tape, so blending it with the consolidated feed would put
two different prices on the same bar and double every snapshot row.

The module contains no campaign, broker, order-submission, or UI code.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Iterable, Sequence

import httpx
import psycopg

from app.providers.alpaca import (
    ALPACA_STOCK_BARS_ENDPOINT,
    SUPPORTED_TIMEFRAMES,
    ensure_alpaca_stock_symbol,
    normalize_stock_bars,
    upsert_candles,
)
from app.services.intraday_session_calendar import NEW_YORK
from app.settings import settings

CANDLE_INGEST_VERSION = "intraday_candle_ingest_v1"

RESEARCH_FEEDS = ("sip", "iex")
FEED_SOURCES = {"sip": "alpaca_sip", "iex": "alpaca_iex"}
PAGE_LIMIT = 10_000
MAX_ATTEMPTS = 6
BASE_BACKOFF_SECONDS = 1.5
MAX_BACKOFF_SECONDS = 60.0
# Alpaca meters by requests per minute; a small floor between calls keeps a
# long backfill from tripping the limiter in the first place rather than
# relying on retries to recover from it.
MIN_REQUEST_INTERVAL_SECONDS = 0.12
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def feed_source(feed: str) -> str:
    try:
        return FEED_SOURCES[feed]
    except KeyError as error:
        raise ValueError(f"unsupported research feed {feed!r}; use one of {RESEARCH_FEEDS}") from error


@dataclass
class ChunkResult:
    symbol: str
    timeframe: str
    feed: str
    chunk_start: date
    chunk_end: date
    status: str
    bars_received: int = 0
    bars_upserted: int = 0
    invalid_bars: int = 0
    pages: int = 0
    attempts: int = 0
    error: str | None = None
    skipped: bool = False


@dataclass
class RateLimiter:
    """Serialize requests with a minimum interval between them."""

    min_interval: float = MIN_REQUEST_INTERVAL_SECONDS
    _last: float = field(default=0.0, repr=False)

    async def wait(self) -> None:
        loop = asyncio.get_running_loop()
        elapsed = loop.time() - self._last
        if elapsed < self.min_interval:
            await asyncio.sleep(self.min_interval - elapsed)
        self._last = loop.time()


def month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Split ``[start, end]`` into calendar-month windows.

    Chunking is what makes the run resumable: each window is checkpointed on
    its own, so an interrupted ten-year backfill restarts at the month it
    stopped rather than at the beginning.
    """
    if end < start:
        raise ValueError("ingestion end must not precede start")
    chunks: list[tuple[date, date]] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        following = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )
        chunks.append((max(cursor, start), min(following - timedelta(days=1), end)))
        cursor = following
    return chunks


def completed_chunks(
    conn: psycopg.Connection,
    *,
    symbol: str,
    timeframe: str,
    feed: str,
) -> set[date]:
    rows = conn.execute(
        """
        SELECT chunk_start
        FROM intraday_candle_ingest_checkpoints
        WHERE symbol = %s AND timeframe = %s AND feed = %s
          AND status = 'completed' AND ingest_version = %s
        """,
        (symbol, timeframe, feed, CANDLE_INGEST_VERSION),
    ).fetchall()
    return {row["chunk_start"] for row in rows}


def record_checkpoint(conn: psycopg.Connection, result: ChunkResult) -> None:
    conn.execute(
        """
        INSERT INTO intraday_candle_ingest_checkpoints(
            symbol, timeframe, feed, chunk_start, chunk_end, status,
            bars_received, bars_upserted, invalid_bars, pages, attempts,
            error, ingest_version, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (symbol, timeframe, feed, chunk_start) DO UPDATE SET
            chunk_end = EXCLUDED.chunk_end,
            status = EXCLUDED.status,
            bars_received = EXCLUDED.bars_received,
            bars_upserted = EXCLUDED.bars_upserted,
            invalid_bars = EXCLUDED.invalid_bars,
            pages = EXCLUDED.pages,
            attempts = EXCLUDED.attempts,
            error = EXCLUDED.error,
            ingest_version = EXCLUDED.ingest_version,
            updated_at = NOW()
        """,
        (
            result.symbol,
            result.timeframe,
            result.feed,
            result.chunk_start,
            result.chunk_end,
            result.status,
            result.bars_received,
            result.bars_upserted,
            result.invalid_bars,
            result.pages,
            result.attempts,
            result.error,
            CANDLE_INGEST_VERSION,
        ),
    )
    conn.commit()


async def _get_with_retry(
    client: httpx.AsyncClient,
    endpoint: str,
    params: dict[str, Any],
    *,
    limiter: RateLimiter,
) -> tuple[httpx.Response, int]:
    """GET with bounded exponential backoff, honouring Retry-After."""
    delay = BASE_BACKOFF_SECONDS
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        await limiter.wait()
        try:
            response = await client.get(endpoint, params=params)
        except httpx.HTTPError as error:
            last_error = error
            if attempt == MAX_ATTEMPTS:
                raise
            await asyncio.sleep(min(delay, MAX_BACKOFF_SECONDS))
            delay *= 2
            continue
        if response.status_code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
            retry_after = response.headers.get("Retry-After")
            wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
            await asyncio.sleep(min(wait, MAX_BACKOFF_SECONDS))
            delay *= 2
            continue
        response.raise_for_status()
        return response, attempt
    raise RuntimeError(f"exhausted retries for {endpoint}") from last_error


async def fetch_bars_range(
    client: httpx.AsyncClient,
    *,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    feed: str,
    limiter: RateLimiter,
) -> tuple[list[dict[str, Any]], int, int]:
    """Every bar in ``[start, end)``, following pagination to exhaustion.

    There is deliberately no result cap here.  A cap would make a truncated
    history indistinguishable from a short one, which is the specific failure
    this module exists to remove.
    """
    params: dict[str, Any] = {
        "timeframe": SUPPORTED_TIMEFRAMES[timeframe],
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "limit": PAGE_LIMIT,
        "adjustment": "all",
        "feed": feed,
        "sort": "asc",
    }
    endpoint = ALPACA_STOCK_BARS_ENDPOINT.format(symbol=symbol)
    bars: list[dict[str, Any]] = []
    pages = 0
    attempts = 0
    while True:
        response, used = await _get_with_retry(client, endpoint, params, limiter=limiter)
        attempts += used
        pages += 1
        payload = response.json()
        page_bars = payload.get("bars") or []
        bars.extend(page_bars)
        token = payload.get("next_page_token")
        if not token:
            break
        params["page_token"] = token
    return bars, pages, attempts


async def ingest_symbol_range(
    conn: psycopg.Connection,
    *,
    symbol: str,
    timeframe: str,
    start: date,
    end: date,
    feed: str = "sip",
    resume: bool = True,
    limiter: RateLimiter | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Ingest one symbol over a date range, month by month, resumably."""
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError(f"unsupported timeframe {timeframe!r}")
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        raise RuntimeError("Set ALPACA_API_KEY and ALPACA_API_SECRET to ingest research candles.")
    normalized = symbol.upper()
    source = feed_source(feed)
    limiter = limiter or RateLimiter()

    ensure_alpaca_stock_symbol(conn, normalized)
    already = completed_chunks(conn, symbol=normalized, timeframe=timeframe, feed=feed) if resume else set()

    headers = {
        "APCA-API-KEY-ID": settings.alpaca_api_key,
        "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
    }
    results: list[ChunkResult] = []
    async with httpx.AsyncClient(
        base_url=settings.alpaca_data_base_url, timeout=60, headers=headers
    ) as client:
        for chunk_start, chunk_end in month_chunks(start, end):
            if chunk_start in already:
                results.append(
                    ChunkResult(
                        symbol=normalized,
                        timeframe=timeframe,
                        feed=feed,
                        chunk_start=chunk_start,
                        chunk_end=chunk_end,
                        status="completed",
                        skipped=True,
                    )
                )
                continue
            result = ChunkResult(
                symbol=normalized,
                timeframe=timeframe,
                feed=feed,
                chunk_start=chunk_start,
                chunk_end=chunk_end,
                status="running",
            )
            try:
                raw, pages, attempts = await fetch_bars_range(
                    client,
                    symbol=normalized,
                    timeframe=timeframe,
                    start=datetime.combine(chunk_start, datetime.min.time(), tzinfo=UTC),
                    end=datetime.combine(
                        chunk_end + timedelta(days=1), datetime.min.time(), tzinfo=UTC
                    ),
                    feed=feed,
                    limiter=limiter,
                )
                candles, invalid = normalize_stock_bars(normalized, timeframe, raw)
                for candle in candles:
                    candle["source"] = source
                upserted = upsert_candles(conn, candles)
                conn.commit()
                result.status = "completed"
                result.bars_received = len(candles)
                result.bars_upserted = upserted
                result.invalid_bars = invalid
                result.pages = pages
                result.attempts = attempts
            except Exception as error:  # noqa: BLE001 - recorded, then re-raised by caller policy
                conn.rollback()
                result.status = "failed"
                result.error = f"{type(error).__name__}: {error}"
            record_checkpoint(conn, result)
            results.append(result)
            if progress is not None:
                progress(result)

    completed = [item for item in results if item.status == "completed"]
    failed = [item for item in results if item.status == "failed"]
    return {
        "ingest_version": CANDLE_INGEST_VERSION,
        "symbol": normalized,
        "timeframe": timeframe,
        "feed": feed,
        "source": source,
        "requested_start": start,
        "requested_end": end,
        "chunks": len(results),
        "chunks_completed": len(completed),
        "chunks_skipped": sum(1 for item in results if item.skipped),
        "chunks_failed": len(failed),
        "bars_received": sum(item.bars_received for item in completed),
        "bars_upserted": sum(item.bars_upserted for item in completed),
        "invalid_bars": sum(item.invalid_bars for item in completed),
        "pages": sum(item.pages for item in completed),
        "failures": [
            {"chunk_start": str(item.chunk_start), "error": item.error} for item in failed
        ],
    }


async def ingest_universe_range(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    timeframe: str,
    start: date,
    end: date,
    feed: str = "sip",
    resume: bool = True,
    progress: Any = None,
) -> dict[str, Any]:
    limiter = RateLimiter()
    per_symbol: list[dict[str, Any]] = []
    for symbol in symbols:
        summary = await ingest_symbol_range(
            conn,
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            feed=feed,
            resume=resume,
            limiter=limiter,
            progress=progress,
        )
        per_symbol.append(summary)
    return {
        "ingest_version": CANDLE_INGEST_VERSION,
        "timeframe": timeframe,
        "feed": feed,
        "source": feed_source(feed),
        "requested_start": start,
        "requested_end": end,
        "symbols": len(per_symbol),
        "bars_upserted": sum(item["bars_upserted"] for item in per_symbol),
        "symbols_with_failures": [
            item["symbol"] for item in per_symbol if item["chunks_failed"]
        ],
        "per_symbol": per_symbol,
    }


def expected_sessions(start: date, end: date) -> list[date]:
    """Exchange sessions the calendar says should exist in the range."""
    from app.services.labs.intraday.session import trading_schedule

    schedule = trading_schedule(start, end, padding_days=0)
    sessions = [
        value.astimezone(NEW_YORK).date()
        for value in schedule["market_open"].tolist()
    ]
    return [item for item in sessions if start <= item <= end]


def reconcile_sessions(
    conn: psycopg.Connection,
    *,
    symbol: str,
    timeframe: str,
    source: str,
    start: date,
    end: date,
) -> dict[str, Any]:
    """Compare stored sessions against the exchange calendar."""
    rows = conn.execute(
        """
        SELECT DISTINCT (timestamp AT TIME ZONE 'America/New_York')::date AS session_date
        FROM candles
        WHERE symbol = %s AND timeframe = %s AND source = %s
          AND (timestamp AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s
        """,
        (symbol.upper(), timeframe, source, start, end),
    ).fetchall()
    received = {row["session_date"] for row in rows}
    expected = expected_sessions(start, end)
    missing = [item for item in expected if item not in received]
    unexpected = sorted(received - set(expected))
    return {
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "source": source,
        "expected_sessions": len(expected),
        "received_sessions": len(received),
        "missing_sessions": len(missing),
        "missing_session_dates": [str(item) for item in missing[:50]],
        "sessions_outside_calendar": [str(item) for item in unexpected[:50]],
        "coverage": round(len(received & set(expected)) / len(expected), 6) if expected else None,
        # A symbol that listed partway through the window is not a data
        # failure, so first/last received bound the honest comparison.
        "first_received_session": str(min(received)) if received else None,
        "last_received_session": str(max(received)) if received else None,
    }


def reconcile_universe(
    conn: psycopg.Connection,
    *,
    symbols: Iterable[str],
    timeframe: str,
    source: str,
    start: date,
    end: date,
) -> dict[str, Any]:
    reports = [
        reconcile_sessions(
            conn,
            symbol=symbol,
            timeframe=timeframe,
            source=source,
            start=start,
            end=end,
        )
        for symbol in symbols
    ]
    incomplete = [item for item in reports if (item["coverage"] or 0) < 0.99]
    return {
        "timeframe": timeframe,
        "source": source,
        "symbols": len(reports),
        "symbols_below_99pct_coverage": len(incomplete),
        "worst_coverage": min((item["coverage"] or 0 for item in reports), default=None),
        "per_symbol": reports,
    }
