import asyncio
import logging
import time
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb

from app.domain.market_data import MarketDataSyncResult
from app.providers.yfinance_provider import STATIC_STOCK_METADATA, valid_ohlc
from app.settings import settings

logger = logging.getLogger(__name__)

ALPACA_SOURCE = "alpaca_iex"
ALPACA_STOCK_BARS_ENDPOINT = "/v2/stocks/{symbol}/bars"
ALPACA_STOCK_QUOTES_ENDPOINT = "/v2/stocks/{symbol}/quotes"
ALPACA_STOCK_TRADES_ENDPOINT = "/v2/stocks/{symbol}/trades"
ALPACA_ASSETS_ENDPOINT = "/v2/assets"
ALPACA_FEED = "iex"
MAX_PAGE_LIMIT = 10000
MAX_STOCK_BAR_PAGES = 25
MAX_STOCK_QUOTE_PAGES = 100
# One session of a liquid name can exceed a million prints, so the ceiling is
# high; the caller folds each page away rather than holding the range.
MAX_STOCK_TRADE_PAGES = 400
# Quotes arrive at several times the rate of trades, so the streaming pager
# needs a correspondingly higher ceiling than the trade one.
MAX_STOCK_QUOTE_STREAM_PAGES = 4000
SUPPORTED_TIMEFRAMES = {
    "1m": "1Min",
    "3m": "3Min",
    "5m": "5Min",
    "15m": "15Min",
    "30m": "30Min",
    "1h": "1Hour",
    "4h": "1Hour",
    "1d": "1Day",
}
TIMEFRAME_SECONDS = {
    "1m": 60,
    "3m": 3 * 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
}

async def sync_alpaca_stock_assets(conn: psycopg.Connection) -> dict[str, Any]:
    t = time.perf_counter()

    logger.info("STEP 1: Starting fetch_stock_assets()")
    assets = await fetch_stock_assets()
    logger.info(
        "STEP 2: fetch_stock_assets() finished in %.2f seconds (%d assets)",
        time.perf_counter() - t,
        len(assets),
    )

    t = time.perf_counter()

    logger.info("STEP 3: Starting import_alpaca_stock_assets()")
    imported = import_alpaca_stock_assets(conn, assets)
    logger.info(
        "STEP 4: import_alpaca_stock_assets() finished in %.2f seconds (%d imported)",
        time.perf_counter() - t,
        imported,
    )

    return {
        "assets": assets,
        "total": len(assets),
        "imported": imported,
        "source": "alpaca",
    }

class AlpacaMarketDataProvider:
    name = "alpaca_iex"

    async def sync_candles(self, conn: psycopg.Connection, symbol: str, timeframe: str, limit: int) -> MarketDataSyncResult:
        return await sync_alpaca_candles(conn, symbol=symbol, timeframe=timeframe, limit=limit)


async def sync_alpaca_candles(conn: psycopg.Connection, symbol: str, timeframe: str = "1h", limit: int = 5000) -> MarketDataSyncResult:
    normalized_symbol = symbol.upper()
    if timeframe not in SUPPORTED_TIMEFRAMES:
        supported = ", ".join(sorted(SUPPORTED_TIMEFRAMES))
        raise ValueError(f"alpaca_iex supports these timeframes only: {supported}")
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        raise RuntimeError("Set ALPACA_API_KEY and ALPACA_API_SECRET to use the alpaca_iex provider.")

    ensure_alpaca_stock_symbol(conn, normalized_symbol)
    fetch_timeframe = "1h" if timeframe == "4h" else timeframe
    fetch_limit = limit * 4 if timeframe == "4h" else limit
    status, raw_bars, request_log, request_id = await fetch_stock_bars(normalized_symbol, fetch_timeframe, fetch_limit)
    candles, invalid_ohlc_count = normalize_stock_bars(normalized_symbol, fetch_timeframe, raw_bars)
    if timeframe == "4h":
        candles = aggregate_intraday_candles(candles, target_timeframe="4h")[-limit:]
    candles, incomplete_excluded = exclude_incomplete_latest(candles, timeframe)
    duplicate_count = count_duplicate_bars(raw_bars)
    missing_intervals = detect_missing_intervals(candles, timeframe)
    log_alpaca_response(conn, normalized_symbol, timeframe, limit, status, request_log, request_id, raw_bars)
    upserted = upsert_candles(conn, candles)
    conn.commit()

    return MarketDataSyncResult(
        symbol=normalized_symbol,
        timeframe=timeframe,
        provider="alpaca_iex",
        received=len(candles),
        upserted=upserted,
        candle_count=len(candles),
        missing_intervals=missing_intervals,
        duplicate_count=duplicate_count,
        incomplete_latest_candle_excluded=incomplete_excluded,
        first_timestamp=candles[0]["timestamp"] if candles else None,
        last_timestamp=candles[-1]["timestamp"] if candles else None,
        invalid_ohlc_count=invalid_ohlc_count,
    )


async def sync_alpaca_stock_assets(conn: psycopg.Connection) -> dict[str, Any]:
    assets = await fetch_stock_assets()
    imported = import_alpaca_stock_assets(conn, assets)
    return {
        "assets": assets,
        "total": len(assets),
        "imported": imported,
        "source": "alpaca",
    }


async def fetch_stock_assets() -> list[dict[str, Any]]:
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        raise RuntimeError("Set ALPACA_API_KEY and ALPACA_API_SECRET to import the Alpaca stock universe.")
    headers = {
        "APCA-API-KEY-ID": settings.alpaca_api_key,
        "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
    }
    params = {"status": "active", "asset_class": "us_equity"}
    try:
        async with httpx.AsyncClient(base_url=settings.alpaca_trading_base_url, timeout=30, headers=headers) as client:
            response = await client.get(ALPACA_ASSETS_ENDPOINT, params=params)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as error:
        raise RuntimeError(f"Alpaca asset import failed: {error}") from error

    normalized = [normalize_alpaca_asset(row) for row in payload if isinstance(row, dict)]
    assets = [asset for asset in normalized if asset is not None]
    return sorted(assets, key=lambda asset: asset["symbol"])


def normalize_alpaca_asset(row: dict[str, Any]) -> dict[str, Any] | None:
    symbol = str(row.get("symbol") or "").strip().upper()
    asset_class = str(row.get("class") or row.get("asset_class") or "").lower()
    status = str(row.get("status") or "").lower()
    if not symbol or asset_class != "us_equity" or status != "active" or row.get("tradable") is not True:
        return None
    return {
        "id": str(row.get("id") or symbol),
        "symbol": symbol,
        "name": str(row.get("name") or symbol).strip(),
        "exchange": str(row.get("exchange") or "UNKNOWN").upper(),
        "asset_class": "us_equity",
        "status": status,
        "tradable": True,
        "marginable": bool(row.get("marginable")),
        "shortable": bool(row.get("shortable")),
        "fractionable": bool(row.get("fractionable")),
    }


def import_alpaca_stock_assets(conn: psycopg.Connection, assets: list[dict[str, Any]]) -> int:
    if not assets:
        return 0

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO symbols(symbol, asset_class, exchange, currency, name, provider_symbol, primary_provider, index_membership, is_active)
            VALUES (%s, 'us_equity', %s, 'USD', %s, %s, 'alpaca_iex', %s, TRUE)
            ON CONFLICT (symbol)
            DO UPDATE SET
                asset_class = CASE WHEN symbols.asset_class = 'etf' THEN symbols.asset_class ELSE EXCLUDED.asset_class END,
                exchange = EXCLUDED.exchange,
                currency = EXCLUDED.currency,
                name = EXCLUDED.name,
                provider_symbol = EXCLUDED.provider_symbol,
                primary_provider = EXCLUDED.primary_provider,
                is_active = TRUE
            """,
            [
                (
                    asset["symbol"],
                    asset["exchange"],
                    asset["name"],
                    asset["symbol"],
                    Jsonb([]),
                )
                for asset in assets
            ],
        )

    conn.commit()
    return len(assets)

async def fetch_stock_bars(symbol: str, timeframe: str, limit: int) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]], str | None]:
    requested_limit = max(1, limit)
    page_limit = min(MAX_PAGE_LIMIT, requested_limit)
    start = start_for_limit(timeframe, requested_limit)
    end = datetime.now(tz=UTC)
    params: dict[str, Any] = {
        "timeframe": SUPPORTED_TIMEFRAMES[timeframe],
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "limit": page_limit,
        "adjustment": "all",
        "feed": ALPACA_FEED,
        "sort": "desc",
    }
    headers = {
        "APCA-API-KEY-ID": settings.alpaca_api_key,
        "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
    }
    bars: list[dict[str, Any]] = []
    request_log: list[dict[str, Any]] = []
    endpoint = ALPACA_STOCK_BARS_ENDPOINT.format(symbol=symbol)
    status = 200
    request_id: str | None = None

    async with httpx.AsyncClient(base_url=settings.alpaca_data_base_url, timeout=30, headers=headers) as client:
        page_count = 0
        while page_count < MAX_STOCK_BAR_PAGES:
            page_count += 1
            response = await client.get(endpoint, params=params)
            status = response.status_code
            request_id = response.headers.get("X-Request-ID") or request_id
            response.raise_for_status()
            payload = response.json()
            page_bars = payload.get("bars", [])
            request_log.append(
                {
                    "start": params["start"],
                    "end": params["end"],
                    "timeframe": params["timeframe"],
                    "feed": ALPACA_FEED,
                    "received": len(page_bars),
                    "request_id": response.headers.get("X-Request-ID"),
                }
            )
            bars.extend(page_bars)
            if len(bars) >= requested_limit:
                break
            token = payload.get("next_page_token")
            if not token or not page_bars:
                break
            params["page_token"] = token

    selected = bars[:requested_limit]
    selected.sort(key=lambda row: str(row.get("t") or ""))
    return status, selected, request_log, request_id


async def fetch_stock_quotes(
    symbol: str,
    *,
    start: datetime,
    end: datetime,
    limit: int = 10_000,
    feed: str = ALPACA_FEED,
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Fetch historical NBBO-like quote updates for cost research.

    The configured Basic Alpaca plan uses IEX, so the feed is stored on every
    observation and is never presented as consolidated-SIP coverage.
    """
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        raise RuntimeError("Set ALPACA_API_KEY and ALPACA_API_SECRET to fetch Alpaca quotes.")
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Quote start/end must be timezone-aware.")
    if end <= start:
        raise ValueError("Quote end must be after start.")

    requested_limit = max(1, limit)
    params: dict[str, Any] = {
        "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "end": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "limit": min(MAX_PAGE_LIMIT, requested_limit),
        "feed": feed,
        "sort": "asc",
    }
    headers = {
        "APCA-API-KEY-ID": settings.alpaca_api_key,
        "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
    }
    endpoint = ALPACA_STOCK_QUOTES_ENDPOINT.format(symbol=symbol.upper())
    quotes: list[dict[str, Any]] = []
    request_log: list[dict[str, Any]] = []
    status = 200
    request_id: str | None = None

    async with httpx.AsyncClient(base_url=settings.alpaca_data_base_url, timeout=30, headers=headers) as client:
        for _ in range(MAX_STOCK_QUOTE_PAGES):
            response = await client.get(endpoint, params=params)
            status = response.status_code
            request_id = response.headers.get("X-Request-ID") or request_id
            response.raise_for_status()
            payload = response.json()
            page_quotes = payload.get("quotes", [])
            token = payload.get("next_page_token")
            request_log.append(
                {
                    "start": params["start"],
                    "end": params["end"],
                    "feed": feed,
                    "received": len(page_quotes),
                    "request_id": response.headers.get("X-Request-ID"),
                    "next_page_token_present": bool(token),
                }
            )
            quotes.extend(page_quotes)
            if len(quotes) >= requested_limit:
                break
            if not token or not page_quotes:
                break
            params["page_token"] = token

    selected = quotes[:requested_limit]
    selected.sort(key=lambda row: str(row.get("t") or ""))
    return status, selected, request_log, request_id


def parse_rfc3339_nanoseconds(value: str) -> int:
    """Parse an RFC-3339 timestamp to integer nanoseconds since the epoch.

    ``datetime.fromisoformat`` floors to microseconds.  Alpaca timestamps carry
    nanoseconds, so parsing through ``datetime`` alone discards precision that
    distinguishes consecutive NBBO updates -- the Stage 0 probe measured 37,526
    of 23,016,760 updates (0.163%) sharing a microsecond with their predecessor.
    Small, but the survivor of a collapsed microsecond is its *latest* state,
    which is future information relative to anything inside it.
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, tail = text.split(".", 1)
        digits = ""
        for character in tail:
            if character.isdigit():
                digits += character
            else:
                tail = tail[len(digits) :]
                break
        else:
            tail = ""
        fraction_ns = int((digits + "0" * 9)[:9])
        base = datetime.fromisoformat(head + (tail or "+00:00"))
    else:
        fraction_ns = 0
        base = datetime.fromisoformat(text)
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    whole_seconds = int(base.replace(microsecond=0).timestamp())
    return whole_seconds * 1_000_000_000 + fraction_ns


# Rejection reasons, kept as constants so a counter key cannot drift from the
# branch that increments it.
QUOTE_REJECT_UNPARSEABLE = "rejected_unparseable"
QUOTE_REJECT_NONPOSITIVE_PRICE = "rejected_nonpositive_price"
QUOTE_REJECT_CROSSED = "rejected_crossed"
QUOTE_REJECT_NEGATIVE_SIZE = "rejected_negative_size"
QUOTE_REJECT_REASONS = (
    QUOTE_REJECT_UNPARSEABLE,
    QUOTE_REJECT_NONPOSITIVE_PRICE,
    QUOTE_REJECT_CROSSED,
    QUOTE_REJECT_NEGATIVE_SIZE,
)


class QuoteNormalizationCounters:
    """Explicit accounting for every quote seen, accepted or dropped.

    Row-by-row silent rejection is how an abnormal period disappears.  A
    trading halt produces exactly the rows this normalizer discards -- crossed
    and locked markets from mass cancellation -- so a session that was halted
    and a session that was quiet look identical once the drops are unrecorded.
    The Stage 0 probe measured 241-1,436 crossed quotes per symbol-session in
    ordinary conditions, none of which were counted anywhere.
    """

    __slots__ = ("received", "accepted", *QUOTE_REJECT_REASONS)

    def __init__(self) -> None:
        self.received = 0
        self.accepted = 0
        for reason in QUOTE_REJECT_REASONS:
            setattr(self, reason, 0)

    def record_received(self) -> None:
        self.received += 1

    def record_accepted(self) -> None:
        self.accepted += 1

    def record_rejected(self, reason: str) -> None:
        if reason not in QUOTE_REJECT_REASONS:
            raise ValueError(f"unknown quote rejection reason {reason!r}")
        setattr(self, reason, getattr(self, reason) + 1)

    @property
    def rejected(self) -> int:
        return sum(getattr(self, reason) for reason in QUOTE_REJECT_REASONS)

    def as_dict(self) -> dict[str, Any]:
        rejected = self.rejected
        return {
            "received": self.received,
            "accepted": self.accepted,
            "rejected": rejected,
            "rejection_rate": (round(rejected / self.received, 6) if self.received else None),
            **{reason: getattr(self, reason) for reason in QUOTE_REJECT_REASONS},
        }


def normalize_stock_quote(
    symbol: str,
    row: dict[str, Any],
    *,
    feed: str = ALPACA_FEED,
    counters: QuoteNormalizationCounters | None = None,
) -> dict[str, Any] | None:
    """Normalize one Alpaca quote and reject crossed/empty markets.

    Pass ``counters`` to account for what was dropped and why.  The return
    contract is unchanged, so existing callers keep working -- they just do not
    learn what they discarded.
    """
    if counters is not None:
        counters.record_received()

    def reject(reason: str) -> None:
        if counters is not None:
            counters.record_rejected(reason)

    try:
        bid = Decimal(str(row["bp"]))
        ask = Decimal(str(row["ap"]))
        bid_size = Decimal(str(row.get("bs", 0)))
        ask_size = Decimal(str(row.get("as", 0)))
        raw_timestamp = str(row["t"])
        timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00")).astimezone(UTC)
        timestamp_ns = parse_rfc3339_nanoseconds(raw_timestamp)
    except (InvalidOperation, KeyError, TypeError, ValueError):
        reject(QUOTE_REJECT_UNPARSEABLE)
        return None
    # Split what used to be one condition, so the counter names the cause.
    if bid <= 0 or ask <= 0:
        reject(QUOTE_REJECT_NONPOSITIVE_PRICE)
        return None
    if ask < bid:
        reject(QUOTE_REJECT_CROSSED)
        return None
    if bid_size < 0 or ask_size < 0:
        reject(QUOTE_REJECT_NEGATIVE_SIZE)
        return None
    midpoint = (bid + ask) / Decimal(2)
    spread_bps = ((ask - bid) / midpoint) * Decimal(10000)
    if counters is not None:
        counters.record_accepted()
    return {
        "symbol": symbol.upper(),
        "provider": "alpaca",
        "feed": feed,
        "timestamp": timestamp,
        # Full source precision, carried alongside the microsecond datetime so
        # persistence can key on the instant the venue actually reported.
        "timestamp_ns": timestamp_ns,
        "bid_price": bid,
        "ask_price": ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "midpoint": midpoint,
        "spread_bps": spread_bps,
        "raw_payload": dict(row),
    }


def normalize_stock_quotes(
    symbol: str,
    rows: Sequence[dict[str, Any]],
    *,
    feed: str = ALPACA_FEED,
) -> tuple[list[dict[str, Any]], QuoteNormalizationCounters]:
    """Normalize a page of quotes and return the accounting alongside them."""
    counters = QuoteNormalizationCounters()
    normalized = [
        quote
        for quote in (
            normalize_stock_quote(symbol, row, feed=feed, counters=counters) for row in rows
        )
        if quote is not None
    ]
    return normalized, counters


async def iter_stock_quote_pages(
    symbol: str,
    *,
    start: datetime,
    end: datetime,
    feed: str = ALPACA_FEED,
    max_pages: int = MAX_STOCK_QUOTE_STREAM_PAGES,
    rate_limit_retries: int = 12,
    rate_limit_base_sleep: float = 30.0,
    request_pause_seconds: float = 0.0,
) -> AsyncIterator[tuple[list[dict[str, Any]], dict[str, Any]]]:
    """Yield pages of historical NBBO quotes without accumulating the range.

    The sibling of :func:`iter_stock_trade_pages`, and needed for the same
    reason with more force: quotes arrive at roughly ten times the volume of
    trades, so a liquid symbol can print several million in one session.
    :func:`fetch_stock_quotes` materialises its result in a list and stops at
    ``MAX_STOCK_QUOTE_PAGES``, which makes a truncated session indistinguishable
    from a quiet one -- exactly the failure the trade pager was written to
    avoid.  Each page here carries ``exhausted`` so the caller can record
    whether the window was actually drained.
    """
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        raise RuntimeError("Set ALPACA_API_KEY and ALPACA_API_SECRET to fetch Alpaca quotes.")
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Quote start/end must be timezone-aware.")
    if end <= start:
        raise ValueError("Quote end must be after start.")

    params: dict[str, Any] = {
        "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "end": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "limit": MAX_PAGE_LIMIT,
        "feed": feed,
        "sort": "asc",
    }
    headers = {
        "APCA-API-KEY-ID": settings.alpaca_api_key,
        "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
    }
    endpoint = ALPACA_STOCK_QUOTES_ENDPOINT.format(symbol=symbol.upper())

    async with httpx.AsyncClient(
        base_url=settings.alpaca_data_base_url, timeout=60, headers=headers
    ) as client:
        for page in range(max_pages):
            response = None
            for attempt in range(rate_limit_retries + 1):
                response = await client.get(endpoint, params=params)
                if response.status_code != 429:
                    break
                if attempt >= rate_limit_retries:
                    break
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.replace(".", "", 1).isdigit()
                    else min(rate_limit_base_sleep * (2 ** min(attempt, 4)), 900.0)
                )
                logger.warning(
                    "Alpaca quote rate limit; retrying",
                    extra={"symbol": symbol, "page": page, "attempt": attempt + 1, "sleep": delay},
                )
                await asyncio.sleep(delay)
            assert response is not None
            response.raise_for_status()
            payload = response.json()
            quotes = payload.get("quotes") or []
            token = payload.get("next_page_token")
            yield quotes, {
                "page": page,
                "feed": feed,
                "received": len(quotes),
                "status": response.status_code,
                "request_id": response.headers.get("X-Request-ID"),
                "next_page_token_present": bool(token),
                "exhausted": not token or not quotes,
            }
            if not token or not quotes:
                return
            params["page_token"] = token
            if request_pause_seconds > 0:
                await asyncio.sleep(request_pause_seconds)


async def iter_stock_trade_pages(
    symbol: str,
    *,
    start: datetime,
    end: datetime,
    feed: str = ALPACA_FEED,
    max_pages: int = MAX_STOCK_TRADE_PAGES,
    rate_limit_retries: int = 12,
    rate_limit_base_sleep: float = 30.0,
    request_pause_seconds: float = 0.0,
) -> AsyncIterator[tuple[list[dict[str, Any]], dict[str, Any]]]:
    """Yield pages of historical trades without accumulating the whole range.

    A single liquid symbol can print well over a million trades in one session,
    so this is an iterator rather than a list-returning fetch: the caller folds
    each page into its aggregate and drops it.  Materialising the range would
    reproduce the out-of-memory failures the candle work already hit.
    """
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        raise RuntimeError("Set ALPACA_API_KEY and ALPACA_API_SECRET to fetch Alpaca trades.")
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Trade start/end must be timezone-aware.")
    if end <= start:
        raise ValueError("Trade end must be after start.")

    params: dict[str, Any] = {
        "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "end": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "limit": MAX_PAGE_LIMIT,
        "feed": feed,
        "sort": "asc",
    }
    headers = {
        "APCA-API-KEY-ID": settings.alpaca_api_key,
        "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
    }
    endpoint = ALPACA_STOCK_TRADES_ENDPOINT.format(symbol=symbol.upper())

    async with httpx.AsyncClient(
        base_url=settings.alpaca_data_base_url, timeout=60, headers=headers
    ) as client:
        for page in range(max_pages):
            response = None
            for attempt in range(rate_limit_retries + 1):
                response = await client.get(endpoint, params=params)
                if response.status_code != 429:
                    break
                if attempt >= rate_limit_retries:
                    break
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.replace(".", "", 1).isdigit()
                    else min(rate_limit_base_sleep * (2 ** min(attempt, 4)), 900.0)
                )
                logger.warning(
                    "Alpaca trade rate limit; retrying",
                    extra={"symbol": symbol, "page": page, "attempt": attempt + 1, "sleep": delay},
                )
                await asyncio.sleep(delay)
            assert response is not None
            response.raise_for_status()
            payload = response.json()
            trades = payload.get("trades") or []
            token = payload.get("next_page_token")
            yield trades, {
                "page": page,
                "feed": feed,
                "received": len(trades),
                "status": response.status_code,
                "request_id": response.headers.get("X-Request-ID"),
                "next_page_token_present": bool(token),
                # A truthful record of whether the window was fully drained.
                "exhausted": not token or not trades,
            }
            if not token or not trades:
                return
            params["page_token"] = token
            if request_pause_seconds > 0:
                await asyncio.sleep(request_pause_seconds)


def normalize_stock_trade(
    symbol: str,
    row: dict[str, Any],
    *,
    feed: str = ALPACA_FEED,
) -> dict[str, Any] | None:
    """Normalize one Alpaca trade, rejecting rows that cannot be priced."""
    try:
        price = Decimal(str(row["p"]))
        size = Decimal(str(row["s"]))
        timestamp = datetime.fromisoformat(str(row["t"]).replace("Z", "+00:00")).astimezone(UTC)
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return None
    if price <= 0 or size <= 0:
        return None
    conditions = row.get("c")
    return {
        "symbol": symbol.upper(),
        "provider": "alpaca",
        "feed": feed,
        "timestamp": timestamp,
        "price": price,
        "size": size,
        "exchange": row.get("x"),
        "tape": row.get("z"),
        "conditions": [str(item) for item in conditions] if conditions else [],
    }


def start_for_limit(timeframe: str, limit: int) -> datetime:
    seconds = TIMEFRAME_SECONDS[timeframe]
    if timeframe == "1d":
        calendar_days = max(365, int(limit * 1.8))
    else:
        regular_session_bars_per_day = max(1, int((6.5 * 60 * 60) / seconds))
        trading_days = max(252, int(limit / regular_session_bars_per_day) + 30)
        calendar_days = min(int(trading_days * 1.6), settings.alpaca_intraday_max_lookback_days)
    return datetime.now(tz=UTC) - timedelta(days=calendar_days)


def normalize_stock_bars(symbol: str, timeframe: str, bars: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    candles = []
    invalid = 0
    for row in bars:
        candle = normalize_stock_bar(symbol, timeframe, row)
        if candle is None:
            invalid += 1
            continue
        candles.append(candle)
    candles.sort(key=lambda candle: candle["timestamp"])
    return candles, invalid


def normalize_stock_bar(symbol: str, timeframe: str, row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        open_price = Decimal(str(row["o"]))
        high = Decimal(str(row["h"]))
        low = Decimal(str(row["l"]))
        close = Decimal(str(row["c"]))
        volume = Decimal(str(row.get("v", 0)))
        timestamp = datetime.fromisoformat(str(row["t"]).replace("Z", "+00:00")).astimezone(UTC)
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return None

    if not valid_ohlc(open_price, high, low, close, volume):
        return None
    return {
        "symbol": symbol,
        "source": ALPACA_SOURCE,
        "timeframe": timeframe,
        "timestamp": timestamp,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def aggregate_intraday_candles(candles: list[dict[str, Any]], *, target_timeframe: str) -> list[dict[str, Any]]:
    if target_timeframe != "4h":
        return candles
    buckets: dict[datetime, list[dict[str, Any]]] = {}
    for candle in candles:
        timestamp = candle["timestamp"].astimezone(UTC)
        bucket_time = timestamp.replace(hour=(timestamp.hour // 4) * 4, minute=0, second=0, microsecond=0)
        buckets.setdefault(bucket_time, []).append(candle)
    aggregated = []
    for bucket_time in sorted(buckets):
        rows = sorted(buckets[bucket_time], key=lambda item: item["timestamp"])
        if not rows:
            continue
        aggregated.append(
            {
                "symbol": rows[0]["symbol"],
                "source": ALPACA_SOURCE,
                "timeframe": "4h",
                "timestamp": bucket_time,
                "open": rows[0]["open"],
                "high": max(row["high"] for row in rows),
                "low": min(row["low"] for row in rows),
                "close": rows[-1]["close"],
                "volume": sum((row["volume"] for row in rows), Decimal(0)),
            }
        )
    return aggregated


def exclude_incomplete_latest(candles: list[dict[str, Any]], timeframe: str, now: datetime | None = None) -> tuple[list[dict[str, Any]], bool]:
    if not candles:
        return candles, False
    seconds = TIMEFRAME_SECONDS[timeframe]
    now = now or datetime.now(tz=UTC)
    latest_open = candles[-1]["timestamp"]
    if latest_open + timedelta(seconds=seconds) > now:
        return candles[:-1], True
    return candles, False


def count_duplicate_bars(bars: list[dict[str, Any]]) -> int:
    seen: set[str] = set()
    duplicates = 0
    for row in bars:
        timestamp = str(row.get("t"))
        if timestamp in seen:
            duplicates += 1
        seen.add(timestamp)
    return duplicates


def detect_missing_intervals(candles: list[dict[str, Any]], timeframe: str) -> int:
    if timeframe == "1d" or len(candles) < 2:
        return 0
    seconds = TIMEFRAME_SECONDS[timeframe]
    missing = 0
    for previous, current in zip(candles, candles[1:]):
        gap = int((current["timestamp"] - previous["timestamp"]).total_seconds())
        if gap > seconds and gap < 12 * 60 * 60:
            missing += (gap // seconds) - 1
    return missing


def ensure_alpaca_stock_symbol(conn: psycopg.Connection, symbol: str) -> None:
    if symbol not in STATIC_STOCK_METADATA:
        registered = conn.execute(
            "SELECT symbol FROM symbols WHERE symbol = %s AND primary_provider = 'alpaca_iex' AND is_active = TRUE",
            (symbol,),
        ).fetchone()
        if registered:
            return
        raise ValueError(f"Unsupported Alpaca stock symbol '{symbol}'. Import the Alpaca asset catalog first.")

    metadata = STATIC_STOCK_METADATA[symbol]
    conn.execute(
        """
        INSERT INTO symbols(symbol, asset_class, exchange, currency, name, provider_symbol, primary_provider, sector, index_membership)
        VALUES (%s, %s, %s, %s, %s, %s, 'alpaca_iex', %s, %s)
        ON CONFLICT (symbol)
        DO UPDATE SET
            asset_class = EXCLUDED.asset_class,
            exchange = EXCLUDED.exchange,
            currency = EXCLUDED.currency,
            name = EXCLUDED.name,
            provider_symbol = EXCLUDED.provider_symbol,
            primary_provider = EXCLUDED.primary_provider,
            sector = EXCLUDED.sector,
            index_membership = EXCLUDED.index_membership
        """,
        (
            symbol,
            metadata["asset_class"],
            metadata["exchange"],
            metadata["currency"],
            metadata["name"],
            symbol,
            metadata["sector"],
            Jsonb(metadata["index_membership"]),
        ),
    )


def log_alpaca_response(
    conn: psycopg.Connection,
    symbol: str,
    timeframe: str,
    limit: int,
    status: int,
    request_log: list[dict[str, Any]],
    request_id: str | None,
    bars: list[dict[str, Any]],
) -> None:
    conn.execute(
        """
        INSERT INTO raw_api_logs(source, endpoint, request_params, response_status, response_body)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            ALPACA_SOURCE,
            ALPACA_STOCK_BARS_ENDPOINT,
            Jsonb({"symbol": symbol, "timeframe": timeframe, "limit": limit, "feed": ALPACA_FEED, "requests": request_log}),
            status,
            Jsonb({"request_id": request_id, "bar_count": len(bars), "first": bars[0].get("t") if bars else None, "last": bars[-1].get("t") if bars else None}),
        ),
    )


def upsert_candles(conn: psycopg.Connection, candles: list[dict[str, Any]]) -> int:
    affected = 0
    for candle in candles:
        result = conn.execute(
            """
            INSERT INTO candles(symbol, source, timeframe, timestamp, open, high, low, close, volume)
            VALUES (%(symbol)s, %(source)s, %(timeframe)s, %(timestamp)s, %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s)
            ON CONFLICT(symbol, source, timeframe, timestamp)
            DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume
            """,
            candle,
        )
        affected += result.rowcount or 0
    return affected
