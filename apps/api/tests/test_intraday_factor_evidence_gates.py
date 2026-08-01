from datetime import UTC
from decimal import Decimal

import app.services.intraday_factor_diagnostics as diagnostics
from app.services.intraday_factor_diagnostics import (
    FACTOR_SPECS,
    evaluate_factor_discovery,
    first_to_last_half_hour_observations,
    overnight_gap_acceptance_absorption_observations,
)
from app.services.intraday_session_calendar import NEW_YORK, bar_slot

from tests.test_intraday_factor_diagnostics import market_candles

COST_MODEL = {
    "observed_round_trip_bps": 2.0,
    "stressed_round_trip_bps": 4.0,
    "conservative_round_trip_bps": 30.0,
}
STABLE_POWER = {"subperiods": {"quarterly_stability": {"stable": True}}}


def extended_hours_bar(row, *, slot: str, close: float):
    hour, minute = (int(part) for part in slot.split(":"))
    timestamp = row["timestamp"].astimezone(NEW_YORK).replace(hour=hour, minute=minute)
    return {
        **row,
        "timestamp": timestamp.astimezone(UTC),
        "open": Decimal(str(close)),
        "high": Decimal(str(close * 1.001)),
        "low": Decimal(str(close * 0.999)),
        "close": Decimal(str(close)),
    }


def contaminate(rows, *, slots):
    output = list(rows)
    for row in rows:
        if bar_slot(row["timestamp"]) == "09:30":
            for slot, close in slots:
                output.append(extended_hours_bar(row, slot=slot, close=close))
    return output


def test_first_to_last_ignores_premarket_and_post_close_bars():
    clean = market_candles("SPY", sessions=12)
    contaminated = contaminate(clean, slots=(("09:00", 1.0), ("16:30", 999.0)))

    baseline = first_to_last_half_hour_observations({"SPY": clean}, timeframe="30m")
    polluted = first_to_last_half_hour_observations(
        {"SPY": contaminated}, timeframe="30m"
    )

    assert baseline
    assert len(baseline) == len(polluted)
    assert [row["score"] for row in baseline] == [row["score"] for row in polluted]
    assert [row["target_return"] for row in baseline] == [
        row["target_return"] for row in polluted
    ]


def test_gap_is_measured_from_the_regular_open_not_a_premarket_bar():
    clean = market_candles("AAPL", sessions=6)
    for row in clean:
        row["session_relative_volume"] = Decimal("2")
    contaminated = contaminate(clean, slots=(("09:00", 50.0),))

    baseline = overnight_gap_acceptance_absorption_observations(
        {"AAPL": clean}, timeframe="30m"
    )
    polluted = overnight_gap_acceptance_absorption_observations(
        {"AAPL": contaminated}, timeframe="30m"
    )

    assert [row["gap_return"] for row in baseline] == [
        row["gap_return"] for row in polluted
    ]


def test_every_observation_carries_its_timing_provenance():
    observations = first_to_last_half_hour_observations(
        {"SPY": market_candles("SPY", sessions=12)}, timeframe="30m"
    )

    assert observations
    for row in observations:
        assert row["decision_timestamp"] <= row["entry_bar_timestamp"]
        assert row["entry_bar_timestamp"] <= row["exit_bar_timestamp"]
        assert row["signal_bar_timestamp"] < row["entry_bar_timestamp"]
        assert row["timestamp"] == row["entry_bar_timestamp"]


def gate_metrics(**overrides):
    metrics = {
        "rank_ic": 0.2,
        "mean_cross_sectional_rank_ic": 0.05,
        "net_stressed_edge_bps": 8.0,
        "net_top_minus_bottom_spread_bps": 4.0,
        "day_clustered_t_statistic": 3.5,
        "rank_ic_stability": {"stable": True},
        "evidence_quality": {
            "independent_evidence_ready": True,
            "selection_adjusted_signal": True,
        },
        "net_evidence_quality": {
            "selection_adjusted_signal": True,
            "block_bootstrap": {"confidence_interval_95": [2.0, 14.0]},
        },
    }
    metrics.update(overrides)
    return metrics


def apply_gate(factor_key, metrics, *, power=STABLE_POWER):
    return diagnostics.factor_evidence_gate(
        FACTOR_SPECS[factor_key],
        metrics,
        cost_clearance={"clears_stressed": True},
        q_value=0.01,
        power_report=power,
        dataset_ready=True,
        cost_ready=True,
    )


def test_a_directional_event_is_not_judged_on_rank_information_coefficient():
    gate = apply_gate(
        "gap_down_acceptance_continuation",
        gate_metrics(rank_ic=-0.4, mean_cross_sectional_rank_ic=None),
    )

    assert gate["factor_type"] == "directional_event"
    assert "positive_information_coefficient" not in gate["gates"]
    assert gate["passed"] is True


def test_a_directional_event_still_needs_a_positive_bootstrap_lower_bound():
    gate = apply_gate(
        "gap_down_acceptance_continuation",
        gate_metrics(
            net_evidence_quality={
                "selection_adjusted_signal": True,
                "block_bootstrap": {"confidence_interval_95": [-3.0, 14.0]},
            }
        ),
    )

    assert gate["passed"] is False
    assert "positive_net_bootstrap_lower_bound" in gate["failed"]


def test_a_directional_event_needs_stable_subperiods():
    gate = apply_gate(
        "gap_down_acceptance_continuation",
        gate_metrics(),
        power={"subperiods": {"quarterly_stability": {"stable": False}}},
    )

    assert gate["passed"] is False
    assert "stable_subperiods" in gate["failed"]


def test_a_directional_event_needs_a_positive_net_event_return():
    gate = apply_gate(
        "gap_down_acceptance_continuation", gate_metrics(net_stressed_edge_bps=-2.0)
    )

    assert gate["passed"] is False
    assert "positive_event_conditioned_net_return" in gate["failed"]


def test_a_continuous_factor_is_judged_on_its_information_coefficient():
    gate = apply_gate(
        "first_to_last_half_hour_market_momentum", gate_metrics(rank_ic=-0.1)
    )

    assert gate["factor_type"] == "continuous"
    assert gate["passed"] is False
    assert "positive_information_coefficient" in gate["failed"]


def test_a_continuous_factor_needs_stable_rank_performance():
    gate = apply_gate(
        "first_to_last_half_hour_market_momentum",
        gate_metrics(rank_ic_stability={"stable": False}),
    )

    assert gate["passed"] is False
    assert "stable_rank_performance" in gate["failed"]


def test_a_cross_sectional_factor_needs_an_executable_spread():
    gate = apply_gate(
        "cross_sectional_same_slot_continuation",
        gate_metrics(net_top_minus_bottom_spread_bps=-1.0),
    )

    assert gate["factor_type"] == "cross_sectional"
    assert gate["passed"] is False
    assert "executable_long_short_spread" in gate["failed"]


def test_a_cross_sectional_factor_needs_a_cross_sectional_information_coefficient():
    gate = apply_gate(
        "cross_sectional_same_slot_continuation",
        gate_metrics(rank_ic=0.9, mean_cross_sectional_rank_ic=-0.02),
    )

    assert gate["passed"] is False
    assert "positive_cross_sectional_information_coefficient" in gate["failed"]


def test_the_universal_integrity_gates_apply_to_every_factor_type():
    for key in (
        "first_to_last_half_hour_market_momentum",
        "gap_down_acceptance_continuation",
        "cross_sectional_same_slot_continuation",
    ):
        gate = diagnostics.factor_evidence_gate(
            FACTOR_SPECS[key],
            gate_metrics(day_clustered_t_statistic=1.2),
            cost_clearance={"clears_stressed": False},
            q_value=0.5,
            power_report=STABLE_POWER,
            dataset_ready=False,
            cost_ready=True,
        )

        assert gate["passed"] is False, key
        assert "minimum_day_clustered_t" in gate["failed"]
        assert "clears_stressed_costs" in gate["failed"]
        assert "false_discovery_rate_controlled" in gate["failed"]
        assert "dataset_research_ready" in gate["failed"]


def test_a_run_without_a_certification_is_marked_as_such():
    result = evaluate_factor_discovery(
        {"SPY": market_candles("SPY", sessions=40)},
        timeframe="30m",
        factor_keys=["first_to_last_half_hour_market_momentum"],
        cost_model=COST_MODEL,
    )

    assert result["instrument_certification"]["certified"] is False
    assert result["instrument_certification"]["status"] == "not_supplied"
    assert result["session_calendar_audit"]["extended_hours_rows"] == 0


def test_the_ledger_trial_count_drives_the_correction_not_the_run_size():
    result = evaluate_factor_discovery(
        {"SPY": market_candles("SPY", sessions=40)},
        timeframe="30m",
        factor_keys=["first_to_last_half_hour_market_momentum"],
        cost_model=COST_MODEL,
        effective_trials=64,
        trial_ledger={"effective_trials": 64, "historical_trials": 63},
    )

    assert result["effective_trials"] == 64
    assert result["trial_ledger"]["historical_trials"] == 63
    validation = result["factors"]["first_to_last_half_hour_market_momentum"][
        "validation"
    ]
    assert validation["evidence_quality"]["effective_trials"] == 64


def test_discovery_reports_power_and_stability_for_every_measured_factor():
    result = evaluate_factor_discovery(
        {"SPY": market_candles("SPY", sessions=120)},
        timeframe="30m",
        factor_keys=["first_to_last_half_hour_market_momentum"],
        cost_model=COST_MODEL,
    )

    report = result["factors"]["first_to_last_half_hour_market_momentum"][
        "power_and_stability"
    ]

    assert "sessions_required_for_the_observed_effect" in report["power"]
    assert "required_event_count" in report["power"]
    assert "quarterly" in report["subperiods"]
    assert "concentration" in report
    assert "effect_size_drift" in report
    assert report["effect_size_drift"]["status"] == "measured"


def shift_sessions(rows, *, days):
    from datetime import timedelta

    return [
        {**row, "timestamp": row["timestamp"] + timedelta(days=days)}
        for row in rows
    ]


def test_a_gap_is_not_measured_across_a_membership_hole():
    from datetime import timedelta

    from app.services.intraday_session_calendar import bar_slot, session_date

    early = market_candles("AAPL", sessions=6)
    for row in early:
        row["session_relative_volume"] = Decimal("2")
    # The same symbol rejoining the universe months later. Without an
    # adjacency rule the first session back is compared against a close from
    # before the hole, producing an "overnight" gap spanning months.
    late = shift_sessions(early, days=200)

    joined = overnight_gap_acceptance_absorption_observations(
        {"AAPL": early + late}, timeframe="30m"
    )
    session_dates = {row["session_date"] for row in joined}
    first_after_hole = min(session_date(row["timestamp"]) for row in late)

    assert first_after_hole not in session_dates


def test_the_first_session_of_a_symbol_never_produces_a_gap():
    clean = market_candles("AAPL", sessions=4)
    for row in clean:
        row["session_relative_volume"] = Decimal("2")

    observations = overnight_gap_acceptance_absorption_observations(
        {"AAPL": clean}, timeframe="30m"
    )
    from app.services.intraday_session_calendar import ordered_regular_sessions

    first_session = ordered_regular_sessions(clean, timeframe="30m")[0][0]

    assert all(row["session_date"] != first_session for row in observations)


def test_first_to_last_also_refuses_a_stale_previous_close():
    from app.services.intraday_session_calendar import session_date

    early = market_candles("SPY", sessions=6)
    late = shift_sessions(early, days=200)

    observations = first_to_last_half_hour_observations(
        {"SPY": early + late}, timeframe="30m"
    )
    session_dates = {row["session_date"] for row in observations}
    first_after_hole = min(session_date(row["timestamp"]) for row in late)

    assert first_after_hole not in session_dates


def test_adjacent_sessions_across_a_weekend_still_count():
    from datetime import date as _date

    from app.services.intraday_session_calendar import is_consecutive_session

    # Friday to Monday is three calendar days and is a normal overnight.
    assert is_consecutive_session(_date(2025, 3, 7), _date(2025, 3, 10)) is True
    # A long weekend with a holiday is four.
    assert is_consecutive_session(_date(2025, 8, 29), _date(2025, 9, 2)) is True
    # A membership hole is not.
    assert is_consecutive_session(_date(2025, 3, 7), _date(2025, 9, 2)) is False
    assert is_consecutive_session(None, _date(2025, 3, 10)) is False
