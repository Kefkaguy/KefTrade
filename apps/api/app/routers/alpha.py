from typing import Any

from fastapi import APIRouter, Depends, Query
import psycopg

from app.db import get_connection
from app.domain.assets import DEFAULT_DEV_SYMBOL, DEFAULT_DEV_TIMEFRAME
from app.services.alpha_discovery import run_alpha_discovery
from app.services.features import load_candles
from app.services.regimes import load_regimes, sync_market_regimes

router = APIRouter(tags=["alpha-discovery"])


@router.post("/alpha/discover")
def discover_alpha(
    symbol: str = Query(DEFAULT_DEV_SYMBOL),
    timeframe: str = Query(DEFAULT_DEV_TIMEFRAME),
    max_candidates: int = Query(250, ge=1, le=5000),
    monte_carlo_runs: int = Query(200, ge=10, le=2000),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    sync_market_regimes(conn, symbol=symbol, timeframe=timeframe)
    candles = load_candles(conn, symbol=symbol, timeframe=timeframe)
    features = conn.execute(
        """
        SELECT *
        FROM features
        WHERE symbol = %s AND timeframe = %s
        ORDER BY timestamp ASC
        """,
        (symbol, timeframe),
    ).fetchall()
    regimes = load_regimes(conn, symbol=symbol, timeframe=timeframe)
    report = run_alpha_discovery(
        candles=candles,
        features=list(features),
        regimes=regimes,
        max_candidates=max_candidates,
        monte_carlo_runs=monte_carlo_runs,
    )
    return {"symbol": symbol, "timeframe": timeframe, **report}
