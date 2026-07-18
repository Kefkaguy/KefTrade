from datetime import UTC, datetime
from dataclasses import asdict
from decimal import Decimal
import math

from app.services import research_campaigns
from app.services.strategy_discovery import jsonable
from app.services.research_campaigns import (
    candidate_consistency_summaries,
    classify_worker_error,
    claim_campaign_jobs,
    closed_trade_attribution,
    create_research_campaign,
    calculate_evidence_drift,
    data_readiness_for_job,
    forward_validation_state,
    generate_discovery_candidates,
    campaign_list_row_with_eta,
    passes_cross_validation,
    passes_single_market_validation,
    overfit_regime_robustness_blueprint,
    quality_first_campaign_blueprint,
    research_heatmaps,
    run_parallel_campaign_batch,
    run_research_campaign_batch,
    single_asset_generalization_blueprint,
    strategy_redesign_blueprint,
    transferability_sample_size_blueprint,
    volatility_adaptive_relative_strength_blueprint,
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
        self.workers = []
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
                "scheduling_config": jsonb(params[5]),
                "target_workers": 0,
                "execution_status": "idle",
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
        if "UPDATE research_campaigns" in query and "target_workers" in query:
            campaign = self.campaign(params[3])
            campaign["status"] = "running"
            campaign["target_workers"] = params[0]
            campaign["scheduling_config"] = jsonb(params[2])
            return Result([])
        if "UPDATE research_campaigns" in query and "status = 'running'" in query:
            campaign = self.campaign(params[0])
            campaign["status"] = "running"
            return Result([])
        if "INSERT INTO research_campaign_workers" in query:
            existing = next((row for row in self.workers if row["worker_id"] == params[0]), None)
            row = existing or {
                "worker_id": params[0],
                "campaign_id": None,
                "process_id": params[1],
                "hostname": params[2],
                "status": params[3],
                "registered_at": self.now,
                "heartbeat_at": self.now,
                "last_heartbeat_at": self.now,
                "current_job_id": None,
                "processed_jobs": 0,
                "error_count": 0,
                "simulation_only": True,
            }
            row["status"] = params[3]
            if existing is None:
                self.workers.append(row)
            return Result([row])
        if "UPDATE research_campaign_workers" in query and "current_job_id" in query:
            worker = next((row for row in self.workers if row["worker_id"] == params[3]), None)
            if worker:
                worker["campaign_id"] = params[0]
                worker["current_job_id"] = params[1]
                worker["status"] = params[2]
            return Result([])
        if "UPDATE research_campaign_workers" in query:
            return Result([])
        if "FROM research_campaign_workers" in query and "campaign_id" in query:
            return Result([row for row in self.workers if row.get("campaign_id") == params[0]])
        if "FROM research_campaign_workers" in query:
            return Result(self.workers)
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


def test_jsonable_normalizes_non_finite_values_for_postgres_jsonb() -> None:
    assert jsonable({"nan": math.nan, "infinite": math.inf, "decimal_nan": Decimal("NaN")}) == {
        "nan": None,
        "infinite": None,
        "decimal_nan": None,
    }


def test_worker_error_classifier_does_not_mistake_invalid_json_for_missing_data() -> None:
    error = ValueError('invalid input syntax for type json: Token "NaN" is invalid')

    assert classify_worker_error(error) == "database_error"


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
    commits_before_run = conn.commits
    result = run_research_campaign_batch(conn, campaign_id=created["campaign"]["id"], batch_size=10)

    assert created["jobs_created"] == 2
    assert result["processed"] == 2
    assert conn.campaigns[0]["status"] == "completed"
    assert conn.campaigns[0]["promoted_candidates"] == 1
    assert conn.elite[0]["simulation_only"] is True
    assert conn.jobs[0]["consistency_score"] == 1.0
    assert conn.commits - commits_before_run >= 4


def test_worker_dataset_cache_survives_multiple_claim_batches(monkeypatch) -> None:
    conn = CampaignConn()
    shared_cache = {}
    observed_caches = []

    def fake_campaign_job(_conn, job):
        observed_caches.append(job["_dataset_cache"])
        return {
            "candidate_id": job["candidate_id"],
            "family_id": job["family_id"],
            "metrics": {"profit_factor": 0, "expectancy_per_trade": 0, "max_drawdown": 0, "number_of_trades": 0, "walk_forward": {"enabled": True}},
            "paper_readiness": {"paper_ready": False},
            "regime_analysis": {},
            "research_score": 0,
            "failure_reasons": ["insufficient_trades"],
        }

    monkeypatch.setattr(research_campaigns, "run_campaign_job", fake_campaign_job)
    created = create_research_campaign(conn, universe_key="sp500_leaders", max_candidates=1, asset_limit=2, timeframes=["1h"])

    run_research_campaign_batch(conn, campaign_id=created["campaign"]["id"], batch_size=1, dataset_cache=shared_cache)
    run_research_campaign_batch(conn, campaign_id=created["campaign"]["id"], batch_size=1, dataset_cache=shared_cache)

    assert observed_caches == [shared_cache, shared_cache]


def test_parallel_scale_request_persists_new_target_without_api_pool() -> None:
    conn = CampaignConn()
    created = create_research_campaign(conn, universe_key="sp500_leaders", max_candidates=1, asset_limit=2, timeframes=["1h"])
    campaign_id = created["campaign"]["id"]

    first = run_parallel_campaign_batch(conn, campaign_id=campaign_id, workers=1, jobs_per_worker=20)
    second = run_parallel_campaign_batch(conn, campaign_id=campaign_id, workers=8, jobs_per_worker=20)

    assert first["started"] is True
    assert second["already_active"] is True
    assert second["scaled"] is True
    assert second["workers"] == min(8, research_campaigns.campaign_worker_limit())
    assert conn.campaign(campaign_id)["target_workers"] == second["workers"]


def test_campaign_eta_uses_rolling_or_profiled_backend_method() -> None:
    row = {
        "status": "running",
        "total_jobs": 100,
        "terminal_jobs": 20,
        "blocked_jobs": 0,
        "deferred_jobs": 0,
        "terminal_jobs_5m": 20,
        "terminal_jobs_15m": 20,
        "average_profiled_runtime_ms": 2500,
        "profiled_jobs": 20,
        "target_workers": 4,
    }

    calculated = campaign_list_row_with_eta(row)

    assert calculated["eta_method"] == "rolling_5m"
    assert calculated["eta_seconds"] == 1200
    assert calculated["executable_remaining_jobs"] == 80


def test_consistency_summary_records_failure_causes() -> None:
    rows = [
        {"candidate_id": "sd_1", "family_id": "family_1", "symbol": "AAPL", "timeframe": "1h", "status": "rejected", "validation_score": 0, "failure_reasons": ["poor_expectancy"], "result": {}},
        {"candidate_id": "sd_1", "family_id": "family_1", "symbol": "MSFT", "timeframe": "1h", "status": "promoted", "validation_score": 2, "failure_reasons": [], "result": {"metrics": {"profit_factor": 1.3, "expectancy_per_trade": 1, "max_drawdown": 0.03, "number_of_trades": 35}}},
    ]

    summary = candidate_consistency_summaries(rows)[0]

    assert summary["stability"] == 0.5
    assert summary["assets_passed"] == 1
    assert summary["failure_reasons"] == ["poor_expectancy"]


def test_no_loss_metrics_pass_and_pool_without_becoming_zero_profit_factor() -> None:
    result = {
        "metrics": {
            "gross_profit": 120,
            "gross_loss": 0,
            "profit_factor": None,
            "profit_factor_is_infinite": True,
            "expectancy_per_trade": 3,
            "max_drawdown": 0.02,
            "number_of_trades": 35,
            "walk_forward": {"enabled": True},
        },
        "paper_readiness": {"paper_ready": True},
    }
    rows = [
        {"candidate_id": "sd_inf", "family_id": "family_inf", "symbol": "AAPL", "timeframe": "1h", "status": "promoted", "validation_score": 5, "failure_reasons": [], "result": result},
        {"candidate_id": "sd_inf", "family_id": "family_inf", "symbol": "MSFT", "timeframe": "1h", "status": "promoted", "validation_score": 5, "failure_reasons": [], "result": result},
    ]

    summary = candidate_consistency_summaries(rows)[0]

    assert passes_single_market_validation(result) is True
    assert summary["profit_factor_is_infinite"] is True
    assert summary["profit_factor"] > 1.2
    assert passes_cross_validation(summary) is True


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


def test_evidence_drift_waits_for_a_closed_forward_sample() -> None:
    elite = {
        "candidate_id": "sd_test",
        "profit_factor": 2.0,
        "expectancy": 10.0,
        "max_drawdown": 0.05,
        "validation_history": [],
    }
    paper = {
        "closed_trade_count": 0,
        "paper_profit_factor": 0,
        "paper_expectancy": 0,
        "paper_max_drawdown": 0,
    }

    drift = calculate_evidence_drift(elite, paper)

    assert drift
    assert {row["drift_classification"] for row in drift} == {"insufficient_forward_sample"}
    assert all(row["paper_value"] is None for row in drift)


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


def test_quality_first_blueprint_targets_phase_96_survivors_without_relaxing_gates() -> None:
    parents = generate_discovery_candidates(max_candidates=50)[:50]
    survivor_ids = {parents[0].candidate_id, parents[1].candidate_id, parents[2].candidate_id}
    rows = []
    for index, candidate in enumerate(parents):
        promoted = candidate.candidate_id in survivor_ids
        symbol = "AAPL" if promoted else ("LLY" if index % 3 == 0 else "AVGO")
        rows.append(
            {
                "id": index + 1,
                "campaign_id": 1,
                "candidate_id": candidate.candidate_id,
                "family_id": candidate.family_id,
                "strategy_family": "Pullback" if promoted else "Breakout",
                "symbol": symbol,
                "timeframe": "1h",
                "status": "promoted" if promoted else "rejected",
                "validation_score": 7.0 if promoted else -20.0,
                "candidate": asdict(candidate),
                "result": {
                    "metrics": {
                        "profit_factor": 1.6 if promoted or symbol in {"LLY", "AVGO"} else 0.8,
                        "expectancy_per_trade": 25 if promoted or symbol in {"LLY", "AVGO"} else -4,
                        "max_drawdown": 0.04,
                        "number_of_trades": 40 if promoted else 12,
                        "walk_forward": {"enabled": True},
                    },
                    "regime_analysis": {
                        "by_market_regime": [{"regime": "bull_trend", "metrics": {"profit_factor": 1.2, "expectancy_per_trade": 4, "number_of_trades": 8}}],
                        "by_volatility_regime": [{"regime": "low_volatility", "metrics": {"profit_factor": 1.1, "expectancy_per_trade": 2, "number_of_trades": 8}}],
                    },
                },
                "failure_reasons": [] if promoted else ["Trade count 12 must be >= 30."],
            }
        )

    blueprint = quality_first_campaign_blueprint(rows, max_variants_per_parent=4, asset_limit=3)

    assert blueprint["diagnostics"]["generated_candidates"] == 50
    assert blueprint["diagnostics"]["candidate_level_failures"] == 47
    assert blueprint["diagnostics"]["single_market_research_candidates"] == 3
    assert blueprint["targeting"]["parents"] == sorted(survivor_ids, reverse=True)
    assert blueprint["targeting"]["assets"][0] == "AAPL"
    assert {"LLY", "AVGO"}.issubset(set(blueprint["targeting"]["assets"]))
    assert blueprint["targeting"]["timeframes"] == ["1h"]
    assert len(blueprint["candidates"]) == 12
    assert any(candidate.parent_candidate_id in survivor_ids for candidate in blueprint["candidates"])

    single_asset_summary = {
        "research_score": 7,
        "profit_factor": 1.6,
        "expectancy": 25,
        "max_drawdown": 0.04,
        "trade_count": 40,
        "stability": 1.0,
        "assets_passed": 1,
        "timeframes_passed": 1,
    }
    assert passes_cross_validation(single_asset_summary) is False


def test_phase_98_blueprint_is_focused_versioned_and_lineaged() -> None:
    candidates = generate_discovery_candidates(max_candidates=30)
    pullback_parent = next(candidate for candidate in candidates if candidate.blocks["entry"] == "pullback")
    trend_parent = next(candidate for candidate in candidates if candidate.blocks["entry"] == "trend_continuation")
    pullback_jobs = []
    for index in range(3):
        payload = asdict(pullback_parent)
        payload["candidate_id"] = f"pullback_parent_{index}"
        pullback_jobs.append(
            {
                "id": index + 1,
                "candidate_id": payload["candidate_id"],
                "family_id": payload["family_id"],
                "strategy_family": "Pullback",
                "symbol": "AAPL",
                "timeframe": "1h",
                "status": "promoted",
                "validation_score": 8 - index,
                "candidate": payload,
                "result": {"metrics": {"profit_factor": 1.4, "expectancy_per_trade": 20, "number_of_trades": 40, "max_drawdown": 0.04}},
            }
        )
    trend_jobs = []
    for index in range(2):
        payload = asdict(trend_parent)
        payload["candidate_id"] = f"trend_parent_{index}"
        trend_jobs.append(
            {
                "id": index + 20,
                "candidate_id": payload["candidate_id"],
                "family_id": payload["family_id"],
                "strategy_family": "Trend Following",
                "symbol": "NVDA",
                "timeframe": "1h",
                "status": "rejected",
                "validation_score": 5 - index,
                "candidate": payload,
                "result": {"metrics": {"profit_factor": 1.2, "expectancy_per_trade": 8, "number_of_trades": 20, "max_drawdown": 0.05}},
            }
        )

    blueprint = transferability_sample_size_blueprint(pullback_jobs, trend_jobs)

    assert blueprint["targeting"]["assets"] == ["AAPL", "NVDA", "AVGO", "LLY", "GOOGL", "JPM"]
    assert blueprint["targeting"]["timeframes"] == ["1h", "4h"]
    assert blueprint["targeting"]["candidate_count"] == 24
    assert blueprint["targeting"]["job_count"] == 288
    assert blueprint["tracks"]["breakout_containment"]["included"] is False
    assert all(row["parent_candidate_id"] for row in blueprint["lineage"])
    assert all(row["campaign_version"] == "phase_9_8_transferability_sample_size_v1" for row in blueprint["lineage"])
    assert {"Pullback", "Trend Following"} == {row["strategy_family"] for row in blueprint["lineage"]}


def test_phase_99_blueprint_is_overfit_focused_versioned_and_lineaged() -> None:
    candidates = generate_discovery_candidates(max_candidates=30)
    pullback_parent = next(candidate for candidate in candidates if candidate.blocks["entry"] == "pullback")
    trend_parent = next(candidate for candidate in candidates if candidate.blocks["entry"] == "trend_continuation")
    pullback_payload = asdict(pullback_parent)
    pullback_payload["candidate_id"] = "sd_7ce7b8ddc81b07"
    trend_payload = asdict(trend_parent)
    trend_payload["candidate_id"] = "sd_5cbcdf7c9eabcf"
    jobs = [
        {
            "id": 1,
            "candidate_id": pullback_payload["candidate_id"],
            "family_id": pullback_payload["family_id"],
            "strategy_family": "Pullback",
            "symbol": "AAPL",
            "timeframe": "1h",
            "status": "promoted",
            "validation_score": 8,
            "candidate": pullback_payload,
            "result": {"metrics": {"profit_factor": 1.4, "expectancy_per_trade": 20, "number_of_trades": 40, "max_drawdown": 0.04}},
        },
        {
            "id": 2,
            "candidate_id": trend_payload["candidate_id"],
            "family_id": trend_payload["family_id"],
            "strategy_family": "Trend Following",
            "symbol": "AAPL",
            "timeframe": "1h",
            "status": "rejected",
            "validation_score": 5,
            "candidate": trend_payload,
            "result": {"metrics": {"profit_factor": 1.7, "expectancy_per_trade": 13, "number_of_trades": 141, "max_drawdown": 0.03}},
        },
    ]

    blueprint = overfit_regime_robustness_blueprint(jobs)

    assert blueprint["targeting"]["assets"] == ["AAPL", "NVDA"]
    assert blueprint["targeting"]["timeframes"] == ["1h", "4h"]
    assert blueprint["targeting"]["candidate_count"] == 24
    assert blueprint["targeting"]["job_count"] == 96
    assert blueprint["tracks"]["breakout_containment"]["included"] is False
    assert blueprint["tracks"]["single_asset_robust_candidate"]["eligible_for_elite"] is False
    assert all(row["parent_candidate_id"] for row in blueprint["lineage"])
    assert all(row["hypothesis"] and row["data_window"] and row["regime_scope"] for row in blueprint["lineage"])
    assert all(row["campaign_version"] == "phase_9_9_overfit_regime_robustness_v1" for row in blueprint["lineage"])
    assert {"Pullback", "Trend Following"} == {row["strategy_family"] for row in blueprint["lineage"]}
    assert not any(row["strategy_family"] == "Breakout" for row in blueprint["lineage"])


def test_phase_910_blueprint_is_single_asset_generalization_focused_and_lineaged() -> None:
    candidates = generate_discovery_candidates(max_candidates=30)
    pullback_parent = next(candidate for candidate in candidates if candidate.blocks["entry"] == "pullback")
    trend_parent = next(candidate for candidate in candidates if candidate.blocks["entry"] == "trend_continuation")
    pullback_payload = asdict(pullback_parent)
    pullback_payload["candidate_id"] = "sd_a8d9508bee3c46"
    trend_payload = asdict(trend_parent)
    trend_payload["candidate_id"] = "sd_phase99_trend_parent"
    jobs = [
        {
            "id": 1,
            "candidate_id": pullback_payload["candidate_id"],
            "family_id": pullback_payload["family_id"],
            "strategy_family": "Pullback",
            "symbol": "AAPL",
            "timeframe": "1h",
            "status": "promoted",
            "validation_score": 80,
            "candidate": pullback_payload,
            "result": {"metrics": {"profit_factor": 1.6808, "expectancy_per_trade": 28.2448, "number_of_trades": 115, "max_drawdown": 0.0414}},
        },
        {
            "id": 2,
            "candidate_id": trend_payload["candidate_id"],
            "family_id": trend_payload["family_id"],
            "strategy_family": "Trend Following",
            "symbol": "AAPL",
            "timeframe": "1h",
            "status": "rejected",
            "validation_score": 5,
            "candidate": trend_payload,
            "result": {"metrics": {"profit_factor": 1.2, "expectancy_per_trade": 6, "number_of_trades": 34, "max_drawdown": 0.08}},
        },
    ]

    blueprint = single_asset_generalization_blueprint(jobs)

    assert blueprint["targeting"]["assets"] == ["AAPL", "MSFT", "GOOGL", "META", "QQQ", "SPY", "NVDA"]
    assert blueprint["targeting"]["timeframes"] == ["1h", "4h"]
    assert blueprint["targeting"]["candidate_count"] == 25
    assert blueprint["targeting"]["pullback_candidate_count"] == 20
    assert blueprint["targeting"]["trend_candidate_count"] == 5
    assert blueprint["targeting"]["job_count"] == 350
    assert blueprint["tracks"]["breakout_containment"]["included"] is False
    assert blueprint["diagnostics"]["elite_gate_policy"] == "Informational diagnostics never override passes_cross_validation."
    assert all(row["parent_candidate_id"] for row in blueprint["lineage"])
    assert all(row["hypothesis"] and row["temporal_window"] and row["regime_scope"] for row in blueprint["lineage"])
    assert all(row["normalized_or_fixed_logic"] and row["asset_scope"] and row["timeframe_sampling"] for row in blueprint["lineage"])
    assert all(row["campaign_version"] == "phase_9_10_single_asset_generalization_v1" for row in blueprint["lineage"])
    assert {"Pullback", "Trend Following"} == {row["strategy_family"] for row in blueprint["lineage"]}
    assert not any(row["strategy_family"] == "Breakout" for row in blueprint["lineage"])


def test_phase_911_blueprint_is_structural_redesign_not_local_tuning() -> None:
    candidates = generate_discovery_candidates(max_candidates=30)
    pullback_parent = next(candidate for candidate in candidates if candidate.blocks["entry"] == "pullback")
    trend_parent = next(candidate for candidate in candidates if candidate.blocks["entry"] == "trend_continuation")
    pullback_payload = asdict(pullback_parent)
    pullback_payload["candidate_id"] = "sd_3ffffc89f82b5c"
    trend_payload = asdict(trend_parent)
    trend_payload["candidate_id"] = "sd_phase910_trend_parent"
    jobs = [
        {
            "id": 1,
            "candidate_id": pullback_payload["candidate_id"],
            "family_id": pullback_payload["family_id"],
            "strategy_family": "Pullback",
            "symbol": "AAPL",
            "timeframe": "1h",
            "status": "promoted",
            "validation_score": 8,
            "candidate": pullback_payload,
            "result": {"metrics": {"profit_factor": 1.1861, "expectancy_per_trade": 3.4271, "number_of_trades": 282, "max_drawdown": 0.058}},
        },
        {
            "id": 2,
            "candidate_id": trend_payload["candidate_id"],
            "family_id": trend_payload["family_id"],
            "strategy_family": "Trend Following",
            "symbol": "AAPL",
            "timeframe": "1h",
            "status": "rejected",
            "validation_score": 0,
            "candidate": trend_payload,
            "result": {"metrics": {"profit_factor": 0.9, "expectancy_per_trade": -1, "number_of_trades": 40, "max_drawdown": 0.08}},
        },
    ]

    blueprint = strategy_redesign_blueprint(jobs)
    mix = blueprint["targeting"]["strategy_mix"]

    assert blueprint["targeting"]["candidate_count"] == 30
    assert blueprint["targeting"]["job_count"] == 210
    assert blueprint["targeting"]["timeframes"] == ["1h"]
    assert mix["Pullback"] == 20
    assert mix["Trend Following"] == 2
    assert sum(count for family, count in mix.items() if family not in {"Pullback", "Trend Following"}) == 8
    assert blueprint["tracks"]["trend_following_pause_decision"]["candidate_count"] == 2
    assert all(row["economic_hypothesis"] and row["new_rule"] and row["falsification_condition"] for row in blueprint["lineage"])
    assert all(row["added_complexity"]["added_rules"] <= 2 for row in blueprint["lineage"])
    assert all(row["campaign_version"] == "phase_9_11_strategy_redesign_v1" for row in blueprint["lineage"])


def test_phase_912_blueprint_is_focused_and_asset_agnostic() -> None:
    parent = next(candidate for candidate in generate_discovery_candidates(max_candidates=30) if candidate.blocks["entry"] == "pullback")
    payload = asdict(parent)
    jobs = [
        {
            "id": 1,
            "candidate_id": parent.candidate_id,
            "family_id": parent.family_id,
            "strategy_family": "Relative Strength Continuation",
            "symbol": "AAPL",
            "timeframe": "1h",
            "status": "rejected",
            "validation_score": 5,
            "candidate": payload,
            "result": {"metrics": {"profit_factor": 1.36, "expectancy_per_trade": 19, "number_of_trades": 34, "max_drawdown": .06}},
        }
    ]

    blueprint = volatility_adaptive_relative_strength_blueprint(jobs)

    assert blueprint["targeting"] == {"assets": ["AAPL", "NVDA"], "timeframes": ["1h"], "candidate_count": 12, "job_count": 24}
    assert all(candidate.parameters["adaptive_volatility_profiles"] is True for candidate in blueprint["candidates"])
    assert all("symbol" not in candidate.parameters for candidate in blueprint["candidates"])
    assert all(row["campaign_version"] == "phase_9_12_volatility_adaptive_relative_strength_v1" for row in blueprint["lineage"])
