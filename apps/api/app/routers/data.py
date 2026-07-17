from typing import Any
import time

from fastapi import APIRouter, Depends, HTTPException, Query
import psycopg

from app.db import get_connection
from app.domain.assets import DEFAULT_DEV_PROVIDER, DEFAULT_DEV_SYMBOL, DEFAULT_DEV_TIMEFRAME
from app.observability import elapsed_ms, log_event, log_exception
from app.providers.alpaca import sync_alpaca_stock_assets
from app.providers.registry import get_market_data_provider

router = APIRouter(tags=["market-data"])


@router.post("/data/alpaca/assets/sync")
async def sync_alpaca_assets(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    started = time.perf_counter()
    log_event("Alpaca asset catalog sync started")
    try:
        result = await sync_alpaca_stock_assets(conn)
        log_event("Alpaca asset catalog sync finished", imported=result.get("imported"), elapsed_ms=elapsed_ms(started))
        return result
    except (RuntimeError, ValueError) as error:
        log_exception("Alpaca asset catalog sync failed", error, elapsed_ms=elapsed_ms(started))
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/data/sync")
async def sync_data(
    symbol: str = Query(DEFAULT_DEV_SYMBOL),
    timeframe: str = Query(DEFAULT_DEV_TIMEFRAME),
    provider: str = Query(DEFAULT_DEV_PROVIDER),
    limit: int = Query(1500, ge=1, le=5000),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    started = time.perf_counter()
    log_event("Starting asset sync", asset=symbol, provider=provider, timeframe=timeframe, retry_count=0)
    try:
        market_data_provider = get_market_data_provider(provider)
        download_started = time.perf_counter()
        log_event("Download start", asset=symbol, provider=provider, timeframe=timeframe)
        result = await market_data_provider.sync_candles(conn, symbol=symbol, timeframe=timeframe, limit=limit)
        log_event("Download finished", asset=symbol, provider=provider, timeframe=timeframe, candles_received=result.candle_count, elapsed_ms=elapsed_ms(download_started))
        log_event("Asset sync complete", asset=symbol, provider=provider, timeframe=timeframe, candles_inserted=result.candle_count, elapsed_ms=elapsed_ms(started))
        return result.__dict__
    except (KeyError, RuntimeError, ValueError) as error:
        log_exception("Asset sync failure", error, asset=symbol, provider=provider, timeframe=timeframe, retry_count=0, elapsed_ms=elapsed_ms(started))
        raise HTTPException(status_code=400, detail=str(error)) from error


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
