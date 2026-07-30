from app.services.intraday_factor_diagnostics import (
    FACTOR_SPECS,
    first_to_last_half_hour_observations,
)
from app.services.intraday_research_controls import (
    PUBLISHED_INTRADAY_MOMENTUM,
    certify_measurement_instrument,
    measure_observations,
    negative_controls,
    positive_control,
    published_intraday_momentum_replication,
    synthetic_intraday_candles,
)


def test_pipeline_recovers_a_planted_positive_factor():
    result = positive_control(
        first_to_last_half_hour_observations,
        sessions=260,
        injected_effect_bps=12.0,
        injected_sign=1,
    )

    assert result["passed"] is True
    assert result["measured"]["day_clustered_t_statistic"] >= 3.0
    # The recovered size must be near the planted size, not merely positive.
    assert 8.0 <= result["measured"]["mean_return_bps"] <= 16.0


def test_pipeline_recovers_a_planted_negative_factor_with_the_right_sign():
    result = positive_control(
        first_to_last_half_hour_observations,
        sessions=260,
        injected_effect_bps=12.0,
        injected_sign=-1,
    )

    assert result["passed"] is True
    assert result["recovered_sign_matches"] is True
    assert result["measured"]["mean_return_bps"] < 0


def test_pipeline_reports_nothing_on_a_market_with_no_factor():
    candles = synthetic_intraday_candles(
        symbols=("SPY", "QQQ"), sessions=260, injected_effect_bps=0.0, seed=42
    )

    measured = measure_observations(
        first_to_last_half_hour_observations(candles, timeframe="30m")
    )

    assert measured["detected_either_sign"] is False


def test_a_planted_directional_event_factor_is_recovered():
    candles = synthetic_intraday_candles(
        symbols=("SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMD"),
        sessions=300,
        injected_effect_bps=0.0,
        gap_effect_bps=30.0,
        gap_probability=0.25,
        seed=11,
    )

    observations = FACTOR_SPECS["overnight_gap_acceptance_absorption"].builder(
        candles, timeframe="30m"
    )
    measured = measure_observations(observations)

    assert observations
    assert measured["detected"] is True
    assert measured["mean_return_bps"] > 15.0


def test_every_placebo_fails_on_data_containing_a_real_factor():
    candles = synthetic_intraday_candles(
        symbols=("SPY", "QQQ"), sessions=260, injected_effect_bps=15.0, seed=5
    )
    observations = first_to_last_half_hour_observations(candles, timeframe="30m")

    assert measure_observations(observations)["detected"] is True
    placebos = negative_controls(observations)

    assert placebos["placebos_failing"] == []
    assert placebos["passed"] is True


def test_published_replication_reproduces_a_planted_slope():
    candles = synthetic_intraday_candles(
        symbols=("SPY",), sessions=400, injected_effect_bps=20.0, seed=9
    )

    replication = published_intraday_momentum_replication(candles)

    assert replication["status"] == "measured"
    assert replication["reproduces_published_sign"] is True
    assert replication["predictive_regression"]["slope"] > 0
    assert replication["timing_strategy_gross"]["mean_return_bps"] > 0
    assert replication["timing_strategy_gross"]["gross_of_costs"] is True
    assert replication["published"] == PUBLISHED_INTRADAY_MOMENTUM
    assert replication["predictive_regression"]["newey_west_t_statistic"] is not None


def test_published_replication_reports_a_decayed_sign_rather_than_failing():
    candles = synthetic_intraday_candles(
        symbols=("SPY",),
        sessions=400,
        injected_effect_bps=20.0,
        injected_sign=-1,
        seed=9,
    )

    replication = published_intraday_momentum_replication(candles)

    assert replication["status"] == "measured"
    assert replication["reproduces_published_sign"] is False
    assert replication["predictive_regression"]["slope"] < 0


def test_published_replication_refuses_a_sample_too_short_to_measure():
    candles = synthetic_intraday_candles(symbols=("SPY",), sessions=10, seed=1)

    replication = published_intraday_momentum_replication(candles)

    assert replication["status"] == "insufficient_sessions"


def test_full_certification_passes_on_the_shipped_specs():
    report = certify_measurement_instrument(FACTOR_SPECS, sessions=260)

    assert report["certified"] is True
    assert all(report["checks"].values())
    assert report["checks"]["recovers_injected_directional_event_factor"] is True
    assert report["negative_controls"]["placebos_failing"] == []
