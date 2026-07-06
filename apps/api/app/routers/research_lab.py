from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
import psycopg
from psycopg.types.json import Jsonb

from app.db import get_connection
from app.domain.assets import CRYPTO_VALIDATION_UNIVERSE, VALIDATION_TIMEFRAMES
from app.services.alpha_validation import DEFAULT_VALIDATION_THRESHOLDS, ValidationDataset
from app.services.features import load_candles
from app.services.regimes import load_regimes, sync_market_regimes
from app.services.research_lab import ResearchHypothesis, run_research_experiment

router = APIRouter(tags=["research-lab"])


class HypothesisPayload(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    hypothesis: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)


@router.post("/research/hypotheses")
def create_hypothesis(payload: HypothesisPayload, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    row = conn.execute(
        """
        INSERT INTO research_hypotheses(title, hypothesis, tags)
        VALUES (%s, %s, %s)
        RETURNING id, title, hypothesis, status, tags, created_at, updated_at
        """,
        (payload.title, payload.hypothesis, Jsonb(payload.tags)),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO research_journal_entries(hypothesis_id, experiment_id, entry_type, dataset, parameters, results, conclusion, next_actions)
        VALUES (%s, NULL, 'hypothesis_created', %s, %s, %s, %s, %s)
        """,
        (
            row["id"],
            Jsonb({}),
            Jsonb({"tags": payload.tags}),
            Jsonb({"status": row["status"]}),
            "Hypothesis created and ready for deterministic testing.",
            Jsonb(["Run a reproducible strategy experiment against validated datasets."]),
        ),
    )
    conn.commit()
    return dict(row)


@router.get("/research/hypotheses")
def list_hypotheses(
    q: str | None = Query(None),
    conn: psycopg.Connection = Depends(get_connection),
) -> list[dict[str, Any]]:
    if q:
        rows = conn.execute(
            """
            SELECT id, title, hypothesis, status, tags, created_at, updated_at
            FROM research_hypotheses
            WHERE title ILIKE %s OR hypothesis ILIKE %s OR tags::text ILIKE %s
            ORDER BY created_at DESC
            LIMIT 100
            """,
            (f"%{q}%", f"%{q}%", f"%{q}%"),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, title, hypothesis, status, tags, created_at, updated_at
            FROM research_hypotheses
            ORDER BY created_at DESC
            LIMIT 100
            """
        ).fetchall()
    return list(rows)


@router.post("/research/hypotheses/{hypothesis_id}/experiments")
def run_hypothesis_experiment(
    hypothesis_id: int,
    symbols: list[str] = Query(default=list(CRYPTO_VALIDATION_UNIVERSE)),
    timeframes: list[str] = Query(default=list(VALIDATION_TIMEFRAMES)),
    max_candidates: int = Query(25, ge=1, le=500),
    min_trades: int = Query(100, ge=1, le=10000),
    min_profit_factor: float = Query(1.2, ge=0),
    min_stability_score: float = Query(0.6, ge=0, le=1),
    max_confidence_interval_width: float = Query(0.35, ge=0),
    monte_carlo_runs: int = Query(50, ge=10, le=2000),
    bootstrap_runs: int = Query(50, ge=10, le=2000),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    hypothesis_row = conn.execute(
        """
        SELECT id, title, hypothesis, tags
        FROM research_hypotheses
        WHERE id = %s
        """,
        (hypothesis_id,),
    ).fetchone()
    if not hypothesis_row:
        raise HTTPException(status_code=404, detail="Hypothesis not found.")

    datasets = load_validation_datasets(conn, symbols, timeframes)
    thresholds = {
        **DEFAULT_VALIDATION_THRESHOLDS,
        "min_trades": min_trades,
        "min_profit_factor": min_profit_factor,
        "min_stability_score": min_stability_score,
        "max_confidence_interval_width": max_confidence_interval_width,
    }
    report = run_research_experiment(
        hypothesis=ResearchHypothesis(hypothesis_row["title"], hypothesis_row["hypothesis"], list(hypothesis_row["tags"])),
        datasets=datasets,
        max_candidates=max_candidates,
        thresholds=thresholds,
        monte_carlo_runs=monte_carlo_runs,
        bootstrap_runs=bootstrap_runs,
    )
    experiment_id = persist_experiment(conn, hypothesis_id, report)
    journal_id = persist_journal_entry(conn, hypothesis_id, experiment_id, report)
    update_hypothesis_status(conn, hypothesis_id, report["summary"]["best_recommendation"])
    conn.commit()
    return {"id": experiment_id, "journal_id": journal_id, **report}


@router.get("/research/journal")
def list_research_journal(
    q: str | None = Query(None),
    conn: psycopg.Connection = Depends(get_connection),
) -> list[dict[str, Any]]:
    if q:
        rows = conn.execute(
            """
            SELECT id, hypothesis_id, experiment_id, entry_type, dataset, parameters, results, conclusion, next_actions, created_at
            FROM research_journal_entries
            WHERE conclusion ILIKE %s OR results::text ILIKE %s OR next_actions::text ILIKE %s
            ORDER BY created_at DESC
            LIMIT 100
            """,
            (f"%{q}%", f"%{q}%", f"%{q}%"),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, hypothesis_id, experiment_id, entry_type, dataset, parameters, results, conclusion, next_actions, created_at
            FROM research_journal_entries
            ORDER BY created_at DESC
            LIMIT 100
            """
        ).fetchall()
    return list(rows)


def load_validation_datasets(conn: psycopg.Connection, symbols: list[str], timeframes: list[str]) -> list[ValidationDataset]:
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
    return datasets


def persist_experiment(conn: psycopg.Connection, hypothesis_id: int, report: dict[str, Any]) -> int:
    top = report["leaderboard"][0] if report["leaderboard"] else {}
    row = conn.execute(
        """
        INSERT INTO strategy_experiments(
            hypothesis_id, name, dataset, strategy_name, strategy_version, parameters,
            comparison_plan, evidence_rules, result, recommendation, markdown_report
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            hypothesis_id,
            report["hypothesis"]["title"],
            Jsonb(report["datasets"]),
            top.get("strategy_name", "generated_alpha"),
            top.get("strategy_version", "v1"),
            Jsonb(top.get("parameters", {})),
            Jsonb(top.get("experiment_dimensions", {})),
            Jsonb(top.get("evidence_rules", {})),
            Jsonb(report),
            report["summary"].get("best_recommendation") or "Reject",
            report["markdown_report"],
        ),
    ).fetchone()
    return int(row["id"])


def persist_journal_entry(conn: psycopg.Connection, hypothesis_id: int, experiment_id: int, report: dict[str, Any]) -> int:
    entry = report["journal_entry"]
    row = conn.execute(
        """
        INSERT INTO research_journal_entries(hypothesis_id, experiment_id, entry_type, dataset, parameters, results, conclusion, next_actions)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            hypothesis_id,
            experiment_id,
            entry["entry_type"],
            Jsonb(report["datasets"]),
            Jsonb({"thresholds": report["thresholds"], "candidate_count": report["candidate_count"]}),
            Jsonb(entry["results"]),
            entry["conclusion"],
            Jsonb(entry["next_actions"]),
        ),
    ).fetchone()
    return int(row["id"])


def update_hypothesis_status(conn: psycopg.Connection, hypothesis_id: int, recommendation: str | None) -> None:
    status_by_recommendation = {
        "Reject": "rejected",
        "Research More": "research_more",
        "Candidate for Paper Trading": "candidate_for_paper_trading",
        "Validated Alpha": "validated",
    }
    conn.execute(
        """
        UPDATE research_hypotheses
        SET status = %s, updated_at = NOW()
        WHERE id = %s
        """,
        (status_by_recommendation.get(recommendation or "", "research_more"), hypothesis_id),
    )
