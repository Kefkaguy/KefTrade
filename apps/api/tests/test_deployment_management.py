from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.deployment_management import (
    build_deployment_management,
    bulk_pause_deployments,
    deployment_due_for_scheduler,
    resume_deployment,
    update_deployment_controls,
)
from app.services.paper_trading import PaperTradingError


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class DeploymentConn:
    def __init__(self):
        now = datetime.now(UTC)
        self.deployments = [
            {
                "id": 1,
                "account_id": 1,
                "strategy_name": "momentum",
                "strategy_version": "bull_v2",
                "symbol": "TSLA",
                "timeframe": "1h",
                "parameters": {},
                "status": "active",
                "simulation_only": True,
                "created_at": now - timedelta(days=3),
                "last_scan_at": now - timedelta(hours=2),
                "last_signal": "setup",
                "scan_cadence": "scheduler",
                "max_simulated_exposure_pct": Decimal("0.10"),
            },
            {
                "id": 2,
                "account_id": 1,
                "strategy_name": "mean_reversion",
                "strategy_version": "v1",
                "symbol": "TSLA",
                "timeframe": "1h",
                "parameters": {},
                "status": "active",
                "simulation_only": True,
                "created_at": now - timedelta(days=2),
                "last_scan_at": now - timedelta(hours=1),
                "last_signal": "avoid",
                "scan_cadence": "60m",
                "max_simulated_exposure_pct": Decimal("0.01"),
            },
            {
                "id": 3,
                "account_id": 1,
                "strategy_name": "breakout",
                "strategy_version": "v1",
                "symbol": "AAPL",
                "timeframe": "1d",
                "parameters": {},
                "status": "paused",
                "simulation_only": True,
                "created_at": now - timedelta(days=1),
                "last_scan_at": None,
                "last_signal": None,
                "scan_cadence": "manual",
                "max_simulated_exposure_pct": Decimal("0.15"),
            },
            {"id": 99, "account_id": 1, "strategy_name": "live", "strategy_version": "broker", "symbol": "TSLA", "timeframe": "1h", "status": "active", "simulation_only": False, "created_at": now},
        ]
        self.accounts = [{"id": 1, "cash_balance": Decimal("9000"), "realized_pnl": Decimal("25"), "simulation_only": True, "created_at": now}]
        self.positions = [{"account_id": 1, "symbol": "TSLA", "quantity": Decimal("2"), "average_price": Decimal("100"), "realized_pnl": Decimal("25"), "market_value": Decimal("500"), "unrealized_pnl": Decimal("300"), "simulation_only": True}]
        self.orders = [{"id": 1, "account_id": 1, "deployment_id": 1, "symbol": "TSLA", "status": "filled", "submitted_at": now, "simulation_only": True}]
        self.fills = [{"id": 1, "order_id": 1, "deployment_id": 1, "account_id": 1, "symbol": "TSLA", "filled_at": now, "simulation_only": True}]
        self.alerts = [{"id": 1, "symbol": "TSLA", "timeframe": "1h", "strategy_id": "momentum_bull_v2", "severity": "info", "alert_type": "entry_setup_review", "evidence_summary": "Setup review.", "created_at": now, "simulation_only": True}]
        self.logs = []
        self.commits = 0

    def execute(self, query, params=None):
        if "INSERT INTO execution_logs" in query:
            self.logs.append({"event_type": params[3], "deployment_id": params[1], "message": params[4], "created_at": datetime.now(UTC), "simulation_only": True})
            return Result([])
        if "SELECT * FROM strategy_deployments WHERE id = %s AND simulation_only = TRUE" in query:
            return Result([row for row in self.deployments if row["id"] == params[0] and row["simulation_only"] is True])
        if "UPDATE strategy_deployments" in query and "status = 'active'" in query and "resumed_at = NOW()" in query:
            row = next((item for item in self.deployments if item["id"] == params[0] and item["simulation_only"] is True), None)
            if not row:
                return Result([])
            row["status"] = "active"
            row["paused_at"] = None
            row["resumed_at"] = datetime.now(UTC)
            return Result([row])
        if "UPDATE strategy_deployments" in query and "scan_cadence = %s" in query:
            row = next((item for item in self.deployments if item["id"] == params[2] and item["simulation_only"] is True), None)
            row["scan_cadence"] = params[0]
            row["max_simulated_exposure_pct"] = params[1]
            return Result([row])
        if "UPDATE strategy_deployments" in query and "paper_deployment_bulk_paused" not in query:
            row = next((item for item in self.deployments if item["id"] == params[0] and item["simulation_only"] is True and item["status"] == "active"), None)
            if not row:
                return Result([])
            row["status"] = "paused"
            row["paused_at"] = datetime.now(UTC)
            return Result([row])
        if "FROM strategy_deployments" in query:
            return Result([row for row in self.deployments if row.get("simulation_only") is True])
        if "FROM paper_accounts" in query:
            return Result(self.accounts)
        if "FROM paper_positions" in query:
            if "LEFT JOIN LATERAL" in query:
                raise AssertionError("force fallback")
            return Result(self.positions)
        if "FROM paper_orders" in query:
            return Result(self.orders)
        if "FROM paper_fills" in query:
            if "LEFT JOIN paper_orders" in query:
                raise AssertionError("force fallback")
            return Result(self.fills)
        if "FROM execution_logs" in query:
            return Result(self.logs)
        if "FROM evidence_alerts" in query:
            return Result(self.alerts)
        raise AssertionError(query)

    def commit(self):
        self.commits += 1


def test_deployment_cadence_due_logic() -> None:
    now = datetime(2026, 1, 2, 12, tzinfo=UTC)

    assert deployment_due_for_scheduler({"status": "active", "simulation_only": True, "scan_cadence": "manual"}, now) is False
    assert deployment_due_for_scheduler({"status": "active", "simulation_only": True, "scan_cadence": "scheduler"}, now) is True
    assert deployment_due_for_scheduler({"status": "active", "simulation_only": True, "scan_cadence": "60m", "last_scan_at": now - timedelta(minutes=30)}, now) is False
    assert deployment_due_for_scheduler({"status": "active", "simulation_only": True, "scan_cadence": "60m", "last_scan_at": now - timedelta(minutes=61)}, now) is True


def test_resume_deployment_is_simulation_only_and_audited() -> None:
    conn = DeploymentConn()
    conn.deployments[2]["status"] = "paused"

    resumed = resume_deployment(conn, 3)

    assert resumed["status"] == "active"
    assert resumed["simulation_only"] is True
    assert any(log["event_type"] == "paper_deployment_resumed" for log in conn.logs)


def test_update_deployment_controls_validates_bounds() -> None:
    conn = DeploymentConn()

    updated = update_deployment_controls(conn, 1, scan_cadence="30m", max_simulated_exposure_pct=Decimal("0.20"))

    assert updated["scan_cadence"] == "30m"
    assert updated["max_simulated_exposure_pct"] == Decimal("0.20")
    with pytest.raises(PaperTradingError):
        update_deployment_controls(conn, 1, scan_cadence="live", max_simulated_exposure_pct=Decimal("0.20"))
    with pytest.raises(PaperTradingError):
        update_deployment_controls(conn, 1, scan_cadence="30m", max_simulated_exposure_pct=Decimal("1.50"))


def test_bulk_pause_only_active_simulation_deployments() -> None:
    conn = DeploymentConn()

    result = bulk_pause_deployments(conn, [1, 2, 3, 99])

    assert result["paused"] == 2
    assert conn.deployments[0]["status"] == "paused"
    assert conn.deployments[1]["status"] == "paused"
    assert conn.deployments[2]["status"] == "paused"
    assert conn.deployments[3]["status"] == "active"


def test_deployment_management_detects_conflicts_and_risk_summary() -> None:
    snapshot = build_deployment_management(DeploymentConn())

    assert snapshot["simulation_only"] is True
    assert snapshot["summary"]["deployment_count"] == 3
    assert snapshot["summary"]["conflict_count"] >= 1
    assert any(conflict["type"] == "shared_asset_exposure" for conflict in snapshot["conflicts"])
    assert any(conflict["type"] == "exposure_limit_breach" for conflict in snapshot["conflicts"])
    assert snapshot["portfolio_risk"]["open_positions"] == 1
    assert snapshot["asset_comparison"][0]["name"] == "TSLA"
