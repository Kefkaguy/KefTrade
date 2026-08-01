from datetime import UTC, date, datetime, timedelta

from app.services.intraday_factor_diagnostics import (
    factor_metrics,
    first_to_last_half_hour_observations,
)
from app.services.intraday_research_controls import synthetic_intraday_candles
from app.services.intraday_research_power import (
    benchmark_session_context,
    concentration_report,
    effect_size_drift,
    power_and_stability_report,
    required_sessions_for_power,
)

COST_MODEL = {
    "observed_round_trip_bps": 1.0,
    "stressed_round_trip_bps": 2.0,
    "conservative_round_trip_bps": 30.0,
}


def observations_and_metrics(sessions: int = 400, effect: float = 10.0):
    candles = synthetic_intraday_candles(
        symbols=("SPY", "QQQ"),
        sessions=sessions,
        injected_effect_bps=effect,
        seed=21,
    )
    observations = first_to_last_half_hour_observations(candles, timeframe="30m")
    metrics = factor_metrics(observations, effective_trials=1, cost_model=COST_MODEL)
    return candles, observations, metrics


def test_required_sessions_scales_with_the_square_of_the_noise_ratio():
    assert required_sessions_for_power(effect_bps=10.0, session_dispersion_bps=20.0) == 32
    # Halving the effect quadruples the sessions needed.
    assert required_sessions_for_power(effect_bps=5.0, session_dispersion_bps=20.0) == 126
    assert required_sessions_for_power(effect_bps=0.0, session_dispersion_bps=20.0) is None
    assert required_sessions_for_power(effect_bps=10.0, session_dispersion_bps=0.0) is None


def test_report_states_what_the_sample_would_have_needed():
    _candles, observations, metrics = observations_and_metrics()

    report = power_and_stability_report(
        observations,
        evidence_quality=metrics["evidence_quality"],
        net_evidence_quality=metrics["net_evidence_quality"],
    )
    power = report["power"]

    assert power["observed_sessions"] > 0
    # Reported for context, but not the standard a null is judged against.
    assert power["sessions_required_for_the_observed_effect"] > 0
    assert power["minimum_detectable_effect_bps"] is not None
    assert power["adequately_powered"] is True
    assert power["null_result_is_interpretable"] is True


def test_a_predeclared_requirement_decides_whether_a_null_is_interpretable():
    _candles, observations, metrics = observations_and_metrics()

    met = power_and_stability_report(
        observations,
        evidence_quality=metrics["evidence_quality"],
        net_evidence_quality=metrics["net_evidence_quality"],
        required_event_count=100,
        required_sessions=50,
    )
    unmet = power_and_stability_report(
        observations,
        evidence_quality=metrics["evidence_quality"],
        net_evidence_quality=metrics["net_evidence_quality"],
        required_event_count=10_000_000,
        required_sessions=50,
    )

    assert met["power"]["null_result_is_interpretable"] is True
    assert met["power"]["meets_required_event_count"] is True
    assert unmet["power"]["null_result_is_interpretable"] is False
    assert unmet["power"]["meets_required_event_count"] is False


def test_a_tiny_observed_effect_does_not_make_a_met_requirement_underpowered():
    # The circular failure: an effect near zero implies a near-infinite
    # requirement, so a real null could never be retired. The predeclared
    # count must override that.
    _candles, observations, metrics = observations_and_metrics(
        sessions=400, effect=0.01
    )

    report = power_and_stability_report(
        observations,
        evidence_quality=metrics["evidence_quality"],
        net_evidence_quality=metrics["net_evidence_quality"],
        required_event_count=100,
        required_sessions=50,
    )

    assert report["power"]["sessions_required_for_the_observed_effect"] > 10_000
    assert report["power"]["null_result_is_interpretable"] is True


def test_without_a_predeclared_requirement_the_observed_estimate_is_the_fallback():
    _candles, observations, metrics = observations_and_metrics(
        sessions=60, effect=0.4
    )

    report = power_and_stability_report(
        observations,
        evidence_quality=metrics["evidence_quality"],
        net_evidence_quality=metrics["net_evidence_quality"],
    )

    assert report["power"]["required_event_count"] is None
    assert report["power"]["adequately_powered"] is False
    assert report["power"]["null_result_is_interpretable"] is False


def test_quarterly_and_annual_subperiods_are_reported_with_stability():
    _candles, observations, metrics = observations_and_metrics()

    report = power_and_stability_report(
        observations,
        evidence_quality=metrics["evidence_quality"],
        net_evidence_quality=metrics["net_evidence_quality"],
    )
    quarterly = report["subperiods"]["quarterly"]

    assert len(quarterly) >= 4
    assert all("mean_return_bps" in value for value in quarterly.values())
    assert report["subperiods"]["quarterly_stability"]["stable"] is True
    assert report["subperiods"]["annual_stability"]["scored_subperiods"] >= 1


def test_a_thin_subperiod_is_flagged_rather_than_scored():
    _candles, observations, metrics = observations_and_metrics(sessions=15)

    report = power_and_stability_report(
        observations,
        evidence_quality=metrics["evidence_quality"],
        net_evidence_quality=metrics["net_evidence_quality"],
    )
    quarterly = report["subperiods"]["quarterly"]

    assert any(not value["sufficient_sessions"] for value in quarterly.values())


def test_regime_split_reports_market_direction_and_volatility():
    candles, observations, metrics = observations_and_metrics()
    context = benchmark_session_context(candles, timeframe="30m")

    report = power_and_stability_report(
        observations,
        evidence_quality=metrics["evidence_quality"],
        net_evidence_quality=metrics["net_evidence_quality"],
        benchmark_context=context,
    )
    regimes = report["regimes"]

    assert set(regimes["market_direction"]) == {"market_up", "market_down"}
    assert set(regimes["volatility"]) == {"high_volatility", "low_volatility"}
    assert regimes["benchmark_coverage"] == 1.0
    assert isinstance(regimes["regime_independent"], bool)


def test_trailing_volatility_never_reads_the_session_it_labels():
    candles = synthetic_intraday_candles(symbols=("SPY",), sessions=40, seed=4)

    context = benchmark_session_context(candles, timeframe="30m")
    first_session = sorted(context)[0]

    # The first session has no prior window, so it carries no volatility label
    # rather than borrowing one from its own or later returns.
    assert context[first_session]["trailing_volatility"] is None


def test_concentration_reports_sector_coverage_alongside_the_share():
    outcomes = [
        {"value": 0.001, "session_date": date(2025, 1, 2), "symbol": "AAPL"},
        {"value": 0.001, "session_date": date(2025, 1, 2), "symbol": "MSFT"},
        {"value": 0.001, "session_date": date(2025, 1, 3), "symbol": "JPM"},
        {"value": 0.001, "session_date": date(2025, 1, 3), "symbol": "SPY"},
    ]

    report = concentration_report(
        outcomes,
        sector_by_symbol={"AAPL": "Technology", "MSFT": "Technology", "JPM": "Financials"},
    )

    assert report["distinct_symbols"] == 4
    assert report["max_symbol_share"] == 0.25
    assert report["largest_sector"] == "Technology"
    assert report["max_sector_share"] == 0.5
    # SPY has no sector, and the coverage says so rather than the report
    # implying full sector knowledge.
    assert report["sector_coverage"] == 0.75


def test_effect_size_drift_flags_decay_and_sign_flips():
    decayed = effect_size_drift(
        {"gross_directional_edge_bps": 20.0}, {"gross_directional_edge_bps": 6.0}
    )
    flipped = effect_size_drift(
        {"gross_directional_edge_bps": 20.0}, {"gross_directional_edge_bps": -5.0}
    )
    missing = effect_size_drift(None, {"gross_directional_edge_bps": 6.0})

    assert decayed["retained_fraction"] == 0.3
    assert decayed["decay_beyond_published_norm"] is True
    assert decayed["sign_flipped"] is False
    assert flipped["sign_flipped"] is True
    assert missing["status"] == "not_comparable"


def test_report_surfaces_a_trial_count_larger_than_the_correction_applied():
    _candles, observations, metrics = observations_and_metrics()

    report = power_and_stability_report(
        observations,
        evidence_quality=metrics["evidence_quality"],
        net_evidence_quality=metrics["net_evidence_quality"],
        trials_recorded=99,
    )

    assert report["trials"]["trials_recorded_in_ledger"] == 99
    assert report["trials"]["ledger_covers_applied_trials"] is False
