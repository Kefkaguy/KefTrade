from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Query

from app.db import get_connection
from app.services.strategy_diagnostics import diagnostics_summary, elite_deployment_audit, list_evaluations
from app.services.portfolio_risk import portfolio_readiness
from app.services.shared_cache import get_or_load_json

router = APIRouter(tags=["strategy-diagnostics"])


@router.get("/strategy-diagnostics")
def get_strategy_diagnostics(
    deployment_id: int | None = Query(None),
    limit: int = Query(25, ge=1, le=100),
    cursor: int | None = Query(None, ge=1),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return list_evaluations(conn, deployment_id=deployment_id, limit=limit, cursor=cursor)


@router.get("/strategy-diagnostics/summary")
def get_strategy_diagnostics_summary(
    deployment_id: int | None = Query(None),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    key = f"summary:strategy-diagnostics:{deployment_id or 'all'}"
    return get_or_load_json(key, 60, lambda: diagnostics_summary(conn, deployment_id=deployment_id))


@router.get("/elite-deployments/audit")
def get_elite_deployment_audit(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    key = "summary:elite-deployment-audit"
    return get_or_load_json(key, 60, lambda: elite_deployment_audit(conn))


@router.get("/portfolio/readiness")
def get_portfolio_readiness(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return portfolio_readiness(conn)
