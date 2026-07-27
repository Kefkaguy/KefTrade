"""Phase C: families are judged by the structure of their response surface."""

import pytest

from app.services.labs.intraday.response_surface import (
    DISCOVERY_BEST_DECILE_PROFIT_FACTOR,
    MINIMUM_STABLE_REGION_VARIANTS,
    analyze_family_response_surface,
    are_parameter_neighbors,
    concentration,
    cost_scenarios,
    family_response_surface_report,
    largest_stable_region,
    recost_net_pnl,
    trade_concentration,
)


def trade(candidate_id, symbol, gross, *, fees=1.0, slippage=0.5, month="2026-01"):
    return {
        "candidate_id": candidate_id,
        "symbol": symbol,
        "month_key": month,
        "gross_pnl": gross,
        "fees": fees,
        "slippage_cost": slippage,
        "strategy_architecture": "demo_v2",
    }


def variant(candidate_id, params, expectancy):
    return {"candidate_id": candidate_id, "parameters": params, "expectancy": expectancy}


# ---------------------------------------------------------------------------
# Re-costing
# ---------------------------------------------------------------------------

def test_recosting_scales_only_the_cost_components():
    row = trade("c1", "NVDA", 100.0, fees=10.0, slippage=4.0)

    assert recost_net_pnl(row, fee_multiplier=1.0, slippage_multiplier=1.0) == pytest.approx(86.0)
    assert recost_net_pnl(row, fee_multiplier=0.1, slippage_multiplier=0.5) == pytest.approx(97.0)


def test_removing_costs_entirely_leaves_gross_pnl():
    row = trade("c1", "NVDA", 100.0, fees=10.0, slippage=4.0)

    assert recost_net_pnl(row, fee_multiplier=0.0, slippage_multiplier=0.0) == pytest.approx(100.0)


def test_cost_scenarios_derive_multipliers_from_the_live_configuration():
    """Hardcoding the multipliers would let the comparison drift silently if
    the base parameters ever change."""
    scenarios = cost_scenarios()

    assert scenarios["as_simulated"]["fee_multiplier"] == 1.0
    assert scenarios["realistic_retail"]["fee_multiplier"] < 1.0
    expected = scenarios["realistic_retail"]["fee_rate"] / scenarios["as_simulated"]["fee_rate"]
    assert scenarios["realistic_retail"]["fee_multiplier"] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Parameter neighborhoods
# ---------------------------------------------------------------------------

def test_variants_differing_in_one_parameter_are_neighbors():
    assert are_parameter_neighbors({"a": 1, "b": 2}, {"a": 1, "b": 3}) is True


def test_variants_differing_in_two_parameters_are_not_neighbors():
    assert are_parameter_neighbors({"a": 1, "b": 2}, {"a": 9, "b": 3}) is False


def test_identical_variants_are_not_neighbors():
    assert are_parameter_neighbors({"a": 1}, {"a": 1}) is False


def test_a_connected_plateau_is_found():
    variants = [
        variant("c1", {"x": 1, "y": 1}, 5.0),
        variant("c2", {"x": 2, "y": 1}, 4.0),
        variant("c3", {"x": 3, "y": 1}, 3.0),
    ]

    region = largest_stable_region(variants)

    assert region["size"] == 3
    assert region["candidate_ids"] == ["c1", "c2", "c3"]


def test_scattered_lucky_variants_do_not_form_a_region():
    """Three positive variants that are not adjacent to one another are noise,
    not a plateau -- this is the distinction the mean-based screen missed."""
    variants = [
        variant("c1", {"x": 1, "y": 1}, 5.0),
        variant("c2", {"x": 9, "y": 9}, 4.0),
        variant("c3", {"x": 4, "y": 7}, 3.0),
    ]

    assert largest_stable_region(variants)["size"] == 1


def test_a_losing_variant_between_two_winners_breaks_the_region():
    """Adjacency is one step in the explored grid, so a losing configuration
    sitting between two winners genuinely separates them. Counting them as a
    plateau would be exactly the "one lucky variant" error this screen exists
    to catch."""
    variants = [
        variant("c1", {"x": 1}, 5.0),
        variant("c2", {"x": 2}, -4.0),
        variant("c3", {"x": 3}, 3.0),
    ]

    assert largest_stable_region(variants)["size"] == 1


def test_neighbors_must_be_one_step_apart_in_the_explored_grid():
    order = {"x": [1, 2, 3]}

    assert are_parameter_neighbors({"x": 1}, {"x": 2}, order) is True
    assert are_parameter_neighbors({"x": 1}, {"x": 3}, order) is False


def test_neighborhood_falls_back_to_single_key_difference_without_a_grid():
    assert are_parameter_neighbors({"x": 1}, {"x": 3}) is True


def test_a_surface_with_no_positive_variants_has_no_region():
    assert largest_stable_region([variant("c1", {"x": 1}, -1.0)])["size"] == 0


# ---------------------------------------------------------------------------
# Concentration
# ---------------------------------------------------------------------------

def test_concentration_finds_the_dominant_contributor():
    result = concentration({"NVDA": 90.0, "TSLA": 10.0})

    assert result["largest_key"] == "NVDA"
    assert result["largest_share"] == pytest.approx(0.9)


def test_concentration_ignores_losing_contributors_when_sharing_profit():
    result = concentration({"NVDA": 100.0, "TSLA": -50.0})

    assert result["largest_share"] == pytest.approx(1.0)
    assert result["contributors"] == 1


def test_concentration_of_a_losing_family_has_no_share():
    result = concentration({"NVDA": -10.0})

    assert result["largest_share"] is None


def test_trade_concentration_detects_a_result_carried_by_one_trade():
    pnls = [1000.0] + [1.0] * 19

    result = trade_concentration(pnls, top_fraction=0.05)

    assert result["top_trade_count"] == 1
    assert result["top_trade_share"] > 0.9


def test_trade_concentration_is_low_when_profits_are_spread():
    result = trade_concentration([10.0] * 20, top_fraction=0.05)

    assert result["top_trade_share"] == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# Family verdicts
# ---------------------------------------------------------------------------

def _healthy_family():
    """A plateau of three neighboring positive variants across two symbols,
    with profits spread across trades and months."""
    trades = []
    for index, candidate in enumerate(("c1", "c2", "c3")):
        params = {"threshold": index, "window": 5}
        for symbol in ("NVDA", "TSLA"):
            for month in ("2026-01", "2026-02"):
                for _ in range(4):
                    trades.append(trade(candidate, symbol, 30.0, fees=1.0, slippage=0.5, month=month))
                trades.append(trade(candidate, symbol, -10.0, fees=1.0, slippage=0.5, month=month))
        params_by_candidate[candidate] = params
    return trades


params_by_candidate: dict[str, dict] = {}


def test_a_structurally_sound_family_is_promising():
    trades = _healthy_family()

    surface = analyze_family_response_surface(
        trades, params_by_candidate, architecture="demo_v2"
    )

    assert surface["promising"] is True, surface["exclusion_reasons"]
    assert surface["stable_region"]["size"] >= MINIMUM_STABLE_REGION_VARIANTS
    assert surface["positive_symbol_count"] == 2


def test_a_family_carried_by_one_symbol_is_rejected():
    trades = []
    for index, candidate in enumerate(("c1", "c2", "c3")):
        params_by_candidate[candidate] = {"threshold": index, "window": 5}
        for _ in range(12):
            trades.append(trade(candidate, "NVDA", 60.0))
        trades.append(trade(candidate, "TSLA", -5.0))

    surface = analyze_family_response_surface(trades, params_by_candidate, architecture="demo_v2")

    assert surface["promising"] is False
    assert "PROFIT_CONCENTRATED_IN_ONE_SYMBOL" in surface["exclusion_reasons"]
    assert "TOO_FEW_POSITIVE_SYMBOLS" in surface["exclusion_reasons"]


def test_a_family_carried_by_a_single_trade_is_rejected():
    trades = []
    for index, candidate in enumerate(("c1", "c2", "c3")):
        params_by_candidate[candidate] = {"threshold": index, "window": 5}
        for symbol in ("NVDA", "TSLA"):
            for _ in range(9):
                trades.append(trade(candidate, symbol, 1.0, fees=0.0, slippage=0.0))
    trades.append(trade("c1", "NVDA", 100000.0, fees=0.0, slippage=0.0))

    surface = analyze_family_response_surface(trades, params_by_candidate, architecture="demo_v2")

    assert surface["promising"] is False
    assert "PROFIT_CONCENTRATED_IN_FEW_TRADES" in surface["exclusion_reasons"]


def test_a_family_with_one_lucky_variant_has_no_stable_region():
    """The failure mode the mean-based screen could not see: a single spike
    can lift a family average without any reproducible region beneath it."""
    trades = []
    params_by_candidate.clear()
    params_by_candidate["winner"] = {"threshold": 1, "window": 5}
    for _ in range(20):
        trades.append(trade("winner", "NVDA", 50.0, fees=0.0, slippage=0.0))
        trades.append(trade("winner", "TSLA", 50.0, fees=0.0, slippage=0.0))
    for index, loser in enumerate(("l1", "l2", "l3", "l4")):
        params_by_candidate[loser] = {"threshold": 10 + index, "window": 9 + index}
        for _ in range(10):
            trades.append(trade(loser, "NVDA", -5.0, fees=0.0, slippage=0.0))

    surface = analyze_family_response_surface(trades, params_by_candidate, architecture="demo_v2")

    assert surface["stable_region"]["size"] < MINIMUM_STABLE_REGION_VARIANTS
    assert "NO_STABLE_PARAMETER_REGION" in surface["exclusion_reasons"]


def test_a_family_with_too_little_evidence_gets_no_verdict():
    params_by_candidate.clear()
    params_by_candidate["c1"] = {"threshold": 1}
    trades = [trade("c1", "NVDA", 10.0) for _ in range(5)]

    surface = analyze_family_response_surface(trades, params_by_candidate, architecture="demo_v2")

    assert surface["promising"] is False
    assert "INSUFFICIENT_TRADES_FOR_A_VERDICT" in surface["exclusion_reasons"]


def test_the_discovery_floor_applies_to_the_best_decile_not_the_mean():
    """A grid deliberately contains losing variants; screening on the mean
    measures grid width, not edge."""
    assert DISCOVERY_BEST_DECILE_PROFIT_FACTOR == 1.2


# ---------------------------------------------------------------------------
# Dual-cost campaign report
# ---------------------------------------------------------------------------

class FakeReportConn:
    def __init__(self, trades, parameter_rows):
        self.trades = trades
        self.parameter_rows = parameter_rows

    def execute(self, query, params=None):
        stripped = query.strip()

        class Result:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

        if stripped.startswith("SELECT strategy_architecture"):
            return Result(self.trades)
        if stripped.startswith("SELECT DISTINCT ON (candidate_id)"):
            return Result(self.parameter_rows)
        raise AssertionError(f"unexpected query: {stripped[:60]}")


def test_the_report_flags_a_family_that_only_survives_corrected_costs():
    """The whole point of reporting both scenarios: a cost-calibration
    finding must not be silently resolved into an edge claim."""
    trades = []
    parameter_rows = []
    for index, candidate in enumerate(("c1", "c2", "c3")):
        parameter_rows.append({"candidate_id": candidate, "parameters": {"threshold": index, "window": 5}})
        for symbol in ("NVDA", "TSLA"):
            for month in ("2026-01", "2026-02"):
                for _ in range(5):
                    # Gross edge is real but the simulated cost swallows it.
                    trades.append(trade(candidate, symbol, 12.0, fees=10.0, slippage=2.0, month=month))
                trades.append(trade(candidate, symbol, -4.0, fees=10.0, slippage=2.0, month=month))

    report = family_response_surface_report(FakeReportConn(trades, parameter_rows), 101)

    assert report["families_analyzed"] == 1
    family = report["families"][0]
    assert family["promising_as_simulated"] is False
    assert family["promising_at_realistic_costs"] is True
    assert family["cost_sensitive"] is True
    assert "cost-calibration finding" in report["interpretation"]


def test_the_report_says_when_costs_change_nothing():
    trades = []
    parameter_rows = []
    for index, candidate in enumerate(("c1", "c2")):
        parameter_rows.append({"candidate_id": candidate, "parameters": {"threshold": index}})
        for _ in range(20):
            trades.append(trade(candidate, "NVDA", -50.0, fees=1.0, slippage=1.0))

    report = family_response_surface_report(FakeReportConn(trades, parameter_rows), 101)

    assert report["cost_sensitive_families"] == []
    assert "No family changes verdict" in report["interpretation"]
