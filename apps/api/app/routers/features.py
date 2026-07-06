from typing import Any

from fastapi import APIRouter, Depends, Query
import psycopg

from app.db import get_connection
from app.domain.assets import DEFAULT_DEV_SYMBOL, DEFAULT_DEV_TIMEFRAME
from app.services.features import sync_features

router = APIRouter(tags=["features"])


@router.post("/features/sync")
def calculate_and_store_features(
    symbol: str = Query(DEFAULT_DEV_SYMBOL),
    timeframe: str = Query(DEFAULT_DEV_TIMEFRAME),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return sync_features(conn, symbol=symbol, timeframe=timeframe)


@router.get("/features/{symbol}")
def get_features(
    symbol: str,
    timeframe: str = Query(DEFAULT_DEV_TIMEFRAME),
    limit: int = Query(300, ge=1, le=1000),
    conn: psycopg.Connection = Depends(get_connection),
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM features
        WHERE symbol = %s AND timeframe = %s
        ORDER BY timestamp DESC
        LIMIT %s
        """,
        (symbol, timeframe, limit),
    ).fetchall()
    return list(reversed(rows))
