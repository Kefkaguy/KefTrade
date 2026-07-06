from typing import Any

from fastapi import APIRouter, Depends, Query
import psycopg

from app.db import get_connection
from app.domain.assets import DEFAULT_DEV_PROVIDER, DEFAULT_DEV_SYMBOL, DEFAULT_DEV_TIMEFRAME
from app.providers.registry import get_market_data_provider

router = APIRouter(tags=["market-data"])


@router.post("/data/sync")
async def sync_data(
    symbol: str = Query(DEFAULT_DEV_SYMBOL),
    timeframe: str = Query(DEFAULT_DEV_TIMEFRAME),
    provider: str = Query(DEFAULT_DEV_PROVIDER),
    limit: int = Query(1500, ge=1, le=5000),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    market_data_provider = get_market_data_provider(provider)
    result = await market_data_provider.sync_candles(conn, symbol=symbol, timeframe=timeframe, limit=limit)
    return result.__dict__


@router.get("/candles/{symbol}")
def get_candles(
    symbol: str,
    timeframe: str = Query(DEFAULT_DEV_TIMEFRAME),
    limit: int = Query(300, ge=1, le=1000),
    conn: psycopg.Connection = Depends(get_connection),
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT symbol, source, timeframe, timestamp, open, high, low, close, volume
        FROM candles
        WHERE symbol = %s AND timeframe = %s
        ORDER BY timestamp DESC
        LIMIT %s
        """,
        (symbol, timeframe, limit),
    ).fetchall()
    return list(reversed(rows))
