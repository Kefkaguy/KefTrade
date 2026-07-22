from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.mission_control import classify_candle_freshness, compact_mission_control_snapshot, get_mission_control


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class MissionConn:
    def __init__(self):
        now = datetime.now(UTC)
        self.scheduler = {
            "id": True,
            "enabled": True,
            "cadence": "60m",
            "last_run_at": now - timedelta(hours=1),
            "next_run_at": now + timedelta(hours=1),
            "latest_result": "Scheduled scan completed: 2 result(s), 0 error(s).",
            "latest_error": None,
            "is_running": False,
        }
        self.symbols = [
            {"symbol": "TSLA", "asset_class": "equity", "is_active": True},
            {"symbol": "AAPL", "asset_class": "equity", "is_active": True},
            {"symbol": "BTCUSDT", "asset_class": "crypto", "is_active": True},
        ]
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
                "created_at": now - timedelta(days=2),
                "last_scan_at": now - timedelta(hours=1),
                "last_signal": "setup",
                "last_check_result": "Stored setup review.",
                "last_scan_payload": {},
                "last_scanned_candle_timestamp": now - timedelta(hours=2),
            },
            {
                "id": 2,
                "account_id": 1,
                "strategy_name": "momentum",
                "strategy_version": "bull_v2",
                "symbol": "AAPL",
                "timeframe": "1h",
                "parameters": {},
                "status": "paused",
                "simulation_only": True,
                "created_at": now - timedelta(days=1),
                "last_scan_at": None,
                "last_signal": None,
                "last_check_result": None,
                "last_scan_payload": {},
                "last_scanned_candle_timestamp": None,
            },
            {
                "id": 99,
                "account_id": 1,
                "strategy_name": "live",
                "strategy_version": "broker",
                "symbol": "TSLA",
                "timeframe": "1h",
                "status": "active",
                "simulation_only": False,
                "created_at": now,
            },
        ]
        self.accounts = [{"id": 1, "cash_balance": Decimal("9500"), "realized_pnl": Decimal("120"), "simulation_only": True}]
        self.positions = [{"account_id": 1, "symbol": "TSLA", "quantity": Decimal("2"), "average_price": Decimal("100"), "realized_pnl": Decimal("120"), "market_value": Decimal("230"), "unrealized_pnl": Decimal("30"), "simulation_only": True}]
        self.orders = [{"id": 1, "account_id": 1, "symbol": "TSLA", "side": "buy", "order_type": "market", "status": "filled", "submitted_at": now - timedelta(hours=1), "simulation_only": True}]
        self.fills = [{"id": 1, "order_id": 1, "account_id": 1, "symbol": "TSLA", "side": "buy", "quantity": Decimal("2"), "fill_price": Decimal("100"), "filled_at": now - timedelta(hours=1), "simulation_only": True}]
        self.equity = [{"id": 1, "account_id": 1, "timestamp": now - timedelta(hours=1), "equity": Decimal("9730"), "cash_balance": Decimal("9500"), "unrealized_pnl": Decimal("30"), "realized_pnl": Decimal("120"), "simulation_only": True}]
        self.alerts = [
            {
                "id": 1,
                "symbol": "TSLA",
                "timeframe": "1h",
                "strategy_id": "momentum_bull_v2_007",
                "alert_type": "entry_setup_review",
                "severity": "info",
                "verdict": "Research Opportunity",
                "evidence_summary": "Stored setup evidence.",
                "matched_rules": ["Rule matched"],
                "failed_rules": [],
                "profit_factor": Decimal("1.521992854765452"),
                "expectancy": Decimal("17.200148212785386"),
                "trade_count": 56,
                "max_drawdown": Decimal("0.03670592788406391"),
                "regime": "bull_trend",
                "candle_timestamp": now - timedelta(hours=2),
                "created_at": now - timedelta(hours=1),
                "acknowledged_at": None,
                "simulation_only": True,
            },
            {
                "id": 2,
                "symbol": "AAPL",
                "timeframe": "1h",
                "strategy_id": "momentum_bull_v2",
                "alert_type": "stale_data_warning",
                "severity": "warning",
                "verdict": "No Setup",
                "evidence_summary": "Stale data blocked.",
                "matched_rules": [],
                "failed_rules": ["Stale"],
                "profit_factor": None,
                "expectancy": None,
                "trade_count": None,
                "max_drawdown": None,
                "regime": None,
                "candle_timestamp": now - timedelta(days=8),
                "created_at": now - timedelta(hours=2),
                "acknowledged_at": None,
                "simulation_only": True,
            },
        ]
        self.reviews = [
            {
                "id": 1,
                "account_id": 1,
                "deployment_id": 1,
                "symbol": "TSLA",
                "timeframe": "1h",
                "strategy_id": "momentum_bull_v2_007",
                "status": "Setup Worth Reviewing",
                "verdict": "Setup Worth Reviewing",
                "regime": "bull_trend",
                "evidence_score": "5/5",
                "matched_rules": ["Rule matched"],
                "failed_rules": [],
                "profit_factor": Decimal("1.521992854765452"),
                "expectancy": Decimal("17.200148212785386"),
                "trade_count": 56,
                "max_drawdown": Decimal("0.03670592788406391"),
                "latest_candle_timestamp": now - timedelta(hours=2),
                "data_freshness": "2.0h old",
                "created_at": now - timedelta(minutes=55),
                "reviewed_at": None,
                "ignored_at": None,
                "simulation_only": True,
            }
        ]
        self.logs = [
            {"id": 1, "event_type": "paper_scan_completed", "message": "Scan completed.", "payload": {"symbol": "TSLA"}, "created_at": now - timedelta(hours=1), "simulation_only": True},
            {"id": 2, "event_type": "paper_scan_duplicate_candle_skipped", "message": "Duplicate candle skipped.", "payload": {"symbol": "TSLA"}, "created_at": now - timedelta(minutes=30), "simulation_only": True},
            {"id": 3, "event_type": "paper_scan_stale_data_skipped", "message": "Stale data blocked.", "payload": {"symbol": "AAPL"}, "created_at": now - timedelta(minutes=20), "simulation_only": True},
        ]
        self.candles = {
            ("TSLA", "1h"): {"symbol": "TSLA", "timeframe": "1h", "timestamp": now - timedelta(hours=2), "close": Decimal("115"), "source": "alpaca_iex"},
            ("AAPL", "1h"): {"symbol": "AAPL", "timeframe": "1h", "timestamp": now - timedelta(days=8), "close": Decimal("210"), "source": "alpaca_iex"},
            ("TSLA", "1d"): {"symbol": "TSLA", "timeframe": "1d", "timestamp": now - timedelta(hours=20), "close": Decimal("115"), "source": "alpaca_iex"},
            ("AAPL", "1d"): {"symbol": "AAPL", "timeframe": "1d", "timestamp": now - timedelta(hours=20), "close": Decimal("210"), "source": "alpaca_iex"},
        }
        self.campaigns = [
            {
                "id": 1,
                "name": "S&P 500 Leaders strategy discovery campaign",
                "universe_key": "sp500_leaders",
                "status": "running",
                "queued_jobs": 100,
                "completed_jobs": 35,
                "failed_jobs": 1,
                "promoted_candidates": 2,
                "rejected_candidates": 8,
                "analytics": {"strategies_generated": 10},
                "started_at": now - timedelta(hours=2),
                "completed_at": None,
                "updated_at": now,
                "simulation_only": True,
            },
            {
                "id": 4,
                "name": "phase_9_9_overfit_diagnosis_regime_robustness_v1",
                "universe_key": "phase_9_9_overfit_diagnosis_regime_robustness_v1",
                "status": "completed",
                "requested_candidates": 24,
                "queued_jobs": 96,
                "completed_jobs": 96,
                "failed_jobs": 0,
                "promoted_candidates": 0,
                "rejected_candidates": 24,
                "analytics": {"strategies_generated": 24},
                "started_at": now - timedelta(hours=8),
                "completed_at": now - timedelta(hours=1),
                "updated_at": now - timedelta(hours=1),
                "simulation_only": True,
            },
        ]
        self.campaign_scheduler = {
            "id": True,
            "enabled": True,
            "cadence_seconds": 300,
            "last_cycle_at": now - timedelta(minutes=10),
            "next_cycle_at": now + timedelta(minutes=5),
            "latest_result": "Processed 4 campaign job(s).",
            "latest_error": None,
            "is_running": False,
            "simulation_only": True,
        }
        self.campaign_jobs = [
            {"id": 1, "status": "running", "worker_id": "worker-1", "heartbeat_at": now, "lease_expires_at": now + timedelta(minutes=10), "created_at": now - timedelta(hours=1), "completed_at": None, "execution_runtime_ms": None, "failure_classification": None},
            {"id": 2, "status": "blocked_data", "worker_id": None, "heartbeat_at": None, "lease_expires_at": None, "created_at": now - timedelta(hours=2), "completed_at": None, "execution_runtime_ms": None, "failure_classification": "stale_data"},
            {"id": 3, "status": "promoted", "worker_id": None, "heartbeat_at": None, "lease_expires_at": None, "created_at": now - timedelta(hours=3), "completed_at": now - timedelta(minutes=20), "execution_runtime_ms": 1200, "failure_classification": None},
        ]
        self.campaign_jobs.extend(self.phase_99_jobs(now))
        self.campaign_workers = [
            {"worker_id": "worker-1", "hostname": "test-host", "status": "running", "heartbeat_at": now, "started_at": now - timedelta(hours=1), "stopped_at": None, "latest_error": None, "simulation_only": True}
        ]
        self.rollbacks = 0

    def phase_99_jobs(self, now):
        rows = []
        job_id = 100
        failure_reasons = [
            "insufficient_trades",
            "fails_in_sideways",
            "fails_in_low_volatility",
            "weak_profit_factor",
            "poor_expectancy",
        ]

        def result(candidate_id, strategy_family, metrics):
            return {
                "candidate_id": candidate_id,
                "blocks": {"entry": "pullback" if strategy_family == "Pullback" else "trend_continuation"},
                "parameters": {"entry": "pullback" if strategy_family == "Pullback" else "trend_continuation"},
                "metrics": metrics,
            }

        def add_candidate(candidate_id, strategy_family, promoted, metrics, reasons):
            nonlocal job_id
            markets = [("AAPL", "1h"), ("AAPL", "4h"), ("NVDA", "1h"), ("GOOGL", "1h")]
            for index, (symbol, timeframe) in enumerate(markets):
                status = "promoted" if promoted and index == 0 else "rejected"
                rows.append(
                    {
                        "id": job_id,
                        "campaign_id": 4,
                        "candidate_id": candidate_id,
                        "family_id": f"family_{strategy_family.lower().replace(' ', '_')}",
                        "strategy_family": strategy_family,
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "status": status,
                        "result": result(candidate_id, strategy_family, metrics) if status == "promoted" or not promoted else {},
                        "failure_reasons": [] if status == "promoted" else reasons,
                        "validation_score": Decimal("78.5") if status == "promoted" else Decimal("0"),
                        "worker_id": None,
                        "heartbeat_at": None,
                        "lease_expires_at": None,
                        "created_at": now - timedelta(hours=7),
                        "completed_at": now - timedelta(hours=2),
                        "execution_runtime_ms": 1500,
                        "failure_classification": None,
                        "latest_error": None,
                    }
                )
                job_id += 1

        best_metrics = {
            "profit_factor": Decimal("1.6808"),
            "expectancy_per_trade": Decimal("28.2448"),
            "number_of_trades": 115,
            "max_drawdown": Decimal("0.0414"),
        }
        add_candidate("sd_a8d9508bee3c46", "Pullback", True, best_metrics, failure_reasons)
        for index in range(9):
            add_candidate(
                f"research_candidate_{index}",
                "Pullback",
                True,
                {
                    "profit_factor": Decimal("1.30"),
                    "expectancy_per_trade": Decimal("8.0"),
                    "number_of_trades": 72,
                    "max_drawdown": Decimal("0.08"),
                },
                failure_reasons[:3],
            )
        for index in range(14):
            add_candidate(
                f"needs_more_evidence_{index}",
                "Pullback" if index < 10 else "Trend Following",
                False,
                {
                    "profit_factor": Decimal("1.05"),
                    "expectancy_per_trade": Decimal("1.0"),
                    "number_of_trades": 12,
                    "max_drawdown": Decimal("0.09"),
                },
                [failure_reasons[index % len(failure_reasons)]],
            )
        return rows

    def execute(self, query, params=None):
        if "FROM paper_scan_scheduler" in query:
            return Result([self.scheduler])
        if "FROM strategy_deployments" in query:
            if "COUNT(1)" in query:
                return Result([{"count": 0}])
            rows = [row for row in self.deployments if row.get("simulation_only") is True]
            return Result(rows)
        if "FROM paper_accounts" in query:
            return Result(self.accounts)
        if "FROM paper_positions" in query:
            return Result(self.positions)
        if "FROM paper_orders" in query:
            return Result(self.orders)
        if "FROM paper_fills" in query:
            return Result(self.fills)
        if "FROM paper_equity_curve" in query:
            return Result(self.equity)
        if "FROM evidence_alerts" in query:
            return Result(self.alerts)
        if "FROM signal_reviews" in query:
            return Result(self.reviews)
        if "FROM execution_logs" in query:
            return Result(self.logs)
        if "FROM symbols" in query:
            return Result(self.symbols)
        if "FROM candles" in query:
            rows = []
            for index in range(0, len(params), 2):
                row = self.candles.get((params[index], params[index + 1]))
                if row:
                    rows.append(row)
            return Result(rows)
        if query.strip().startswith("CREATE TABLE") or query.strip().startswith("ALTER TABLE") or "DROP CONSTRAINT" in query or "ADD CONSTRAINT" in query:
            return Result([])
        if "INSERT INTO research_campaign_scheduler" in query:
            return Result([])
        if "FROM research_campaign_scheduler" in query:
            return Result([self.campaign_scheduler])
        if "FROM research_campaign_workers" in query:
            return Result(self.campaign_workers)
        if "FROM elite_research_candidates" in query:
            if "COUNT(1)" in query:
                return Result([{"count": 0}])
            return Result([])
        if "FROM research_campaigns" in query:
            if "completed_at >= NOW()" in query:
                return Result([{"count": 0}])
            return Result(self.campaigns)
        if "FROM research_campaign_jobs" in query:
            if "SELECT *" in query and "WHERE campaign_id = %s" in query:
                return Result([row for row in self.campaign_jobs if row.get("campaign_id") == params[0]])
            if "GROUP BY COALESCE" in query:
                return Result([{"classification": "stale_data", "count": 1}])
            if "COUNT(DISTINCT worker_id)" in query:
                return Result([{"count": 1}])
            if "COUNT(*) AS count" in query and "GROUP BY status" in query:
                counts = {}
                for row in self.campaign_jobs:
                    counts[row["status"]] = counts.get(row["status"], 0) + 1
                return Result([{"status": key, "count": value} for key, value in counts.items()])
            if "COUNT(*) AS count" in query:
                return Result([{"count": 1}])
            if "MIN(created_at)" in query:
                return Result([{"oldest": self.campaign_jobs[1]["created_at"]}])
            if "AVG(execution_runtime_ms)" in query:
                return Result([{"average_runtime": 1200}])
            if "GROUP BY worker_id" in query:
                return Result([{"worker_id": "worker-1", "claimed_jobs": 1, "last_heartbeat": self.campaign_jobs[0]["heartbeat_at"], "lease_expires_at": self.campaign_jobs[0]["lease_expires_at"]}])
        raise AssertionError(query)

    def rollback(self):
        self.rollbacks += 1


def test_mission_control_aggregates_multi_asset_simulation_state() -> None:
    snapshot = get_mission_control(MissionConn())

    assert snapshot["simulation_only"] is True
    assert snapshot["safety"]["live_routing_enabled"] is False
    assert snapshot["research_summary"]["assets_monitored"] >= 3
    assert snapshot["research_summary"]["active_deployments"] == 1
    assert snapshot["research_summary"]["open_simulated_positions"] == 1
    assert snapshot["research_summary"]["scheduler_failures"] == 0
    assert snapshot["system_health"]["duplicate_candle_skips"] == 1
    assert snapshot["paper_account"]["label"] == "All values are simulated."
    assert snapshot["research_campaigns"]["active_campaigns"] == 1
    assert snapshot["research_summary"]["elite_candidates_promoted"] == 2


def test_mission_control_prefers_stored_candidate_metrics() -> None:
    snapshot = get_mission_control(MissionConn())
    tsla = next(row for row in snapshot["assets"] if row["symbol"] == "TSLA" and row["timeframe"] == "1h")

    assert tsla["profit_factor"] == Decimal("1.521992854765452")
    assert tsla["expectancy"] == Decimal("17.200148212785386")
    assert tsla["trade_count"] == 56
    assert tsla["max_drawdown"] == Decimal("0.03670592788406391")


def test_mission_control_marks_stale_data_without_setup() -> None:
    snapshot = get_mission_control(MissionConn())
    aapl = next(row for row in snapshot["assets"] if row["symbol"] == "AAPL" and row["timeframe"] == "1h")

    assert aapl["status"] == "Stale Data"
    assert aapl["latest_verdict"] == "No Setup"


def test_mission_control_filters_non_simulation_deployments() -> None:
    snapshot = get_mission_control(MissionConn())

    assert all(deployment["strategy"] != "live_broker" for deployment in snapshot["deployments"])
    assert snapshot["research_summary"]["active_deployments"] == 1


def test_mission_control_returns_partial_data_when_subsystem_fails() -> None:
    conn = MissionConn()

    def failing_execute(query, params=None):
        if "FROM evidence_alerts" in query:
            raise RuntimeError("alerts unavailable")
        return MissionConn.execute(conn, query, params)

    conn.execute = failing_execute

    snapshot = get_mission_control(conn)

    assert snapshot["subsystem_errors"][0]["subsystem"] == "evidence_alerts"
    assert snapshot["subsystem_errors"][0]["recommended_fix"]
    assert snapshot["assets"]
    assert snapshot["system_health"]["overall_status"] == "Warning"
    assert conn.rollbacks >= 1


def test_mission_control_rolls_back_aborted_transaction_before_continuing() -> None:
    conn = MissionConn()

    def failing_execute(query, params=None):
        if "WITH monitored(symbol, timeframe)" in query:
            raise RuntimeError("current transaction is aborted, commands ignored until end of transaction block")
        return MissionConn.execute(conn, query, params)

    conn.execute = failing_execute

    snapshot = get_mission_control(conn)

    assert conn.rollbacks >= 1
    market_data_error = next(error for error in snapshot["subsystem_errors"] if error["subsystem"] == "market_data")
    assert "current transaction is aborted" not in market_data_error["error"]
    assert snapshot["paper_account"]["account_count"] == 1


def test_equity_market_closed_freshness_is_not_false_failure() -> None:
    friday_close = datetime(2026, 7, 10, 20, tzinfo=UTC)
    saturday = datetime(2026, 7, 11, 16, tzinfo=UTC)

    freshness = classify_candle_freshness(friday_close, "1h", "equity", saturday)

    assert freshness["classification"] == "Healthy"
    assert freshness["detail"] == "Market closed: latest completed candle is expected"


def test_scheduler_disabled_classification() -> None:
    conn = MissionConn()
    conn.scheduler["enabled"] = False

    snapshot = get_mission_control(conn)

    assert snapshot["system_health"]["scheduler_status"] == "Disabled"


def test_scheduler_error_classification() -> None:
    conn = MissionConn()
    conn.scheduler["latest_error"] = "scan failed"
    conn.alerts.append(
        {
            **conn.alerts[0],
            "id": 3,
            "symbol": "SYSTEM",
            "timeframe": "scheduler",
            "alert_type": "scheduler_error",
            "severity": "critical",
            "verdict": "Avoid",
            "created_at": datetime.now(UTC),
        }
    )

    snapshot = get_mission_control(conn)

    assert snapshot["system_health"]["scheduler_status"] == "Error"
    assert snapshot["system_health"]["overall_status"] == "Error"
    assert snapshot["research_summary"]["scheduler_failures"] >= 1


def test_mission_control_authoritative_snapshot_is_consistent() -> None:
    snapshot = get_mission_control(MissionConn())

    assert snapshot["snapshot_version"] == "mission_control_v2"
    assert snapshot["readiness"]["phase_10_allowed"] is (snapshot["readiness"]["state"] == "ready_for_phase_10" and snapshot["readiness"]["blocking_gate_count"] == 0)
    assert snapshot["readiness"]["blocking_gate_count"] == len(snapshot["readiness"]["blocking_gates"])
    if snapshot["readiness"]["blocking_gate_count"]:
        assert snapshot["readiness"]["blocking_gates"]
    assert snapshot["diagnostics"]["active_count"] == len(snapshot["subsystem_errors"])
    assert all(error["active"] for error in snapshot["diagnostics"]["active"])


def test_compact_mission_control_omits_large_detail_collections() -> None:
    full = get_mission_control(MissionConn())
    compact = get_mission_control(MissionConn(), compact=True)

    assert compact["asset_count"] == len(MissionConn().symbols)
    assert compact["assets"] == []
    assert "campaigns" not in compact["research_campaigns"]
    assert compact["research_campaigns"]["active_campaigns"] == full["research_campaigns"]["active_campaigns"]


def test_latest_completed_campaign_summary_matches_phase_99_lifecycle_totals() -> None:
    snapshot = get_mission_control(MissionConn())

    latest = snapshot["research_campaigns"]["latest_completed_campaign"]
    summaries = snapshot["research_campaigns"]["completed_campaign_summaries"]
    lifecycle = latest["candidate_lifecycle_counts"]

    assert any(row["id"] == latest["id"] for row in summaries)
    assert latest["name"] == "phase_9_9_overfit_diagnosis_regime_robustness_v1"
    assert latest["generated_candidates"] == 24
    assert latest["tested_candidates"] == 24
    assert lifecycle["research_candidate"] == 10
    assert lifecycle["needs_more_evidence"] == 14
    assert lifecycle["elite_candidate"] == 0
    assert lifecycle["rejected"] == 0
    assert latest["jobs_executed"] == 96
    assert latest["jobs_rejected_by_evidence"] == 86
    assert latest["operationally_failed_jobs"] == 0
    assert latest["promoted_single_market_jobs"] == 10
    assert sum(latest["job_status_counts"].values()) == latest["jobs_executed"]
    assert sum(lifecycle.values()) == latest["generated_candidates"]

    best = latest["best_candidate"]
    assert best["candidate_id"] == "sd_a8d9508bee3c46"
    assert best["strategy_family"] == "Pullback"
    assert best["symbol"] == "AAPL"
    assert best["timeframe"] == "1h"
    assert best["profit_factor"] == 1.6808
    assert best["expectancy"] == 28.2448
    assert best["trade_count"] == 115
    assert best["max_drawdown"] == 0.0414
    assert best["stability"] == 0.25

    reasons = [row["reason"] for row in latest["top_failure_reasons"]]
    assert "insufficient_trades" in reasons
    assert "fails_in_sideways" in reasons
    assert "fails_in_low_volatility" in reasons
    assert "weak_profit_factor" in reasons
    assert "poor_expectancy" in reasons


def test_forward_evidence_is_present_even_when_failing() -> None:
    snapshot = get_mission_control(MissionConn())

    assert "closed_trades" in snapshot["forward_evidence"]
    assert "expectancy" in snapshot["forward_evidence"]
    if snapshot["forward_evidence"]["closed_trades"] > 0:
        assert snapshot["forward_evidence"]["has_data"] is True
