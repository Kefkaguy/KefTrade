from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
import psycopg

from app.db import get_connection
from app.services.evidence_alerts import acknowledge_evidence_alert, list_evidence_alerts
from app.services.paper_trading import (
    PaperTradingError,
    account_balances,
    cancel_order,
    create_deployment,
    create_order,
    create_paper_account,
    ensure_tsla_momentum_bull_deployment,
    get_deployment,
    list_accounts,
    list_deployments,
    list_equity_curve,
    list_fills,
    list_execution_logs,
    list_orders,
    list_positions,
    pause_deployment,
    process_pending_orders,
    reconcile_account,
    run_deployment_scan,
)
from app.services.paper_scheduler import get_scheduler_status, run_scheduled_scan_once, update_scheduler_status
from app.services.signal_reviews import add_signal_review_note, generate_signal_review, latest_signal_review, list_signal_reviews, mark_signal_review

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
    stop_loss_price: Decimal | None = Field(default=None, gt=0)
    take_profit_price: Decimal | None = Field(default=None, gt=0)


class ReconcileRequest(BaseModel):
    repair: bool = False


class StrategyDeploymentCreate(BaseModel):
    account_id: int
    strategy_name: str
    symbol: str
    timeframe: str = "1d"
    strategy_version: str = "v1"
    parameters: dict[str, Any] = Field(default_factory=dict)


class SchedulerUpdate(BaseModel):
    enabled: bool | None = None
    cadence: str | None = None


class SignalReviewNote(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


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
            stop_loss_price=payload.stop_loss_price,
            take_profit_price=payload.take_profit_price,
        )
    except PaperTradingError as error:
        raise paper_error(error) from error


@router.get("/accounts/{account_id}/fills")
def get_fills(account_id: int, conn: psycopg.Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    return list_fills(conn, account_id)


@router.post("/orders/{order_id}/cancel")
def cancel_pending_order(order_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    try:
        return cancel_order(conn, order_id)
    except PaperTradingError as error:
        raise paper_error(error) from error


@router.post("/orders/process")
def process_orders(account_id: int | None = Query(None), conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    try:
        return process_pending_orders(conn, account_id)
    except PaperTradingError as error:
        raise paper_error(error) from error


@router.get("/accounts/{account_id}/execution-logs")
def get_execution_logs(account_id: int, limit: int = Query(200, ge=1, le=1000), conn: psycopg.Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    return list_execution_logs(conn, account_id, limit)


@router.post("/accounts/{account_id}/reconcile")
def reconcile_paper_account(account_id: int, payload: ReconcileRequest, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    try:
        return reconcile_account(conn, account_id, payload.repair)
    except PaperTradingError as error:
        raise paper_error(error) from error


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


@router.post("/deployments/tsla-momentum-bull")
def deploy_tsla_momentum_bull(account_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    try:
        return ensure_tsla_momentum_bull_deployment(conn, account_id)
    except PaperTradingError as error:
        raise paper_error(error) from error


@router.post("/deployments/{deployment_id}/scan")
async def scan_strategy_deployment(deployment_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    try:
        return await run_deployment_scan(conn, deployment_id)
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


@router.get("/scheduler")
def get_paper_scheduler(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return get_scheduler_status(conn)


@router.get("/alerts")
def get_evidence_alerts(
    limit: int = Query(100, ge=1, le=500),
    include_acknowledged: bool = Query(True),
    conn: psycopg.Connection = Depends(get_connection),
) -> list[dict[str, Any]]:
    return list_evidence_alerts(conn, limit=limit, include_acknowledged=include_acknowledged)


@router.get("/signal-reviews")
def get_signal_reviews(
    account_id: int | None = Query(None),
    limit: int = Query(25, ge=1, le=100),
    conn: psycopg.Connection = Depends(get_connection),
) -> list[dict[str, Any]]:
    return list_signal_reviews(conn, account_id=account_id, limit=limit)


@router.get("/signal-reviews/latest")
def get_latest_signal_review(account_id: int | None = Query(None), conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any] | None:
    return latest_signal_review(conn, account_id=account_id)


@router.post("/deployments/{deployment_id}/signal-review")
def generate_deployment_signal_review(deployment_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    try:
        return generate_signal_review(conn, get_deployment(conn, deployment_id))
    except (PaperTradingError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/signal-reviews/{review_id}/mark-reviewed")
def mark_reviewed(review_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    try:
        return mark_signal_review(conn, review_id, "reviewed")
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/signal-reviews/{review_id}/ignore")
def ignore_review(review_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    try:
        return mark_signal_review(conn, review_id, "ignored")
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/signal-reviews/{review_id}/send-to-paper-simulation")
def send_review_to_paper_simulation(review_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    try:
        return mark_signal_review(conn, review_id, "sent_to_paper_simulation")
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/signal-reviews/{review_id}/note")
def add_review_note(review_id: int, payload: SignalReviewNote, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    try:
        return add_signal_review_note(conn, review_id, payload.note)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/alerts/{alert_id}/acknowledge")
def acknowledge_alert(alert_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    try:
        return acknowledge_evidence_alert(conn, alert_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.put("/scheduler")
def update_paper_scheduler(payload: SchedulerUpdate, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    try:
        return update_scheduler_status(conn, enabled=payload.enabled, cadence=payload.cadence)
    except PaperTradingError as error:
        raise paper_error(error) from error


@router.post("/scheduler/run")
async def run_paper_scheduler_now(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    try:
        return await run_scheduled_scan_once(conn, force=True)
    except PaperTradingError as error:
        raise paper_error(error) from error
