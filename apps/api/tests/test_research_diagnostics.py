"""Phase F: why a family lost, and what single change to test next."""

import pytest

from app.services.research_diagnostics import (
    FAILURE_REASONS,
    MUTATION_RULES,
    decompose_family_performance,
    diagnose_failure,
    propose_next_experiment,
)


def trade(
    *,
    symbol="NVDA",
    direction="long",
    gross=10.0,
    fees=1.0,
    slippage=0.5,
    exit_reason="take_profit",
    holding=3.0,
    mfe_r=1.5,
    mae_r=0.4,
    minutes_from_open=60,
    market_regime=None,
    volatility_regime=None,
    risk_per_unit=1.0,
    quantity=1.0,
):
    return {
        "symbol": symbol,
        "direction": direction,
        "gross_pnl": gross,
        "fees": fees,
        "slippage_cost": slippage,
        "net_pnl": gross - fees - slippage,
        "exit_reason": exit_reason,
        "holding_period_hours": holding,
        "mfe_r": mfe_r,
        "mae_r": mae_r,
        "entry_minutes_from_open": minutes_from_open,
        "market_regime": market_regime,
        "volatility_regime": volatility_regime,
        "risk_per_unit": risk_per_unit,
        "quantity": quantity,
    }


def _diagnose(trades):
    return diagnose_failure(decompose_family_performance(trades, architecture="demo_v2"))


# ---------------------------------------------------------------------------
# Decomposition
# ---------------------------------------------------------------------------

def test_costs_are_separated_from_gross_edge():
    trades = [trade(gross=10.0, fees=1.0, slippage=0.5) for _ in range(10)]

    result = decompose_family_performance(trades, architecture="demo_v2")

    costs = result["cost_decomposition"]
    assert costs["gross_edge_before_costs"] == pytest.approx(100.0)
    assert costs["fees"] == pytest.approx(10.0)
    assert costs["slippage"] == pytest.approx(5.0)
    assert costs["net_pnl"] == pytest.approx(85.0)
    assert costs["cost_share_of_gross_edge"] == pytest.approx(0.15)


def test_an_empty_family_is_not_evaluable():
    result = decompose_family_performance([], architecture="demo_v2")

    assert result["evaluable"] is False
    assert "no stored trades" in result["reason"]


def test_regime_breakdown_is_unavailable_when_every_tag_is_unknown():
    """Intraday jobs pass an empty context_by_time, so regime tags read
    'unknown'. Reporting a breakdown over one bucket would imply a
    measurement that was never made."""
    trades = [trade() for _ in range(10)]

    result = decompose_family_performance(trades, architecture="demo_v2")

    assert result["by_market_regime"] is None
    assert "unavailable" in result["regime_note"]


def test_regime_breakdown_appears_when_regimes_are_tagged():
    trades = [trade(market_regime="bull_trend") for _ in range(5)]
    trades += [trade(market_regime="sideways", gross=-5.0) for _ in range(5)]

    result = decompose_family_performance(trades, architecture="demo_v2")

    assert result["by_market_regime"] is not None
    assert {row["market_regime"] for row in result["by_market_regime"]} == {"bull_trend", "sideways"}


def test_breakdowns_cover_direction_symbol_exit_and_time_of_day():
    trades = [trade(symbol="NVDA", direction="long", exit_reason="take_profit", minutes_from_open=10)]
    trades += [trade(symbol="TSLA", direction="short", exit_reason="stop_loss", minutes_from_open=400)]

    result = decompose_family_performance(trades, architecture="demo_v2")

    assert {row["symbol"] for row in result["by_symbol"]} == {"NVDA", "TSLA"}
    assert {row["direction"] for row in result["by_direction"]} == {"long", "short"}
    assert {row["exit_reason"] for row in result["by_exit_reason"]} == {"take_profit", "stop_loss"}
    assert {row["_time_of_day"] for row in result["by_time_of_day"]} == {"open_0_30m", "close_5h_plus"}


# ---------------------------------------------------------------------------
# Failure taxonomy
# ---------------------------------------------------------------------------

def test_no_signal_is_diagnosed_before_anything_else():
    """When there is no edge before costs, every other observation describes
    noise. Reporting 'wrong exit logic' here would send someone to redesign
    an exit that was never the problem."""
    trades = [trade(gross=-10.0, mfe_r=0.1, mae_r=5.0) for _ in range(20)]

    diagnosis = _diagnose(trades)

    assert diagnosis["failure_reason"] == "NO_RAW_SIGNAL"
    assert diagnosis["confidence"] == "high"


def test_a_real_signal_eaten_by_costs_is_distinguished_from_no_signal():
    """The distinction that decides whether to retire or restructure."""
    trades = [trade(gross=1.0, fees=1.0, slippage=0.5) for _ in range(20)]

    diagnosis = _diagnose(trades)

    assert diagnosis["failure_reason"] == "COST_DESTROYED_SIGNAL"
    assert diagnosis["evidence"]["gross_edge_before_costs"] > 0


def test_a_one_sided_hypothesis_is_diagnosed_as_wrong_direction():
    trades = [trade(direction="long", gross=20.0, fees=0.1, slippage=0.1) for _ in range(10)]
    trades += [trade(direction="short", gross=-14.0, fees=0.1, slippage=0.1) for _ in range(10)]

    diagnosis = _diagnose(trades)

    assert diagnosis["failure_reason"] == "WRONG_DIRECTION"


def test_giving_back_favorable_excursion_is_diagnosed_as_an_exit_problem():
    trades = [
        trade(gross=1.0, fees=0.05, slippage=0.05, mfe_r=5.0, mae_r=0.2, risk_per_unit=1.0, quantity=1.0)
        for _ in range(20)
    ]

    diagnosis = _diagnose(trades)

    assert diagnosis["failure_reason"] == "WRONG_EXIT_LOGIC"
    assert diagnosis["evidence"]["mean_favorable_r"] > diagnosis["evidence"]["mean_realized_r"]


def test_price_moving_against_the_fill_is_diagnosed_as_entry_latency():
    trades = [
        trade(gross=5.0, fees=0.05, slippage=0.05, mfe_r=0.4, mae_r=2.0, risk_per_unit=1.0, quantity=1.0)
        for _ in range(20)
    ]

    diagnosis = _diagnose(trades)

    assert diagnosis["failure_reason"] == "ENTRY_LATENCY_PROBLEM"


def test_one_symbol_carrying_the_family_is_diagnosed():
    trades = [trade(symbol="NVDA", gross=50.0, fees=0.1, slippage=0.1, mfe_r=1.0, mae_r=0.3) for _ in range(10)]
    trades += [trade(symbol="TSLA", gross=1.0, fees=0.1, slippage=0.1, mfe_r=1.0, mae_r=0.3) for _ in range(10)]

    diagnosis = _diagnose(trades)

    assert diagnosis["failure_reason"] == "ONE_SYMBOL_DEPENDENCE"
    assert diagnosis["evidence"]["largest_symbol_profit_share"] > 0.6


def test_a_healthy_family_reports_no_failure():
    trades = [
        trade(symbol=symbol, gross=10.0, fees=0.2, slippage=0.1, mfe_r=1.2, mae_r=0.4)
        for symbol in ("NVDA", "TSLA", "AMD", "SPY")
        for _ in range(6)
    ]

    diagnosis = _diagnose(trades)

    assert diagnosis["failure_reason"] == "PASSED_NO_FAILURE"


def test_every_diagnosis_uses_a_declared_reason():
    trades = [trade() for _ in range(10)]

    assert _diagnose(trades)["failure_reason"] in FAILURE_REASONS


# ---------------------------------------------------------------------------
# Mutation engine
# ---------------------------------------------------------------------------

def test_a_family_with_no_signal_is_retired_not_mutated():
    """Mutating noise produces variants of noise -- exactly how a search ends
    up with 120 configurations and no hypothesis."""
    experiment = propose_next_experiment(
        {"failure_reason": "NO_RAW_SIGNAL"}, architecture="demo_v2"
    )

    assert experiment["mutate"] is False
    assert experiment["recommendation"] == "retire"


def test_a_healthy_family_is_sent_to_confirmation_not_mutated():
    experiment = propose_next_experiment(
        {"failure_reason": "PASSED_NO_FAILURE"}, architecture="demo_v2"
    )

    assert experiment["mutate"] is False
    assert experiment["recommendation"] == "advance_to_confirmation"


def test_a_cost_destroyed_signal_widens_the_stop_and_preserves_the_entry():
    """Risk-based sizing makes cost in R scale as 1/stop, so widening the stop
    is the only change that lowers cost without touching the signal."""
    experiment = propose_next_experiment(
        {"failure_reason": "COST_DESTROYED_SIGNAL"}, architecture="demo_v2"
    )

    assert experiment["mutate"] is True
    assert experiment["changes"] == "stop_atr_multiple"
    assert experiment["change_direction"] == "increase"
    assert "entry signal" in experiment["preserves"]


def test_every_mutation_changes_exactly_one_thing():
    for reason, rule in MUTATION_RULES.items():
        experiment = propose_next_experiment({"failure_reason": reason}, architecture="demo_v2")

        assert experiment["mutate"] is True, reason
        assert isinstance(experiment["changes"], str), reason
        assert "," not in experiment["changes"], f"{reason} changes more than one parameter"


def test_a_mutation_carries_the_hypothesis_it_is_testing():
    hypothesis = {
        "hypothesis": "Opening range breakouts continue when volume confirms.",
        "invalidation_conditions": ["No continuation after breakout"],
    }

    experiment = propose_next_experiment(
        {"failure_reason": "WRONG_EXIT_LOGIC"}, architecture="demo_v2", hypothesis=hypothesis
    )

    assert experiment["hypothesis_under_test"] == hypothesis["hypothesis"]
    assert experiment["invalidation_conditions"] == hypothesis["invalidation_conditions"]


def test_an_undiagnosed_failure_asks_for_investigation_rather_than_guessing():
    experiment = propose_next_experiment({"failure_reason": "SOMETHING_NEW"}, architecture="demo_v2")

    assert experiment["mutate"] is False
    assert experiment["recommendation"] == "investigate"


def test_every_failure_reason_has_a_defined_response():
    """No failure may fall through to silence."""
    for reason in FAILURE_REASONS:
        experiment = propose_next_experiment({"failure_reason": reason}, architecture="demo_v2")

        assert experiment["recommendation"] in {
            "retire",
            "advance_to_confirmation",
            "single_change_experiment",
            "investigate",
        }, reason
