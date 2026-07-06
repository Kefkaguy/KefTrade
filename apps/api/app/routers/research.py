from typing import Any

from fastapi import APIRouter, Depends, Query
import psycopg

from app.db import get_connection
from app.domain.assets import DEFAULT_DEV_SYMBOL, DEFAULT_DEV_TIMEFRAME
from app.services.features import load_candles
from app.services.regimes import load_regimes, sync_market_regimes
from app.services.strategy_research import run_strategy_research

router = APIRouter(tags=["strategy-research"])


@router.post("/research/strategies")
def create_strategy_research_report(
    symbol: str = Query(DEFAULT_DEV_SYMBOL),
    timeframe: str = Query(DEFAULT_DEV_TIMEFRAME),
    strategy: str | None = Query(None),
    trend_regime: str | None = Query(None),
    volatility_regime: str | None = Query(None),
    trend_strength_bucket: str | None = Query(None),
    outcome: str | None = Query(None),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    sync_market_regimes(conn, symbol=symbol, timeframe=timeframe)
    candles = load_candles(conn, symbol, timeframe)
    regimes = load_regimes(conn, symbol=symbol, timeframe=timeframe)
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
        regimes=regimes,
        strategy_name=strategy,
        filters={
            "trend_regime": trend_regime or "",
            "volatility_regime": volatility_regime or "",
            "trend_strength_bucket": trend_strength_bucket or "",
            "outcome": outcome or "",
        },
    )
    return {"symbol": symbol, "timeframe": timeframe, **report}
