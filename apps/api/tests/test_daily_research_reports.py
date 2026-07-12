from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.services.daily_research_reports import auto_generate_daily_report_after_scheduler_run, build_daily_report_analytics, build_daily_summary, generate_daily_research_report


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class DailyReportConn:
    def __init__(self):
        self.report_rows = {}
        self.commits = 0
        self.day = date(2026, 7, 12)
        start = datetime(2026, 7, 12, tzinfo=UTC)
        self.logs = [
            {"id": 1, "event_type": "paper_scan_completed", "message": "TSLA scan completed.", "payload": {"symbol": "TSLA"}, "created_at": start + timedelta(hours=1), "simulation_only": True},
            {"id": 2, "event_type": "paper_scan_stale_data_skipped", "message": "AAPL stale.", "payload": {"symbol": "AAPL"}, "created_at": start + timedelta(hours=2), "simulation_only": True},
            {"id": 3, "event_type": "paper_scheduler_scan_error", "message": "scan failed", "payload": {"symbol": "MSFT"}, "created_at": start + timedelta(hours=3), "simulation_only": True},
            {"id": 4, "event_type": "paper_scan_completed", "message": "live should be filtered", "payload": {"symbol": "LIVE"}, "created_at": start + timedelta(hours=4), "simulation_only": False},
        ]
        self.alerts = [
            {"id": 1, "symbol": "TSLA", "timeframe": "1h", "strategy_id": "momentum_bull_v2_007", "alert_type": "entry_setup_review", "severity": "info", "verdict": "Research Opportunity", "evidence_summary": "Setup review.", "created_at": start + timedelta(hours=1), "simulation_only": True},
            {"id": 2, "symbol": "AAPL", "timeframe": "1h", "strategy_id": "momentum_bull_v2", "alert_type": "stale_data_warning", "severity": "warning", "verdict": "No Setup", "evidence_summary": "Stale.", "created_at": start + timedelta(hours=2), "simulation_only": True},
            {"id": 3, "symbol": "SYSTEM", "timeframe": "scheduler", "strategy_id": "paper_scheduler", "alert_type": "scheduler_error", "severity": "critical", "verdict": "Avoid", "evidence_summary": "Scheduler failed.", "created_at": start + timedelta(hours=3), "simulation_only": True},
        ]
        self.reviews = [
            {"id": 1, "symbol": "TSLA", "timeframe": "1h", "strategy_id": "momentum_bull_v2_007", "status": "Setup Worth Reviewing", "verdict": "Setup Worth Reviewing", "created_at": start + timedelta(hours=1), "simulation_only": True},
            {"id": 2, "symbol": "AAPL", "timeframe": "1h", "strategy_id": "momentum_bull_v2", "status": "No Setup", "verdict": "No Setup", "created_at": start + timedelta(hours=2), "simulation_only": True},
        ]
        self.orders = [{"id": 1, "symbol": "TSLA", "side": "buy", "order_type": "market", "status": "filled", "submitted_at": start + timedelta(hours=1), "simulation_only": True}]
        self.fills = [{"id": 1, "order_id": 1, "symbol": "TSLA", "side": "buy", "quantity": Decimal("2"), "fill_price": Decimal("100"), "filled_at": start + timedelta(hours=1, minutes=2), "simulation_only": True}]
        self.equity = [{"id": 1, "account_id": 1, "timestamp": start + timedelta(hours=23), "equity": Decimal("10050"), "realized_pnl": Decimal("25"), "unrealized_pnl": Decimal("50"), "simulation_only": True}]
        self.scheduler = {"id": True, "enabled": True, "cadence": "60m", "latest_error": None}
        self.freshness_rows = [
            {"symbol": "TSLA", "timeframe": "1h", "asset_class": "us_equity", "timestamp": start + timedelta(hours=22)},
            {"symbol": "AAPL", "timeframe": "1h", "asset_class": "us_equity", "timestamp": start - timedelta(days=8)},
        ]

    def execute(self, query, params=None):
        if "SELECT *" in query and "FROM daily_research_reports" in query and "report_date = %s" in query:
            row = self.report_rows.get(params[0])
            return Result([row] if row else [])
        if "SELECT *" in query and "FROM daily_research_reports" in query and "ORDER BY report_date ASC" in query:
            return Result(sorted(self.report_rows.values(), key=lambda row: row["report_date"]))
        if "INSERT INTO daily_research_reports" in query:
            row = {
                "id": 1,
                "report_date": params[0],
                "summary": jsonb_value(params[1]),
                "markdown_report": params[2],
                "generated_at": datetime.now(UTC),
                "simulation_only": True,
            }
            self.report_rows[params[0]] = row
            return Result([row])
        if "INSERT INTO execution_logs" in query:
            self.logs.append({"id": len(self.logs) + 1, "event_type": params[0], "message": params[1], "payload": jsonb_value(params[2]), "created_at": datetime.now(UTC), "simulation_only": True})
            return Result([])
        if "FROM execution_logs" in query:
            return Result([row for row in self.logs if row["simulation_only"] is True])
        if "FROM evidence_alerts" in query and "JOIN monitored" not in query:
            return Result([row for row in self.alerts if row["simulation_only"] is True])
        if "FROM signal_reviews" in query and "JOIN monitored" not in query:
            return Result([row for row in self.reviews if row["simulation_only"] is True])
        if "FROM paper_orders" in query:
            return Result([row for row in self.orders if row["simulation_only"] is True])
        if "FROM paper_fills" in query:
            return Result([row for row in self.fills if row["simulation_only"] is True])
        if "FROM paper_equity_curve" in query:
            return Result([row for row in self.equity if row["simulation_only"] is True])
        if "FROM paper_scan_scheduler" in query:
            return Result([self.scheduler])
        if "WITH monitored AS" in query:
            return Result(self.freshness_rows)
        raise AssertionError(query)

    def commit(self):
        self.commits += 1


def jsonb_value(value):
    return getattr(value, "obj", value)


def test_daily_report_summary_covers_required_daily_operations() -> None:
    summary = build_daily_summary(DailyReportConn(), date(2026, 7, 12))

    assert summary["assets_scanned"]["symbols"] == ["AAPL", "MSFT", "TSLA"]
    assert summary["setups_found"]["count"] == 2
    assert summary["no_setup_decisions"]["count"] == 1
    assert summary["stale_data_blocks"]["count"] == 2
    assert summary["scheduler_errors"]["count"] == 2
    assert summary["paper_orders"]["count"] == 1
    assert summary["paper_fills"]["count"] == 1
    assert summary["pnl"]["realized"] == Decimal("25")
    assert summary["pnl"]["unrealized"] == Decimal("50")
    assert summary["data_freshness"]["counts"]["Healthy"] == 1
    assert summary["data_freshness"]["counts"]["Stale"] == 1
    assert summary["scheduler_uptime"] == Decimal("50.00")
    assert summary["simulation_only"] is True


def test_daily_report_is_persisted_as_simulation_only_markdown() -> None:
    conn = DailyReportConn()

    report = generate_daily_research_report(conn, date(2026, 7, 12))

    assert report["simulation_only"] is True
    assert report["summary"]["paper_orders"]["count"] == 1
    assert "Daily Research Report" in report["markdown_report"]
    assert "No live trading" in report["markdown_report"]
    assert conn.commits == 1


def test_auto_generation_after_final_scheduled_scan_prevents_duplicates() -> None:
    conn = DailyReportConn()
    completed_at = datetime(2026, 7, 12, 23, 45, tzinfo=UTC)
    next_run_at = datetime(2026, 7, 13, 0, 45, tzinfo=UTC)

    first = auto_generate_daily_report_after_scheduler_run(conn, completed_at=completed_at, next_run_at=next_run_at)
    second = auto_generate_daily_report_after_scheduler_run(conn, completed_at=completed_at, next_run_at=next_run_at)

    assert first["status"] == "created"
    assert second["status"] == "exists"
    assert len(conn.report_rows) == 1
    assert any(log["event_type"] == "daily_research_report_duplicate_prevented" for log in conn.logs)


def test_auto_generation_skips_non_final_scheduled_scan() -> None:
    conn = DailyReportConn()
    completed_at = datetime(2026, 7, 12, 12, tzinfo=UTC)
    next_run_at = datetime(2026, 7, 12, 13, tzinfo=UTC)

    result = auto_generate_daily_report_after_scheduler_run(conn, completed_at=completed_at, next_run_at=next_run_at)

    assert result["status"] == "skipped"
    assert conn.report_rows == {}


def test_daily_report_analytics_builds_trends_comparisons_and_weekly_summary() -> None:
    conn = DailyReportConn()
    for offset in range(3):
        report_date = date(2026, 7, 10 + offset)
        report = generate_daily_research_report(conn, report_date)
        report["summary"]["assets_scanned"]["symbols"] = ["TSLA", "AAPL"] if offset else ["TSLA"]
        report["summary"]["setups_found"]["alerts"] = [{"symbol": "TSLA", "strategy_id": "momentum_bull_v2_007"}]
        report["summary"]["no_setup_decisions"]["reviews"] = [{"symbol": "AAPL", "strategy_id": "momentum_bull_v2"}]
        conn.report_rows[report_date] = report

    analytics = build_daily_report_analytics(conn)

    assert len(analytics["series"]) == 3
    assert analytics["windows"]["7d"]["setups_found"] >= 3
    assert analytics["asset_comparison"][0]["symbol"] in {"AAPL", "TSLA"}
    assert analytics["strategy_comparison"]
    assert analytics["recurring_operational_failures"]
    assert analytics["weekly_summary"]["simulation_only"] is True
