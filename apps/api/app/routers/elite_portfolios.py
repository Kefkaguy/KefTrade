from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.db import get_connection
from app.services.elite_portfolio_repository import (
    PortfolioNotFound,
    PortfolioStale,
    PortfolioStateError,
    approve_run,
    backfill_correlation_evidence,
    create_run,
    get_run,
    options,
    preview_from_database,
    recalculate_run,
)
from app.services.elite_portfolio_activation import PortfolioActivationError, activate_internal
from app.services.champion_validation import (
    ChampionValidationError,
    champion_validation_diagnostics,
    champion_validation_queue,
    champion_validation_run,
    run_champion_validation,
)
from app.services.research_champion_import import import_research_champions, research_champion_status
from app.settings import settings


router = APIRouter(prefix="/research/elite-portfolios", tags=["elite-portfolios"])


class PortfolioConfiguration(BaseModel):
    universe: list[str] = Field(default_factory=list)
    families: list[str] = Field(default_factory=list)
    directions: list[str] = Field(default_factory=lambda: ["long", "short"])
    timeframes: list[str] = Field(default_factory=list)
    thresholds: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    objective: str = "balanced"
    custom_size: int | None = Field(default=None, ge=1, le=20)


class ChampionValidationRequest(BaseModel):
    # The queue query is already bounded to champions in an eligible
    # validation_state, so a caller can request "all of them" via a large
    # limit without needing to know the exact queue size up front. Each
    # champion runs a full battery of backtests, so this is deliberately the
    # slowest endpoint on the page -- see the timeout notes in lib/api.ts and
    # deploy/production/nginx/keftrade.conf.
    limit: int = Field(default=5, ge=1, le=2000)
    elite_candidate_ids: list[int] = Field(default_factory=list)
    # Overrides may only tighten a gate. `run_champion_validation` rejects any
    # value looser than the shipped default rather than quietly accepting it.
    threshold_overrides: dict[str, Any] = Field(default_factory=dict)
    revalidate: bool = False
    require_frozen_datasets: bool = False


class ApprovalRequest(BaseModel):
    snapshot_hash: str = Field(min_length=64, max_length=64)


class ActivationRequest(BaseModel):
    snapshot_hash: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=200)


@router.get("/options")
def get_options(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    return options(conn)


@router.get("/research-champions/status")
def get_research_champion_status(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    return research_champion_status(conn)


@router.post("/research-champions/import")
def import_research_champions_endpoint(
    max_champions: int = Query(25, ge=1, le=5000),
    min_profit_factor: float = Query(1.25, ge=1.0, le=10.0),
    min_trades: int = Query(30, ge=1, le=1000),
    max_drawdown: float = Query(0.12, ge=0.0, le=1.0),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    _require_builder()
    return import_research_champions(
        conn,
        max_champions=max_champions,
        min_profit_factor=min_profit_factor,
        min_trades=min_trades,
        max_drawdown=max_drawdown,
    )


@router.get("/champion-validation/queue")
def get_champion_validation_queue(
    limit: int = Query(25, ge=1, le=200),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    _require_builder()
    return champion_validation_queue(conn, limit=limit)


@router.get("/champion-validation/diagnostics")
def get_champion_validation_diagnostics(
    limit: int = Query(25, ge=1, le=100),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    _require_builder()
    return champion_validation_diagnostics(conn, limit=limit)


@router.post("/champion-validation/run")
def run_champion_validation_endpoint(
    payload: ChampionValidationRequest | None = None,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    _require_builder()
    request = payload or ChampionValidationRequest()
    try:
        return run_champion_validation(
            conn,
            limit=request.limit,
            elite_candidate_ids=request.elite_candidate_ids or None,
            threshold_overrides=request.threshold_overrides or None,
            revalidate=request.revalidate,
            require_frozen=request.require_frozen_datasets,
        )
    except ValueError as error:
        # Weakened or unknown thresholds: a rejected request, never a silent
        # fallback to the shipped defaults.
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/champion-validation/runs/{run_id}")
def get_champion_validation_run(run_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    try:
        return champion_validation_run(conn, run_id)
    except ChampionValidationError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/preview")
def preview_portfolio(payload: PortfolioConfiguration, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    return preview_from_database(conn, payload.model_dump())


@router.post("")
def persist_portfolio(payload: PortfolioConfiguration, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    return create_run(conn, payload.model_dump())


@router.post("/evidence/backfill")
def backfill_portfolio_evidence(limit: int = 20, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    return backfill_correlation_evidence(conn, limit=limit)


@router.get("/{portfolio_id}")
def portfolio_detail(portfolio_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    return _translate(lambda: get_run(conn, portfolio_id))


@router.post("/{portfolio_id}/recalculate")
def recalculate_portfolio(portfolio_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    return _translate(lambda: recalculate_run(conn, portfolio_id))


@router.post("/{portfolio_id}/approve")
def approve_portfolio(portfolio_id: int, payload: ApprovalRequest, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    return _translate(lambda: approve_run(conn, portfolio_id, payload.snapshot_hash))


@router.post("/{portfolio_id}/activate-internal")
def activate_portfolio(portfolio_id: int, payload: ActivationRequest, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    if not settings.elite_portfolio_activation_enabled:
        raise HTTPException(status_code=503, detail="elite portfolio internal activation is disabled")
    try:
        return activate_internal(conn, portfolio_id, payload.idempotency_key, payload.snapshot_hash)
    except PortfolioActivationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _require_builder() -> None:
    if not settings.elite_portfolio_builder_enabled:
        raise HTTPException(status_code=503, detail="elite portfolio builder is disabled")


def _translate(operation):
    try:
        return operation()
    except PortfolioNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PortfolioStale as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except PortfolioStateError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
