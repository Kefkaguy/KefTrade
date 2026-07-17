from datetime import UTC, datetime, timedelta
from decimal import Decimal
import math

from app.services.research_architecture import (
    ARCHITECTURE_VERSION,
    CLUSTER_VERSION,
    build_candidate_stage_evidence,
    calculate_asset_clusters,
    calculate_asset_profile,
    generate_hypotheses_from_intelligence,
    generate_targeted_candidates,
    interpret_hypothesis_result,
    select_campaign_hypothesis,
    stable_hash,
    validation_funnel,
    validation_gate_diagnostics,
)
from app.services.research_campaigns import passes_cross_validation
from app.services.strategy_discovery import candidate_execution_key


def candles(count: int = 220, *, symbol: str = "AAPL", trend: float = 0.18) -> list[dict]:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    rows = []
    previous = 100.0
    for index in range(count):
        cycle = math.sin(index / 6) * 0.35
        close = 100 + index * trend + cycle
        open_price = previous
        high = max(open_price, close) + 0.45
        low = min(open_price, close) - 0.45
        rows.append(
            {
                "symbol": symbol,
                "timeframe": "1h",
                "timestamp": start + timedelta(hours=index),
                "open": Decimal(str(round(open_price, 6))),
                "high": Decimal(str(round(high, 6))),
                "low": Decimal(str(round(low, 6))),
                "close": Decimal(str(round(close, 6))),
                "volume": Decimal(str(1000 + (index % 12) * 30)),
            }
        )
        previous = close
    return rows


def passing_result() -> dict:
    return {
        "metrics": {
            "profit_factor": 1.5,
            "expectancy_per_trade": 2.0,
            "max_drawdown": 0.04,
            "number_of_trades": 40,
            "walk_forward": {"enabled": True},
        },
        "paper_readiness": {"paper_ready": True},
    }


def test_asset_profile_is_versionable_transparent_and_complete_about_limitations() -> None:
    profile = calculate_asset_profile(candles())

    assert profile["calculation_version"] == "asset_behavior_profile_v1"
    assert profile["metrics"]["sample_size"] == 220
    assert profile["metrics"]["realized_volatility"] >= 0
    assert 0 <= profile["metrics"]["breakout_follow_through"] <= 1
    assert profile["behavior_labels"]["trend_persistence"] in {"low", "moderate", "high"}
    assert profile["limitations"][0]["metric"] == "earnings_behavior"
    assert profile["limitations"][0]["status"] == "unavailable"


def test_measured_clustering_groups_similar_behavior_profiles() -> None:
    def profile(profile_id: int, symbol: str, volatility: float, trend: float, reversion: float, correlations: dict[str, float]) -> dict:
        metrics = {
            "realized_volatility": volatility,
            "atr_ratio": volatility * 1.3,
            "trend_persistence": trend,
            "trend_strength": trend / 50,
            "mean_reversion_score": reversion,
            "breakout_follow_through": 0.65 if trend > 4 else 0.35,
            "median_pullback_depth": volatility,
            "momentum_persistence": 0.60 if trend > 4 else 0.45,
            "volume_expansion_ratio": 1.4 if trend > 4 else 0.9,
            "gap_frequency": volatility,
            "sample_size": 500,
        }
        return {"id": profile_id, "symbol": symbol, "timeframe": "1h", "metrics": metrics, "correlations": correlations}

    profiles = [
        profile(1, "AAPL", 0.012, 5.5, 0.02, {"MSFT": 0.9, "SPY": 0.1, "QQQ": 0.1}),
        profile(2, "MSFT", 0.013, 5.2, 0.03, {"AAPL": 0.9, "SPY": 0.1, "QQQ": 0.1}),
        profile(3, "SPY", 0.004, 1.8, 0.25, {"QQQ": 0.9, "AAPL": 0.1, "MSFT": 0.1}),
        profile(4, "QQQ", 0.005, 1.9, 0.23, {"SPY": 0.9, "AAPL": 0.1, "MSFT": 0.1}),
    ]

    clusters = calculate_asset_clusters(profiles, target_clusters=2)
    member_sets = {frozenset(member["symbol"] for member in cluster["members"]) for cluster in clusters}

    assert member_sets == {frozenset({"AAPL", "MSFT"}), frozenset({"SPY", "QQQ"})}
    assert all(cluster["algorithm_version"] == CLUSTER_VERSION for cluster in clusters)
    assert all(0 < member["similarity_score"] <= 1 for cluster in clusters for member in cluster["members"])


def test_hypotheses_are_explicit_scoped_and_traceable_to_profiles() -> None:
    profiles = []
    for profile_id, symbol in enumerate(("AAPL", "MSFT"), start=1):
        profiles.append(
            {
                "id": profile_id,
                "symbol": symbol,
                "timeframe": "1h",
                "metrics": {
                    "sample_size": 500,
                    "breakout_follow_through": 0.66,
                    "volume_expansion_ratio": 1.5,
                    "momentum_persistence": 0.61,
                    "trend_persistence": 2.5,
                    "trend_strength": 0.03,
                    "mean_reversion_score": 0.01,
                    "reversal_rate": 0.42,
                    "gap_frequency": 0.02,
                },
                "evidence_window": {"start": "2025-01-01", "end": "2026-01-01", "candle_count": 500},
            }
        )
    clusters = [{"id": 9, "cluster_key": "cluster_growth", "centroid": profiles[0]["metrics"], "members": [{"asset_profile_id": 1}, {"asset_profile_id": 2}]}]

    hypotheses = generate_hypotheses_from_intelligence(profiles, clusters, dataset_id=4)
    cluster_hypothesis = next(row for row in hypotheses if row["scope_type"] == "cluster")

    assert cluster_hypothesis["scope_ref"] == "cluster_growth"
    assert cluster_hypothesis["strategy_family"] == "Breakout"
    assert cluster_hypothesis["status"] == "proposed"
    assert cluster_hypothesis["supporting_evidence"] == ["asset_profile:1", "asset_profile:2", "asset_cluster:9"]
    assert cluster_hypothesis["test_summary"]["source_dataset_id"] == 4


def test_incohesive_forced_cluster_cannot_create_transfer_hypothesis() -> None:
    profiles = [
        {
            "id": index,
            "symbol": symbol,
            "timeframe": "1h",
            "metrics": {"sample_size": 500, "trend_persistence": 5, "momentum_persistence": 0.55, "breakout_follow_through": 0.6},
            "evidence_window": {"candle_count": 500},
        }
        for index, symbol in enumerate(("AAPL", "MSFT"), start=1)
    ]
    clusters = [
        {
            "id": 9,
            "cluster_key": "forced_cluster",
            "centroid": profiles[0]["metrics"],
            "members": [{"asset_profile_id": 1}, {"asset_profile_id": 2}],
            "quality_metrics": {"average_distance_to_centroid": 2.1},
        }
    ]

    hypotheses = generate_hypotheses_from_intelligence(profiles, clusters, dataset_id=4)

    assert all(row["scope_type"] == "asset" for row in hypotheses)


def test_targeted_generator_enforces_seventy_twenty_ten_and_lineage() -> None:
    hypothesis = {
        "id": 7,
        "hypothesis_key": "hyp_breakout",
        "scope_type": "cluster",
        "scope_ref": "cluster_growth",
        "strategy_family": "Breakout",
        "expected_behavior": "Continuation after a measured break.",
        "relevant_regimes": ["bull_trend"],
    }

    generated = generate_targeted_candidates(hypothesis, max_candidates=20)

    assert len(generated["candidates"]) == 20
    assert generated["allocation"]["requested"] == {"exploitation": 14, "nearby": 4, "exploration": 2}
    assert generated["allocation"]["actual"] == generated["allocation"]["requested"]
    assert all(candidate.parameters["hypothesis_version_id"] == 7 for candidate in generated["candidates"])
    assert all(candidate.parameters["research_architecture_version"] == ARCHITECTURE_VERSION for candidate in generated["candidates"])
    assert all(candidate.parent_candidate_id for candidate in generated["candidates"] if candidate.parameters["generation_channel"] == "nearby")
    assert all(candidate.parameters["frequency_screen_min_opportunities"] == 30 for candidate in generated["candidates"])
    assert len({candidate_execution_key(candidate) for candidate in generated["candidates"]}) == 20


def test_post_hoc_same_dataset_pass_cannot_become_supported() -> None:
    same_evidence = interpret_hypothesis_result(
        {"cluster_elite": 1},
        {"post_hoc": True, "source_dataset_id": 1},
        1,
    )
    independent = interpret_hypothesis_result(
        {"cluster_elite": 1},
        {"post_hoc": True, "source_dataset_id": 1},
        2,
    )

    assert same_evidence == {
        "status": "testing",
        "interpretation": "same_evidence_pass_unconfirmed",
        "same_evidence_post_hoc": True,
    }
    assert independent["status"] == "supported"
    assert independent["same_evidence_post_hoc"] is False


def test_hypothesis_selection_prioritizes_untested_cluster_evidence() -> None:
    hypotheses = [
        {
            "id": 1,
            "hypothesis_key": "tested_supported_asset",
            "scope_type": "asset",
            "status": "supported",
            "confidence_score": 0.95,
            "test_summary": {"campaign_id": 40},
        },
        {
            "id": 2,
            "hypothesis_key": "tested_supported_cluster",
            "scope_type": "cluster",
            "status": "supported",
            "confidence_score": 0.90,
            "test_summary": {"campaign_id": 41},
        },
        {
            "id": 3,
            "hypothesis_key": "untested_cluster",
            "scope_type": "cluster",
            "status": "proposed",
            "confidence_score": 0.65,
            "test_summary": {},
        },
    ]

    assert select_campaign_hypothesis(hypotheses)["id"] == 3
    assert select_campaign_hypothesis(hypotheses, hypothesis_id=1)["id"] == 1


def test_hypothesis_selection_prioritizes_validation_sample_capacity_before_confidence() -> None:
    hypotheses = [
        {
            "id": 1,
            "hypothesis_key": "high_confidence_4h",
            "scope_type": "cluster",
            "status": "proposed",
            "confidence_score": 0.91,
            "evidence_window": {"sample_size": 10_150},
            "test_summary": {"symbols": ["A", "B", "C", "D", "E", "F", "G"]},
        },
        {
            "id": 2,
            "hypothesis_key": "sufficient_1h",
            "scope_type": "cluster",
            "status": "proposed",
            "confidence_score": 0.88,
            "evidence_window": {"sample_size": 25_000},
            "test_summary": {"symbols": ["A", "B", "C", "D", "E"]},
        },
    ]

    assert select_campaign_hypothesis(hypotheses)["id"] == 2


def test_automatic_selection_does_not_repeat_already_tested_weak_hypothesis() -> None:
    hypotheses = [
        {"id": 1, "hypothesis_key": "tested_weak", "scope_type": "cluster", "status": "weak", "confidence_score": 0.95, "test_summary": {"campaign_id": 50}},
        {"id": 2, "hypothesis_key": "untested", "scope_type": "asset", "status": "proposed", "confidence_score": 0.5, "test_summary": {}},
    ]

    assert select_campaign_hypothesis(hypotheses)["id"] == 2
    assert select_campaign_hypothesis(hypotheses, hypothesis_id=1)["id"] == 1


def test_automatic_selection_skips_known_underpowered_evidence_window() -> None:
    hypotheses = [
        {
            "id": 1,
            "hypothesis_key": "underpowered_cluster",
            "scope_type": "cluster",
            "status": "proposed",
            "confidence_score": 0.95,
            "evidence_window": {"sample_size": 2_900},
            "test_summary": {"symbols": ["AMD", "TSLA"]},
        },
        {
            "id": 2,
            "hypothesis_key": "powered_asset",
            "scope_type": "asset",
            "status": "proposed",
            "confidence_score": 0.75,
            "evidence_window": {"candle_count": 5_000},
            "test_summary": {"symbols": ["GOOGL"]},
        },
    ]

    assert select_campaign_hypothesis(hypotheses)["id"] == 2
    assert select_campaign_hypothesis(hypotheses, hypothesis_id=1)["id"] == 1


def test_candidate_levels_keep_asset_specialists_without_calling_them_elite() -> None:
    campaign = {"id": 12, "universe_key": "core", "hypothesis_version_id": 7, "controls": {"target_scope": {"type": "cluster", "ref": "growth"}}}
    jobs = [
        {"id": 1, "candidate_id": "candidate", "symbol": "AAPL", "timeframe": "1h", "status": "promoted", "result": passing_result()},
        {"id": 2, "candidate_id": "candidate", "symbol": "MSFT", "timeframe": "1h", "status": "rejected", "result": {"metrics": {"profit_factor": 0.8, "expectancy_per_trade": -1, "max_drawdown": 0.05, "number_of_trades": 40, "walk_forward": {"enabled": True}}, "paper_readiness": {"paper_ready": False}}},
    ]
    summary = {"candidate_id": "candidate", "research_score": 2, "profit_factor": 1.15, "expectancy": 0.5, "max_drawdown": 0.045, "trade_count": 80, "stability": 0.5, "assets_passed": 1, "timeframes_passed": 1, "regimes_passed": 0}

    stages = build_candidate_stage_evidence(campaign, jobs, [summary])
    levels = {row["candidate_level"] for row in stages}

    assert {"generated", "research_candidate", "asset_specialist"}.issubset(levels)
    assert "cluster_candidate" not in levels
    assert "cluster_elite" not in levels


def test_complete_funnel_and_cluster_elite_require_every_unchanged_gate() -> None:
    campaign = {"id": 13, "universe_key": "core", "hypothesis_version_id": 8, "controls": {"target_scope": {"type": "cluster", "ref": "growth"}}}
    jobs = [
        {"id": 1, "candidate_id": "candidate", "symbol": "AAPL", "timeframe": "1h", "status": "promoted", "result": passing_result()},
        {"id": 2, "candidate_id": "candidate", "symbol": "MSFT", "timeframe": "1h", "status": "promoted", "result": passing_result()},
    ]
    summary = {"candidate_id": "candidate", "research_score": 4, "profit_factor": 1.5, "expectancy": 2, "max_drawdown": 0.04, "trade_count": 80, "stability": 1.0, "assets_passed": 2, "timeframes_passed": 1, "regimes_passed": 2}

    stages = build_candidate_stage_evidence(campaign, jobs, [summary])
    funnel = validation_funnel(jobs, stages)

    assert any(row["candidate_level"] == "cluster_elite" for row in stages)
    assert next(row["count"] for row in funnel if row["stage"] == "passed_paper_readiness") == 2
    assert next(row["count"] for row in funnel if row["stage"] == "cluster_elite") == 1
    assert all(row["passed"] for row in validation_gate_diagnostics(passing_result()))


def test_no_loss_profit_factor_is_not_rejected_by_validation_diagnostics() -> None:
    result = passing_result()
    result["metrics"]["profit_factor"] = None
    result["metrics"]["profit_factor_is_infinite"] = True

    profit_factor_gate = next(row for row in validation_gate_diagnostics(result) if row["name"] == "profit_factor")

    assert profit_factor_gate["actual"] == "infinite"
    assert profit_factor_gate["passed"] is True


def test_phase_9_12_elite_gate_regression_remains_pinned() -> None:
    known_good_summary = {
        "research_score": 71.4,
        "profit_factor": 1.52,
        "expectancy": 17.2,
        "max_drawdown": 0.037,
        "trade_count": 112,
        "stability": 1.0,
        "assets_passed": 2,
        "timeframes_passed": 1,
    }

    assert passes_cross_validation(known_good_summary) is True


def test_stable_hash_is_order_independent_for_archive_checksums() -> None:
    assert stable_hash({"b": Decimal("2.0"), "a": [1, 2]}) == stable_hash({"a": [1, 2], "b": Decimal("2.0")})
