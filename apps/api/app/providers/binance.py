from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb

from app.domain.market_data import MarketDataSyncResult
from app.settings import settings

BINANCE_KLINES_ENDPOINT = "/api/v3/klines"
TIMEFRAME_MS = {
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}


class BinanceMarketDataProvider:
    name = "binance_dev"

    async def sync_candles(self, conn: psycopg.Connection, symbol: str, timeframe: str, limit: int) -> MarketDataSyncResult:
        result = await sync_binance_candles(conn, symbol=symbol, timeframe=timeframe, limit=limit)
        return MarketDataSyncResult(
            symbol=result["symbol"],
            timeframe=result["timeframe"],
            provider=self.name,
            received=result["received"],
            upserted=result["upserted"],
            candle_count=result["candle_count"],
            missing_intervals=result["missing_intervals"],
            duplicate_count=result["duplicate_count"],
            incomplete_latest_candle_excluded=result["incomplete_latest_candle_excluded"],
            first_timestamp=result["first_timestamp"],
            last_timestamp=result["last_timestamp"],
        )


def _dt_from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def normalize_kline(symbol: str, timeframe: str, raw: list[Any]) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "source": "binance",
        "timeframe": timeframe,
        "timestamp": _dt_from_ms(raw[0]),
        "open": Decimal(raw[1]),
        "high": Decimal(raw[2]),
        "low": Decimal(raw[3]),
        "close": Decimal(raw[4]),
        "volume": Decimal(raw[5]),
    }


async def fetch_klines_page(
    symbol: str = "BTCUSDT",
    interval: str = "4h",
    limit: int = 1000,
    end_time: int | None = None,
) -> tuple[int, list[Any], dict[str, Any]]:
    params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
    if end_time is not None:
        params["endTime"] = end_time
    async with httpx.AsyncClient(base_url=settings.binance_base_url, timeout=20) as client:
        response = await client.get(BINANCE_KLINES_ENDPOINT, params=params)
        response.raise_for_status()
        return response.status_code, response.json(), params


async def fetch_klines(symbol: str = "BTCUSDT", interval: str = "4h", limit: int = 1500) -> tuple[int, list[Any], list[dict[str, Any]], int]:
    remaining = limit
    end_time: int | None = None
    all_rows: list[Any] = []
    request_log: list[dict[str, Any]] = []
    status = 200

    while remaining > 0:
        page_limit = min(1000, remaining)
        status, page, params = await fetch_klines_page(symbol=symbol, interval=interval, limit=page_limit, end_time=end_time)
        request_log.append({"params": params, "received": len(page)})
        if not page:
            break
        all_rows = page + all_rows
        remaining -= len(page)
        if len(page) < page_limit:
            break
        end_time = int(page[0][0]) - 1

    duplicate_count = count_duplicate_raw_klines(all_rows)
    deduped = dedupe_raw_klines(all_rows)
    return status, deduped[-limit:], request_log, duplicate_count


def dedupe_raw_klines(rows: list[Any]) -> list[Any]:
    by_open_time = {int(row[0]): row for row in rows}
    return [by_open_time[key] for key in sorted(by_open_time)]


def exclude_incomplete_latest(raw: list[Any], now_ms: int | None = None) -> tuple[list[Any], bool]:
    if not raw:
        return raw, False
    now_ms = now_ms if now_ms is not None else int(datetime.now(tz=UTC).timestamp() * 1000)
    latest_close_time = int(raw[-1][6])
    if latest_close_time > now_ms:
        return raw[:-1], True
    return raw, False


def count_duplicate_raw_klines(raw: list[Any]) -> int:
    seen: set[int] = set()
    duplicates = 0
    for row in raw:
        open_time = int(row[0])
        if open_time in seen:
            duplicates += 1
        seen.add(open_time)
    return duplicates


def detect_missing_intervals(candles: list[dict[str, Any]], timeframe: str = "4h") -> int:
    interval_ms = TIMEFRAME_MS.get(timeframe)
    if interval_ms is None or len(candles) < 2:
        return 0
    missing = 0
    ordered = sorted(candles, key=lambda candle: candle["timestamp"])
    for previous, current in zip(ordered, ordered[1:]):
        previous_ms = int(previous["timestamp"].timestamp() * 1000)
        current_ms = int(current["timestamp"].timestamp() * 1000)
        gap = current_ms - previous_ms
        if gap > interval_ms:
            missing += (gap // interval_ms) - 1
    return missing


def log_raw_response(
    conn: psycopg.Connection,
    endpoint: str,
    params: dict[str, Any],
    status: int,
    body: Any,
) -> None:
    conn.execute(
        """
        INSERT INTO raw_api_logs(source, endpoint, request_params, response_status, response_body)
        VALUES ('binance', %s, %s, %s, %s)
        """,
        (endpoint, Jsonb(params), status, Jsonb(body)),
    )


def upsert_candles(conn: psycopg.Connection, candles: list[dict[str, Any]]) -> int:
    if not candles:
        return 0

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


async def sync_binance_candles(conn: psycopg.Connection, symbol: str = "BTCUSDT", timeframe: str = "4h", limit: int = 1500) -> dict[str, Any]:
    status, raw_with_possible_incomplete, request_log, duplicate_count = await fetch_klines(symbol=symbol, interval=timeframe, limit=limit)
    raw, incomplete_excluded = exclude_incomplete_latest(raw_with_possible_incomplete)
    params = {"symbol": symbol, "interval": timeframe, "limit": limit, "requests": request_log}
    log_raw_response(conn, BINANCE_KLINES_ENDPOINT, params, status, raw_with_possible_incomplete)
    candles = [normalize_kline(symbol, timeframe, row) for row in raw]
    missing_intervals = detect_missing_intervals(candles, timeframe)
    upserted = upsert_candles(conn, candles)
    conn.commit()
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "source": "binance",
        "received": len(candles),
        "upserted": upserted,
        "candle_count": len(candles),
        "missing_intervals": missing_intervals,
        "duplicate_count": duplicate_count,
        "incomplete_latest_candle_excluded": incomplete_excluded,
        "first_timestamp": candles[0]["timestamp"] if candles else None,
        "last_timestamp": candles[-1]["timestamp"] if candles else None,
    }
