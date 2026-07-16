from typing import Any

from fastapi import APIRouter, Depends, Query
import psycopg

from app.db import get_connection
from app.services.research_intelligence import (
    build_archive,
    build_research_intelligence,
    collect_evidence,
    ensure_research_snapshot_table,
    filter_archive,
    persist_research_ranking_snapshots,
)

router = APIRouter(tags=["research-intelligence"])


@router.get("/research/intelligence")
def get_research_intelligence(
    persist_snapshot: bool = Query(False),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    report = build_research_intelligence(*load_research_history(conn), **load_research_context(conn))
    if persist_snapshot and report.get("rankings"):
        persist_research_ranking_snapshots(conn, report["rankings"])
        conn.commit()
    return report


@router.post("/research/intelligence/snapshots")
def create_research_intelligence_snapshot(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    report = build_research_intelligence(*load_research_history(conn), **load_research_context(conn))
    persist_research_ranking_snapshots(conn, report["rankings"])
    conn.commit()
    return {
        "created": len(report["rankings"]),
        "calculation_version": report["score_methodology"]["calculation_version"],
        "simulation_only": True,
    }


@router.get("/research/knowledge-graph")
def get_research_knowledge_graph(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    report = build_research_intelligence(*load_research_history(conn))
    return report["knowledge_graph"]


@router.get("/research/timeline")
def get_research_timeline(conn: psycopg.Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    report = build_research_intelligence(*load_research_history(conn))
    return report["timeline"]


@router.get("/research/archive")
def get_research_archive(
    strategy: str | None = Query(None),
    hypothesis: str | None = Query(None),
    indicator: str | None = Query(None),
    asset: str | None = Query(None),
    timeframe: str | None = Query(None),
    market_regime: str | None = Query(None),
    recommendation: str | None = Query(None),
    failure_reason: str | None = Query(None),
    validation_status: str | None = Query(None),
    conn: psycopg.Connection = Depends(get_connection),
) -> list[dict[str, Any]]:
    hypotheses, experiments, _journal_entries, validation_runs = load_research_history(conn)
    evidence = collect_evidence(experiments, validation_runs)
    archive = build_archive(evidence)
    rows = filter_archive(
        archive,
        {
            "strategy": strategy,
            "indicator": indicator,
            "asset": asset,
            "timeframe": timeframe,
            "market_regime": market_regime,
            "recommendation": recommendation,
            "failure_reason": failure_reason,
            "validation_status": validation_status,
        },
    )
    if hypothesis:
        experiment_ids = {
            str(row["id"])
            for row in experiments
            if row.get("hypothesis_id") in {item["id"] for item in hypotheses if hypothesis.lower() in item["hypothesis"].lower() or hypothesis.lower() in item["title"].lower()}
        }
        rows = [row for row in rows if row["evidence_ref"].startswith("experiment:") and row["evidence_ref"].split(":", 1)[1] in experiment_ids]
    return rows


def load_research_history(conn: psycopg.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    hypotheses = conn.execute(
        """
        SELECT id, title, hypothesis, status, tags, created_at, updated_at
        FROM research_hypotheses
        ORDER BY created_at ASC
        """
    ).fetchall()
    experiments = conn.execute(
        """
        SELECT id, hypothesis_id, name, dataset, strategy_name, strategy_version, parameters,
               comparison_plan, evidence_rules, result, recommendation, markdown_report, created_at
        FROM strategy_experiments
        ORDER BY created_at ASC
        """
    ).fetchall()
    journal_entries = conn.execute(
        """
        SELECT id, hypothesis_id, experiment_id, entry_type, dataset, parameters, results, conclusion, next_actions, created_at
        FROM research_journal_entries
        ORDER BY created_at ASC
        """
    ).fetchall()
    validation_runs = conn.execute(
        """
        SELECT id, symbol_set, timeframe_set, candidate_count, thresholds, summary, report, markdown_report, created_at
        FROM alpha_validation_runs
        ORDER BY created_at ASC
        """
    ).fetchall()
    return list(hypotheses), list(experiments), list(journal_entries), list(validation_runs)


def load_research_context(conn: psycopg.Connection) -> dict[str, list[dict[str, Any]]]:
    alerts = conn.execute(
        """
        SELECT *
        FROM evidence_alerts
        WHERE simulation_only = TRUE
        ORDER BY created_at DESC, id DESC
        LIMIT 500
        """
    ).fetchall()
    reviews = conn.execute(
        """
        SELECT *
        FROM signal_reviews
        WHERE simulation_only = TRUE
        ORDER BY created_at DESC, id DESC
        LIMIT 500
        """
    ).fetchall()
    deployments = conn.execute(
        """
        SELECT *
        FROM strategy_deployments
        WHERE simulation_only = TRUE
        ORDER BY created_at DESC, id DESC
        """
    ).fetchall()
    symbols = conn.execute("SELECT * FROM symbols ORDER BY symbol").fetchall()
    latest_candles = conn.execute(
        """
        SELECT DISTINCT ON (symbol, timeframe) symbol, timeframe, timestamp, close, source
        FROM candles
        ORDER BY symbol, timeframe, timestamp DESC
        """
    ).fetchall()
    ensure_research_snapshot_table(conn)
    previous_snapshots = conn.execute(
        """
        SELECT DISTINCT ON (candidate_id) *
        FROM research_ranking_snapshots
        ORDER BY candidate_id, created_at DESC, id DESC
        """
    ).fetchall()
    return {
        "alerts": list(alerts),
        "reviews": list(reviews),
        "deployments": list(deployments),
        "symbols": list(symbols),
        "latest_candles": list(latest_candles),
        "previous_snapshots": list(previous_snapshots),
    }
