from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg

from app.services.external_execution import feature_flags
from app.settings import settings


def broker_status(conn: psycopg.Connection) -> dict[str, Any]:
    account = latest_account(conn)
    sync = latest_row(conn, "broker_sync_runs", "completed_at")
    reconciliation = latest_row(conn, "broker_reconciliation_runs", "completed_at")
    adapter = latest_row(conn, "broker_adapter_releases", "created_at")
    active_halts = rows(conn, "SELECT * FROM execution_halts WHERE cleared_at IS NULL ORDER BY severity DESC, last_seen_at DESC LIMIT 100")
    deployments = rows(conn, "SELECT * FROM external_paper_deployments ORDER BY updated_at DESC")
    epochs = rows(conn, "SELECT * FROM external_execution_epochs ORDER BY activated_at DESC LIMIT 100")
    shadows = rows(conn, "SELECT * FROM shadow_executions ORDER BY created_at DESC LIMIT 50")
    return {
        "provider": "alpaca",
        "environment": "paper",
        "feature_flags": feature_flags(),
        "execution_enabled": False,
        "order_submission_implemented": False,
        "account": account,
        "latest_sync": sync,
        "latest_reconciliation": reconciliation,
        "adapter": adapter,
        "active_halts": active_halts,
        "deployments": deployments,
        "epochs": epochs,
        "shadow_executions": shadows,
        "generated_at": datetime.now(UTC),
    }


def broker_account(conn: psycopg.Connection) -> dict[str, Any]:
    account = latest_account(conn)
    state = dict(conn.execute("SELECT * FROM broker_account_state ORDER BY updated_at DESC LIMIT 1").fetchone() or {})
    return {"account": account, "state": state, "allocated_capital": settings.broker_allocated_capital, "buying_power_used_for_sizing": False, "environment": "paper"}


def broker_clock(conn: psycopg.Connection) -> dict[str, Any]:
    return dict(conn.execute("SELECT * FROM broker_clock_state ORDER BY updated_at DESC LIMIT 1").fetchone() or {})


def broker_orders(conn: psycopg.Connection, limit: int = 100) -> list[dict[str, Any]]:
    return rows(conn, "SELECT * FROM broker_orders ORDER BY updated_at DESC LIMIT %s", (limit,))


def broker_positions(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return rows(conn, "SELECT * FROM broker_positions WHERE quantity > 0 ORDER BY symbol")


def broker_reconciliation(conn: psycopg.Connection) -> dict[str, Any]:
    run = latest_row(conn, "broker_reconciliation_runs", "completed_at")
    findings = rows(conn, "SELECT * FROM broker_reconciliation_findings WHERE resolved_at IS NULL ORDER BY severity DESC, created_at DESC LIMIT 200")
    return {"latest_run": run, "unresolved_findings": findings, "clean": bool(run and run.get("status") == "clean" and not findings)}


def execution_readiness(conn: psycopg.Connection) -> dict[str, Any]:
    status = broker_status(conn)
    deployments = status["deployments"]
    return {
        "execution_enabled": False,
        "highest_reachable_state": "enabled_observe_only",
        "eligible_deployments": [row for row in deployments if row.get("state") == "enabled_observe_only"],
        "blocked_deployments": [row for row in deployments if row.get("state") in {"readiness_blocked", "risk_halted", "reconciliation_halted", "manually_halted", "invalidated"}],
        "active_halts": status["active_halts"],
        "feature_flags": status["feature_flags"],
        "submission_proof": {"broker_order_submission_enabled": False, "external_paper_execution_enabled": False, "adapter_submit_order": "raises BrokerMutationDisabled", "enabled_execution_database_state": "prohibited by constraint"},
    }


def latest_account(conn: psycopg.Connection) -> dict[str, Any]:
    return dict(conn.execute("SELECT * FROM broker_accounts ORDER BY last_successful_sync_at DESC NULLS LAST, created_at DESC LIMIT 1").fetchone() or {})


def latest_row(conn: psycopg.Connection, table: str, timestamp_column: str) -> dict[str, Any]:
    return dict(conn.execute(f"SELECT * FROM {table} ORDER BY {timestamp_column} DESC NULLS LAST LIMIT 1").fetchone() or {})


def rows(conn: psycopg.Connection, query: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, params or ()).fetchall()]
