import pytest

from app.services.intraday_factor_diagnostics import FACTOR_SPECS
from app.services.intraday_hypotheses import (
    GAP_EXPERIMENT_KEY,
    REQUIRED_FIELDS,
    Hypothesis,
    assert_not_retired,
    gap_experiment_hypotheses,
)


class Result:
    def __init__(self, rows=None, row=None):
        self.rows = rows or []
        self.row = row

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.row


class RetirementConn:
    def __init__(self, retired=()):
        self.retired = list(retired)

    def execute(self, query, params=None):
        return Result(
            rows=[
                {"factor_key": key, "spec_hash": spec_hash}
                for key, spec_hash in self.retired
            ]
        )


def valid_fields(**overrides):
    fields = {
        "key": "test",
        "factor_key": "gap_down_acceptance_continuation",
        "title": "Test",
        "forced_participant": "margin-driven sellers",
        "why_they_cannot_wait": "risk limits are evaluated at the open",
        "how_the_flow_appears_in_data": "gap down with elevated 10:00 participation",
        "signal_timestamp": "close of the 10:00 ET bar",
        "decision_timestamp": "10:30 ET",
        "executable_entry_timestamp": "open of the 10:30 ET bar",
        "exit_horizon": "1 bar",
        "expected_direction": "short",
        "universe": "point-in-time liquid US equities",
        "cost_model": "stressed p90 SIP round trip",
        "required_event_count": 850,
        "invalidation_conditions": ("net edge not positive",),
        "success_criteria": ("t >= 3",),
    }
    fields.update(overrides)
    return fields


def test_the_bounded_experiment_is_exactly_six_tests():
    hypotheses = gap_experiment_hypotheses()

    assert len(hypotheses) == 6
    assert len({item.key for item in hypotheses}) == 6
    assert {item.horizon_bars for item in hypotheses} == {1, 2, 4}
    assert all(item.key.startswith(GAP_EXPERIMENT_KEY) for item in hypotheses)


def test_every_experiment_hypothesis_maps_to_a_registered_factor():
    for hypothesis in gap_experiment_hypotheses():
        assert hypothesis.factor_key in FACTOR_SPECS, hypothesis.factor_key
        assert FACTOR_SPECS[hypothesis.factor_key].factor_type == "directional_event"


def test_acceptance_is_short_and_absorption_is_long():
    by_key = {item.key: item for item in gap_experiment_hypotheses()}

    for horizon in (1, 2, 4):
        assert by_key[f"{GAP_EXPERIMENT_KEY}:acceptance:{horizon}bar"].expected_direction == "short"
        assert by_key[f"{GAP_EXPERIMENT_KEY}:absorption:{horizon}bar"].expected_direction == "long"


def test_the_six_tests_share_one_fixed_parameter_set():
    parameter_sets = {item.parameters for item in gap_experiment_hypotheses()}

    # No threshold search: every test uses identical thresholds and differs
    # only in the predeclared holding horizon.
    assert len(parameter_sets) == 1


def test_every_required_field_is_enforced():
    for name in REQUIRED_FIELDS:
        supplied = valid_fields()[name]
        blank = "" if isinstance(supplied, str) else 0 if isinstance(supplied, int) else ()
        with pytest.raises(ValueError) as error:
            Hypothesis(**valid_fields(**{name: blank}))
        assert name in str(error.value)


def test_an_unknown_direction_is_refused():
    with pytest.raises(ValueError):
        Hypothesis(**valid_fields(expected_direction="sideways"))


def test_changing_any_parameter_changes_the_hypothesis_hash():
    base = Hypothesis(**valid_fields(parameters=(("minimum_gap_fraction", 0.003),)))
    tweaked = Hypothesis(**valid_fields(parameters=(("minimum_gap_fraction", 0.004),)))
    rehoused = Hypothesis(**valid_fields(exit_horizon="4 bars"))

    assert base.hypothesis_hash() != tweaked.hypothesis_hash()
    assert base.hypothesis_hash() != rehoused.hypothesis_hash()
    assert base.hypothesis_hash() == Hypothesis(
        **valid_fields(parameters=(("minimum_gap_fraction", 0.003),))
    ).hypothesis_hash()


def test_horizon_variants_are_distinct_registered_factors():
    keys = {item.factor_key for item in gap_experiment_hypotheses()}

    # Each horizon is its own trial in the ledger, not a knob on one trial.
    assert "gap_down_acceptance_continuation" in keys
    assert "gap_down_acceptance_continuation_2bar" in keys
    assert "gap_down_acceptance_continuation_4bar" in keys


def test_a_retired_factor_version_cannot_be_re_run():
    conn = RetirementConn(retired=[("gap_down_absorption_reversal", "hash-a")])

    with pytest.raises(ValueError) as error:
        assert_not_retired(
            conn,
            timeframe="30m",
            factor_keys=["gap_down_absorption_reversal"],
            spec_hash="hash-a",
        )

    assert "permanently retired" in str(error.value)


def test_a_different_specification_is_not_blocked_by_a_retirement():
    conn = RetirementConn(retired=[("gap_down_absorption_reversal", "hash-a")])

    assert_not_retired(
        conn,
        timeframe="30m",
        factor_keys=["gap_down_absorption_reversal"],
        spec_hash="hash-b",
    )
