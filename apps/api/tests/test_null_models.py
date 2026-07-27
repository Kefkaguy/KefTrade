"""Phase B: baselines and null models, checked against known answers."""

import math

import pytest

from app.services.null_models import (
    benchmark_report,
    bootstrap_confidence_interval,
    buy_and_hold_return,
    cost_break_even_multiplier,
    deflated_sharpe_ratio,
    expected_maximum_sharpe,
    moving_average_trend_return,
    null_percentile,
    probability_of_backtest_overfitting,
    random_entry_null_distribution,
    sharpe_ratio,
    time_shuffled_null_distribution,
)


def _rising(count=300, start=100.0, step=0.5):
    return [start + step * index for index in range(count)]


def _flat(count=300, level=100.0):
    return [level] * count


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------

def test_buy_and_hold_is_the_plain_price_change():
    assert buy_and_hold_return([100.0, 110.0]) == pytest.approx(0.10)
    assert buy_and_hold_return([100.0, 90.0]) == pytest.approx(-0.10)


def test_buy_and_hold_is_zero_without_two_prices():
    assert buy_and_hold_return([100.0]) == 0.0
    assert buy_and_hold_return([]) == 0.0


def test_moving_average_trend_profits_on_a_rising_market():
    assert moving_average_trend_return(_rising(), window=20) > 0


def test_moving_average_trend_is_flat_on_a_flat_market():
    assert moving_average_trend_return(_flat(), window=20) == pytest.approx(0.0)


def test_switching_costs_reduce_the_trend_baseline():
    free = moving_average_trend_return(_rising(), window=20, cost_per_switch=0.0)
    costly = moving_average_trend_return(_rising(), window=20, cost_per_switch=0.01)

    assert costly < free


# ---------------------------------------------------------------------------
# Matched random null
# ---------------------------------------------------------------------------

def test_random_entries_profit_on_a_rising_market():
    """The point of the matched null: on a drifting market even uninformed
    entries make money, so a positive return is not evidence of skill."""
    distribution = random_entry_null_distribution(
        _rising(), trade_count=10, holding_bars=5, samples=200, seed=1
    )

    assert distribution
    assert distribution[len(distribution) // 2] > 0


def test_the_random_null_is_deterministic_for_a_seed():
    first = random_entry_null_distribution(_rising(), trade_count=5, holding_bars=5, samples=50, seed=7)
    second = random_entry_null_distribution(_rising(), trade_count=5, holding_bars=5, samples=50, seed=7)

    assert first == second


def test_costs_shift_the_random_null_down():
    free = random_entry_null_distribution(_rising(), trade_count=10, holding_bars=5, samples=200, seed=1)
    costly = random_entry_null_distribution(
        _rising(), trade_count=10, holding_bars=5, samples=200, seed=1, round_trip_cost=0.01
    )

    assert costly[len(costly) // 2] < free[len(free) // 2]


def test_a_short_null_mirrors_the_long_null_on_the_same_path():
    long_null = random_entry_null_distribution(_rising(), trade_count=10, holding_bars=5, samples=100, seed=3)
    short_null = random_entry_null_distribution(
        _rising(), trade_count=10, holding_bars=5, direction="short", samples=100, seed=3
    )

    assert short_null[len(short_null) // 2] < 0 < long_null[len(long_null) // 2]


def test_an_impossible_null_returns_empty_rather_than_a_fabricated_distribution():
    assert random_entry_null_distribution(_rising(10), trade_count=5, holding_bars=50, samples=10) == []
    assert random_entry_null_distribution(_rising(), trade_count=0, holding_bars=5) == []


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------

def test_null_percentile_locates_the_observed_result():
    distribution = [0.0, 1.0, 2.0, 3.0]

    assert null_percentile(4.0, distribution) == 1.0
    assert null_percentile(-1.0, distribution) == 0.0
    assert null_percentile(1.5, distribution) == 0.5


def test_null_percentile_is_none_without_a_distribution():
    """Never fabricate a neutral 0.5 for an absent null."""
    assert null_percentile(1.0, []) is None


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def test_bootstrap_interval_brackets_the_mean():
    values = [0.01, 0.02, -0.005, 0.015, 0.008, 0.012, -0.002, 0.02]
    result = bootstrap_confidence_interval(values, samples=500, seed=2)

    assert result["lower"] <= result["mean"] <= result["upper"]
    assert result["sample_size"] == len(values)


def test_a_clearly_positive_sample_excludes_zero():
    result = bootstrap_confidence_interval([0.05] * 30, samples=500, seed=2)

    assert result["excludes_zero"] is True


def test_a_noisy_sample_around_zero_does_not_exclude_zero():
    values = [0.05, -0.05] * 20
    result = bootstrap_confidence_interval(values, samples=500, seed=2)

    assert result["excludes_zero"] is False


def test_bootstrap_reports_no_interval_for_a_single_observation():
    result = bootstrap_confidence_interval([0.01])

    assert result["lower"] is None and result["upper"] is None


# ---------------------------------------------------------------------------
# Deflated Sharpe
# ---------------------------------------------------------------------------

def test_expected_maximum_sharpe_grows_with_the_number_of_trials():
    few = expected_maximum_sharpe(trials=10, sharpe_variance=0.01)
    many = expected_maximum_sharpe(trials=10000, sharpe_variance=0.01)

    assert many > few > 0


def test_a_single_trial_has_no_selection_bar():
    assert expected_maximum_sharpe(trials=1, sharpe_variance=0.01) == 0.0


def test_more_trials_deflate_the_same_observed_sharpe():
    """The same track record is less impressive after 5000 attempts than
    after 5 -- that is the entire point of the deflation."""
    returns = [0.01, 0.02, -0.005, 0.015, 0.008, 0.012, -0.002, 0.02, 0.01, 0.011] * 5
    few = deflated_sharpe_ratio(returns, trials=5)
    many = deflated_sharpe_ratio(returns, trials=5000)

    assert few["deflated_sharpe"] > many["deflated_sharpe"]
    assert many["expected_maximum_sharpe"] > few["expected_maximum_sharpe"]


def test_deflated_sharpe_needs_a_real_sample():
    result = deflated_sharpe_ratio([0.01, 0.02], trials=10)

    assert result["deflated_sharpe"] is None
    assert "reason" in result


def test_sharpe_is_none_for_a_constant_series():
    assert sharpe_ratio([0.01, 0.01, 0.01]) is None


# ---------------------------------------------------------------------------
# Probability of backtest overfitting
# ---------------------------------------------------------------------------

def test_pbo_is_high_when_in_sample_ranking_carries_no_information():
    """Variants whose fold-to-fold performance alternates: whichever wins
    in-sample loses out-of-sample by construction."""
    matrix = [
        [1.0, 0.0],
        [0.0, 1.0],
        [1.0, 0.0],
        [0.0, 1.0],
    ]
    result = probability_of_backtest_overfitting(matrix)

    assert result["probability_of_backtest_overfitting"] > 0.5


def test_pbo_is_low_when_one_variant_is_genuinely_better_everywhere():
    matrix = [
        [2.0, 0.1],
        [2.1, 0.0],
        [1.9, 0.2],
        [2.2, 0.1],
    ]
    result = probability_of_backtest_overfitting(matrix)

    assert result["probability_of_backtest_overfitting"] == 0.0


def test_pbo_needs_folds_and_variants():
    assert probability_of_backtest_overfitting([[1.0, 2.0]])["probability_of_backtest_overfitting"] is None
    assert probability_of_backtest_overfitting([[1.0], [2.0]])["probability_of_backtest_overfitting"] is None


def test_pbo_rejects_a_ragged_matrix():
    with pytest.raises(ValueError):
        probability_of_backtest_overfitting([[1.0, 2.0], [1.0]])


# ---------------------------------------------------------------------------
# Cost break-even
# ---------------------------------------------------------------------------

def test_cost_break_even_separates_no_signal_from_cost_destroyed_signal():
    """The distinction that decides what to do next: there is nothing to
    rescue in the first case and a real edge to protect in the second."""
    no_signal = cost_break_even_multiplier(
        gross_profit_factor=0.8, total_costs=50.0, gross_profit=80.0, gross_loss=100.0
    )
    destroyed = cost_break_even_multiplier(
        gross_profit_factor=1.4, total_costs=50.0, gross_profit=140.0, gross_loss=100.0
    )
    survives = cost_break_even_multiplier(
        gross_profit_factor=3.0, total_costs=10.0, gross_profit=300.0, gross_loss=100.0
    )

    assert no_signal["diagnosis"] == "no_gross_edge"
    assert destroyed["diagnosis"] == "cost_destroyed_signal"
    assert survives["diagnosis"] == "edge_survives_costs"
    assert survives["survives_current_costs"] is True


def test_cost_break_even_is_undefined_without_costs():
    result = cost_break_even_multiplier(
        gross_profit_factor=1.5, total_costs=0.0, gross_profit=150.0, gross_loss=100.0
    )

    assert result["cost_break_even_multiplier"] is None


# ---------------------------------------------------------------------------
# Time-shuffled null
# ---------------------------------------------------------------------------

def test_time_shuffling_produces_a_drawdown_distribution():
    returns = [0.02, -0.01, 0.03, -0.02, 0.01, -0.015] * 5
    distribution = time_shuffled_null_distribution(returns, samples=200, seed=4)

    assert len(distribution) == 200
    assert all(value >= 0 for value in distribution)


def test_time_shuffling_is_empty_without_trades():
    assert time_shuffled_null_distribution([], samples=10) == []


# ---------------------------------------------------------------------------
# Combined report
# ---------------------------------------------------------------------------

def test_a_strategy_matching_random_entries_is_not_called_promising():
    """A profit factor above 1.0 on a rising market is exactly what the null
    produces. The report must refuse to call that evidence."""
    closes = _rising()
    report = benchmark_report(
        strategy_total_return=0.01,
        trade_returns=[0.001] * 20,
        closes=closes,
        holding_bars=5,
        trials=120,
        samples=200,
        seed=5,
    )

    assert report["verdict"] == "not_distinguishable_from_random"
    assert report["beats_random_at_95"] is False
    assert report["excess_return"] < 0


def test_the_report_names_the_baseline_it_had_to_beat():
    report = benchmark_report(
        strategy_total_return=0.5,
        trade_returns=[0.01] * 20,
        closes=_rising(),
        holding_bars=5,
        trials=10,
        samples=200,
        seed=5,
    )

    assert "buy_and_hold" in report["baselines"]
    assert "moving_average_trend" in report["baselines"]
    assert "random_entry_median" in report["baselines"]
    assert report["baseline_return"] == max(
        value for value in report["baselines"].values() if value is not None
    )


def test_an_exceptional_strategy_beats_the_matched_null():
    report = benchmark_report(
        strategy_total_return=100.0,
        trade_returns=[0.05] * 30,
        closes=_rising(),
        holding_bars=5,
        trials=10,
        samples=200,
        seed=5,
    )

    assert report["null_percentile"] == 1.0
    assert report["verdict"] == "beats_matched_random_null"


def test_normal_helpers_round_trip():
    from app.services.null_models import _normal_cdf, _normal_ppf

    for probability in (0.01, 0.1, 0.5, 0.9, 0.99):
        assert _normal_cdf(_normal_ppf(probability)) == pytest.approx(probability, abs=1e-6)
    assert _normal_cdf(0.0) == pytest.approx(0.5)
    assert math.isclose(_normal_ppf(0.975), 1.959964, abs_tol=1e-5)
