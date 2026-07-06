from typing import Any

from fastapi import APIRouter, Depends, Query
import psycopg

from app.db import get_connection
from app.domain.assets import DEFAULT_DEV_SYMBOL, DEFAULT_DEV_TIMEFRAME
from app.services.features import load_candles, sync_features
from app.services.strategy import get_strategy_version
from app.services.strategy_research import run_strategy_research

router = APIRouter(tags=["strategy-research"])


@router.post("/research/strategies")
def create_strategy_research_report(
    symbol: str = Query(DEFAULT_DEV_SYMBOL),
    timeframe: str = Query(DEFAULT_DEV_TIMEFRAME),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    sync_features(conn, symbol=symbol, timeframe=timeframe)
    strategy = get_strategy_version(conn)
    candles = load_candles(conn, symbol, timeframe)
    features = conn.execute(
        """
        SELECT *
        FROM features
        WHERE symbol = %s AND timeframe = %s
        ORDER BY timestamp ASC
        """,
        (symbol, timeframe),
    ).fetchall()
    report = run_strategy_research(
        candles=candles,
        features=list(features),
        strategy_name=strategy["name"],
        strategy_version=strategy["version"],
        base_params=strategy["parameters"],
    )
    return {"symbol": symbol, "timeframe": timeframe, **report}
