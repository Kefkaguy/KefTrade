from datetime import UTC, datetime

from app.services import research_campaigns
from app.services.research_campaigns import (
    candidate_consistency_summaries,
    claim_campaign_jobs,
    closed_trade_attribution,
    create_research_campaign,
    calculate_evidence_drift,
    data_readiness_for_job,
    forward_validation_state,
    generate_discovery_candidates,
    research_heatmaps,
    run_research_campaign_batch,
)


class Result:
    def __init__(self, rows=None):
        self.rows = rows or []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class CampaignConn:
    def __init__(self):
        now = datetime.now(UTC)
        self.now = now
        self.universes = []
        self.campaigns = []
        self.jobs = []
        self.batches = []
        self.elite = []
        self.commits = 0

    def execute(self, query, params=None):
        params = params or ()
        stripped = query.strip()
        if stripped.startswith("CREATE TABLE") or stripped.startswith("ALTER TABLE") or stripped.startswith("DROP INDEX") or stripped.startswith("CREATE INDEX"):
            return Result([])
        if "DROP CONSTRAINT" in query or "ADD CONSTRAINT" in query:
            return Result([])
        if "INSERT INTO research_campaign_scheduler" in query:
            return Result([])
        if "INSERT INTO research_universes" in query:
            key = params[0]
            if not any(row["universe_key"] == key for row in self.universes):
                self.universes.append(
                    {
                        "universe_key": key,
                        "name": params[1],
                        "description": params[2],
                        "assets": jsonb(params[3]),
                        "default_timeframes": jsonb(params[4]),
                        "metadata": jsonb(params[5]),
                        "is_active": True,
                        "updated_at": self.now,
                        "simulation_only": True,
                    }
                )
            return Result([])
        if "FROM research_universes" in query and "WHERE universe_key" in query:
            return Result([row for row in self.universes if row["universe_key"] == params[0]])
        if "INSERT INTO research_campaigns" in query:
            existing = next((row for row in self.campaigns if row["campaign_key"] == params[0]), None)
            if existing:
                return Result([existing])
            row = {
                "id": len(self.campaigns) + 1,
                "campaign_key": params[0],
                "name": params[1],
                "universe_key": params[2],
                "status": "queued",
                "requested_candidates": params[3],
                "queued_jobs": 0,
                "completed_jobs": 0,
                "failed_jobs": 0,
                "rejected_candidates": 0,
                "promoted_candidates": 0,
                "analytics": {},
                "controls": jsonb(params[4]),
                "safety_statement": params[6],
                "created_at": self.now,
                "updated_at": self.now,
                "simulation_only": True,
            }
            self.campaigns.append(row)
            return Result([row])
        if "WITH claimable AS" in query:
            rows = [row for row in self.jobs if row["campaign_id"] == params[0] and row["status"] in {"queued", "retrying", "blocked_data", "deferred_rate_limit"} and row["attempts"] < params[1]][: params[2]]
            for row in rows:
                row["status"] = "running"
                row["worker_id"] = params[3]
                row["attempts"] += 1
            return Result(rows)
        if "INSERT INTO research_campaign_batches" in query:
            existing = next((row for row in self.batches if row["batch_key"] == params[1]), None)
            if existing:
                return Result([existing])
            row = {"id": len(self.batches) + 1, "campaign_id": params[0], "batch_key": params[1], "batch_number": params[2], "job_count": 0, "simulation_only": True}
            self.batches.append(row)
            return Result([row])
        if "UPDATE research_campaign_batches SET job_count" in query:
            batch = next(row for row in self.batches if row["id"] == params[0])
            batch["job_count"] += 1
            return Result([])
        if "UPDATE research_campaign_batches" in query:
            batch = next(row for row in self.batches if row["id"] == params[5])
            batch["status"] = params[0]
            batch["job_count"] = params[1]
            batch["completed_jobs"] = params[2]
            batch["failed_jobs"] = params[3]
            return Result([])
        if "INSERT INTO research_campaign_jobs" in query:
            if any(row["job_key"] == params[2] for row in self.jobs):
                return Result([])
            row = {
                "id": len(self.jobs) + 1,
                "campaign_id": params[0],
                "batch_id": params[1],
                "job_key": params[2],
                "candidate_id": params[3],
                "family_id": params[4],
                "symbol": params[5],
                "timeframe": params[6],
                "strategy_family": params[7],
                "status": "queued",
                "candidate": jsonb(params[8]),
                "result": {},
                "validation_score": 0,
                "consistency_score": 0,
                "failure_reasons": [],
                "attempts": 0,
                "latest_error": None,
                "updated_at": self.now,
                "simulation_only": True,
            }
            self.jobs.append(row)
            return Result([{"id": row["id"]}])
        if "SELECT * FROM research_campaigns WHERE id" in query:
            return Result([row for row in self.campaigns if row["id"] == params[0]])
        if "SELECT symbol, asset_class, is_active FROM symbols" in query:
            return Result([{"symbol": params[0], "asset_class": "equity", "is_active": True}])
        if "COUNT(*) AS candle_count" in query:
            return Result([{"candle_count": 200, "latest_candle_timestamp": self.now}])
        if "COUNT(*) AS feature_count" in query:
            return Result([{"feature_count": 200}])
        if "UPDATE research_campaigns" in query and "status = 'running'" in query:
            campaign = self.campaign(params[0])
            campaign["status"] = "running"
            return Result([])
        if "FROM research_campaign_jobs" in query and "status = 'queued'" in query and "LIMIT" in query:
            rows = [row for row in self.jobs if row["campaign_id"] == params[0] and row["status"] == "queued"]
            return Result(rows[: params[1]])
        if "UPDATE research_campaign_jobs SET status = 'running'" in query:
            job = self.job(params[0])
            job["status"] = "running"
            job["attempts"] += 1
            return Result([])
        if "UPDATE research_campaign_jobs" in query and "heartbeat_at = NOW()" in query:
            return Result([])
        if "UPDATE research_campaign_jobs" in query and "validation_score" in query:
            job = self.job(params[4])
            job["status"] = params[0]
            job["result"] = jsonb(params[1])
            job["validation_score"] = params[2]
            job["failure_reasons"] = jsonb(params[3])
            return Result([])
        if "UPDATE research_campaign_jobs" in query and "worker_timeout" in query:
            return Result([])
        if "SELECT COUNT(*) AS count" in query and "status IN" in query and "campaign_id" in query:
            return Result([{"count": sum(1 for row in self.jobs if row["campaign_id"] == params[0] and row["status"] in {"queued", "running", "retrying", "blocked_data", "deferred_rate_limit"})}])
        if "SELECT COUNT(*) AS count" in query:
            return Result([{"count": sum(1 for row in self.jobs if row["campaign_id"] == params[0] and row["status"] == "queued")}])
        if "SELECT status, COUNT(*) AS count" in query:
            counts = {}
            for row in self.jobs:
                if row["campaign_id"] == params[0]:
                    counts[row["status"]] = counts.get(row["status"], 0) + 1
            return Result([{"status": key, "count": value} for key, value in counts.items()])
        if "UPDATE research_campaigns" in query and "queued_jobs" in query:
            campaign = self.campaign(params[3])
            campaign["queued_jobs"] = params[0]
            campaign["completed_jobs"] = params[1]
            campaign["failed_jobs"] = params[2]
            return Result([])
        if "SELECT batch_id, status, COUNT(*) AS count" in query:
            counts = {}
            for row in self.jobs:
                if row["campaign_id"] == params[0] and row.get("batch_id") is not None:
                    key = (row["batch_id"], row["status"])
                    counts[key] = counts.get(key, 0) + 1
            return Result([{"batch_id": key[0], "status": key[1], "count": value} for key, value in counts.items()])
        if "FROM research_campaign_jobs" in query and "ORDER BY candidate_id" in query:
            return Result([row for row in self.jobs if row["campaign_id"] == params[0]])
        if "FROM research_campaign_jobs" in query and "WHERE campaign_id" in query:
            return Result([row for row in self.jobs if row["campaign_id"] == params[0]])
        if "INSERT INTO elite_research_candidates" in query:
            self.elite.append({"campaign_id": params[0], "candidate_id": params[1], "research_score": params[5], "simulation_only": True})
            return Result([])
        if "FROM elite_research_candidates" in query:
            return Result([row for row in self.elite if row.get("campaign_id") == params[0]])
        if "FROM research_campaign_batches" in query:
            return Result([row for row in self.batches if row["campaign_id"] == params[0]])
        if "INSERT INTO research_campaign_analytics_snapshots" in query:
            return Result([])
        if "INSERT INTO research_campaign_reports" in query:
            return Result([{"id": 1, "campaign_id": params[0], "report_key": params[1], "title": params[2], "summary": jsonb(params[3]), "recommendations": jsonb(params[4]), "markdown_report": params[5], "simulation_only": True}])
        if "FROM paper_accounts" in query:
            return Result([])
        if "FROM paper_equity_curve" in query:
            return Result([])
        if "UPDATE research_campaigns" in query and "status = 'completed'" in query:
            campaign = self.campaign(params[3])
            campaign["status"] = "completed"
            campaign["promoted_candidates"] = params[0]
            campaign["rejected_candidates"] = params[1]
            campaign["analytics"] = jsonb(params[2])
            return Result([])
        if "UPDATE research_campaign_jobs" in query and "consistency_score" in query:
            for row in self.jobs:
                if row["campaign_id"] == params[1] and row["candidate_id"] == params[2]:
                    row["consistency_score"] = params[0]
            return Result([])
        if "UPDATE research_campaigns SET analytics" in query:
            self.campaign(params[1])["analytics"] = jsonb(params[0])
            return Result([])
        raise AssertionError(query)

    def campaign(self, campaign_id):
        return next(row for row in self.campaigns if row["id"] == campaign_id)

    def job(self, job_id):
        return next(row for row in self.jobs if row["id"] == job_id)

    def commit(self):
        self.commits += 1


def jsonb(value):
    return getattr(value, "obj", value)


def test_large_strategy_generation_supports_thousands_without_duplicates() -> None:
    candidates = generate_discovery_candidates(max_candidates=1000)

    assert len(candidates) == 1000
    assert len({candidate.candidate_id for candidate in candidates}) == 1000
    assert all(candidate.parent_candidate_id is None for candidate in candidates)


def test_campaign_lifecycle_promotes_only_cross_validated_candidates(monkeypatch) -> None:
    conn = CampaignConn()

    def fake_campaign_job(_conn, job):
        return {
            "candidate_id": job["candidate_id"],
            "family_id": job["family_id"],
            "symbol": job["symbol"],
            "timeframe": job["timeframe"],
            "metrics": {
                "profit_factor": 1.5,
                "expectancy_per_trade": 2.0,
                "max_drawdown": 0.04,
                "number_of_trades": 40,
                "walk_forward": {"enabled": True},
            },
            "paper_readiness": {"paper_ready": True, "failed_reasons": []},
            "regime_analysis": {"by_market_regime": [], "by_volatility_regime": []},
            "research_score": 4.2,
            "failure_reasons": [],
        }

    monkeypatch.setattr(research_campaigns, "run_campaign_job", fake_campaign_job)

    created = create_research_campaign(conn, universe_key="sp500_leaders", max_candidates=1, asset_limit=2, timeframes=["1h"])
    result = run_research_campaign_batch(conn, campaign_id=created["campaign"]["id"], batch_size=10)

    assert created["jobs_created"] == 2
    assert result["processed"] == 2
    assert conn.campaigns[0]["status"] == "completed"
    assert conn.campaigns[0]["promoted_candidates"] == 1
    assert conn.elite[0]["simulation_only"] is True
    assert conn.jobs[0]["consistency_score"] == 1.0


def test_consistency_summary_records_failure_causes() -> None:
    rows = [
        {"candidate_id": "sd_1", "family_id": "family_1", "symbol": "AAPL", "timeframe": "1h", "status": "rejected", "validation_score": 0, "failure_reasons": ["poor_expectancy"], "result": {}},
        {"candidate_id": "sd_1", "family_id": "family_1", "symbol": "MSFT", "timeframe": "1h", "status": "promoted", "validation_score": 2, "failure_reasons": [], "result": {"metrics": {"profit_factor": 1.3, "expectancy_per_trade": 1, "max_drawdown": 0.03, "number_of_trades": 35}}},
    ]

    summary = candidate_consistency_summaries(rows)[0]

    assert summary["stability"] == 0.5
    assert summary["assets_passed"] == 1
    assert summary["failure_reasons"] == ["poor_expectancy"]


def test_worker_claims_are_idempotent_across_workers() -> None:
    conn = CampaignConn()
    created = create_research_campaign(conn, universe_key="sp500_leaders", max_candidates=1, asset_limit=2, timeframes=["1h"])

    first = claim_campaign_jobs(conn, campaign_id=created["campaign"]["id"], worker_id="worker-a", batch_size=10, lease_seconds=900, retry_limit=3)
    second = claim_campaign_jobs(conn, campaign_id=created["campaign"]["id"], worker_id="worker-b", batch_size=10, lease_seconds=900, retry_limit=3)

    assert len(first) == 2
    assert second == []
    assert {row["worker_id"] for row in conn.jobs} == {"worker-a"}


def test_data_readiness_blocks_stale_campaign_jobs() -> None:
    conn = CampaignConn()
    conn.now = datetime(2020, 1, 1, tzinfo=UTC)

    readiness = data_readiness_for_job(conn, {"symbol": "AAPL", "timeframe": "1h"})

    assert readiness["ready"] is False
    assert readiness["status"] == "blocked_data"
    assert readiness["failure_classification"] == "stale_data"


def test_forward_validation_transition_requires_real_paper_sample() -> None:
    thresholds = {
        "minimum_active_paper_days": 5,
        "minimum_closed_trades": 5,
        "minimum_paper_profit_factor": 1.1,
        "minimum_paper_expectancy": 0,
        "maximum_paper_drawdown": 0.15,
        "maximum_execution_error_rate": 0.05,
        "maximum_stale_data_block_rate": 0.2,
    }
    insufficient = {"active_paper_trading_days": 2, "closed_trade_count": 2, "simulated_orders": 4}
    passed = {
        "active_paper_trading_days": 7,
        "closed_trade_count": 6,
        "simulated_orders": 8,
        "paper_expectancy": 12,
        "paper_profit_factor": 1.4,
        "paper_max_drawdown": 0.05,
        "execution_error_rate": 0.0,
        "stale_data_block_rate": 0.0,
    }

    assert forward_validation_state(insufficient, thresholds, deployed=True) == "insufficient_forward_sample"
    assert forward_validation_state(passed, thresholds, deployed=True) == "forward_validation_passed"


def test_evidence_drift_classifies_severe_forward_underperformance() -> None:
    elite = {
        "candidate_id": "sd_test",
        "profit_factor": 2.0,
        "expectancy": 10.0,
        "max_drawdown": 0.05,
        "validation_history": [{"metrics": {"win_rate": 0.6, "number_of_trades": 50}, "parameters": {"slippage_rate": 0.001}}],
    }
    paper = {
        "paper_profit_factor": 0.5,
        "paper_expectancy": -2.0,
        "paper_win_rate": 0.2,
        "paper_max_drawdown": 0.18,
        "signal_frequency": 2,
        "average_simulated_slippage": 0.004,
    }

    drift = calculate_evidence_drift(elite, paper)

    severe = {row["metric_name"] for row in drift if row["drift_classification"] == "severe"}
    assert "profit_factor" in severe
    assert "expectancy" in severe
    assert "drawdown" in severe


def test_closed_trade_attribution_pairs_simulated_entries_and_exits() -> None:
    fills = [
        {"side": "buy", "quantity": 2, "fill_price": 100, "fee": 1, "slippage": 0.1, "filled_at": "2026-01-01T00:00:00+00:00"},
        {"side": "sell", "quantity": 1, "fill_price": 110, "fee": 1, "slippage": 0.1, "filled_at": "2026-01-02T00:00:00+00:00"},
        {"side": "sell", "quantity": 1, "fill_price": 90, "fee": 1, "slippage": 0.1, "filled_at": "2026-01-03T00:00:00+00:00"},
    ]

    attribution = closed_trade_attribution(fills)

    assert attribution["closed_trade_count"] == 2
    assert attribution["paper_win_rate"] == 0.5
    assert attribution["average_trade_duration_hours"] == 36
    assert len(attribution["closed_trades"]) == 2


def test_research_heatmaps_group_assets_families_and_timeframes() -> None:
    jobs = [
        {"symbol": "AAPL", "strategy_family": "Breakout", "timeframe": "1h", "validation_score": 2, "status": "promoted"},
        {"symbol": "AAPL", "strategy_family": "Breakout", "timeframe": "4h", "validation_score": 0, "status": "rejected"},
        {"symbol": "MSFT", "strategy_family": "Trend Following", "timeframe": "1h", "validation_score": 1, "status": "promoted"},
    ]

    heatmaps = research_heatmaps(jobs)

    assert heatmaps["asset_heatmap"]
    assert heatmaps["strategy_family_heatmap"]
    assert any(row["x"] == "AAPL" and row["promotion_rate"] == 0.5 for row in heatmaps["asset_heatmap"])
