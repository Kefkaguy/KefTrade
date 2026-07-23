from __future__ import annotations

import time

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
