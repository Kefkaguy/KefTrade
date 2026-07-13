from datetime import UTC, datetime
from decimal import Decimal

from app.services import research_automation
from app.services.research_automation import (
    analyze_research_automation,
    generate_failure_hypothesis,
    queue_research_automation,
    run_research_automation_batch,
)


class Result:
    def __init__(self, rows=None):
        self.rows = rows or []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class AutomationConn:
    def __init__(self):
        self.queue = []
        self.runs = []
        self.strategy_experiments = []
        self.commits = 0
        self.universe = [
            {
                "symbol": "TSLA",
                "timeframe": "1h",
                "asset_class": "us_equity",
                "name": "Tesla",
                "candle_count": 250,
                "feature_count": 230,
                "latest_candle_timestamp": datetime(2026, 7, 12, tzinfo=UTC),
            }
        ]
        self.features = [{"timestamp": datetime(2026, 7, 1, tzinfo=UTC)} for _ in range(120)]

    def execute(self, query, params=None):
        if "CREATE TABLE" in query or "CREATE INDEX" in query:
            return Result([])
        if "SELECT c.symbol, c.timeframe" in query:
            return Result(self.universe)
        if "INSERT INTO research_automation_queue" in query:
            if any(row["job_key"] == params[0] for row in self.queue):
                return Result([])
            row = {
                "id": len(self.queue) + 1,
                "job_key": params[0],
                "symbol": params[1],
                "timeframe": params[2],
                "experiment_id": params[3],
                "strategy_name": params[4],
                "status": "queued",
                "priority": params[5],
                "reason": params[6],
                "attempts": 0,
                "simulation_only": True,
                "created_at": datetime(2026, 7, 12, tzinfo=UTC),
            }
            self.queue.append(row)
            return Result([{"id": row["id"]}])
        if "FROM research_automation_queue" in query and "WHERE status = 'queued'" in query:
            queued = [row for row in self.queue if row["status"] == "queued"][: params[0]]
            return Result(queued)
        if "UPDATE research_automation_queue" in query and "status = 'running'" in query:
            self.queue[params[0] - 1]["status"] = "running"
            self.queue[params[0] - 1]["attempts"] += 1
            return Result([])
        if "UPDATE research_automation_queue" in query and "status = 'completed'" in query:
            self.queue[params[0] - 1]["status"] = "completed"
            return Result([])
        if "UPDATE research_automation_queue" in query and "status = 'failed'" in query:
            self.queue[params[1] - 1]["status"] = "failed"
            self.queue[params[1] - 1]["latest_error"] = params[0]
            return Result([])
        if "FROM features" in query:
            return Result(self.features)
        if "INSERT INTO research_automation_runs" in query:
            self.runs.append(
                {
                    "queue_id": params[0],
                    "symbol": params[1],
                    "timeframe": params[2],
                    "experiment_id": params[3],
                    "strategy_name": params[4],
                    "result": jsonb(params[5]),
                    "generated_hypothesis": jsonb(params[6]),
                    "objective_metrics": jsonb(params[7]),
                    "automation_version": params[8],
                    "simulation_only": True,
                    "created_at": datetime(2026, 7, 12, tzinfo=UTC),
                }
            )
            return Result([])
        if "INSERT INTO strategy_experiments" in query:
            self.strategy_experiments.append({"name": params[0], "result": jsonb(params[7]), "recommendation": params[8]})
            return Result([])
        if "SELECT status, COUNT(*) AS count" in query:
            counts = {}
            for row in self.queue:
                counts[row["status"]] = counts.get(row["status"], 0) + 1
            return Result([{"status": key, "count": value} for key, value in counts.items()])
        if "SELECT symbol, timeframe, experiment_id" in query:
            return Result(self.runs[-20:])
        if "SELECT *" in query and "FROM research_automation_runs" in query:
            return Result(self.runs)
        raise AssertionError(query)

    def commit(self):
        self.commits += 1


def jsonb(value):
    return getattr(value, "obj", value)


def automation_report(profit_factor=0.8, expectancy=-5, trades=12, drawdown=0.03):
    return {
        "experiment": {"strategy": "momentum"},
        "max_runs": 2,
        "effective_sweep": {"returns_5_min": [0.004, 0.01]},
        "ranking_table": [
            {
                "run_id": "momentum_v1_001",
                "strategy_name": "momentum",
                "strategy_version": "v1",
                "parameters": {"returns_5_min": 0.004, "risk_reward": 1.5},
                "metrics": {
                    "profit_factor": profit_factor,
                    "expectancy_per_trade": expectancy,
                    "number_of_trades": trades,
                    "max_drawdown": drawdown,
                    "walk_forward": {"enabled": True},
                },
                "by_market_regime": [{"regime": "sideways", "metrics": {"number_of_trades": 8, "expectancy_per_trade": -4}}],
                "by_volatility_regime": [],
                "by_year": [],
                "paper_readiness": {"paper_ready": False},
                "recommendation": "Reject",
                "rank_score": -2,
            },
            {
                "run_id": "momentum_v1_002",
                "strategy_name": "momentum",
                "strategy_version": "v1",
                "parameters": {"returns_5_min": 0.01, "risk_reward": 1.5},
                "metrics": {"profit_factor": 0.5, "expectancy_per_trade": -10, "number_of_trades": 10, "max_drawdown": 0.04},
                "by_market_regime": [],
                "by_volatility_regime": [],
                "by_year": [],
                "paper_readiness": {"paper_ready": False},
                "recommendation": "Reject",
                "rank_score": -5,
            },
        ],
        "experiment_report": "# Automated report",
    }


def test_queue_research_automation_creates_jobs_and_skips_duplicates() -> None:
    conn = AutomationConn()

    first = queue_research_automation(conn, asset_limit=1, timeframes=["1h"], max_experiments_per_asset=2)
    second = queue_research_automation(conn, asset_limit=1, timeframes=["1h"], max_experiments_per_asset=2)

    assert first["queued"] == 2
    assert second["queued"] == 0
    assert second["skipped_duplicates"] == 2
    assert all(row["simulation_only"] is True for row in conn.queue)


def test_run_research_automation_batch_persists_run_and_strategy_experiment(monkeypatch) -> None:
    conn = AutomationConn()
    queue_research_automation(conn, asset_limit=1, timeframes=["1h"], max_experiments_per_asset=1)
    monkeypatch.setattr(research_automation, "load_candles", lambda conn, symbol, timeframe: [{"timestamp": datetime(2026, 7, 1, tzinfo=UTC)} for _ in range(150)])
    monkeypatch.setattr(research_automation, "load_regimes", lambda conn, symbol, timeframe: [])
    monkeypatch.setattr(research_automation, "run_strategy_experiment", lambda **kwargs: automation_report())

    result = run_research_automation_batch(conn, batch_size=1, max_runs_per_experiment=2)

    assert result["completed"] == 1
    assert conn.queue[0]["status"] == "completed"
    assert conn.runs[0]["automation_version"] == "research_automation_v1"
    assert conn.strategy_experiments[0]["recommendation"] == "Reject"
    assert "sideways" in conn.runs[0]["generated_hypothesis"]["hypothesis"]


def test_generate_failure_hypothesis_identifies_sideways_and_weak_metrics() -> None:
    job = {"symbol": "TSLA", "timeframe": "1h", "experiment_id": "momentum_trend_return_sweep"}

    hypothesis = generate_failure_hypothesis(job, automation_report())

    assert "weak_profit_factor" in hypothesis["failure_reasons"]
    assert "insufficient_trades" in hypothesis["failure_reasons"]
    assert hypothesis["dominant_failure_regime"] == "sideways"
    assert "trend confirmation" in hypothesis["hypothesis"]


def test_analyze_research_automation_uses_stored_evidence_only() -> None:
    conn = AutomationConn()
    conn.runs = [
        {
            "symbol": "TSLA",
            "timeframe": "1h",
            "strategy_name": "momentum",
            "objective_metrics": {"profit_factor": 1.2, "expectancy_per_trade": 3, "max_drawdown": 0.04, "number_of_trades": 50, "paper_ready": False},
            "generated_hypothesis": {"failure_reasons": ["weak_profit_factor"], "dominant_failure_regime": "sideways"},
            "result": automation_report(1.2, 3, 50, 0.04),
            "created_at": datetime(2026, 7, 12, tzinfo=UTC),
            "simulation_only": True,
        },
        {
            "symbol": "NVDA",
            "timeframe": "1h",
            "strategy_name": "breakout",
            "objective_metrics": {"profit_factor": 0.7, "expectancy_per_trade": -2, "max_drawdown": 0.08, "number_of_trades": 20, "paper_ready": False},
            "generated_hypothesis": {"failure_reasons": ["poor_expectancy"], "dominant_failure_regime": "bear_trend"},
            "result": automation_report(0.7, -2, 20, 0.08),
            "created_at": datetime(2026, 7, 13, tzinfo=UTC),
            "simulation_only": True,
        },
    ]

    analysis = analyze_research_automation(conn)

    assert analysis["run_count"] == 2
    assert analysis["best_strategy_families"][0]["name"] == "momentum"
    assert analysis["weak_regimes"][0]["regime"] == "sideways"
    assert analysis["simulation_only"] is True
