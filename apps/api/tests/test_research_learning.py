from datetime import UTC, datetime, timedelta

from app.services.research_learning import (
    build_adaptive_campaign_plan,
    build_campaign_learning,
    calculate_campaign_confidence,
    detect_failure_patterns,
    detect_success_patterns,
    generate_evidence_based_mutations,
    generate_research_recommendations,
    normalize_job,
)


def campaign() -> dict:
    return {"id": 42, "name": "Learning campaign", "simulation_only": True}


def job(job_id: int, status: str, candidate_id: str, *, symbol: str = "TSLA", timeframe: str = "1h", params: dict | None = None, blocks: dict | None = None, failure_reasons: list[str] | None = None) -> dict:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    promoted = status == "promoted"
    return {
        "id": job_id,
        "campaign_id": 42,
        "candidate_id": candidate_id,
        "family_id": "family_momentum",
        "strategy_family": "ema_20_50+rsi_55+breakout+atr_stop_20",
        "symbol": symbol,
        "timeframe": timeframe,
        "status": status,
        "candidate": {
            "candidate_id": candidate_id,
            "family_id": "family_momentum",
            "parent_candidate_id": "parent_1",
            "blocks": blocks
            or {
                "trend": "ema_20_50",
                "momentum": "rsi_55",
                "entry": "breakout",
                "exit": "atr_stop_20",
            },
            "parameters": params
            or {
                "trend_fast": 20,
                "trend_slow": 50,
                "rsi_min": 55,
                "risk_reward": 2.0,
                "atr_multiplier": 3.0 if promoted else 1.5,
                "max_holding_bars": 18,
            },
        },
        "result": {
            "metrics": {
                "profit_factor": 1.8 if promoted else 0.82,
                "expectancy_per_trade": 14 if promoted else -6,
                "number_of_trades": 120 if promoted else 18,
                "max_drawdown": 0.04 if promoted else 0.22,
            },
            "regime_analysis": {
                "by_market_regime": [
                    {"regime": "bull_trend" if promoted else "sideways", "metrics": {"profit_factor": 1.6 if promoted else 0.7, "expectancy_per_trade": 10 if promoted else -8}},
                ],
                "by_volatility_regime": [
                    {"regime": "low_volatility", "metrics": {"profit_factor": 1.4 if promoted else 0.8, "expectancy_per_trade": 8 if promoted else -4}},
                ],
            },
            "forward_validation": {"pass_rate": 0.75 if promoted else 0.0},
            "paper_performance": {"profit_factor": 1.5 if promoted else 0.0},
            "evidence_drift": {"drift_score": 0.1 if promoted else 0.7},
        },
        "failure_reasons": failure_reasons or ([] if promoted else ["sample_size_too_small", "drawdown_excessive"]),
        "validation_score": 88 if promoted else 35,
        "consistency_score": 0.82 if promoted else 0.2,
        "created_at": now - timedelta(hours=job_id),
        "completed_at": now,
        "simulation_only": True,
    }


def sample_jobs() -> list[dict]:
    return [
        job(1, "promoted", "elite_a"),
        job(2, "rejected", "reject_a"),
        job(3, "rejected", "reject_b", symbol="AAPL", timeframe="4h"),
    ]


def test_failure_and_success_detection_are_explainable_and_ranked() -> None:
    normalized = [normalize_job(row) for row in sample_jobs()]

    failures = detect_failure_patterns(normalized)
    successes = detect_success_patterns(normalized)

    assert failures[0]["frequency"] >= failures[-1]["frequency"]
    assert any(row["value"] == "drawdown_excessive" for row in failures)
    assert any("evidence_refs" in row and row["calculation"]["version"] == "research_learning_v1" for row in failures)
    assert any(row["pattern_type"] == "atr_multiplier_range" and row["value"] == "atr_multiplier:3.0" for row in successes)


def test_recommendations_and_mutations_are_deterministic_with_supporting_evidence() -> None:
    normalized = [normalize_job(row) for row in sample_jobs()]
    failures = detect_failure_patterns(normalized)
    successes = detect_success_patterns(normalized)

    recommendations = generate_research_recommendations(failures, successes)
    first_mutations = generate_evidence_based_mutations(normalized, failures, successes)
    second_mutations = generate_evidence_based_mutations(normalized, failures, successes)

    assert recommendations
    assert all(row["evidence_refs"] and row["explainability"]["why"] for row in recommendations)
    assert first_mutations == second_mutations
    assert all(row["parent_candidate_id"] and row["supporting_evidence"] and row["expected_improvement"] for row in first_mutations)
    assert any("trend_fast" in row["mutation"] or "atr_multiplier" in row["mutation"] for row in first_mutations)


def test_confidence_scores_expose_components_and_rank_elite_evidence() -> None:
    normalized = [normalize_job(row) for row in sample_jobs()]

    confidence = calculate_campaign_confidence(normalized)

    assert len(confidence) == 1
    assert confidence[0]["candidate_id"] == "elite_a"
    assert confidence[0]["confidence_score"] > 50
    assert {"historical_validation", "forward_validation", "paper_performance", "evidence_drift", "sample_size"}.issubset(confidence[0]["components"])
    assert "not future profit" in confidence[0]["explanation"]


def test_adaptive_campaign_plan_balances_exploration_and_confirmation() -> None:
    normalized = [normalize_job(row) for row in sample_jobs()]
    failures = detect_failure_patterns(normalized)
    successes = detect_success_patterns(normalized)
    recommendations = generate_research_recommendations(failures, successes)

    plan = build_adaptive_campaign_plan(normalized, failures, successes, recommendations)

    assert plan["priorities"]
    assert plan["exploration_targets"]
    assert plan["confirmation_targets"]
    assert "duplication_control" in plan["rationale"]


def test_full_campaign_learning_builds_versioned_knowledge_timeline_and_rankings() -> None:
    learning = build_campaign_learning(campaign(), sample_jobs())

    assert learning["summary"]["jobs_analyzed"] == 3
    assert learning["knowledge"]
    assert all(row["calculation_version"] == "research_learning_v1" for row in learning["knowledge"])
    assert any(event["event_type"] == "promotion" for event in learning["timeline"])
    assert any(event["event_type"] == "rejection" for event in learning["timeline"])
    assert learning["elite_rankings"][0]["candidate_id"] == "elite_a"
    assert learning["safety"]["simulation_only"] is True
