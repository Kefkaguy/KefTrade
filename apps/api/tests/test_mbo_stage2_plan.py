"""Stage 2A: the frozen statistical plan and its multiplicity accounting.

These tests exist to make the plan hard to weaken quietly. Every one of them
guards a rule that would be tempting to relax once results exist.
"""

from __future__ import annotations

from app.services.mbo_feature_engine import CADENCES, FEATURE_VOCABULARY
from app.services.mbo_label_engine import HORIZON_NAMES
from app.services.mbo_stage2_plan import (
    BH_FALSE_DISCOVERY_RATE,
    EMBARGO_HORIZON,
    MONOTONICITY_MINIMUM,
    PBO_AUTHORIZATION_CEILING,
    PLAN_HASH,
    PRIOR_EFFECTIVE_TRIALS,
    SPLIT_FRACTIONS,
    declared_cell_count,
    statistical_plan,
)


def test_the_plan_declares_no_result():
    plan = statistical_plan()
    assert plan["contains_predictive_result"] is False
    assert plan["declared_before_any_outcome_viewed"] is True


def test_the_grid_is_the_full_frozen_vocabulary_not_a_preselection():
    """Screening features now would be arbitrary at best, and outcome-informed
    at worst."""
    counts = declared_cell_count()
    assert counts["features"] == len(FEATURE_VOCABULARY) == 59
    assert counts["cadences"] == len(CADENCES) == 4
    assert counts["horizons"] == len(HORIZON_NAMES) == 7


def test_the_declared_cell_count_is_exact():
    counts = declared_cell_count()
    assert counts["feature_cells"] == 59 * 1 * 4 * 7 == 1_652
    assert counts["incremental_information_tests"] == 4 * 7 == 28
    assert counts["declared_trials_this_stage"] == 1_680


def test_the_multiplicity_ledger_does_not_reset_for_a_new_dataset_family():
    """Tier-1 is better input to the same question, not a new question."""
    counts = declared_cell_count()
    assert counts["ledger_resets"] is False
    assert counts["prior_effective_trials"] == PRIOR_EFFECTIVE_TRIALS == 508
    assert counts["cumulative_effective_trials"] == 508 + 1_680 == 2_188

    ledger = statistical_plan()["multiplicity_ledger"]
    assert ledger["resets_for_new_dataset_family"] is False
    assert ledger["cumulative_effective_trials"] == 2_188
    assert len(ledger["prior_exposure_sources"]) >= 5


def test_splits_are_chronological_fifty_thirty_twenty_with_an_embargo():
    splits = statistical_plan()["splits"]
    assert splits["kind"] == "chronological"
    assert SPLIT_FRACTIONS == (0.50, 0.30, 0.20)
    assert sum(SPLIT_FRACTIONS) == 1.0
    assert splits["fractions"] == {
        "discovery": 0.50,
        "validation": 0.30,
        "confirmation": 0.20,
    }
    assert splits["confirmation_is_single_use"] is True
    assert EMBARGO_HORIZON == "60s", "the embargo must cover the longest label"
    assert "embargo" in splits


def test_only_prior_only_transformations_are_permitted():
    transformations = statistical_plan()["transformations"]
    assert transformations["allowed"] == "expanding / prior-only only"
    assert "full-sample" in transformations["forbidden"]


def test_a_price_only_baseline_is_required_and_the_increment_is_what_is_reported():
    plan = statistical_plan()
    assert plan["baseline"]["name"] == "price_only"
    assert "bid-ask bounce" in plan["baseline"]["purpose"]
    assert plan["incremental_test"]["reported"].startswith("increment")
    assert "level" in plan["incremental_test"]["not_reported_alone"]


def test_inference_clusters_and_bootstraps_by_session():
    inference = statistical_plan()["inference"]
    assert "session" in inference["clustering"]
    assert "symbol" in inference["clustering"]
    assert "effective N" in inference["clustering"]
    assert inference["block_bootstrap"]["unit"] == "symbol-day blocks"
    assert inference["block_bootstrap"]["resamples"] >= 1_000


def test_multiplicity_is_applied_across_every_declared_cell_not_the_reported_subset():
    multiplicity = statistical_plan()["inference"]["multiplicity"]
    assert multiplicity["method"] == "Benjamini-Hochberg"
    assert multiplicity["false_discovery_rate"] == BH_FALSE_DISCOVERY_RATE
    assert "not the reported subset" in multiplicity["applied_across"]


def test_monotonicity_and_pbo_thresholds_are_the_declared_ones():
    inference = statistical_plan()["inference"]
    assert MONOTONICITY_MINIMUM == 0.70
    assert inference["monotonicity"]["minimum"] == 0.70
    assert PBO_AUTHORIZATION_CEILING == 0.50
    assert inference["overfitting"]["authorization_ceiling"] == 0.50
    assert "authorizes no strategy" in inference["overfitting"]["rule"]


def test_the_prohibitions_name_horizon_shopping_explicitly():
    prohibited = statistical_plan()["prohibited"]
    joined = " ".join(prohibited)
    assert "horizon substitution" in joined
    assert "nearest-horizon" in joined
    assert "re-splitting" in joined
    assert "threshold selection" in joined


def test_the_plan_hash_binds_the_label_definitions_and_the_feature_semantics():
    """A plan that silently pairs with different labels or features is a
    different plan."""
    plan = statistical_plan()
    assert len(PLAN_HASH) == 64
    assert plan["label_definition_hash"]
    assert plan["feature_semantics_hash"]


def test_stage_two_pre_authorizes_nothing_economically():
    gate = statistical_plan()["economic_gate"]
    assert gate["minimum_tradeable_net_bps"] == 5.0
    assert gate["required_t_statistic"] == 3.0
    assert "does not pre-authorize" in gate["note"]
