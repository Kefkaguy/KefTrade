from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from app.services.paper_trading import PaperTradingError, log_event, run_deployment_scan

ALLOWED_DEPLOYMENT_CADENCES = {"scheduler", "manual", "15m", "30m", "60m", "daily"}
CADENCE_DELTAS = {
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "60m": timedelta(minutes=60),
    "daily": timedelta(days=1),
}
_DEPLOYMENT_MANAGEMENT_SCHEMA_READY = False


def ensure_deployment_management_schema(conn: psycopg.Connection) -> None:
    global _DEPLOYMENT_MANAGEMENT_SCHEMA_READY
    if _DEPLOYMENT_MANAGEMENT_SCHEMA_READY:
        return
    if deployment_management_schema_ready(conn):
        _DEPLOYMENT_MANAGEMENT_SCHEMA_READY = True
        return
    raise RuntimeError("deployment management schema is missing; apply database migrations")


def deployment_management_schema_ready(conn: psycopg.Connection) -> bool:
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM information_schema.columns
            WHERE table_name = 'strategy_deployments'
              AND column_name IN (
                'scan_cadence',
                'max_simulated_exposure_pct',
                'health_status',
                'health_checked_at',
                'resumed_at'
              )
            """
        ).fetchone()
        if int((row or {}).get("count") or 0) < 5:
            return False
        constraints = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM pg_constraint
            WHERE conrelid = 'strategy_deployments'::regclass
              AND conname IN (
                'strategy_deployments_scan_cadence_check',
                'strategy_deployments_exposure_limit_check'
              )
            """
        ).fetchone()
        return int((constraints or {}).get("count") or 0) >= 2
    except Exception:
        return False


def deployment_due_for_scheduler(deployment: dict[str, Any], now: datetime | None = None) -> bool:
    if deployment.get("status") != "active" or deployment.get("simulation_only") is not True:
        return False
    cadence = deployment.get("scan_cadence") or "scheduler"
    if cadence == "manual":
        return False
    if cadence == "scheduler":
        return True
    delta = CADENCE_DELTAS.get(cadence)
    if delta is None:
        return True
    last_scan_at = deployment.get("last_scan_at")
    if last_scan_at is None:
        return True
    if isinstance(last_scan_at, str):
        last_scan_at = datetime.fromisoformat(last_scan_at.replace("Z", "+00:00"))
    if last_scan_at.tzinfo is None:
        last_scan_at = last_scan_at.replace(tzinfo=UTC)
    return (now or datetime.now(UTC)) - last_scan_at >= delta


def resume_deployment(conn: psycopg.Connection, deployment_id: int) -> dict[str, Any]:
    ensure_deployment_management_schema(conn)
    row = conn.execute(
        """
        UPDATE strategy_deployments
        SET status = 'active',
            paused_at = NULL,
            resumed_at = NOW(),
            updated_at = NOW()
        WHERE id = %s
          AND simulation_only = TRUE
        RETURNING *
        """,
        (deployment_id,),
    ).fetchone()
    if not row:
        raise PaperTradingError("strategy deployment not found or is not simulation-only")
    log_event(conn, row["account_id"], row["id"], None, "paper_deployment_resumed", "Resumed simulation-only strategy deployment.", dict(row))
    conn.commit()
    return dict(row)


def update_deployment_controls(
    conn: psycopg.Connection,
    deployment_id: int,
    scan_cadence: str | None = None,
    max_simulated_exposure_pct: Decimal | None = None,
) -> dict[str, Any]:
    ensure_deployment_management_schema(conn)
    current = conn.execute(
        "SELECT * FROM strategy_deployments WHERE id = %s AND simulation_only = TRUE",
        (deployment_id,),
    ).fetchone()
    if not current:
        raise PaperTradingError("strategy deployment not found or is not simulation-only")
    next_cadence = scan_cadence or current.get("scan_cadence") or "scheduler"
    if next_cadence not in ALLOWED_DEPLOYMENT_CADENCES:
        raise PaperTradingError("scan_cadence must be scheduler, manual, 15m, 30m, 60m, or daily")
    next_exposure = Decimal(max_simulated_exposure_pct if max_simulated_exposure_pct is not None else current.get("max_simulated_exposure_pct") or Decimal("0.10"))
    if next_exposure <= 0 or next_exposure > 1:
        raise PaperTradingError("max_simulated_exposure_pct must be greater than 0 and no more than 1")
    row = conn.execute(
        """
        UPDATE strategy_deployments
        SET scan_cadence = %s,
            max_simulated_exposure_pct = %s,
            updated_at = NOW()
        WHERE id = %s
          AND simulation_only = TRUE
        RETURNING *
        """,
        (next_cadence, next_exposure, deployment_id),
    ).fetchone()
    log_event(
        conn,
        row["account_id"],
        row["id"],
        None,
        "paper_deployment_controls_updated",
        "Updated simulation deployment controls.",
        {"scan_cadence": next_cadence, "max_simulated_exposure_pct": str(next_exposure), "simulation_only": True},
    )
    conn.commit()
    return dict(row)


def bulk_pause_deployments(conn: psycopg.Connection, deployment_ids: list[int] | None = None) -> dict[str, Any]:
    deployments = list_simulation_deployments(conn)
    selected = _selected_deployments(deployments, deployment_ids)
    paused: list[dict[str, Any]] = []
    for deployment in selected:
        if deployment.get("status") != "active":
            continue
        row = conn.execute(
            """
            UPDATE strategy_deployments
            SET status = 'paused',
                paused_at = NOW(),
                updated_at = NOW()
            WHERE id = %s
              AND status = 'active'
              AND simulation_only = TRUE
            RETURNING *
            """,
            (deployment["id"],),
        ).fetchone()
        if row:
            paused.append(dict(row))
            log_event(conn, row["account_id"], row["id"], None, "paper_deployment_bulk_paused", "Bulk paused simulation deployment.", dict(row))
    conn.commit()
    return {"requested": len(selected), "paused": len(paused), "deployments": paused, "simulation_only": True}


async def bulk_scan_deployments(conn: psycopg.Connection, deployment_ids: list[int] | None = None) -> dict[str, Any]:
    deployments = list_simulation_deployments(conn)
    selected = [row for row in _selected_deployments(deployments, deployment_ids) if row.get("status") == "active"]
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for deployment in selected:
        try:
            result = await run_deployment_scan(conn, int(deployment["id"]))
            results.append({"deployment_id": deployment["id"], "symbol": deployment["symbol"], "action": result["action"], "message": result["message"], "simulation_only": True})
        except Exception as error:  # noqa: BLE001 - bulk operation must continue per deployment
            errors.append({"deployment_id": deployment["id"], "symbol": deployment.get("symbol"), "error": str(error), "simulation_only": True})
            log_event(conn, deployment.get("account_id"), deployment.get("id"), None, "paper_deployment_bulk_scan_error", str(error), errors[-1])
            conn.commit()
    return {"requested": len(selected), "completed": len(results), "failed": len(errors), "results": results, "errors": errors, "simulation_only": True}


def build_deployment_management(conn: psycopg.Connection) -> dict[str, Any]:
    ensure_deployment_management_schema(conn)
    deployments = list_simulation_deployments(conn)
    accounts = list_accounts(conn)
    positions = list_positions(conn)
    orders = list_orders(conn)
    fills = list_fills(conn)
    logs = list_logs(conn)
    alerts = list_alerts(conn)
    positions_by_key = {(row.get("account_id"), row.get("symbol")): row for row in positions}
    orders_by_deployment = group_by(orders, "deployment_id")
    fills_by_deployment = group_by(fills, "deployment_id")
    alerts_by_symbol = group_by(alerts, "symbol")
    logs_by_deployment = group_by(logs, "deployment_id")
    market_value_by_account: dict[int, Decimal] = defaultdict(Decimal)
    for position in positions:
        market_value_by_account[int(position.get("account_id"))] += Decimal(position.get("market_value") or 0)
    account_by_id = {
        row["id"]: {**row, "equity": Decimal(row.get("cash_balance") or 0) + market_value_by_account[int(row["id"])]}
        for row in accounts
    }
    positions_by_account = group_by(positions, "account_id")
    orders_by_account = group_by(orders, "account_id")
    fills_by_account = group_by(fills, "account_id")
    logs_by_account = group_by(logs, "account_id")
    account_snapshots = [
        {
            "account": account,
            "balances": account_balance_snapshot(account, positions_by_account.get(account.get("id"), [])),
            "positions": positions_by_account.get(account.get("id"), []),
            "orders": orders_by_account.get(account.get("id"), []),
            "fills": fills_by_account.get(account.get("id"), []),
            "logs": logs_by_account.get(account.get("id"), []),
            "equity": [],
        }
        for account in accounts
    ]

    managed = []
    for deployment in deployments:
        position = positions_by_key.get((deployment.get("account_id"), deployment.get("symbol")))
        account = account_by_id.get(deployment.get("account_id"), {})
        account_equity = Decimal(account.get("equity") or 0)
        market_value = Decimal(position.get("market_value") or 0) if position else Decimal("0")
        exposure_pct = market_value / account_equity if account_equity > 0 else Decimal("0")
        deployment_alerts = _deployment_alerts(deployment, alerts_by_symbol.get(deployment.get("symbol"), []))
        conflicts = detect_deployment_conflicts(deployment, deployments, positions, account_equity)
        health = classify_deployment_health(deployment, deployment_alerts, conflicts)
        managed.append(
            {
                **deployment,
                "scan_cadence": deployment.get("scan_cadence") or "scheduler",
                "max_simulated_exposure_pct": deployment.get("max_simulated_exposure_pct") or Decimal("0.10"),
                "health_status": health["status"],
                "health_detail": health["detail"],
                "position": position,
                "exposure_pct": exposure_pct,
                "orders_count": len(orders_by_deployment.get(deployment.get("id"), [])),
                "fills_count": len(fills_by_deployment.get(deployment.get("id"), [])),
                "latest_alert": deployment_alerts[0] if deployment_alerts else None,
                "audit_events": logs_by_deployment.get(deployment.get("id"), [])[:6],
                "conflicts": conflicts,
                "performance": {
                    "realized_pnl": position.get("realized_pnl", Decimal("0")) if position else Decimal("0"),
                    "unrealized_pnl": position.get("unrealized_pnl", Decimal("0")) if position else Decimal("0"),
                    "market_value": market_value,
                    "exposure_pct": exposure_pct,
                    "orders": len(orders_by_deployment.get(deployment.get("id"), [])),
                    "fills": len(fills_by_deployment.get(deployment.get("id"), [])),
                    "last_signal": deployment.get("last_signal"),
                    "last_scan_at": deployment.get("last_scan_at"),
                },
            }
        )

    conflicts = [conflict for deployment in managed for conflict in deployment["conflicts"]]
    return {
        "generated_at": datetime.now(UTC),
        "simulation_only": True,
        "safety": "Simulation-only deployment management. No live broker routing is enabled.",
        "summary": {
            "deployment_count": len(managed),
            "active_count": sum(1 for row in managed if row.get("status") == "active"),
            "paused_count": sum(1 for row in managed if row.get("status") == "paused"),
            "healthy_count": sum(1 for row in managed if row.get("health_status") == "Healthy"),
            "warning_count": sum(1 for row in managed if row.get("health_status") == "Warning"),
            "error_count": sum(1 for row in managed if row.get("health_status") == "Error"),
            "conflict_count": len(conflicts),
        },
        "portfolio_risk": portfolio_risk_summary(accounts, positions, managed, conflicts),
        "deployments": managed,
        "conflicts": conflicts,
        "asset_comparison": compare_by(managed, "symbol"),
        "strategy_comparison": compare_by(managed, "strategy_name"),
        "audit_history": logs[:30],
        "accounts": accounts,
        "positions": positions,
        "orders": orders,
        "fills": fills,
        "alerts": alerts,
        "logs": logs,
        "account_snapshots": account_snapshots,
    }


def list_simulation_deployments(conn: psycopg.Connection) -> list[dict[str, Any]]:
    ensure_deployment_management_schema(conn)
    return list(conn.execute("SELECT * FROM strategy_deployments WHERE simulation_only = TRUE ORDER BY created_at DESC, id DESC").fetchall())


def list_accounts(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM paper_accounts WHERE simulation_only = TRUE ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


def list_positions(conn: psycopg.Connection) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT p.*, c.close AS last_price, (p.quantity * c.close) AS market_value, ((c.close - p.average_price) * p.quantity) AS unrealized_pnl
            FROM paper_positions p
            LEFT JOIN LATERAL (
              SELECT close FROM candles WHERE symbol = p.symbol ORDER BY timestamp DESC LIMIT 1
            ) c ON TRUE
            WHERE p.simulation_only = TRUE
            ORDER BY p.symbol
            """
        ).fetchall()
    except Exception:
        rows = conn.execute("SELECT * FROM paper_positions WHERE simulation_only = TRUE ORDER BY symbol").fetchall()
    return [dict(row) for row in rows]


def list_orders(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return list(conn.execute("SELECT * FROM paper_orders WHERE simulation_only = TRUE ORDER BY submitted_at DESC, id DESC").fetchall())


def list_fills(conn: psycopg.Connection) -> list[dict[str, Any]]:
    try:
        return list(
            conn.execute(
                """
                SELECT f.*, o.deployment_id
                FROM paper_fills f
                LEFT JOIN paper_orders o ON o.id = f.order_id
                WHERE f.simulation_only = TRUE
                ORDER BY f.filled_at DESC, f.id DESC
                """
            ).fetchall()
        )
    except Exception:
        return list(conn.execute("SELECT * FROM paper_fills WHERE simulation_only = TRUE ORDER BY filled_at DESC, id DESC").fetchall())


def list_logs(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return list(
        conn.execute(
            """
            SELECT *
            FROM execution_logs
            WHERE simulation_only = TRUE
              AND (
                deployment_id IS NOT NULL
                OR event_type LIKE 'paper_scheduler%%'
                OR event_type LIKE 'paper_deployment%%'
              )
            ORDER BY created_at DESC, id DESC
            LIMIT 200
            """
        ).fetchall()
    )


def list_alerts(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return list(conn.execute("SELECT * FROM evidence_alerts WHERE simulation_only = TRUE ORDER BY created_at DESC, id DESC LIMIT 200").fetchall())


def group_by(rows: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row.get(key)].append(row)
    return grouped


def account_balance_snapshot(account: dict[str, Any], positions: list[dict[str, Any]]) -> dict[str, Any]:
    market_value = sum((Decimal(row.get("market_value") or 0) for row in positions), Decimal("0"))
    unrealized_pnl = sum((Decimal(row.get("unrealized_pnl") or 0) for row in positions), Decimal("0"))
    cash_balance = Decimal(account.get("cash_balance") or 0)
    return {
        **account,
        "market_value": market_value,
        "unrealized_pnl": unrealized_pnl,
        "equity": cash_balance + market_value,
    }


def _selected_deployments(deployments: list[dict[str, Any]], deployment_ids: list[int] | None) -> list[dict[str, Any]]:
    if deployment_ids is None:
        return deployments
    wanted = {int(value) for value in deployment_ids}
    return [row for row in deployments if int(row["id"]) in wanted]


def _deployment_alerts(deployment: dict[str, Any], alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    strategy_prefix = f"{deployment.get('strategy_name')}_{deployment.get('strategy_version')}"
    return [
        alert for alert in alerts
        if alert.get("timeframe") == deployment.get("timeframe")
        and str(alert.get("strategy_id") or "").startswith(strategy_prefix)
    ]


def classify_deployment_health(deployment: dict[str, Any], alerts: list[dict[str, Any]], conflicts: list[dict[str, Any]]) -> dict[str, str]:
    if deployment.get("status") == "paused":
        return {"status": "Paused", "detail": "Deployment is paused by user control."}
    if deployment.get("status") != "active":
        return {"status": "Warning", "detail": f"Deployment status is {deployment.get('status')}."}
    critical_alert = next((alert for alert in alerts if alert.get("severity") == "critical"), None)
    if critical_alert:
        return {"status": "Error", "detail": critical_alert.get("evidence_summary") or critical_alert.get("alert_type") or "Critical alert present."}
    if any(conflict.get("severity") == "critical" for conflict in conflicts):
        return {"status": "Error", "detail": "Critical deployment conflict detected."}
    if str(deployment.get("last_signal") or "").lower() == "stale_data_warning":
        return {"status": "Warning", "detail": "Latest scan was blocked by stale data."}
    if alerts:
        return {"status": "Warning", "detail": alerts[0].get("evidence_summary") or alerts[0].get("alert_type") or "Recent deployment alert."}
    if not deployment.get("last_scan_at"):
        return {"status": "Warning", "detail": "Deployment has not completed a scan yet."}
    return {"status": "Healthy", "detail": "Deployment is active with no blocking alerts."}


def detect_deployment_conflicts(
    deployment: dict[str, Any],
    deployments: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    account_equity: Decimal,
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    if deployment.get("status") != "active":
        return conflicts
    same_asset = [
        row for row in deployments
        if row.get("id") != deployment.get("id")
        and row.get("status") == "active"
        and row.get("simulation_only") is True
        and row.get("symbol") == deployment.get("symbol")
    ]
    if same_asset:
        conflicts.append({
            "type": "shared_asset_exposure",
            "severity": "warning",
            "deployment_id": deployment.get("id"),
            "symbol": deployment.get("symbol"),
            "message": f"{len(same_asset) + 1} active deployments target {deployment.get('symbol')}.",
            "related_deployment_ids": [row.get("id") for row in same_asset],
        })
    same_asset_timeframe = [row for row in same_asset if row.get("timeframe") == deployment.get("timeframe")]
    if same_asset_timeframe:
        conflicts.append({
            "type": "duplicate_asset_timeframe",
            "severity": "warning",
            "deployment_id": deployment.get("id"),
            "symbol": deployment.get("symbol"),
            "message": f"Multiple active deployments share {deployment.get('symbol')} {deployment.get('timeframe')}.",
            "related_deployment_ids": [row.get("id") for row in same_asset_timeframe],
        })
    position = next((row for row in positions if row.get("account_id") == deployment.get("account_id") and row.get("symbol") == deployment.get("symbol")), None)
    if position and account_equity > 0:
        exposure_pct = Decimal(position.get("market_value") or 0) / account_equity
        limit = Decimal(deployment.get("max_simulated_exposure_pct") or Decimal("0.10"))
        if exposure_pct > limit:
            conflicts.append({
                "type": "exposure_limit_breach",
                "severity": "critical",
                "deployment_id": deployment.get("id"),
                "symbol": deployment.get("symbol"),
                "message": f"Simulated exposure {exposure_pct:.2%} exceeds deployment limit {limit:.2%}.",
                "exposure_pct": exposure_pct,
                "limit_pct": limit,
            })
    return conflicts


def portfolio_risk_summary(
    accounts: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    deployments: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
) -> dict[str, Any]:
    cash = sum(Decimal(row.get("cash_balance") or 0) for row in accounts)
    realized = sum(Decimal(row.get("realized_pnl") or 0) for row in accounts)
    market_value = sum(Decimal(row.get("market_value") or 0) for row in positions)
    unrealized = sum(Decimal(row.get("unrealized_pnl") or 0) for row in positions)
    equity = cash + market_value
    gross_exposure_pct = market_value / equity if equity > 0 else Decimal("0")
    return {
        "cash": cash,
        "equity": equity,
        "market_value": market_value,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "gross_exposure_pct": gross_exposure_pct,
        "open_positions": sum(1 for row in positions if Decimal(row.get("quantity") or 0) > 0),
        "active_deployments": sum(1 for row in deployments if row.get("status") == "active"),
        "conflict_count": len(conflicts),
        "exposure_limit_breaches": sum(1 for row in conflicts if row.get("type") == "exposure_limit_breach"),
        "top_positions": sorted(positions, key=lambda row: Decimal(row.get("market_value") or 0), reverse=True)[:5],
        "simulation_only": True,
    }


def compare_by(deployments: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for deployment in deployments:
        grouped[str(deployment.get(key) or "unknown")].append(deployment)
    rows = []
    for label, group in grouped.items():
        health_counts = Counter(row.get("health_status") for row in group)
        rows.append(
            {
                "name": label,
                "deployment_count": len(group),
                "active_count": sum(1 for row in group if row.get("status") == "active"),
                "paused_count": sum(1 for row in group if row.get("status") == "paused"),
                "healthy_count": health_counts.get("Healthy", 0),
                "warning_count": health_counts.get("Warning", 0),
                "error_count": health_counts.get("Error", 0),
                "orders": sum(int(row.get("orders_count") or 0) for row in group),
                "fills": sum(int(row.get("fills_count") or 0) for row in group),
                "unrealized_pnl": sum(Decimal(row.get("performance", {}).get("unrealized_pnl") or 0) for row in group),
                "realized_pnl": sum(Decimal(row.get("performance", {}).get("realized_pnl") or 0) for row in group),
            }
        )
    return sorted(rows, key=lambda row: (row["active_count"], row["deployment_count"]), reverse=True)
