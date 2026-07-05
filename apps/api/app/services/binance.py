from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb

from app.settings import settings

BINANCE_KLINES_ENDPOINT = "/api/v3/klines"


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


async def fetch_klines(symbol: str = "BTCUSDT", interval: str = "4h", limit: int = 500) -> tuple[int, list[Any]]:
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    async with httpx.AsyncClient(base_url=settings.binance_base_url, timeout=20) as client:
        response = await client.get(BINANCE_KLINES_ENDPOINT, params=params)
        response.raise_for_status()
        return response.status_code, response.json()


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


async def sync_binance_candles(conn: psycopg.Connection, symbol: str = "BTCUSDT", timeframe: str = "4h", limit: int = 500) -> dict[str, Any]:
    status, raw = await fetch_klines(symbol=symbol, interval=timeframe, limit=limit)
    params = {"symbol": symbol, "interval": timeframe, "limit": limit}
    log_raw_response(conn, BINANCE_KLINES_ENDPOINT, params, status, raw)
    candles = [normalize_kline(symbol, timeframe, row) for row in raw]
    upserted = upsert_candles(conn, candles)
    conn.commit()
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "source": "binance",
        "received": len(candles),
        "upserted": upserted,
        "first_timestamp": candles[0]["timestamp"] if candles else None,
        "last_timestamp": candles[-1]["timestamp"] if candles else None,
    }
