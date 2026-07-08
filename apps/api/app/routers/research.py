from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
import psycopg

from app.db import get_connection
from app.domain.assets import DEFAULT_DEV_SYMBOL, DEFAULT_DEV_TIMEFRAME
from app.services.candidate_lifecycle import METRIC_DEFINITIONS, build_research_portfolio
from app.services.promising_research import build_promising_research_candidates
from app.services.features import load_candles
from app.services.regimes import load_regimes, sync_market_regimes
from app.services.strategy_experiments import list_strategy_experiments, run_strategy_experiment
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


@router.get("/research/strategy-experiments")
def list_strategy_research_experiments(strategy: str | None = Query(None)) -> list[dict[str, Any]]:
    return list_strategy_experiments(strategy)


@router.get("/research/strategy-experiments/{experiment_id}")
def get_strategy_research_experiment(experiment_id: str) -> dict[str, Any]:
    for experiment in list_strategy_experiments():
        if experiment["id"] == experiment_id:
            return experiment
    raise HTTPException(status_code=404, detail="Strategy experiment not found")


@router.post("/research/strategy-experiments/{experiment_id}")
def run_strategy_research_experiment(
    experiment_id: str,
    symbol: str = Query(DEFAULT_DEV_SYMBOL),
    timeframe: str = Query(DEFAULT_DEV_TIMEFRAME),
    max_runs: int = Query(120, ge=1, le=500),
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
    report = run_strategy_experiment(
        candles=candles,
        features=list(features),
        regimes=regimes,
        experiment_id=experiment_id,
        max_runs=max_runs,
    )
    return {"symbol": symbol, "timeframe": timeframe, **report}


@router.get("/research/promising-candidates")
def get_promising_research_candidates(
    max_candidates: int = Query(36, ge=1, le=120),
    max_runs_per_experiment: int = Query(8, ge=1, le=40),
    fold_count: int = Query(3, ge=1, le=6),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return build_promising_research_candidates(
        conn,
        max_candidates=max_candidates,
        max_runs_per_experiment=max_runs_per_experiment,
        fold_count=fold_count,
    )


@router.get("/research/portfolio")
def get_research_portfolio(
    max_candidates: int = Query(24, ge=1, le=80),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return build_research_portfolio(conn, max_candidates=max_candidates)


@router.get("/research/metric-definitions")
def get_research_metric_definitions() -> dict[str, Any]:
    return METRIC_DEFINITIONS
