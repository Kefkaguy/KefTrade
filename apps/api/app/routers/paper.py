from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
import psycopg

from app.db import get_connection
from app.services.paper_trading import (
    PaperTradingError,
    account_balances,
    create_deployment,
    create_order,
    create_paper_account,
    list_accounts,
    list_deployments,
    list_equity_curve,
    list_fills,
    list_orders,
    list_positions,
    pause_deployment,
)

router = APIRouter(prefix="/paper", tags=["paper-trading"])


class PaperAccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    starting_cash: Decimal = Field(gt=0)
    base_currency: str = Field(default="USD", max_length=8)


class PaperOrderCreate(BaseModel):
    account_id: int
    symbol: str
    quantity: Decimal = Field(gt=0)
    side: str = "buy"
    order_type: str = "market"
    timeframe: str = "1d"
    limit_price: Decimal | None = None
    deployment_id: int | None = None


class StrategyDeploymentCreate(BaseModel):
    account_id: int
    strategy_name: str
    symbol: str
    timeframe: str = "1d"
    strategy_version: str = "v1"
    parameters: dict[str, Any] = Field(default_factory=dict)


def paper_error(error: PaperTradingError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(error))


@router.post("/accounts")
def create_account(payload: PaperAccountCreate, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    try:
        return create_paper_account(conn, payload.name, payload.starting_cash, payload.base_currency)
    except PaperTradingError as error:
        raise paper_error(error) from error


@router.get("/accounts")
def get_accounts(conn: psycopg.Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    return list_accounts(conn)


@router.get("/accounts/{account_id}/balances")
def get_balances(account_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    try:
        return account_balances(conn, account_id)
    except PaperTradingError as error:
        raise paper_error(error) from error


@router.get("/accounts/{account_id}/positions")
def get_positions(account_id: int, conn: psycopg.Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    return list_positions(conn, account_id)


@router.get("/accounts/{account_id}/orders")
def get_orders(account_id: int, conn: psycopg.Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    return list_orders(conn, account_id)


@router.post("/orders")
def submit_order(payload: PaperOrderCreate, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    try:
        return create_order(
            conn,
            account_id=payload.account_id,
            symbol=payload.symbol,
            quantity=payload.quantity,
            side=payload.side,
            order_type=payload.order_type,
            timeframe=payload.timeframe,
            limit_price=payload.limit_price,
            deployment_id=payload.deployment_id,
        )
    except PaperTradingError as error:
        raise paper_error(error) from error


@router.get("/accounts/{account_id}/fills")
def get_fills(account_id: int, conn: psycopg.Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    return list_fills(conn, account_id)


@router.get("/accounts/{account_id}/equity-curve")
def get_equity_curve(account_id: int, conn: psycopg.Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    return list_equity_curve(conn, account_id)


@router.post("/deployments")
def create_strategy_deployment(payload: StrategyDeploymentCreate, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    try:
        return create_deployment(
            conn,
            account_id=payload.account_id,
            strategy_name=payload.strategy_name,
            strategy_version=payload.strategy_version,
            symbol=payload.symbol,
            timeframe=payload.timeframe,
            parameters=payload.parameters,
        )
    except PaperTradingError as error:
        raise paper_error(error) from error


@router.post("/deployments/{deployment_id}/pause")
def pause_strategy_deployment(deployment_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    try:
        return pause_deployment(conn, deployment_id)
    except PaperTradingError as error:
        raise paper_error(error) from error


@router.get("/deployments")
def get_deployments(account_id: int | None = Query(None), conn: psycopg.Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    return list_deployments(conn, account_id)
