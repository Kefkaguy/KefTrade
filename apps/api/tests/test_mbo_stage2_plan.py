"""Stage 2A v3: the final frozen executable plan.

Each test guards a rule that would be tempting to relax once results exist, or
a choice that must not be left open until relationships are visible.
"""

from __future__ import annotations

from app.services.mbo_feature_engine import FEATURE_VOCABULARY
from app.services.mbo_label_engine import CHANGE_HORIZONS, TIME_HORIZONS
from app.services.mbo_stage2_plan import (
    BH_FAMILY_SIZE,
    CSCV_BLOCKS,
    CSCV_PARTITIONS,
    EMBARGO_HORIZON,
    MODEL_SPEC,
    PLAN_HASH,
    PRIMARY_CELLS,
    PRIOR_EFFECTIVE_TRIALS,
    RIDGE_ALPHAS,
    SPLIT_DATE_BLOCKS,
    STAGE2_PLAN_VERSION,
    SUPERSEDED_PLAN_VERSIONS,
    TOTAL_SESSION_DATES,
    authorization_scope,
    multiplicity_accounting,
    statistical_plan,
)

TIME_NAMES = {h.name for h in TIME_HORIZONS}
CHANGE_NAMES = {h.name for h in CHANGE_HORIZONS}


def test_the_plan_declares_no_result():
    plan = statistical_plan()
    assert plan["contains_predictive_result"] is False
    assert plan["declared_before_any_outcome_viewed"] is True
    assert plan["stage2_plan_version"] == STAGE2_PLAN_VERSION == "tier1_stage2_plan_v3"


def test_both_earlier_plans_are_preserved_as_superseded_before_outcome():
    assert len(SUPERSEDED_PLAN_VERSIONS) == 2
    v1, v2 = SUPERSEDED_PLAN_VERSIONS
    assert v1["version"] == "tier1_stage2_plan_v1"
    assert v1["commit"].startswith("f3289c9")
    assert "59 sensors as" in v1["reason"]
    assert v2["version"] == "tier1_stage2_plan_v2"
    assert v2["commit"].startswith("a28b322")
    assert "undefined for the asynchronous" in v2["reason"]
    assert "14 cells plus 5 alpha values" in v2["reason"]
    assert all(
        entry["superseded_before_outcome"] == "true"
        for entry in SUPERSEDED_PLAN_VERSIONS
    )


# ---------------------------------------------------------------------------
# C. The 14-cell block-level grid
# ---------------------------------------------------------------------------


def test_the_primary_grid_is_fourteen_block_level_cells():
    assert len(PRIMARY_CELLS) == BH_FAMILY_SIZE == 14
    assert PRIMARY_CELLS == (
        ("1s", "1s"),
        ("1s", "5s"),
        ("1s", "10s"),
        ("1s", "30s"),
        ("1s", "60s"),
        ("5s", "1s"),
        ("5s", "5s"),
        ("5s", "10s"),
        ("5s", "30s"),
        ("5s", "60s"),
        ("50ev", "next_change"),
        ("50ev", "next_2_changes"),
        ("200ev", "next_change"),
        ("200ev", "next_2_changes"),
    )


def test_time_horizons_pair_with_time_cadences_and_change_with_event():
    """A mismatched clock tests the mismatch, not the book."""
    for cadence, horizon in PRIMARY_CELLS:
        if cadence in {"1s", "5s"}:
            assert horizon in TIME_NAMES, (cadence, horizon)
        else:
            assert horizon in CHANGE_NAMES, (cadence, horizon)


def test_no_individual_feature_ranking_in_the_primary_run():
    plan = statistical_plan()
    grid = plan["primary_grid"]
    assert grid["individual_feature_ranking_in_primary_run"] is False
    assert grid["form"].startswith("block-level")
    assert grid["features_in_block"] == len(FEATURE_VOCABULARY) == 59
    deferred = plan["multiplicity"]["deferred_stages_not_counted_here"]
    assert deferred["declared_now"] is False
    assert "only if the block-level" in deferred["feature_decomposition"]


def test_the_primary_hypothesis_is_about_the_block_not_a_sensor():
    hypothesis = statistical_plan()["primary_hypothesis"]
    assert "complete frozen Stage-1 L3 feature block" in hypothesis
    assert "beyond a price-only baseline" in hypothesis


# ---------------------------------------------------------------------------
# D. Whole session-date blocks
# ---------------------------------------------------------------------------


def test_splits_are_whole_date_blocks_of_ten_six_and_four():
    assert SPLIT_DATE_BLOCKS == (("discovery", 10), ("validation", 6), ("confirmation", 4))
    assert TOTAL_SESSION_DATES == 20
    splits = statistical_plan()["splits"]
    assert splits["all_symbols_move_together"] is True
    assert splits["date_never_split_across_sets"] is True
    assert splits["confirmation_is_single_use"] is True
    assert "not independent" in splits["reason"]


def test_the_embargo_is_justified_rather_than_merely_asserted():
    splits = statistical_plan()["splits"]
    assert EMBARGO_HORIZON == "60s"
    assert "already exceed the longest label" in splits["embargo"]


# ---------------------------------------------------------------------------
# E. The executable model is fully specified
# ---------------------------------------------------------------------------


def test_every_required_model_choice_is_declared():
    required = {
        "primary_target",
        "price_only_baseline",
        "l3_model",
        "scaling",
        "cross_sectional_residualization",
        "hyperparameter_rule",
        "out_of_sample_score",
        "inference",
        "bh_family",
        "pass_criteria",
        "pbo",
    }
    assert required <= set(MODEL_SPEC), required - set(MODEL_SPEC)


def test_the_target_and_price_only_inputs_are_exact():
    assert MODEL_SPEC["primary_target"]["column"] == "return_bps"
    assert "never imputed" in MODEL_SPEC["primary_target"]["rows_excluded"]
    baseline = MODEL_SPEC["price_only_baseline"]
    assert "[1, 2, 3, 5, 10]" in baseline["inputs"]
    assert "prior-only" in baseline["inputs"]
    assert baseline["estimator"].startswith("ordinary least squares")


def test_the_l3_model_is_nested_on_the_baseline_not_a_separate_fit():
    l3 = MODEL_SPEC["l3_model"]
    assert l3["estimator"] == "ridge regression"
    assert "PLUS all 59 frozen Stage-1 features" in l3["inputs"]
    assert "never a separate fit" in l3["form"]


def test_scaling_is_prior_only_and_partition_independent():
    scaling = MODEL_SPEC["scaling"]
    assert "expanding-window" in scaling["rule"]
    assert "strictly prior" in scaling["rule"]
    assert "at or after the observation" in scaling["forbidden"]
    # This property is what makes the nested CSCV affordable.
    assert scaling["partition_independent"] is True
    assert "additive" in scaling["consequence"]


def test_the_primary_target_is_the_raw_return_not_residualized():
    """A same-instant cross-sectional mean is undefined for 50ev/200ev."""
    target = MODEL_SPEC["primary_target"]
    assert target["column"] == "return_bps"
    assert target["residualized"] is False
    assert "asynchronous" in target["why_not_residualized"]
    assert "50ev and" in target["why_not_residualized"]
    assert MODEL_SPEC["cross_sectional_residualization"]["applied"] is False


def test_no_residualized_secondary_family_is_declared_now():
    target = MODEL_SPEC["primary_target"]
    assert target["residualized_secondary_family_declared"] is False
    assert "separately and counted separately" in target["note_on_later_declaration"]
    deferred = statistical_plan()["multiplicity"]["deferred_stages_not_counted_here"]
    assert "not declared" in deferred["residualized_target_family"]


def test_the_hyperparameter_rule_confines_chronological_tuning_to_discovery():
    rule = MODEL_SPEC["hyperparameter_rule"]
    assert rule["parameter"] == "ridge alpha"
    assert rule["candidates"] == list(RIDGE_ALPHAS) == [0.01, 0.1, 1.0, 10.0, 100.0]
    assert "DISCOVERY block only" in rule["chronological_path"]
    assert "frozen and reused unchanged" in rule["chronological_path"]
    assert "No validation or confirmation date" in rule["leakage_rule"]
    assert "re-tuning on validation or confirmation" in rule["forbidden"]


def test_the_score_is_the_increment_not_the_level():
    score = MODEL_SPEC["out_of_sample_score"]
    assert "raw return_bps" in score["primary"]
    assert score["incremental_statistic"] == (
        "delta_R2 = R2(l3_model) - R2(price_only_baseline)"
    )
    assert "is not the test" in score["reported"]


def test_inference_is_clustered_by_session_date_with_a_block_bootstrap():
    inference = MODEL_SPEC["inference"]
    assert "session-clustered t" in inference["test_statistic"]
    assert "one observation per session date" in inference["test_statistic"]
    assert "degrees of freedom" in inference["test_statistic"]
    bootstrap = inference["block_bootstrap"]
    assert bootstrap["unit"].startswith("whole session dates")
    assert bootstrap["resamples"] == 2000
    assert bootstrap["statistic"] == "delta_R2"


def test_pass_criteria_are_declared_for_all_three_blocks():
    criteria = MODEL_SPEC["pass_criteria"]
    assert "t >= 3.0" in criteria["discovery"]
    assert "bootstrap lower bound above 0" in criteria["discovery"]
    assert "survives BH" in criteria["validation"]
    assert "half the discovery estimate" in criteria["validation"]
    assert "single use" in criteria["confirmation"]
    assert "No re-run" in criteria["confirmation"]
    # Monotonicity is declared, and declared not to gate the block-level test.
    assert criteria["monotonicity"]["minimum"] == 0.70
    assert "does not gate it" in criteria["monotonicity"]["applies_to"]


def test_pbo_is_nested_with_fourteen_configurations_not_nineteen():
    """Alpha is a hyperparameter inside a cell, not a rival configuration."""
    pbo = MODEL_SPEC["pbo"]
    assert pbo["method"].startswith("nested CSCV")
    assert pbo["configuration_count"] == BH_FAMILY_SIZE == 14
    assert pbo["alpha_is_nested_not_flattened"] is True
    assert "not a configuration competing with cells" in pbo["configuration_set"]
    assert "5 ridge-alpha candidates" not in pbo["configuration_set"]


def test_pbo_implementation_tunes_alpha_inside_each_partition():
    pbo = MODEL_SPEC["pbo"]
    implementation = pbo["implementation"]
    assert f"S={CSCV_BLOCKS}" in implementation
    assert f"{CSCV_PARTITIONS:,}" in implementation
    assert "ONLY that partition" in implementation
    assert "leave-one-block-out" in implementation
    assert "rank among the 14" in implementation
    assert "bottom half out of sample" in implementation
    assert pbo["performance_metric"] == "delta_R2 of the raw return_bps target"
    assert pbo["authorization_ceiling"] == 0.50
    assert "authorizes no strategy" in pbo["rule"]


def test_pbo_feasibility_was_measured_before_outcomes_not_assumed():
    """The declared alternative was removal, never a flattened substitute."""
    feasibility = MODEL_SPEC["pbo"]["feasibility"]
    assert feasibility["assessed_before_outcomes"] is True
    assert feasibility["feasible"] is True
    assert feasibility["total_solves"] == CSCV_PARTITIONS * 14 * (5 * 8 + 5 + 1)
    assert feasibility["measured_single_core_minutes"] < 60
    assert "additive" in feasibility["why"]
    assert "removed as an authorization statistic" in feasibility["if_infeasible"]
    assert "flattened, invalid form" in feasibility["if_infeasible"]


# ---------------------------------------------------------------------------
# F. Lifetime exposure is not the BH family
# ---------------------------------------------------------------------------


def test_bh_family_is_this_run_and_lifetime_exposure_is_separate():
    accounting = multiplicity_accounting()
    assert accounting["bh_family"]["size"] == 14
    assert accounting["bh_family"]["false_discovery_rate"] == 0.10

    ledger = accounting["lifetime_exposure_ledger"]
    assert ledger["prior_effective_trials"] == PRIOR_EFFECTIVE_TRIALS == 508
    assert ledger["added_this_stage"] == 14
    assert ledger["lifetime_effective_trials"] == 522
    assert ledger["resets_for_new_dataset_family"] is False
    assert "not the BH denominator" in ledger["description"]


def test_alpha_is_absorbed_into_pbo_rather_than_counted_as_configurations():
    looks = multiplicity_accounting()["hyperparameter_looks"]
    assert looks["ridge_alpha_candidates"] == 5
    assert looks["counted_as_pbo_configurations"] is False
    assert looks["counted_in_bh_family"] is False
    assert "nested inside each cell" in looks["treated_as"]
    assert "absorbed into the PBO estimate" in looks["handled_by"]


# ---------------------------------------------------------------------------
# Authorization scope
# ---------------------------------------------------------------------------


def test_the_confirmation_block_is_internal_and_single_use():
    scope = authorization_scope()
    assert scope["confirmation_block"]["session_dates"] == 4
    assert scope["confirmation_block"]["single_use"] is True
    assert scope["confirmation_block"]["is_an_external_sample"] is False


def test_passing_stage_two_authorizes_stage_three_and_an_external_sample_only():
    authorized = " ".join(authorization_scope()["passing_stage2_authorizes"])
    assert "Stage-3 economic, cost and latency testing" in authorized
    assert "completely untouched external confirmation sample" in authorized


def test_passing_stage_two_does_not_authorize_real_money():
    scope = authorization_scope()
    denied = " ".join(scope["passing_stage2_does_not_authorize"])
    assert "real-money deployment" in denied
    assert "live capital" in denied
    assert "external validation" in denied
    assert "never touched" in scope["reason"]


def test_the_prohibitions_close_the_obvious_escape_hatches():
    prohibited = " ".join(statistical_plan()["prohibited"])
    for phrase in (
        "horizon substitution",
        "nearest-horizon",
        "moving a date between blocks",
        "re-tuning ridge alpha",
        "validation or confirmation date enter an alpha choice",
        "flattening alpha into the PBO configuration set",
        "individual-feature ranking",
        "residualizing the primary target",
        "threshold selection",
        "re-running confirmation",
        "external sample",
        "real money",
    ):
        assert phrase in prohibited, phrase


def test_the_plan_hash_binds_the_labels_features_cells_and_model():
    plan = statistical_plan()
    assert len(PLAN_HASH) == 64
    assert plan["label_definition_hash"]
    assert plan["feature_semantics_hash"]


def test_stage_two_pre_authorizes_nothing_economically():
    gate = statistical_plan()["economic_gate"]
    assert gate["minimum_tradeable_net_bps"] == 5.0
    assert gate["required_t_statistic"] == 3.0
    assert "pre-authorizes nothing" in gate["note"]
