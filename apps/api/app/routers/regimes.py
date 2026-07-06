from typing import Any

from fastapi import APIRouter, Depends, Query
import psycopg

from app.db import get_connection
from app.domain.assets import DEFAULT_DEV_SYMBOL, DEFAULT_DEV_TIMEFRAME
from app.services.regimes import load_regimes, sync_market_regimes

router = APIRouter(tags=["market-regimes"])


@router.post("/regimes/sync")
def calculate_and_store_regimes(
    symbol: str = Query(DEFAULT_DEV_SYMBOL),
    timeframe: str = Query(DEFAULT_DEV_TIMEFRAME),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return sync_market_regimes(conn, symbol=symbol, timeframe=timeframe)


@router.get("/regimes/{symbol}")
def get_regimes(
    symbol: str,
    timeframe: str = Query(DEFAULT_DEV_TIMEFRAME),
    limit: int = Query(500, ge=1, le=5000),
    conn: psycopg.Connection = Depends(get_connection),
) -> list[dict[str, Any]]:
    rows = load_regimes(conn, symbol=symbol, timeframe=timeframe)
    return rows[-limit:]
