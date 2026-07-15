from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.paper_trading import cancel_order, create_deployment, create_order, create_paper_account, pause_deployment, process_pending_orders


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self):
        self.accounts = {}
        self.orders = {}
        self.fills = {}
        self.positions = {}
        self.deployments = {}
        self.equity = []
        self.logs = []
        self.commits = 0
        self.scheduler = {
            "id": True,
            "enabled": True,
            "cadence": "60m",
            "last_run_at": None,
            "next_run_at": datetime.now(UTC) - timedelta(minutes=1),
            "latest_result": None,
            "latest_error": None,
            "is_running": False,
            "running_since": None,
            "updated_at": datetime.now(UTC),
        }
        self.candle = {
            "symbol": "AAPL",
            "timeframe": "1d",
            "timestamp": "2026-01-02T00:00:00Z",
            "open": Decimal("100"),
            "high": Decimal("104"),
            "low": Decimal("99"),
            "close": Decimal("100"),
            "volume": Decimal("1000000"),
        }

    def execute(self, query, params=None):
        if "INSERT INTO paper_accounts" in query:
            row = {
                "id": len(self.accounts) + 1,
                "name": params[0],
                "base_currency": params[1],
                "starting_cash": params[2],
                "cash_balance": params[3],
                "realized_pnl": Decimal("0"),
                "status": "active",
                "simulation_only": True,
            }
            self.accounts[row["id"]] = row
            return Result([row])
        if "SELECT * FROM paper_accounts WHERE id" in query:
            return Result([self.accounts[params[0]]] if params[0] in self.accounts else [])
        if "INSERT INTO execution_logs" in query:
            self.logs.append({"event_type": params[3], "simulation_only": True})
            return Result([])
        if "SELECT * FROM paper_scan_scheduler WHERE id = TRUE" in query:
            return Result([self.scheduler])
        if "INSERT INTO paper_scan_scheduler" in query:
            return Result([self.scheduler])
        if "UPDATE paper_scan_scheduler" in query and "is_running = TRUE" in query:
            self.scheduler["is_running"] = True
            self.scheduler["running_since"] = datetime.now(UTC)
            self.scheduler["latest_error"] = None
            return Result([self.scheduler])
        if "UPDATE paper_scan_scheduler" in query and "last_run_at = NOW()" in query:
            self.scheduler["last_run_at"] = datetime.now(UTC)
            self.scheduler["next_run_at"] = params[0]
            self.scheduler["latest_result"] = params[1]
            self.scheduler["latest_error"] = params[2]
            self.scheduler["is_running"] = False
            self.scheduler["running_since"] = None
            return Result([self.scheduler])
        if "UPDATE paper_scan_scheduler" in query and "latest_result = %s" in query:
            self.scheduler["latest_result"] = params[0]
            self.scheduler["latest_error"] = None
            return Result([self.scheduler])
        if "UPDATE paper_scan_scheduler" in query and "enabled = %s" in query:
            self.scheduler["enabled"] = params[0]
            self.scheduler["cadence"] = params[1]
            self.scheduler["next_run_at"] = params[2]
            self.scheduler["latest_error"] = None
            return Result([self.scheduler])
        if "SELECT * FROM paper_positions WHERE account_id" in query and "AND symbol" in query:
            return Result([self.positions[(params[0], params[1])]] if (params[0], params[1]) in self.positions else [])
        if "SELECT * FROM paper_positions WHERE account_id" in query:
            return Result([row for (account_id, _symbol), row in self.positions.items() if account_id == params[0]])
        if "INSERT INTO paper_equity_curve" in query:
            row = {
                "id": len(self.equity) + 1,
                "account_id": params[0],
                "cash_balance": params[1],
                "equity": params[2],
                "unrealized_pnl": params[3],
                "realized_pnl": params[4],
            }
            self.equity.append(row)
            return Result([row])
        if "SELECT symbol, timeframe, timestamp" in query:
            return Result([self.candle])
        if "INSERT INTO paper_orders" in query:
            if "parent_order_id" in query:
                row = {
                    "id": len(self.orders) + 1, "account_id": params[0], "deployment_id": params[1],
                    "symbol": params[2], "timeframe": params[3], "side": "sell", "order_type": params[4],
                    "quantity": params[5], "trigger_price": params[6], "parent_order_id": params[7],
                    "status": "pending", "simulation_only": True,
                }
                self.orders[row["id"]] = row
                return Result([row])
            row = {
                "id": len(self.orders) + 1,
                "account_id": params[0],
                "deployment_id": params[1],
                "symbol": params[2],
                "timeframe": params[3],
                "side": params[4],
                "order_type": params[5],
                "quantity": params[6],
                "limit_price": params[7],
                "status": params[8],
                "rejected_reason": params[9],
                "stop_loss_price": params[10],
                "take_profit_price": params[11],
                "simulation_only": True,
            }
            self.orders[row["id"]] = row
            return Result([row])
        if "SELECT * FROM paper_orders WHERE id" in query:
            return Result([self.orders[params[0]]] if params[0] in self.orders else [])
        if "SELECT * FROM paper_orders WHERE status = 'pending'" in query:
            rows = [row for row in self.orders.values() if row["status"] == "pending"]
            if params:
                rows = [row for row in rows if row["account_id"] == params[0]]
            return Result(rows)
        if "SELECT * FROM strategy_deployments WHERE id" in query:
            return Result([self.deployments[params[0]]] if params[0] in self.deployments else [])
        if "UPDATE paper_orders SET status = 'canceled'" in query and "parent_order_id" not in query:
            row = self.orders.get(params[0])
            if not row or row["status"] != "pending":
                return Result([])
            row["status"] = "canceled"
            return Result([row])
        if "UPDATE paper_orders SET status = 'canceled'" in query and "parent_order_id" in query:
            rows = [row for row in self.orders.values() if row.get("parent_order_id") == params[0] and row["id"] != params[1] and row["status"] == "pending"]
            for row in rows:
                row["status"] = "canceled"
            return Result(rows)
        if "SELECT id, account_id, status, simulation_only" in query:
            return Result([self.deployments[params[0]]] if params[0] in self.deployments else [])
        if "SELECT *" in query and "FROM strategy_deployments" in query and "simulation_only = TRUE" in query:
            rows = [row for row in self.deployments.values() if row["status"] == "active" and row["simulation_only"] is True]
            return Result(rows)
        if "INSERT INTO paper_fills" in query:
            row = {
                "id": len(self.fills) + 1,
                "order_id": params[0],
                "account_id": params[1],
                "symbol": params[2],
                "side": params[3],
                "quantity": params[4],
                "fill_price": params[5],
                "gross_amount": params[6],
                "fee": params[7],
                "slippage": params[8],
                "candle_timestamp": params[9],
                "simulation_only": True,
                "filled_at": "2026-01-02T00:00:00Z",
            }
            self.fills[row["id"]] = row
            return Result([row])
        if "INSERT INTO paper_positions" in query:
            self.positions[(params[0], params[1])] = {
                "account_id": params[0],
                "symbol": params[1],
                "quantity": params[2],
                "average_price": params[3],
                "realized_pnl": params[4],
                "simulation_only": True,
            }
            return Result([])
        if "UPDATE paper_accounts" in query:
            account = self.accounts[params[2]]
            account["cash_balance"] += params[0]
            account["realized_pnl"] += params[1]
            return Result([])
        if "UPDATE paper_orders SET status = 'filled'" in query:
            self.orders[params[1]]["status"] = "filled"
            self.orders[params[1]]["filled_at"] = params[0]
            return Result([])
        if "INSERT INTO strategy_deployments" in query:
            row = {
                "id": len(self.deployments) + 1,
                "account_id": params[0],
                "strategy_name": params[1],
                "strategy_version": params[2],
                "symbol": params[3],
                "timeframe": params[4],
                "parameters": {},
                "status": "active",
                "simulation_only": True,
                "last_scanned_candle_timestamp": None,
            }
            self.deployments[row["id"]] = row
            return Result([row])
        if "UPDATE strategy_deployments" in query and "last_scan_at = NOW()" in query:
            row = self.deployments.get(params[3])
            if not row:
                return Result([])
            row["last_signal"] = params[0]
            row["last_check_result"] = params[1]
            row["last_scan_payload"] = params[2]
            row["last_scan_at"] = datetime.now(UTC)
            return Result([row])
        if "UPDATE strategy_deployments" in query and "last_scanned_candle_timestamp" in query:
            row = self.deployments.get(params[1])
            if not row or row["status"] != "active" or row["simulation_only"] is not True:
                return Result([])
            if row.get("last_scanned_candle_timestamp") == params[2]:
                return Result([])
            row["last_scanned_candle_timestamp"] = params[0]
            return Result([row])
        if "UPDATE strategy_deployments" in query:
            row = self.deployments.get(params[0])
            if not row:
                return Result([])
            row["status"] = "paused"
            row["paused_at"] = "2026-01-02T00:00:00Z"
            return Result([row])
        raise AssertionError(query)

    def commit(self):
        self.commits += 1


def test_paper_account_is_simulation_only() -> None:
    conn = FakeConn()

    account = create_paper_account(conn, "Research Paper", Decimal("10000"))

    assert account["simulation_only"] is True
    assert conn.logs[0]["event_type"] == "paper_account_created"
    assert all(log["simulation_only"] is True for log in conn.logs)


def test_market_order_fills_from_candle_and_updates_position_and_cash() -> None:
    conn = FakeConn()
    account = create_paper_account(conn, "Research Paper", Decimal("10000"))

    order = create_order(conn, account["id"], "AAPL", Decimal("10"), side="buy", order_type="market")
    position = conn.positions[(account["id"], "AAPL")]

    assert order["status"] == "filled"
    assert position["quantity"] == Decimal("10")
    assert position["average_price"] > Decimal("100")
    assert conn.accounts[account["id"]]["cash_balance"] < Decimal("10000")
    assert all(fill["simulation_only"] is True for fill in conn.fills.values())


def test_risk_blocks_leverage_and_keeps_order_simulated() -> None:
    conn = FakeConn()
    account = create_paper_account(conn, "Small Paper", Decimal("1000"))

    order = create_order(conn, account["id"], "AAPL", Decimal("100"), side="buy", order_type="market")

    assert order["status"] == "rejected"
    assert "leverage is disabled" in order["rejected_reason"] or "max simulation risk" in order["rejected_reason"]
    assert order["simulation_only"] is True
    assert conn.fills == {}


def test_sell_fill_updates_realized_pnl_without_shorting() -> None:
    conn = FakeConn()
    account = create_paper_account(conn, "Research Paper", Decimal("10000"))
    create_order(conn, account["id"], "AAPL", Decimal("10"), side="buy", order_type="market")
    conn.candle = {**conn.candle, "close": Decimal("110"), "high": Decimal("112"), "low": Decimal("108")}

    sell = create_order(conn, account["id"], "AAPL", Decimal("5"), side="sell", order_type="market")
    position = conn.positions[(account["id"], "AAPL")]

    assert sell["status"] == "filled"
    assert position["quantity"] == Decimal("5")
    assert position["realized_pnl"] > Decimal("0")
    assert conn.accounts[account["id"]]["realized_pnl"] > Decimal("0")


def test_deployment_lifecycle_is_simulation_only() -> None:
    conn = FakeConn()
    account = create_paper_account(conn, "Research Paper", Decimal("10000"))

    deployment = create_deployment(conn, account["id"], "trend_pullback", "AAPL", "1d")
    paused = pause_deployment(conn, deployment["id"])

    assert deployment["simulation_only"] is True
    assert deployment["status"] == "active"
    assert paused["status"] == "paused"
    assert all(log["simulation_only"] is True for log in conn.logs)


def test_paused_deployment_cannot_create_order() -> None:
    conn = FakeConn()
    account = create_paper_account(conn, "Research Paper", Decimal("10000"))
    deployment = create_deployment(conn, account["id"], "trend_pullback", "AAPL", "1d")
    pause_deployment(conn, deployment["id"])

    order = create_order(conn, account["id"], "AAPL", Decimal("1"), deployment_id=deployment["id"])

    assert order["status"] == "rejected"
    assert "paused deployments cannot create paper orders" in order["rejected_reason"]
    assert conn.fills == {}


def test_duplicate_fill_call_does_not_create_second_fill() -> None:
    from app.services.paper_trading import simulate_order_fill

    conn = FakeConn()
    account = create_paper_account(conn, "Research Paper", Decimal("10000"))
    order = create_order(conn, account["id"], "AAPL", Decimal("10"), side="buy", order_type="market")

    simulate_order_fill(conn, order["id"])

    assert len(conn.fills) == 1


def test_pending_limit_order_can_be_canceled() -> None:
    conn = FakeConn()
    account = create_paper_account(conn, "Research Paper", Decimal("10000"))
    order = create_order(conn, account["id"], "AAPL", Decimal("1"), order_type="limit", limit_price=Decimal("90"))

    canceled = cancel_order(conn, order["id"])

    assert order["status"] == "pending"
    assert canceled["status"] == "canceled"
    assert any(log["event_type"] == "paper_order_canceled" for log in conn.logs)


def test_stop_loss_and_take_profit_are_oco_protective_orders() -> None:
    conn = FakeConn()
    account = create_paper_account(conn, "Research Paper", Decimal("10000"))
    create_order(conn, account["id"], "AAPL", Decimal("10"), stop_loss_price=Decimal("95"), take_profit_price=Decimal("110"))
    protective = [row for row in conn.orders.values() if row.get("parent_order_id")]

    assert {row["order_type"] for row in protective} == {"stop_loss", "take_profit"}
    conn.candle = {**conn.candle, "high": Decimal("112"), "low": Decimal("100"), "close": Decimal("110")}
    result = process_pending_orders(conn, account["id"])

    assert result["filled"] == 1
    assert {row["status"] for row in protective} == {"filled", "canceled"}
    assert conn.positions[(account["id"], "AAPL")]["quantity"] == Decimal("0")


def test_execution_log_payloads_are_json_serializable() -> None:
    from fastapi.encoders import jsonable_encoder
    import json

    payload = {"quantity": Decimal("23"), "limit_price": Decimal("2.00")}

    encoded = jsonable_encoder(payload)
    assert json.loads(json.dumps(encoded)) == {"quantity": 23, "limit_price": 2.0}


def test_scheduler_manual_mode_idles_without_scanning() -> None:
    import asyncio
    from app.services.paper_scheduler import run_scheduled_scan_once

    conn = FakeConn()
    conn.scheduler["cadence"] = "manual"
    conn.scheduler["next_run_at"] = None

    result = asyncio.run(run_scheduled_scan_once(conn))

    assert result["status"] == "idle"
    assert "disabled or manual" in result["message"]
    assert not any(log["event_type"] == "paper_scheduler_run_started" for log in conn.logs)


def test_scheduler_scans_only_active_simulation_deployments(monkeypatch) -> None:
    import asyncio
    from app.services import paper_scheduler

    conn = FakeConn()
    account = create_paper_account(conn, "Research Paper", Decimal("10000"))
    active = create_deployment(conn, account["id"], "momentum", "TSLA", "1h", strategy_version="bull_v2")
    paused = create_deployment(conn, account["id"], "momentum", "TSLA", "1h", strategy_version="bull_v2")
    conn.deployments[paused["id"]]["status"] = "paused"
    non_sim = create_deployment(conn, account["id"], "momentum", "TSLA", "1h", strategy_version="bull_v2")
    conn.deployments[non_sim["id"]]["simulation_only"] = False
    scanned: list[int] = []

    async def fake_scan(_conn, deployment_id: int, **_kwargs):
        deployment = _conn.deployments[deployment_id]
        assert deployment["status"] == "active"
        assert deployment["simulation_only"] is True
        scanned.append(deployment_id)
        return {"action": "skipped", "message": "fake simulation scan", "simulation_only": True, "order": None}

    monkeypatch.setattr(paper_scheduler, "run_deployment_scan", fake_scan)

    result = asyncio.run(paper_scheduler.run_scheduled_scan_once(conn, force=True))

    assert scanned == [active["id"]]
    assert result["simulation_only"] is True
    assert result["results"][0]["simulation_only"] is True
    assert any(log["event_type"] == "paper_scheduler_run_finished" for log in conn.logs)


def test_deployment_candle_scan_claim_blocks_duplicate_candle() -> None:
    from app.services.paper_trading import claim_deployment_candle_scan

    conn = FakeConn()
    account = create_paper_account(conn, "Research Paper", Decimal("10000"))
    deployment = create_deployment(conn, account["id"], "momentum", "TSLA", "1h", strategy_version="bull_v2")
    candle_timestamp = datetime(2026, 1, 2, 15, tzinfo=UTC)

    first = claim_deployment_candle_scan(conn, deployment["id"], candle_timestamp)
    second = claim_deployment_candle_scan(conn, deployment["id"], candle_timestamp)

    assert first is not None
    assert first["simulation_only"] is True
    assert second is None
    assert conn.deployments[deployment["id"]]["last_scanned_candle_timestamp"] == candle_timestamp


def test_deployment_candle_scan_claim_requires_active_simulation_deployment() -> None:
    from app.services.paper_trading import claim_deployment_candle_scan

    conn = FakeConn()
    account = create_paper_account(conn, "Research Paper", Decimal("10000"))
    deployment = create_deployment(conn, account["id"], "momentum", "TSLA", "1h", strategy_version="bull_v2")
    conn.deployments[deployment["id"]]["simulation_only"] = False

    claimed = claim_deployment_candle_scan(conn, deployment["id"], datetime(2026, 1, 2, 15, tzinfo=UTC))

    assert claimed is None


def test_stale_scan_blocks_strategy_evaluation_and_order_creation(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace
    from app.services import paper_trading

    conn = FakeConn()
    account = create_paper_account(conn, "Research Paper", Decimal("10000"))
    deployment = create_deployment(conn, account["id"], "momentum", "TSLA", "1h", strategy_version="bull_v2")
    stale_timestamp = datetime.now(UTC) - timedelta(days=10)
    conn.candle = {
        "symbol": "TSLA",
        "timeframe": "1h",
        "timestamp": stale_timestamp,
        "open": Decimal("100"),
        "high": Decimal("101"),
        "low": Decimal("99"),
        "close": Decimal("100"),
        "volume": Decimal("1000"),
    }
    alerts: list[dict] = []

    async def fake_sync_latest(_conn, symbol, timeframe):
        return SimpleNamespace(
            provider="alpaca_iex",
            received=0,
            upserted=0,
            first_timestamp=None,
            last_timestamp=stale_timestamp,
        )

    def fail_evaluate(*args, **kwargs):
        raise AssertionError("stale data must not evaluate strategy setup")

    def fail_order(*args, **kwargs):
        raise AssertionError("stale data must not create paper orders")

    def fake_alert(*args, **kwargs):
        alerts.append(kwargs)
        return {"alert_type": "stale_data_warning", "simulation_only": True}

    monkeypatch.setattr(paper_trading, "sync_latest_deployment_candles", fake_sync_latest)
    monkeypatch.setattr(paper_trading, "sync_features", lambda *args, **kwargs: {"updated": 0})
    monkeypatch.setattr(paper_trading, "evaluate_deployment_decision", fail_evaluate)
    monkeypatch.setattr(paper_trading, "create_deployment_order_from_decision", fail_order)
    monkeypatch.setattr(paper_trading, "detect_paper_scan_alert", fake_alert)
    monkeypatch.setattr(paper_trading, "reconcile_account", lambda *args, **kwargs: {"healthy": True})
    monkeypatch.setattr(paper_trading, "get_position", lambda *args, **kwargs: None)

    result = asyncio.run(paper_trading.run_deployment_scan(conn, deployment["id"]))

    assert result["action"] == "stale_data_warning"
    assert result["order"] is None
    assert result["processed_pending"]["skipped"] is True
    assert alerts[0]["action"] == "stale_data_warning"
    assert any(log["event_type"] == "paper_scan_stale_data_skipped" for log in conn.logs)


def test_candidate_forward_gate_rejects_predeployment_candle() -> None:
    from app.services.paper_trading import candle_is_forward_eligible_for_deployment

    started = datetime(2026, 7, 15, 4, 27, tzinfo=UTC)
    deployment = {
        "campaign_id": 7,
        "candidate_id": "sd_test",
        "forward_validation_started_at": started,
    }

    assert candle_is_forward_eligible_for_deployment(deployment, {"timestamp": started - timedelta(hours=1)}) is False
    assert candle_is_forward_eligible_for_deployment(deployment, {"timestamp": started}) is False
    assert candle_is_forward_eligible_for_deployment(deployment, {"timestamp": started + timedelta(hours=1)}) is True


def test_manual_simulation_does_not_require_forward_timestamp() -> None:
    from app.services.paper_trading import candle_is_forward_eligible_for_deployment

    assert candle_is_forward_eligible_for_deployment({}, {"timestamp": None}) is True
