from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
import psycopg
from psycopg.types.json import Jsonb

from app.db import get_connection
from app.domain.assets import CRYPTO_VALIDATION_UNIVERSE, VALIDATION_TIMEFRAMES
from app.services.alpha_validation import DEFAULT_VALIDATION_THRESHOLDS, ValidationDataset, run_alpha_validation
from app.services.features import load_candles
from app.services.regimes import load_regimes, sync_market_regimes

router = APIRouter(tags=["alpha-validation"])


@router.post("/alpha/validate")
def validate_alpha(
    symbols: list[str] = Query(default=list(CRYPTO_VALIDATION_UNIVERSE)),
    timeframes: list[str] = Query(default=list(VALIDATION_TIMEFRAMES)),
    max_candidates: int = Query(50, ge=1, le=1000),
    min_trades: int = Query(100, ge=1, le=10000),
    min_profit_factor: float = Query(1.2, ge=0),
    min_stability_score: float = Query(0.6, ge=0, le=1),
    max_confidence_interval_width: float = Query(0.35, ge=0),
    monte_carlo_runs: int = Query(200, ge=10, le=2000),
    bootstrap_runs: int = Query(200, ge=10, le=2000),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    datasets = []
    for symbol in symbols:
        for timeframe in timeframes:
            sync_market_regimes(conn, symbol=symbol, timeframe=timeframe)
            candles = load_candles(conn, symbol=symbol, timeframe=timeframe)
            if not candles:
                continue
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
            datasets.append(ValidationDataset(symbol=symbol, timeframe=timeframe, candles=candles, features=list(features), regimes=regimes))

    thresholds = {
        **DEFAULT_VALIDATION_THRESHOLDS,
        "min_trades": min_trades,
        "min_profit_factor": min_profit_factor,
        "min_stability_score": min_stability_score,
        "max_confidence_interval_width": max_confidence_interval_width,
    }
    report = run_alpha_validation(
        datasets=datasets,
        max_candidates=max_candidates,
        monte_carlo_runs=monte_carlo_runs,
        bootstrap_runs=bootstrap_runs,
        thresholds=thresholds,
    )
    run_id = persist_validation_run(conn, symbols, timeframes, max_candidates, thresholds, report)
    return {"id": run_id, "symbols": symbols, "timeframes": timeframes, **report}


@router.get("/alpha/validation-runs")
def list_validation_runs(conn: psycopg.Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, symbol_set, timeframe_set, candidate_count, thresholds, summary, created_at
        FROM alpha_validation_runs
        ORDER BY created_at DESC
        LIMIT 50
        """
    ).fetchall()
    return list(rows)


@router.get("/alpha/validation-runs/{run_id}")
def get_validation_run(run_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, symbol_set, timeframe_set, candidate_count, thresholds, summary, report, markdown_report, created_at
        FROM alpha_validation_runs
        WHERE id = %s
        """,
        (run_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Validation run not found")
    return dict(row)


def persist_validation_run(
    conn: psycopg.Connection,
    symbols: list[str],
    timeframes: list[str],
    candidate_count: int,
    thresholds: dict[str, Any],
    report: dict[str, Any],
) -> int:
    row = conn.execute(
        """
        INSERT INTO alpha_validation_runs(symbol_set, timeframe_set, candidate_count, thresholds, summary, report, markdown_report)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            Jsonb(symbols),
            Jsonb(timeframes),
            candidate_count,
            Jsonb(thresholds),
            Jsonb(report["summary"]),
            Jsonb(report),
            report["markdown_report"],
        ),
    ).fetchone()
    conn.commit()
    return int(row["id"])
