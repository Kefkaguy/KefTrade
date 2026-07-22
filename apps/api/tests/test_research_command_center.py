from __future__ import annotations

from datetime import UTC, datetime
import json

from app.services.research_command_center import analyze_campaign, complete_command_center_payload, deterministic_evidence_plan, prepare_job, research_command_center


CAMPAIGN = {
    "id": 7,
    "campaign_key": "campaign_quality_v1",
    "name": "Candidate quality campaign",
    "universe_key": "test",
    "status": "running",
    "requested_candidates": 20,
}


def job(
    job_id: int,
    candidate_id: str,
    *,
    symbol: str = "AAPL",
    timeframe: str = "4h",
    status: str = "rejected",
    profit_factor: float = 1.0,
    expectancy: float = -1.0,
    trades: int = 20,
    drawdown: float = 0.1,
    canonical: str | None = None,
    parameter: int = 20,
) -> dict:
    checks = [
        {"name": "profit_factor", "passed": profit_factor >= 1.25},
        {"name": "positive_expectancy", "passed": expectancy > 0},
        {"name": "drawdown", "passed": drawdown <= 0.2},
        {"name": "trade_count", "passed": trades >= 30},
        {"name": "walk_forward_oos", "passed": True},
        {"name": "regime_stability", "passed": True},
    ]
    failures = [check["name"] for check in checks if not check["passed"]]
    blocks = {"entry": "pullback", "trend": "ema_20_50", "momentum": "rsi_55"}
    parameters = {"ema_fast": parameter, "risk_reward": 2.0}
    return {
        "id": job_id,
        "campaign_id": 7,
        "candidate_id": candidate_id,
        "family_id": "family-1",
        "strategy_family": "Pullback",
        "symbol": symbol,
        "timeframe": timeframe,
        "status": status,
        "candidate": {
            "candidate_id": candidate_id,
            "canonical_key": canonical or f"canonical-{candidate_id}",
            "blocks": blocks,
            "parameters": parameters,
        },
        "result": {
            "metrics": {
                "profit_factor": profit_factor,
                "expectancy_per_trade": expectancy,
                "number_of_trades": trades,
                "max_drawdown": drawdown,
                "win_rate": 0.5,
            },
            "blocks": blocks,
            "parameters": parameters,
            "failure_reasons": failures,
            "paper_readiness": {
                "checks": checks,
                "thresholds": {
                    "profit_factor": 1.25,
                    "expectancy_per_trade": 0.0,
                    "number_of_trades": 30,
                    "max_drawdown": 0.2,
                },
            },
            "walk_forward_metrics": {"enabled": True},
            "regime_analysis": {
                "by_market_regime": [{
                    "regime": "bull_trend",
                    "metrics": {
                        "profit_factor": profit_factor,
                        "expectancy_per_trade": expectancy,
                        "number_of_trades": trades,
                        "max_drawdown": drawdown,
                        "win_rate": 0.5,
                    },
                }],
            },
        },
        "validation_score": profit_factor * 10 + expectancy,
        "consistency_score": 1.0,
        "failure_reasons": failures,
        "created_at": datetime(2026, 7, 14, tzinfo=UTC),
    }


def analyze(rows: list[dict], **kwargs) -> dict:
    return analyze_campaign(CAMPAIGN, rows, **kwargs)


def test_campaign_backed_counts_and_funnel_reconcile() -> None:
    rows = [
        job(1, "rejected-a", symbol="AAPL"),
        job(2, "rejected-a", symbol="MSFT"),
        job(3, "passed-b", status="promoted", profit_factor=1.4, expectancy=2, trades=40),
    ]
    payload = analyze(
        rows,
        elite=[{"candidate_id": "passed-b"}],
        deployments=[{"candidate_id": "passed-b", "status": "active", "lifecycle_state": "active_forward_validation"}],
    )

    assert payload["overview"] == {
        "campaign_jobs": 3,
        "candidates_generated": 2,
        "candidates_tested": 2,
        "candidates_rejected": 1,
        "candidates_completed": 2,
        "needs_more_evidence": 0,
        "research_candidates": 0,
        "elite_candidates": 1,
        "candidate_linked_deployments": 1,
    }
    assert all(payload["reconciliation"].values())


def test_legacy_metrics_are_not_mixed_into_current_campaign_analysis() -> None:
    payload = analyze([job(1, "candidate-a")])

    assert "historical_research" not in payload
    assert "validated_strategies" not in payload["overview"]
    assert payload["overview"]["campaign_jobs"] == 1


def test_rejection_reasons_are_canonical_and_count_candidates() -> None:
    payload = analyze([
        job(1, "candidate-a", symbol="AAPL"),
        job(2, "candidate-a", symbol="MSFT"),
        job(3, "candidate-b", symbol="AAPL", profit_factor=1.3),
    ])
    rules = {row["name"]: row for row in payload["rejection_analysis"]["validation_rules"]}

    assert rules["minimum_trade_count"]["count"] == 3
    assert rules["minimum_trade_count"]["candidate_count"] == 2
    assert rules["profit_factor"]["count"] == 2
    assert rules["profit_factor"]["candidate_count"] == 1


def test_duplicate_detection_distinguishes_exact_and_structural_near_duplicates() -> None:
    rows = [
        job(1, "candidate-a", canonical="same", parameter=20),
        job(2, "candidate-b", canonical="same", parameter=20),
        job(3, "candidate-c", canonical="different", parameter=30),
    ]
    duplicates = analyze(rows)["duplicate_analysis"]

    assert duplicates["candidate_ids"] == 3
    assert duplicates["unique_candidates"] == 2
    assert duplicates["exact_duplicates"] == 1
    assert duplicates["near_duplicates"] == 2
    assert len(duplicates["redundant_parameter_regions"]) == 1


def test_near_pass_uses_stored_threshold_distance() -> None:
    payload = analyze([job(1, "candidate-a", profit_factor=1.2, expectancy=1, trades=29)])
    candidate = payload["near_pass_candidates"][0]

    assert candidate["candidate_id"] == "candidate-a"
    assert {row["name"] for row in candidate["failed_gates"]} == {"profit_factor", "minimum_trade_count"}
    assert candidate["further_testing_justified"] is True
    assert candidate["mean_distance"] < 0.05


def test_recommendations_reference_evidence_and_campaign_version() -> None:
    payload = analyze([job(index, f"candidate-{index}") for index in range(1, 5)])

    assert payload["recommendations"]
    for recommendation in payload["recommendations"]:
        assert recommendation["evidence_source"]
        assert recommendation["candidate_count"] > 0
        assert recommendation["campaign_version"] == CAMPAIGN["campaign_key"]
        assert recommendation["falsification_test"]


def test_experiment_history_groups_candidate_runs_without_dropping_distinct_runs() -> None:
    payload = analyze([
        job(1, "candidate-a", symbol="AAPL"),
        job(2, "candidate-a", symbol="MSFT"),
        job(3, "candidate-b", symbol="NVDA"),
    ])
    history = payload["experiment_history"]

    assert len(history) == 2
    row = next(item for item in history if item["candidate_id"] == "candidate-a")
    assert row["distinct_validation_runs"] == 2
    assert row["assets"] == ["AAPL", "MSFT"]


def test_filters_apply_consistently_to_every_campaign_section() -> None:
    payload = analyze(
        [job(1, "candidate-a", symbol="AAPL"), job(2, "candidate-b", symbol="MSFT")],
        filters={"asset": "AAPL", "validation_rule": "profit_factor"},
    )

    assert payload["overview"]["campaign_jobs"] == 1
    assert payload["candidate_funnel"][0]["count"] == 1
    assert [row["name"] for row in payload["rejection_analysis"]["assets"]] == ["AAPL"]
    assert [row["name"] for row in payload["asset_intelligence"]["rows"]] == ["AAPL"]
    assert {row["candidate_id"] for row in payload["experiment_history"]} == {"candidate-a"}


def test_validation_passed_terminology_never_calls_execution_validated() -> None:
    payload = analyze([job(1, "candidate-a")])
    serialized = json.dumps(payload, default=str).lower()

    assert "validated strategy" not in serialized
    assert payload["terminology"]["completed_job"].startswith("A terminal execution outcome")
    assert payload["terminology"]["validation_passed"].startswith("All required evidence gates passed")


def test_next_campaign_proposal_is_review_only_and_preserves_thresholds() -> None:
    proposal = analyze([job(index, f"candidate-{index}") for index in range(1, 5)])["next_campaign_proposal"]

    assert proposal["status"] == "review_required"
    assert proposal["launch_authorized"] is False
    assert proposal["validation_thresholds_changed"] is False


def test_duplicate_reduction_is_clamped_to_valid_percentage_range() -> None:
    rows = [
        job(1, "candidate-a", canonical="same", parameter=20),
        job(2, "candidate-b", canonical="same", parameter=20),
        job(3, "candidate-c", canonical="different", parameter=30),
    ]
    proposal = analyze(rows)["next_campaign_proposal"]

    assert 0 <= proposal["expected_duplicate_work_reduction"] <= 1
    assert proposal["expected_duplicate_work_reduction"] == 0.5


def test_needs_more_evidence_plan_is_deterministic_and_blocks_deployment() -> None:
    row = job(1, "candidate-a", trades=12, profit_factor=1.3, expectancy=2, status="completed")
    prepared = analyze([row])["experiment_history"][0]
    profile = {
        "candidate_id": "candidate-a",
        "campaign_ids": [7],
        "state": "Needs More Evidence",
        "runs": [prepare_job(row, "equity")],
    }

    plan = deterministic_evidence_plan(profile)

    assert plan["status"] == "blocked_from_paper_deployment"
    assert plan["missing_evidence_reason"] == "Minimum Trade Count"
    assert plan["steps"][0]["recommended_test"] == "Longer historical-window test or bounded entry-frequency repair"
    assert "trade count" in plan["steps"][0]["falsification_condition"]
    assert prepared["candidate_id"] == "candidate-a"


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class AllCampaignConn:
    def __init__(self):
        now = datetime(2026, 7, 18, tzinfo=UTC)
        self.campaigns = [
            {"id": 2, "campaign_key": "two", "name": "Running campaign", "universe_key": "two", "status": "running", "requested_candidates": 1, "analytics": {}, "created_at": now, "started_at": now, "completed_at": None, "updated_at": now},
            {"id": 1, "campaign_key": "one", "name": "Completed campaign", "universe_key": "one", "status": "completed", "requested_candidates": 1, "analytics": {}, "created_at": now, "started_at": now, "completed_at": now, "updated_at": now},
        ]
        first = job(1, "shared-id", status="rejected")
        first["campaign_id"] = 1
        second = job(2, "shared-id", status="promoted", profit_factor=1.4, expectancy=2, trades=40)
        second["campaign_id"] = 2
        self.jobs = [first, second]

    def execute(self, query, params=None):
        if "FROM research_campaigns" in query:
            return Result(self.campaigns)
        if "FROM research_campaign_jobs" in query:
            return Result(self.jobs)
        if "FROM elite_research_candidates" in query or "FROM strategy_deployments" in query:
            return Result([])
        if "COUNT(*) AS count FROM alpha_validation_runs" in query or "COUNT(*) AS count FROM strategy_experiments" in query:
            return Result([{"count": 0}])
        if "FROM alpha_validation_runs" in query or "FROM strategy_experiments" in query:
            return Result([])
        raise AssertionError(query)


def test_all_scope_combines_campaigns_and_keeps_running_evidence_visible() -> None:
    payload = research_command_center(AllCampaignConn())

    assert payload["campaign"]["name"] == "All campaign evidence"
    assert payload["live_evidence"] is True
    assert payload["overview"]["campaign_jobs"] == 2
    assert payload["overview"]["candidates_generated"] == 2
    assert payload["overview"]["candidates_tested"] == 2
    assert len(payload["experiment_history"]) == 2


def test_legacy_snapshot_is_upgraded_to_safe_render_contract() -> None:
    payload = complete_command_center_payload(
        {"overview": {"campaign_jobs": 1}, "strategy_intelligence": {}, "next_campaign_proposal": {"candidate_count": 5}},
        {},
    )

    assert payload["campaigns"] == []
    assert payload["candidate_funnel"] == []
    assert payload["strategy_intelligence"] == {"rows": [], "highlights": {}}
    assert payload["next_campaign_proposal"]["new_hypothesis_tests"] == []
    assert payload["filter_options"]["assets"] == []
