from typing import Any

from fastapi import APIRouter, Depends, Query
import psycopg

from app.db import get_connection
from app.services.binance import sync_binance_candles

router = APIRouter(tags=["market-data"])


@router.post("/data/sync")
async def sync_data(
    symbol: str = Query("BTCUSDT"),
    timeframe: str = Query("4h"),
    limit: int = Query(500, ge=1, le=1000),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return await sync_binance_candles(conn, symbol=symbol, timeframe=timeframe, limit=limit)


@router.get("/candles/{symbol}")
def get_candles(
    symbol: str,
    timeframe: str = Query("4h"),
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

