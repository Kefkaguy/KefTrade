from typing import Any
import time

from fastapi import APIRouter, Depends, Query
import psycopg

from app.db import get_connection
from app.domain.assets import DEFAULT_DEV_SYMBOL, DEFAULT_DEV_TIMEFRAME
from app.observability import elapsed_ms, log_event, log_exception
from app.services.features import sync_features

router = APIRouter(tags=["features"])


@router.post("/features/sync")
def calculate_and_store_features(
    symbol: str = Query(DEFAULT_DEV_SYMBOL),
    timeframe: str = Query(DEFAULT_DEV_TIMEFRAME),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    started = time.perf_counter()
    log_event("Feature generation started", asset=symbol, timeframe=timeframe)
    try:
        result = sync_features(conn, symbol=symbol, timeframe=timeframe)
        log_event("Features calculated", asset=symbol, timeframe=timeframe, features=result.get("usable"), elapsed_ms=elapsed_ms(started))
        log_event("Features committed", asset=symbol, timeframe=timeframe, elapsed_ms=elapsed_ms(started))
        return result
    except Exception as error:
        log_exception("Feature generation failed", error, asset=symbol, timeframe=timeframe, elapsed_ms=elapsed_ms(started))
        raise


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
