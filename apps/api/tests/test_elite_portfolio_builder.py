from __future__ import annotations

import time

from app.services.elite_portfolio_builder import SOLVER_VERSION, exact_timeframe_cap_holds, preview


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
    assert result["termination_reason"] == "no_portfolio_satisfies_all_constraints"
    assert result["constraint_relaxation_count"] == 0
    assert any(row["constraint"].endswith("CORRELATION_INSUFFICIENT") for row in result["binding_constraints"])


def test_500_candidate_preview_completes_under_two_seconds() -> None:
    candidates = diversified_candidates(500)
    started = time.perf_counter()
    result = preview(candidates, {"constraints": {"maximum_portfolio_size": 20}})
    elapsed = time.perf_counter() - started

    assert result["candidates_examined"] == 500
    assert elapsed < 2.0
