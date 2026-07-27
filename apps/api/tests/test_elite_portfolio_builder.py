from __future__ import annotations

import time

import pytest

import app.services.elite_portfolio_builder as elite_portfolio_builder
from app.services.elite_portfolio_builder import (
    SOLVER_VERSION,
    exact_timeframe_cap_holds,
    feasibility_report,
    maximum_independent_set,
    parameter_similarity_breakdown,
    preview,
    verify_feasibility,
)


def candidate(index: int, *, timeframe: str, family: str | None = None, symbol: str | None = None) -> dict:
    returns = {f"2026-01-{day:02d}": ((index + 1) * day % 17 - 8) / 1000 for day in range(1, 61)}
    signals = {key: value * (1 if index % 2 else -1) for key, value in returns.items()}
    return {
        "id": index,
        "candidate_id": f"candidate_{index:04d}",
        "campaign_id": 1,
        "strategy_version": f"v{index}",
        "symbol": symbol or f"SYM{index:04d}",
        "timeframe": timeframe,
        "family_id": family or f"family_{index % 10}",
        "strategy_direction": "short" if index % 7 == 0 else "long",
        "execution_capability": "internal_only" if index % 7 == 0 else "external_observe",
        "parameters": {"lookback": index + 10, "threshold": round(0.01 + index / 10000, 4)},
        "research_score": 1000 - index,
        "quality_score": 1000 - index,
        "profit_factor": 1.5,
        "expectancy": 0.02,
        "max_drawdown": 0.05,
        "trade_count": 100,
        "stability": 0.8,
        "assets_passed": 3,
        "timeframes_passed": 2,
        "regimes_passed": 2,
        "health": "healthy",
        "forward_validation_state": "forward_validation_passed",
        "strategy_returns": returns,
        "signal_returns": signals,
    }


def diversified_candidates(count: int) -> list[dict]:
    timeframes = ("1h", "4h", "1d")
    return [candidate(index, timeframe=timeframes[index % 3]) for index in range(count)]


def test_identical_runs_are_deterministic_and_never_relax_constraints() -> None:
    candidates = diversified_candidates(24)
    config = {"constraints": {"maximum_portfolio_size": 12, "minimum_portfolio_size": 5}}

    first = preview(candidates, config)
    second = preview(candidates, config)

    assert first["solver_version"] == SOLVER_VERSION
    assert first["selected"] == second["selected"]
    assert first["snapshot"]["decision_hash"] == second["snapshot"]["decision_hash"]
    assert first["constraint_relaxation_count"] == 0
    assert first["constraint_relaxations"] == []


def test_snapshot_changes_when_any_decision_input_changes() -> None:
    candidates = diversified_candidates(12)
    original = preview(candidates)
    changed = [dict(row) for row in candidates]
    changed[0] = {**changed[0], "profit_factor": 1.6}

    assert preview(changed)["snapshot"]["decision_hash"] != original["snapshot"]["decision_hash"]
    assert preview(candidates, {"objective": "expectancy"})["snapshot"]["decision_hash"] != original["snapshot"]["decision_hash"]


def test_strategy_market_variants_have_distinct_immutable_keys() -> None:
    first = candidate(1, timeframe="1h", symbol="AAPL")
    second = {**candidate(2, timeframe="4h", symbol="AAPL"), "candidate_id": first["candidate_id"]}
    first["candidate_key"] = f"{first['candidate_id']}|AAPL|1h"
    second["candidate_key"] = f"{second['candidate_id']}|AAPL|4h"

    result = preview([first, second], {"constraints": {"minimum_portfolio_size": 1, "minimum_unique_assets": 1, "minimum_families": 1, "minimum_timeframes": 1}})

    keys = [row["candidate_key"] for row in result["snapshot"]["candidate_evidence"]]
    assert keys == [first["candidate_key"], second["candidate_key"]]


def test_exact_timeframe_cap_uses_integer_arithmetic_for_odd_and_even_sizes() -> None:
    assert exact_timeframe_cap_holds([{"timeframe": "1h"}, {"timeframe": "4h"}])
    assert not exact_timeframe_cap_holds([{"timeframe": "1h"}, {"timeframe": "1h"}, {"timeframe": "4h"}])
    assert exact_timeframe_cap_holds([{"timeframe": "1h"}, {"timeframe": "4h"}, {"timeframe": "1d"}])
    assert exact_timeframe_cap_holds([{"timeframe": "1h"}, {"timeframe": "1h"}, {"timeframe": "4h"}, {"timeframe": "4h"}])


def test_constructor_enforces_exact_cap_on_odd_and_even_portfolios() -> None:
    patterns = (
        [1 if day % 2 else -1 for day in range(60)],
        [1 if day % 4 < 2 else -1 for day in range(60)],
        [1 if day % 6 < 3 else -1 for day in range(60)],
        [1 if day % 10 in {0, 3, 7} else -1 for day in range(60)],
    )
    rows = []
    for index, timeframe in enumerate(("1h", "4h", "1d", "1d")):
        row = candidate(index + 100, timeframe=timeframe, family=f"distinct_{index}")
        row["parameters"] = {"unique": index * 100}
        row["strategy_returns"] = {str(day): patterns[index][day] / 100 for day in range(60)}
        row["signal_returns"] = dict(row["strategy_returns"])
        rows.append(row)

    base_constraints = {
        "minimum_unique_assets": 2,
        "minimum_families": 2,
        "minimum_timeframes": 2,
        "maximum_per_family": 2,
    }
    even = preview(rows[:2], {"custom_size": 2, "constraints": {**base_constraints, "minimum_portfolio_size": 2}})
    odd = preview(rows[:3], {"custom_size": 3, "constraints": {**base_constraints, "minimum_portfolio_size": 3}})

    assert even["status"] == "review_ready"
    assert odd["status"] == "review_ready"
    assert even["analytics"]["timeframe_distribution"] == {"1h": 1, "4h": 1}
    assert odd["analytics"]["timeframe_distribution"] == {"1d": 1, "1h": 1, "4h": 1}


def test_insufficient_correlation_is_a_hard_conflict_and_infeasibility_is_explained() -> None:
    candidates = diversified_candidates(5)
    for row in candidates:
        row["strategy_returns"] = {"one": 0.1}
        row["signal_returns"] = {"one": 0.1}

    result = preview(candidates)

    assert result["status"] == "infeasible"
    assert result["maximum_feasible_size"] == 0
    assert result["termination_reason"] == "exact_search_proved_no_feasible_portfolio"
    assert result["verified_infeasible"] is True
    assert result["constraint_relaxation_count"] == 0
    assert any(row["constraint"].endswith("CORRELATION_INSUFFICIENT") for row in result["binding_constraints"])


def test_500_candidate_preview_completes_under_two_seconds() -> None:
    candidates = diversified_candidates(500)
    started = time.perf_counter()
    result = preview(candidates, {"constraints": {"maximum_portfolio_size": 20}})
    elapsed = time.perf_counter() - started

    assert result["candidates_examined"] == 500
    assert elapsed < 2.0


def test_genuine_infeasibility_is_confirmed_by_exact_verification() -> None:
    candidates = diversified_candidates(5)
    for row in candidates:
        row["strategy_returns"] = {"one": 0.1}
        row["signal_returns"] = {"one": 0.1}

    result = preview(candidates)

    assert result["status"] == "infeasible"
    assert result["heuristic_miss"] is False
    assert result["verified_infeasible"] is True
    assert result["verification"]["ran"] is True
    assert result["verification"]["verified"] is True
    assert result["verification"]["feasible"] is False
    assert result["verification"]["maximum_feasible_size"] == 0
    assert result["feasibility_report"]["greedy_missed_a_valid_solution"] is False


def test_exact_verifier_recovers_a_heuristic_miss(monkeypatch) -> None:
    candidates = diversified_candidates(24)
    config = {"constraints": {"maximum_portfolio_size": 12, "minimum_portfolio_size": 5}}

    baseline = preview(candidates, config)
    assert baseline["status"] == "review_ready"  # sanity: a feasible portfolio genuinely exists here

    def fake_infeasible_constructor(*_args, **_kwargs) -> dict:
        return {
            "status": "infeasible",
            "solver_version": SOLVER_VERSION,
            "selected": [],
            "maximum_feasible_size": 0,
            "constraint_relaxations": [],
            "constraint_relaxation_count": 0,
            "candidate_order": [],
            "iterations": 0,
            "operations": [],
            "swap_count": 0,
            "termination_reason": "no_portfolio_satisfies_all_constraints",
            "objective_hierarchy": [],
            "optimization_duration_ms": 0.0,
            "candidates_examined": len(candidates),
            "peak_memory_mb": None,
        }

    monkeypatch.setattr(elite_portfolio_builder, "construct_portfolio", fake_infeasible_constructor)
    result = preview(candidates, config)

    assert result["heuristic_miss"] is True
    assert result["verified_infeasible"] is False
    assert result["status"] == "review_ready"
    assert result["maximum_feasible_size"] > 0
    assert result["verification"]["ran"] is True
    assert result["verification"]["verified"] is True
    assert result["verification"]["feasible"] is True
    assert result["feasibility_report"]["greedy_missed_a_valid_solution"] is True


def test_exact_verifier_skips_pools_above_the_configured_limit() -> None:
    candidates = diversified_candidates(41)
    verification = verify_feasibility(candidates, [], {"constraints": {"minimum_portfolio_size": 5, "maximum_portfolio_size": 20}})

    assert verification["ran"] is False
    assert verification["verified"] is False
    assert verification["termination_reason"] == "pool_exceeds_verification_limit"


def test_verification_is_deterministic_across_repeated_runs() -> None:
    candidates = diversified_candidates(5)
    for row in candidates:
        row["strategy_returns"] = {"one": 0.1}
        row["signal_returns"] = {"one": 0.1}

    first = preview(candidates)
    second = preview(candidates)

    def without_timing(verification: dict) -> dict:
        return {key: value for key, value in verification.items() if key != "duration_ms"}

    assert without_timing(first["verification"]) == without_timing(second["verification"])
    assert first["verified_infeasible"] == second["verified_infeasible"] is True


def test_parameter_similarity_breakdown_handles_missing_parameters_deterministically() -> None:
    breakdown = parameter_similarity_breakdown({"lookback": 10, "threshold": 0.1}, {"lookback": 10})

    threshold_row = next(row for row in breakdown["per_parameter"] if row["parameter"] == "threshold")
    assert threshold_row["missing_on_one_side"] is True
    assert threshold_row["key_similarity"] == 0.0
    assert breakdown["compared_parameter_count"] == 2

    repeated = parameter_similarity_breakdown({"lookback": 10, "threshold": 0.1}, {"lookback": 10})
    assert repeated == breakdown


def test_parameter_similarity_ignores_metadata_and_is_not_triggered_by_family_alone() -> None:
    left = candidate(1, timeframe="1h", symbol="AAA", family="shared_family")
    right = candidate(2, timeframe="4h", symbol="AAA", family="shared_family")
    right["parameters"] = {"lookback": left["parameters"]["lookback"] * 5, "threshold": left["parameters"]["threshold"] + 5}

    breakdown = parameter_similarity_breakdown(left["parameters"], right["parameters"])

    assert breakdown["overall_similarity"] < 0.90


def test_parameter_similarity_conflicts_are_individually_explained() -> None:
    left = candidate(1, timeframe="1h", symbol="AAA")
    right = candidate(2, timeframe="4h", symbol="BBB")
    right["parameters"] = dict(left["parameters"])

    result = preview(
        [left, right],
        {"constraints": {"minimum_portfolio_size": 1, "minimum_unique_assets": 1, "minimum_families": 1, "minimum_timeframes": 1}},
    )

    similarity_conflicts = [row for row in result["conflicts"] if row["conflict_type"] == "PARAMETER_SIMILARITY"]
    assert similarity_conflicts
    evidence = similarity_conflicts[0]["evidence"]
    assert evidence["coefficient"] == 1.0
    assert evidence["compared_parameters"]
    assert "exceeded the" in evidence["reason"]


def test_symbol_family_duplicate_conflicts_carry_explicit_evidence() -> None:
    left = candidate(1, timeframe="1h", symbol="AAA", family="shared_family")
    right = candidate(2, timeframe="4h", symbol="AAA", family="shared_family")
    right["parameters"] = {"unrelated": 12345}

    result = preview(
        [left, right],
        {"constraints": {"minimum_portfolio_size": 1, "minimum_unique_assets": 1, "minimum_families": 1, "minimum_timeframes": 1}},
    )

    duplicate_conflicts = [row for row in result["conflicts"] if row["conflict_type"] == "SYMBOL_FAMILY_DUPLICATE"]
    assert duplicate_conflicts
    evidence = duplicate_conflicts[0]["evidence"]
    assert evidence["symbol"] == "AAA"
    assert evidence["family_id"] == "shared_family"
    assert "one member per symbol-family pair" in evidence["reason"]


def test_feasibility_report_includes_expected_fields() -> None:
    candidates = diversified_candidates(24)
    config = {"constraints": {"maximum_portfolio_size": 12, "minimum_portfolio_size": 5}}

    result = preview(candidates, config)
    report = result["feasibility_report"]

    assert report["pool_size"] == len(result["construction_pool_candidate_ids"])
    assert report["total_possible_pairs"] == report["pool_size"] * (report["pool_size"] - 1) // 2
    assert set(report["conflict_count_by_type"]).issubset(
        {"PARAMETER_SIMILARITY", "SYMBOL_FAMILY_DUPLICATE", "SIGNAL_CORRELATION_LIMIT", "STRATEGY_RETURN_CORRELATION_LIMIT", "SIGNAL_CORRELATION_INSUFFICIENT", "STRATEGY_RETURN_CORRELATION_INSUFFICIENT"}
    )
    assert isinstance(report["available_symbols"], list)
    assert isinstance(report["available_families"], list)
    assert report["minimum_unique_assets_independently_achievable"] is True
    assert report["minimum_families_independently_achievable"] is True


def test_maximum_independent_set_matches_a_hand_verifiable_conflict_graph() -> None:
    rows = diversified_candidates(4)
    conflicts = [
        {"left_candidate_id": rows[0]["candidate_id"], "right_candidate_id": rows[1]["candidate_id"], "conflict_type": "TEST", "hard_conflict": True, "evidence": {}},
        {"left_candidate_id": rows[1]["candidate_id"], "right_candidate_id": rows[2]["candidate_id"], "conflict_type": "TEST", "hard_conflict": True, "evidence": {}},
    ]

    result = maximum_independent_set(rows, conflicts, {})

    assert result["verified"] is True
    assert result["size"] == 3  # rows[0], rows[2], rows[3] are mutually conflict-free


def test_hard_rules_are_surfaced_and_include_symbol_family_uniqueness_independently() -> None:
    result = preview(diversified_candidates(6))
    rule_ids = {rule["id"] for rule in result["hard_rules"]}

    assert "SYMBOL_FAMILY_DUPLICATE" in rule_ids
    assert "PARAMETER_SIMILARITY" in rule_ids
    assert "SIGNAL_CORRELATION_LIMIT" in rule_ids
    assert "STRATEGY_RETURN_CORRELATION_LIMIT" in rule_ids
    assert "TIMEFRAME_50_PERCENT_CAP" in rule_ids


# --- Portfolio profiles (Step 03 feasibility) --------------------------------

from app.services.elite_portfolio_builder import (  # noqa: E402
    DEFAULT_CONSTRAINTS as _DEFAULTS,
    PAPER_LAB_MODE,
    PROFILES_BY_ID,
    blocking_analysis,
    normalized_configuration as _normalize,
    paper_lab_eligibility,
    paper_lab_preview,
    profile_constraints,
    protected_constraint_violations,
    recommend_profile,
    timeframe_cap_holds,
)


def _series(seed: str, days: int = 60) -> dict[str, float]:
    """A daily return series long enough to actually measure correlation on.

    Empty series are not a neutral default here: a pair with fewer than
    `minimum_correlation_observations` overlapping days is a hard
    "insufficient evidence" conflict, so a fixture without returns makes every
    pair conflict and every portfolio above size one infeasible.
    """
    step = 7 + (sum(ord(character) for character in seed) % 11)
    return {f"2026-01-{day + 1:02d}": round(((day * step) % 17 - 8) / 100.0, 4) for day in range(days)}


def _parameters(candidate_id: str, family: str) -> dict[str, object]:
    """Distinct, deterministic parameters per variant.

    Two candidates whose parameters are >90% similar are a hard conflict, so a
    fixture that varies one field by a few percent is a fixture where nothing
    can share a portfolio. Real families also carry their own named
    parameters, which is what actually separates them here. Derived from the
    id rather than hash(), whose value changes between runs.
    """
    index = int("".join(character for character in candidate_id if character.isdigit()) or "1")
    return {"rsi_min": 50 + index, "atr_multiplier": 1.0 + index * 0.25, f"{family}_threshold": index}


def _variant(candidate_id, symbol, timeframe, family, **overrides):
    """An elite variant that clears every quality threshold by default."""
    row = {
        "candidate_key": f"{candidate_id}|{symbol}|{timeframe}",
        "candidate_id": candidate_id,
        "campaign_id": 1,
        "research_job_id": 100,
        "promotion_state": "elite",
        "validation_state": "validated",
        "symbol": symbol,
        "timeframe": timeframe,
        "family_id": family,
        "strategy_direction": "long",
        "execution_capability": "external_observe",
        "parameters": _parameters(candidate_id, family),
        "profit_factor": 1.8,
        "expectancy": 6.0,
        "max_drawdown": 0.05,
        "trade_count": 90,
        "stability": 0.8,
        "assets_passed": 3,
        "timeframes_passed": 2,
        "regimes_passed": 2,
        "health": "healthy",
        "research_score": 0.8,
        "quality_score": 0.8,
        "strategy_returns": _series(candidate_id),
        "signal_returns": _series(candidate_id + "_signal"),
    }
    row.update(overrides)
    return row


def test_timeframe_cap_at_one_half_is_unchanged_from_the_original_rule() -> None:
    half = {"timeframe_cap_numerator": 1, "timeframe_cap_denominator": 2}

    assert timeframe_cap_holds([2, 2], 4, half) is True
    assert timeframe_cap_holds([3, 1], 4, half) is False
    # The exact-half cap is what makes every 3-member two-timeframe portfolio
    # infeasible: 2 of 3 already exceeds half.
    assert timeframe_cap_holds([2, 1], 3, half) is False


def test_two_thirds_cap_makes_an_odd_sized_portfolio_reachable() -> None:
    two_thirds = {"timeframe_cap_numerator": 2, "timeframe_cap_denominator": 3}

    assert timeframe_cap_holds([2, 1], 3, two_thirds) is True
    # Still forbids a single-timeframe portfolio at size 3.
    assert timeframe_cap_holds([3], 3, two_thirds) is False


def test_every_profile_keeps_the_correlation_and_similarity_limits_intact() -> None:
    for profile_id in PROFILES_BY_ID:
        constraints = profile_constraints(profile_id)
        assert constraints["maximum_parameter_similarity"] == _DEFAULTS["maximum_parameter_similarity"]
        assert constraints["maximum_signal_correlation"] == _DEFAULTS["maximum_signal_correlation"]
        assert constraints["maximum_strategy_return_correlation"] == _DEFAULTS["maximum_strategy_return_correlation"]
        assert constraints["minimum_correlation_observations"] == _DEFAULTS["minimum_correlation_observations"]
        assert protected_constraint_violations(constraints) == []


def test_a_configuration_that_loosens_a_protected_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="may not be weakened"):
        _normalize({"constraints": {"maximum_strategy_return_correlation": 0.99}})
    with pytest.raises(ValueError, match="may not be weakened"):
        _normalize({"constraints": {"maximum_parameter_similarity": 0.99}})
    with pytest.raises(ValueError, match="may not be weakened"):
        _normalize({"constraints": {"minimum_correlation_observations": 5}})


def test_a_configuration_may_still_tighten_a_protected_limit() -> None:
    config = _normalize({"constraints": {"maximum_strategy_return_correlation": 0.50}})

    assert config["constraints"]["maximum_strategy_return_correlation"] == 0.50


def test_a_narrow_pool_is_infeasible_strict_but_works_as_a_small_paper_launch() -> None:
    # Two symbols, two families -- a real but narrow research pool, exactly the
    # shape that produced "no feasible portfolio" under the strict profile.
    pool = [
        _variant("c1", "AMD", "30m", "session_momentum"),
        _variant("c2", "NVDA", "15m", "vwap_trend"),
        _variant("c3", "SPY", "30m", "breakout"),
    ]

    strict = preview(pool, {"profile": "strict_diversified"})
    assert strict["status"] == "infeasible"

    small = preview(pool, {"profile": "small_paper_launch"})
    assert small["status"] == "review_ready"
    assert 2 <= small["maximum_feasible_size"] <= 4


def test_single_elite_test_deploys_one_member_and_is_labelled_non_diversified() -> None:
    pool = [_variant("c1", "AMD", "30m", "session_momentum")]

    result = preview(pool, {"profile": "single_elite_test"})

    assert result["status"] == "review_ready"
    assert result["maximum_feasible_size"] == 1
    assert PROFILES_BY_ID["single_elite_test"]["diversified"] is False
    assert "no diversification" in PROFILES_BY_ID["single_elite_test"]["warning"]


def test_blocking_analysis_names_the_setting_that_actually_blocks() -> None:
    pool = [
        _variant("c1", "AMD", "30m", "session_momentum"),
        _variant("c2", "NVDA", "15m", "vwap_trend"),
    ]

    result = preview(pool, {"profile": "strict_diversified"})
    blocker = result["blocking_analysis"]["primary_blocker"]

    assert result["blocking_analysis"]["feasible"] is False
    # Two symbols cannot satisfy a five-symbol minimum by any rearrangement.
    assert blocker["setting"] == "minimum_unique_assets"
    assert blocker["required"] == 5
    assert blocker["available"] == 2
    assert blocker["severity"] == "structural"


def test_recommendation_picks_the_strictest_profile_that_actually_works() -> None:
    pool = [
        _variant("c1", "AMD", "30m", "session_momentum"),
        _variant("c2", "NVDA", "15m", "vwap_trend"),
        _variant("c3", "SPY", "30m", "breakout"),
    ]

    recommendation = recommend_profile(pool)

    assert recommendation["recommended_profile"] == "small_paper_launch"
    assert recommendation["protected_constraints_unchanged"] is True
    relaxed = {row["setting"] for row in recommendation["constraints_relaxed_versus_strict"]}
    # Only portfolio shape moves; nothing that keeps two versions of the same
    # bet out of one portfolio.
    assert "maximum_parameter_similarity" not in relaxed
    assert "maximum_strategy_return_correlation" not in relaxed
    assert "minimum_unique_assets" in relaxed


def test_recommendation_prefers_strict_when_the_pool_can_support_it() -> None:
    pool = [
        _variant("c1", "AMD", "30m", "session_momentum"),
        _variant("c2", "NVDA", "15m", "vwap_trend"),
        _variant("c3", "SPY", "30m", "breakout"),
        _variant("c4", "QQQ", "15m", "gap_fill"),
        _variant("c5", "TSLA", "30m", "opening_fade"),
        _variant("c6", "AAPL", "15m", "trend_pullback"),
    ]

    recommendation = recommend_profile(pool)

    assert recommendation["recommended_profile"] == "strict_diversified"
    assert recommendation["constraints_relaxed_versus_strict"] == []


def test_recommendation_reports_an_evidence_problem_rather_than_inventing_a_profile() -> None:
    # Nothing clears the quality thresholds, so no portfolio shape can help.
    pool = [_variant("c1", "AMD", "30m", "session_momentum", trade_count=3, profit_factor=0.4)]

    recommendation = recommend_profile(pool)

    assert recommendation["recommended_profile"] is None
    assert "evidence problem" in recommendation["reason"]


# --- All Validated Elites Paper Lab ------------------------------------------


def test_paper_lab_includes_every_validated_elite_regardless_of_overlap() -> None:
    # Same symbol, same family, same timeframe, highly correlated returns --
    # exactly what the diversified solver's hard rules would reject. The
    # paper lab is supposed to include all of it anyway.
    pool = [_variant(f"c{i}", "AMD", "30m", "session_momentum") for i in range(13)]

    result = paper_lab_preview(pool)

    assert result["status"] == "review_ready"
    assert result["mode"] == PAPER_LAB_MODE
    assert result["diversified"] is False
    assert result["eligible_count"] == 13
    assert len(result["selected"]) == 13
    assert result["hard_rules"] == []


def test_paper_lab_carries_a_strong_non_diversified_warning() -> None:
    pool = [_variant("c1", "AMD", "30m", "session_momentum")]

    result = paper_lab_preview(pool)

    assert "not a diversified portfolio" in result["warning"]
    assert "correlat" in result["warning"]
    assert result["configuration"]["warning"] == result["warning"]
    assert result["configuration"]["diversified"] is False


def test_paper_lab_excludes_short_strategies() -> None:
    pool = [_variant("c1", "AMD", "30m", "session_momentum", strategy_direction="short")]

    eligible, decisions = paper_lab_eligibility(pool)

    assert eligible == []
    assert decisions[0]["reasons"] == ["SHORT_DIRECTION_EXCLUDED"]


def test_paper_lab_excludes_internal_only_execution_capability() -> None:
    pool = [_variant("c1", "AMD", "30m", "session_momentum", execution_capability="internal_only")]

    eligible, decisions = paper_lab_eligibility(pool)

    assert eligible == []
    assert decisions[0]["reasons"] == ["INTERNAL_ONLY_EXCLUDED"]


def test_paper_lab_excludes_elites_that_never_passed_champion_validation() -> None:
    # promotion_state='elite' alone is not enough: a legacy elite that reached
    # 'elite' through the older pooled-consistency gate (never through the
    # champion validation battery) still shows validation_state != 'validated'.
    pool = [_variant("c1", "AMD", "30m", "session_momentum", validation_state="pending_validation")]

    eligible, decisions = paper_lab_eligibility(pool)

    assert eligible == []
    assert decisions[0]["reasons"] == ["NOT_VALIDATED"]


def test_paper_lab_excludes_rows_with_no_authoritative_lineage() -> None:
    pool = [_variant("c1", "AMD", "30m", "session_momentum", campaign_id=None)]

    eligible, decisions = paper_lab_eligibility(pool)

    assert eligible == []
    assert decisions[0]["reasons"] == ["MISSING_AUTHORITATIVE_LINEAGE"]


def test_paper_lab_reports_every_exclusion_reason_not_just_the_first() -> None:
    # Short AND internal-only at once -- both reasons must be visible, not
    # just whichever check happened to run first.
    pool = [_variant("c1", "AMD", "30m", "session_momentum", strategy_direction="short", execution_capability="internal_only")]

    _, decisions = paper_lab_eligibility(pool)

    assert set(decisions[0]["reasons"]) == {"SHORT_DIRECTION_EXCLUDED", "INTERNAL_ONLY_EXCLUDED"}


def test_paper_lab_deduplicates_the_same_candidate_symbol_timeframe() -> None:
    # Two different elite_research_candidates rows that somehow ended up
    # describing the same (candidate_id, symbol, timeframe) -- the exact shape
    # a data-quality slip would produce. Only one may be deployed.
    better = _variant("dup1", "AMD", "30m", "session_momentum", quality_score=0.9)
    worse = {**_variant("dup1", "AMD", "30m", "session_momentum", quality_score=0.4)}

    eligible, decisions = paper_lab_eligibility([better, worse])

    assert len(eligible) == 1
    kept = next(row for row in decisions if row["eligible"])
    excluded = next(row for row in decisions if not row["eligible"])
    assert excluded["reasons"] == ["DUPLICATE_CANDIDATE_SYMBOL_TIMEFRAME"]
    # The higher-quality row is the one kept.
    assert eligible[0]["quality_score"] == 0.9
    assert kept["candidate_key"] == excluded["candidate_key"]


def test_paper_lab_excluded_reasons_are_visible_with_human_labels() -> None:
    pool = [
        _variant("c1", "AMD", "30m", "session_momentum"),
        _variant("c2", "NVDA", "15m", "vwap_trend", validation_state="pending_validation"),
    ]

    result = paper_lab_preview(pool)

    assert result["eligible_count"] == 1
    assert result["excluded_count"] == 1
    rejection = result["rejection_explanations"][0]
    assert rejection["candidate_id"] == "c2"
    assert rejection["reason_labels"] == ["Has not passed the champion validation battery (validation_state != 'validated')"]


def test_paper_lab_never_produces_a_hard_conflict() -> None:
    # Highly correlated, same symbol, same family, near-identical parameters --
    # every one of the diversified solver's hard-conflict triggers -- and the
    # paper lab must still report every conflict as advisory only.
    pool = [_variant(f"c{i}", "AMD", "30m", "session_momentum") for i in range(4)]

    result = paper_lab_preview(pool)

    assert len(result["selected"]) == 4
    assert result["conflicts"]  # some advisory evidence should exist to flag
    assert all(row["hard_conflict"] is False for row in result["conflicts"])
    assert all(row.get("advisory_only") is True for row in result["conflicts"])


def test_paper_lab_advisory_conflicts_use_the_strict_thresholds_not_looser_ones() -> None:
    # Distinct parameters, distinct symbols -- nothing should be flagged, using
    # the exact same 0.90/0.75 thresholds the diversified solver enforces as
    # hard limits. The paper lab borrows the thresholds for labeling; it does
    # not invent looser ones.
    pool = [
        _variant("c1", "AMD", "30m", "session_momentum"),
        _variant("c2", "NVDA", "15m", "vwap_trend"),
    ]

    result = paper_lab_preview(pool)

    assert result["conflicts"] == []


def test_paper_lab_is_infeasible_only_when_nothing_at_all_qualifies() -> None:
    pool = [_variant("c1", "AMD", "30m", "session_momentum", validation_state="pending_validation")]

    result = paper_lab_preview(pool)

    assert result["status"] == "infeasible"
    assert result["eligible_count"] == 0
    assert result["selected"] == []


def test_paper_lab_snapshot_is_deterministic_and_hashed() -> None:
    pool = [_variant("c1", "AMD", "30m", "session_momentum")]

    first = paper_lab_preview(pool)
    second = paper_lab_preview(pool)

    assert first["snapshot"]["decision_hash"] == second["snapshot"]["decision_hash"]
    assert first["snapshot"]["mode"] == PAPER_LAB_MODE
