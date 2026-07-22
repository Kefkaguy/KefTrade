from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db import get_connection
from app.services.elite_portfolio_repository import (
    PortfolioNotFound,
    PortfolioStale,
    PortfolioStateError,
    approve_run,
    create_run,
    get_run,
    options,
    preview_from_database,
    recalculate_run,
)
from app.services.elite_portfolio_activation import PortfolioActivationError, activate_internal
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


class ApprovalRequest(BaseModel):
    snapshot_hash: str = Field(min_length=64, max_length=64)


class ActivationRequest(BaseModel):
    snapshot_hash: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=200)


@router.get("/options")
def get_options(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    return options(conn)


@router.post("/preview")
def preview_portfolio(payload: PortfolioConfiguration, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    return preview_from_database(conn, payload.model_dump())


@router.post("")
def persist_portfolio(payload: PortfolioConfiguration, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    return create_run(conn, payload.model_dump())


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
