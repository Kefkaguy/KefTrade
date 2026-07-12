from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb

from app.domain.market_data import MarketDataSyncResult
from app.providers.yfinance_provider import STATIC_STOCK_METADATA, valid_ohlc
from app.settings import settings

ALPACA_SOURCE = "alpaca_iex"
ALPACA_STOCK_BARS_ENDPOINT = "/v2/stocks/{symbol}/bars"
ALPACA_FEED = "iex"
MAX_PAGE_LIMIT = 10000
MAX_STOCK_BAR_PAGES = 25
SUPPORTED_TIMEFRAMES = {
    "15m": "15Min",
    "30m": "30Min",
    "1h": "1Hour",
    "1d": "1Day",
}
TIMEFRAME_SECONDS = {
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "1d": 24 * 60 * 60,
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
    if normalized_symbol not in STATIC_STOCK_METADATA:
        supported = ", ".join(sorted(STATIC_STOCK_METADATA))
        raise ValueError(f"Unsupported Alpaca stock symbol '{symbol}'. Supported symbols: {supported}")
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        raise RuntimeError("Set ALPACA_API_KEY and ALPACA_API_SECRET to use the alpaca_iex provider.")

    ensure_alpaca_stock_symbol(conn, normalized_symbol)
    status, raw_bars, request_log, request_id = await fetch_stock_bars(normalized_symbol, timeframe, limit)
    candles, invalid_ohlc_count = normalize_stock_bars(normalized_symbol, timeframe, raw_bars)
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
        "sort": "asc",
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
            token = payload.get("next_page_token")
            if not token or not page_bars:
                break
            params["page_token"] = token

    return status, bars[-requested_limit:], request_log, request_id


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
