from typing import Any

from fastapi import APIRouter, Depends, Query
import psycopg

from app.db import get_connection
from app.services.broker_read_models import broker_account, broker_clock, broker_orders, broker_positions, broker_reconciliation, broker_status, execution_attempts, execution_readiness
from app.services.elite_repair_generator import elite_repair_proposals

router = APIRouter(prefix="/broker", tags=["external-paper-broker"])


@router.get("/status")
def get_broker_status(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return broker_status(conn)


@router.get("/account")
def get_broker_account(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return broker_account(conn)


@router.get("/clock")
def get_broker_clock(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return broker_clock(conn)


@router.get("/orders")
def get_broker_orders(limit: int = Query(25, ge=1, le=500), conn: psycopg.Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    return broker_orders(conn, limit)


@router.get("/positions")
def get_broker_positions(conn: psycopg.Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    return broker_positions(conn)


@router.get("/reconciliation")
def get_broker_reconciliation(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return broker_reconciliation(conn)


@router.get("/execution-readiness")
def get_execution_readiness(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return execution_readiness(conn)


@router.get("/execution-attempts")
def get_execution_attempts(limit: int = Query(25, ge=1, le=500), conn: psycopg.Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    return execution_attempts(conn, limit)


@router.get("/elite-repair-proposals")
def get_elite_repair_proposals(limit: int = Query(50, ge=1, le=100), conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return elite_repair_proposals(conn, limit)
