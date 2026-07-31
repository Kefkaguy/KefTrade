from datetime import date, timedelta

import pytest

from app.services.intraday_dataset_quality import (
    GAP_EXPERIMENT_OBSERVATION_TARGET,
    GAP_EXPERIMENT_SESSION_TARGET,
    factor_power_requirement,
)
from app.services.intraday_factor_diagnostics import (
    chronological_boundaries,
    interpret_factor_failure,
)


def sessions(count: int) -> list[date]:
    return [date(2024, 1, 1) + timedelta(days=index) for index in range(count)]


def test_splits_are_chronological_and_non_overlapping():
    boundaries = chronological_boundaries(sessions(100))

    assert boundaries["discovery_start"] < boundaries["discovery_end"]
    assert boundaries["discovery_end"] < boundaries["validation_start"]
    assert boundaries["validation_end"] < boundaries["confirmation_start"]
    assert boundaries["confirmation_end"] == sessions(100)[-1]


def test_an_embargo_session_belongs_to_no_split():
    boundaries = chronological_boundaries(sessions(100), embargo_sessions=1)

    assert (boundaries["validation_start"] - boundaries["discovery_end"]).days == 2
    assert (boundaries["confirmation_start"] - boundaries["validation_end"]).days == 2
    assert len(boundaries["embargoed_sessions"]) == 2


def test_a_longer_embargo_widens_both_gaps():
    boundaries = chronological_boundaries(sessions(200), embargo_sessions=4)

    assert (boundaries["validation_start"] - boundaries["discovery_end"]).days == 5
    assert (boundaries["confirmation_start"] - boundaries["validation_end"]).days == 5
    assert boundaries["embargo_sessions"] == 4
    assert len(boundaries["embargoed_sessions"]) == 8


def test_zero_embargo_still_produces_adjacent_but_disjoint_splits():
    boundaries = chronological_boundaries(sessions(100), embargo_sessions=0)

    assert (boundaries["validation_start"] - boundaries["discovery_end"]).days == 1
    assert boundaries["embargoed_sessions"] == []


def test_a_history_too_short_for_the_embargo_is_refused():
    with pytest.raises(ValueError):
        chronological_boundaries(sessions(12), embargo_sessions=5)


def measured(**overrides):
    result = {
        "status": "measured",
        "evidence_gate": {"passed": False, "failed": ["minimum_day_clustered_t"]},
        "factor_research_readiness": {"ready": True, "limitations": []},
        "power_and_stability": {
            "power": {
                "null_result_is_interpretable": True,
                "sessions_required_for_80pct_power": 100,
                "observed_sessions": 400,
            }
        },
    }
    result.update(overrides)
    return result


def test_a_powered_null_retires_the_hypothesis():
    verdict = interpret_factor_failure(measured())

    assert verdict["verdict"] == "interpretable_null"
    assert verdict["retire_hypothesis"] is True


def test_an_underpowered_null_retires_nothing():
    verdict = interpret_factor_failure(
        measured(
            power_and_stability={
                "power": {
                    "null_result_is_interpretable": False,
                    "sessions_required_for_80pct_power": 396,
                    "observed_sessions": 42,
                }
            }
        )
    )

    assert verdict["verdict"] == "underpowered_null"
    assert verdict["action"] == "gather_more_data"
    assert verdict["retire_hypothesis"] is False


def test_a_cost_failure_asks_for_retirement_or_a_new_horizon():
    verdict = interpret_factor_failure(
        measured(evidence_gate={"passed": False, "failed": ["clears_stressed_costs"]})
    )

    assert verdict["verdict"] == "fails_on_cost"
    assert verdict["action"] == "retire_or_redesign_holding_horizon"


def test_an_instability_failure_asks_for_a_new_regime_hypothesis():
    verdict = interpret_factor_failure(
        measured(evidence_gate={"passed": False, "failed": ["stable_subperiods"]})
    )

    assert verdict["verdict"] == "unstable"
    assert verdict["retire_hypothesis"] is True


def test_a_readiness_failure_asks_for_data_repair_not_retirement():
    verdict = interpret_factor_failure(
        measured(
            factor_research_readiness={
                "ready": False,
                "limitations": ["institutional_candle_ready"],
            }
        )
    )

    assert verdict["verdict"] == "data_not_ready"
    assert verdict["retire_hypothesis"] is False


def test_a_survivor_is_sent_to_locked_confirmation():
    verdict = interpret_factor_failure(measured(evidence_gate={"passed": True, "failed": []}))

    assert verdict["verdict"] == "survivor"
    assert verdict["action"] == "proceed_to_locked_confirmation"


def test_an_unmeasured_factor_is_not_treated_as_a_null():
    verdict = interpret_factor_failure({"status": "insufficient_evidence"})

    assert verdict["verdict"] == "not_measured"
    assert verdict["retire_hypothesis"] is False


def test_the_gap_experiment_targets_exceed_the_measured_minimum():
    # The plan's targets deliberately carry margin above the 396/707 estimate.
    assert GAP_EXPERIMENT_SESSION_TARGET > 396
    assert GAP_EXPERIMENT_OBSERVATION_TARGET > 707


def test_power_requirement_scales_with_the_assumed_effect():
    weak = factor_power_requirement(
        effect_bps=5.0, session_dispersion_bps=80.0, observations_per_session=1.8
    )
    strong = factor_power_requirement(
        effect_bps=20.0, session_dispersion_bps=80.0, observations_per_session=1.8
    )

    assert weak["sessions_required_for_80pct_power"] > strong["sessions_required_for_80pct_power"]
    assert strong["observations_required_for_80pct_power"] is not None
