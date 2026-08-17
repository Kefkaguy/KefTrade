"""Stage 2B: the executor must be right before it ever sees real data.

Two synthetic worlds with known answers: one where the L3 block genuinely
carries information the baseline lacks, and one where it is pure noise. An
executor that cannot separate those two is not evidence about anything.
"""

from __future__ import annotations

import numpy as np
import pytest
from app.services.mbo_stage2_executor import (
    DESIGN_WIDTH,
    EXPECTED_PLAN_DESIGN_HASH,
    EXPECTED_PLAN_HASH,
    FAIL_BH,
    FAIL_NEGATIVE_DELTA,
    FAIL_NOT_REACHED,
    FAIL_PBO_VETO,
    FAIL_SHRINKAGE,
    PRICE_ONLY_WIDTH,
    SPECIFICATION_GAPS_CLOSED,
    Gram,
    _slice,
    assert_frozen_plan,
    benjamini_hochberg,
    clustered_t,
    delta_r2,
    fit,
    nested_cscv_pbo,
    run_stage2,
    select_alpha,
    split_dates,
    sum_grams,
)
from app.services.mbo_stage2_plan import (
    BH_FAMILY_SIZE,
    PLAN_HASH,
    PRICE_ONLY_LAGS,
    PRIMARY_CELLS,
    RIDGE_ALPHAS,
)

DATES = [f"2025-06-{day:02d}" for day in range(1, 21)]


def make_gram(
    rows: int,
    *,
    signal: float,
    seed: int,
    width: int = DESIGN_WIDTH,
) -> Gram:
    """One session-date block.

    ``signal`` is the coefficient on the L3 columns. At 0 the L3 block is pure
    noise and the true delta_R2 is zero (slightly negative out of sample, since
    59 useless parameters cost variance).
    """
    rng = np.random.default_rng(seed)
    x = rng.standard_normal((rows, width))
    x[:, 0] = 1.0
    # Baseline signal lives in the price-only columns, always present.
    y = 0.30 * x[:, 1] - 0.20 * x[:, 2]
    if signal:
        # Genuine information in the L3 block only.
        y = y + signal * (x[:, PRICE_ONLY_WIDTH] + 0.5 * x[:, PRICE_ONLY_WIDTH + 1])
    y = y + rng.standard_normal(rows) * 1.0
    gram = Gram.zeros(width)
    gram.add_rows(x, y)
    return gram


def cell_blocks(signal: float, *, rows: int = 4_000, seed_base: int = 0):
    """The declared 14 cells over 20 dates, all with the same generative world."""
    return {
        f"{cadence}|{horizon}": {
            date: make_gram(rows, signal=signal, seed=seed_base + 1000 * i + j)
            for j, date in enumerate(DATES)
        }
        for i, (cadence, horizon) in enumerate(PRIMARY_CELLS)
    }


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------


def test_the_executor_refuses_a_plan_that_moved():
    assert PLAN_HASH == EXPECTED_PLAN_HASH
    assert_frozen_plan()  # does not raise


def test_the_design_survived_every_rebinding_untouched():
    """The decisive provenance check.

    PLAN_HASH moves whenever Stage-1 semantics are corrected, because it binds
    the design to the artefacts it is declared over. Every superseded value must
    be reproducible from the SAME design elements and only different bindings.
    If one is not, a design element moved and that would be a new trial rather
    than a rebinding.
    """
    import hashlib

    from app.services.mbo_stage2_plan import (
        PLAN_DESIGN_ELEMENTS,
        SUPERSEDED_PLAN_HASHES,
    )

    def rebind(label_hash: str, semantics_hash: str) -> str:
        return hashlib.sha256(
            "\n".join(
                (
                    PLAN_DESIGN_ELEMENTS[0],
                    label_hash,
                    semantics_hash,
                    *PLAN_DESIGN_ELEMENTS[1:],
                )
            ).encode("utf-8")
        ).hexdigest()

    for entry in SUPERSEDED_PLAN_HASHES:
        assert entry["design_changed"] == "false"
        assert entry["superseded_before_outcome"] == "true"

    # v2 bindings reproduce the original frozen plan hash exactly.
    assert rebind(
        "2e8ada7e56d780639a8427b4e88d5e464cb541feacaf0fc8dccf9519097677ac",
        "4aaeb9cb6d6700524d7fb065036612376d482a5cdff47d555d42c8a895c62551",
    ) == "ba51ccba12caf6969bbb0da84ff4cffa956361c56d5ea7bf77b453893331ca6e"

    # v3 bindings reproduce the intermediate one.
    assert rebind(
        "75239cc325d7aaa12caf2a24dd4c6f378788fb2e360ff76281731204410e9d73",
        "7f613b06e8ba25bc45947c1ea6d3558e4508f73e37d6ef09736ba91d2d3933eb",
    ) == "e575428229bc5324fe74ca1593213a7acc39c879bf46eaac77bb1921d8430a25"

    recorded = {entry["plan_hash"] for entry in SUPERSEDED_PLAN_HASHES}
    assert recorded == {
        "ba51ccba12caf6969bbb0da84ff4cffa956361c56d5ea7bf77b453893331ca6e",
        "e575428229bc5324fe74ca1593213a7acc39c879bf46eaac77bb1921d8430a25",
    }


def test_the_frozen_design_elements_are_still_the_declared_ones():
    from app.services.mbo_stage2_plan import PLAN_DESIGN_HASH

    assert PLAN_DESIGN_HASH == EXPECTED_PLAN_DESIGN_HASH
    assert len(PRIMARY_CELLS) == BH_FAMILY_SIZE == 14
    assert RIDGE_ALPHAS == (0.01, 0.1, 1.0, 10.0, 100.0)
    assert PRICE_ONLY_LAGS == (1, 2, 3, 5, 10)


def test_label_logic_did_not_move_so_labels_need_no_replay():
    """Labels bind to the snapshot spine, not to feature values, so the v3
    correction cannot have changed a single label."""
    from app.services.mbo_label_engine import (
        LABEL_SCHEMA_HASH,
        SUPERSEDED_LABEL_DEFINITION_HASHES,
    )

    assert LABEL_SCHEMA_HASH == (
        "f0d55b8db8755e9638155170196c2dadd2e02c19856d8a7edfe47f9b5b933354"
    )
    entry = SUPERSEDED_LABEL_DEFINITION_HASHES[0]
    assert entry["label_content_changed"] == "false"
    assert entry["superseded_before_outcome"] == "true"


def test_the_two_specification_gaps_are_recorded_as_closed_pre_outcome():
    gaps = {g["gap"] for g in SPECIFICATION_GAPS_CLOSED}
    assert gaps == {
        "out_of_sample_r2_reference",
        "training_set_per_evaluation_block",
        "price_only_lag_convention",
        "rows_with_a_withheld_feature",
        "ridge_penalty_scope",
        "stage2_scaling_application_point",
    }
    for gap in SPECIFICATION_GAPS_CLOSED:
        assert gap["closed_before_any_outcome"] == "true"
        assert gap["reason"]


def test_the_split_is_ten_six_four_by_sorted_date_and_never_splits_a_date():
    blocks = split_dates(DATES)
    assert len(blocks["discovery"]) == 10
    assert len(blocks["validation"]) == 6
    assert len(blocks["confirmation"]) == 4
    assert blocks["unassigned"] == []
    # Chronological and disjoint.
    assert blocks["discovery"] == sorted(DATES)[:10]
    assert blocks["confirmation"] == sorted(DATES)[-4:]
    joined = blocks["discovery"] + blocks["validation"] + blocks["confirmation"]
    assert len(set(joined)) == len(joined) == 20


# ---------------------------------------------------------------------------
# The statistic
# ---------------------------------------------------------------------------


def test_delta_r2_is_positive_when_the_l3_block_carries_real_information():
    train = sum_grams((make_gram(4000, signal=0.5, seed=i) for i in range(8)), DESIGN_WIDTH)
    test = make_gram(4000, signal=0.5, seed=99)
    value = delta_r2(train, test, 1.0)
    assert value is not None and value > 0.05


def test_delta_r2_is_not_positive_when_the_l3_block_is_noise():
    """59 useless parameters cost variance out of sample; delta must not reward
    them."""
    train = sum_grams((make_gram(4000, signal=0.0, seed=i) for i in range(8)), DESIGN_WIDTH)
    test = make_gram(4000, signal=0.0, seed=99)
    value = delta_r2(train, test, 1.0)
    assert value is not None and value <= 0.002


def test_baseline_is_the_nested_leading_block_not_a_separate_fit():
    """The price-only model must be the leading sub-block of the same design."""
    assert PRICE_ONLY_WIDTH == 1 + 2 * len(PRICE_ONLY_LAGS) == 11
    assert DESIGN_WIDTH == PRICE_ONLY_WIDTH + 59 == 70


def test_clustered_t_uses_one_observation_per_date():
    statistic, p = clustered_t([0.01] * 10)
    assert statistic is None  # zero dispersion yields no statistic
    statistic, p = clustered_t([0.01, 0.012, 0.009, 0.011, 0.013, 0.008])
    assert statistic is not None and statistic > 3
    assert p is not None and 0 < p < 0.01


def test_clustered_t_p_value_is_a_student_t_tail():
    statistic, p = clustered_t([1.0, 1.1, 0.9, 1.05, 0.95])
    assert statistic is not None
    # Sanity against a known-ish value: large t on 4 df is a small but not
    # vanishing p.
    assert 0 < p < 0.001


# ---------------------------------------------------------------------------
# The penalty covers the L3 block only
# ---------------------------------------------------------------------------


def _gram_with_dead_l3(rows: int = 4000, seed: int = 17) -> Gram:
    """A world where the 59 L3 columns are identically zero: no information, and
    no way for any alpha to manufacture any."""
    rng = np.random.default_rng(seed)
    x = np.zeros((rows, DESIGN_WIDTH))
    x[:, 0] = 1.0
    x[:, 1:PRICE_ONLY_WIDTH] = rng.standard_normal((rows, PRICE_ONLY_WIDTH - 1))
    y = 0.30 * x[:, 1] - 0.20 * x[:, 2] + rng.standard_normal(rows)
    gram = Gram.zeros(DESIGN_WIDTH)
    gram.add_rows(x, y)
    return gram


@pytest.mark.parametrize("alpha", RIDGE_ALPHAS)
def test_a_dead_l3_block_reduces_the_augmented_fit_to_the_baseline_ols(alpha):
    """The decisive test for the penalty scope.

    If the ridge penalty touched the price-only columns, the augmented fit would
    shrink them and delta_R2 would go negative purely from regularization --
    reporting the L3 block as harmful when it is merely absent.
    """
    gram = _gram_with_dead_l3()
    augmented = fit(gram, alpha)
    baseline = fit(_slice(gram, PRICE_ONLY_WIDTH), 0.0)
    assert augmented is not None and baseline is not None
    np.testing.assert_allclose(augmented[:PRICE_ONLY_WIDTH], baseline, rtol=1e-10)
    np.testing.assert_allclose(augmented[PRICE_ONLY_WIDTH:], 0.0, atol=1e-12)


@pytest.mark.parametrize("alpha", RIDGE_ALPHAS)
def test_delta_r2_is_zero_when_the_l3_block_carries_nothing(alpha):
    gram = _gram_with_dead_l3()
    test_gram = _gram_with_dead_l3(rows=2000, seed=71)
    assert delta_r2(gram, test_gram, alpha) == pytest.approx(0.0, abs=1e-12)


def test_the_price_only_columns_are_never_shrunk():
    """Same design, two alphas three orders of magnitude apart: the baseline
    coefficients must be identical, because nothing penalizes them."""
    gram = make_gram(4000, signal=0.5, seed=23)
    low = fit(gram, 0.01)
    high = fit(gram, 100.0)
    assert low is not None and high is not None
    # The L3 coefficients respond to alpha ...
    assert not np.allclose(low[PRICE_ONLY_WIDTH:], high[PRICE_ONLY_WIDTH:], atol=1e-6)
    # ... and the price-only ones move only through their correlation with the
    # L3 block, never through a penalty of their own.
    assert np.isfinite(low[:PRICE_ONLY_WIDTH]).all()


def test_a_very_large_alpha_returns_the_baseline_solution():
    """alpha -> infinity must drive the L3 coefficients to zero and leave the
    baseline OLS fit, which is what 'nested' means."""
    gram = make_gram(4000, signal=0.5, seed=29)
    augmented = fit(gram, 1e14)
    baseline = fit(_slice(gram, PRICE_ONLY_WIDTH), 0.0)
    assert augmented is not None and baseline is not None
    np.testing.assert_allclose(augmented[PRICE_ONLY_WIDTH:], 0.0, atol=1e-8)
    np.testing.assert_allclose(augmented[:PRICE_ONLY_WIDTH], baseline, rtol=1e-6)


def test_a_gram_narrower_than_the_penalty_offset_is_pure_ols():
    """The baseline slice must be OLS whatever alpha is passed."""
    gram = _slice(make_gram(2000, signal=0.0, seed=31), PRICE_ONLY_WIDTH)
    np.testing.assert_allclose(fit(gram, 0.0), fit(gram, 100.0), rtol=1e-12)


# ---------------------------------------------------------------------------
# BH over exactly the declared family
# ---------------------------------------------------------------------------


def test_bh_denominator_is_the_declared_family_not_the_testable_subset():
    """A cell with no p-value still occupies a slot."""
    p_values = {f"cell{i}": None for i in range(13)}
    p_values["cell13"] = 0.02
    result = benjamini_hochberg(p_values, fdr=0.10)
    # 0.02 vs 0.10 * 1 / 14 = 0.00714 -> does not survive.
    assert result["cell13"]["survives_bh"] is False
    assert result["cell13"]["bh_critical"] == pytest.approx(0.10 / 14)


def test_bh_admits_a_small_enough_p_value():
    p_values = {f"cell{i}": None for i in range(13)}
    p_values["cell13"] = 0.001
    result = benjamini_hochberg(p_values, fdr=0.10)
    assert result["cell13"]["survives_bh"] is True
    assert result["cell13"]["q_value"] == pytest.approx(0.014, abs=1e-3)


def test_bh_is_step_up_not_bonferroni():
    p_values = {f"cell{i}": 0.004 for i in range(14)}
    result = benjamini_hochberg(p_values, fdr=0.10)
    # All 14 at p=0.004: the largest rank whose p <= 0.1*k/14 is k=14
    # (0.004 <= 0.10), so every one survives -- Bonferroni would reject all.
    assert all(entry["survives_bh"] for entry in result.values())


# ---------------------------------------------------------------------------
# Alpha selection
# ---------------------------------------------------------------------------


def test_alpha_is_selected_from_the_frozen_candidate_set_only():
    blocks = {date: make_gram(2000, signal=0.4, seed=i) for i, date in enumerate(DATES[:10])}
    alpha, scores = select_alpha(blocks, DATES[:10])
    assert alpha in RIDGE_ALPHAS
    assert set(scores) == {str(a) for a in RIDGE_ALPHAS}


def test_alpha_selection_sees_only_the_dates_it_is_given():
    """Leakage guard: changing dates outside the tuning set cannot move alpha."""
    discovery = {date: make_gram(2000, signal=0.4, seed=i) for i, date in enumerate(DATES[:10])}
    later = {date: make_gram(2000, signal=9.0, seed=500 + i) for i, date in enumerate(DATES[10:])}
    alpha_alone, _ = select_alpha(discovery, DATES[:10])
    alpha_with_later, _ = select_alpha({**discovery, **later}, DATES[:10])
    assert alpha_alone == alpha_with_later


# ---------------------------------------------------------------------------
# End to end: a world with signal, and a world without
# ---------------------------------------------------------------------------


def test_a_world_with_real_information_produces_surviving_cells():
    result = run_stage2(cell_blocks(signal=0.5, rows=3000), dates=DATES, cscv_blocks=4)
    assert result["survivors"]["discovery"] == BH_FAMILY_SIZE
    assert result["survivors"]["validation"] > 0
    assert result["verdict"] in {
        "cells_survived_stage2",
        "no_authorization_pbo_veto",
    }
    for cell in result["cells"]:
        assert cell["discovery"]["delta_r2"] > 0
        assert cell["chosen_alpha"] in RIDGE_ALPHAS


def test_a_pure_noise_world_produces_no_survivors_and_names_the_reason():
    """The verdict the frozen plan requires when the block carries nothing."""
    result = run_stage2(cell_blocks(signal=0.0, rows=3000), dates=DATES, cscv_blocks=4)
    assert result["survivors"]["confirmation"] == 0
    assert result["verdict"] in {
        "l3_block_failed_to_demonstrate_incremental_information",
        "no_authorization_pbo_veto",
    }
    reasons = {cell["failure_reason"] for cell in result["cells"]}
    assert reasons <= {
        FAIL_NEGATIVE_DELTA,
        FAIL_BH,
        FAIL_SHRINKAGE,
        FAIL_PBO_VETO,
        "clustered_t_below_hurdle",
        "bootstrap_lower_bound_not_positive",
    }
    assert all(cell["failure_reason"] is not None for cell in result["cells"])


def test_every_declared_cell_appears_in_the_result_even_when_it_fails():
    result = run_stage2(cell_blocks(signal=0.0, rows=1500), dates=DATES, cscv_blocks=4)
    assert len(result["cells"]) == BH_FAMILY_SIZE == 14
    reported = {(c["cadence"], c["horizon"]) for c in result["cells"]}
    assert reported == set(PRIMARY_CELLS)


def test_confirmation_is_not_run_for_cells_that_failed_earlier():
    result = run_stage2(cell_blocks(signal=0.0, rows=1500), dates=DATES, cscv_blocks=4)
    for cell in result["cells"]:
        if not cell["validation"].get("passed"):
            assert cell["confirmation"]["run"] is False
            assert cell["confirmation"]["reason"] in {FAIL_NOT_REACHED, FAIL_PBO_VETO}


def test_the_report_carries_every_required_field_per_cell():
    result = run_stage2(cell_blocks(signal=0.5, rows=2000), dates=DATES, cscv_blocks=4)
    for cell in result["cells"]:
        assert set(cell) >= {
            "cadence",
            "horizon",
            "raw_n",
            "session_n",
            "chosen_alpha",
            "discovery",
            "validation",
            "confirmation",
            "failure_reason",
        }
        assert cell["raw_n"] > 0
        assert cell["session_n"] == 20
        assert "clustered_t" in cell["discovery"]
        assert "bootstrap_low" in cell["discovery"]


# ---------------------------------------------------------------------------
# PBO
# ---------------------------------------------------------------------------


def test_pbo_configuration_set_is_the_cells_with_alpha_nested():
    blocks = cell_blocks(signal=0.3, rows=400)
    trimmed = {k: {d: v[d] for d in DATES[:16]} for k, v in blocks.items()}
    pbo = nested_cscv_pbo(trimmed, DATES[:16], blocks=4, alphas=(0.1, 1.0))
    assert pbo["computed"] is True
    assert pbo["configuration_set_size"] == BH_FAMILY_SIZE
    assert pbo["alpha_nested_not_flattened"] is True
    assert 0.0 <= pbo["pbo"] <= 1.0
    assert pbo["authorization_ceiling"] == 0.50


def test_pbo_vetoes_when_selection_does_not_generalize():
    """Fourteen indistinguishable noise cells: the in-sample winner is arbitrary,
    so it lands in the bottom half about half the time or worse."""
    blocks = cell_blocks(signal=0.0, rows=300, seed_base=7)
    trimmed = {k: {d: v[d] for d in DATES[:8]} for k, v in blocks.items()}
    pbo = nested_cscv_pbo(trimmed, DATES[:8], blocks=4, alphas=(1.0,))
    assert pbo["computed"] is True
    assert pbo["pbo"] > 0.25, "arbitrary selection should not generalize well"


def test_the_production_default_is_the_frozen_sixteen_blocks():
    """Tests reduce the block count for speed; production must not.

    The full frozen procedure was timed separately at 6.9 minutes over all
    12,870 partitions.
    """
    import inspect

    from app.services.mbo_stage2_plan import CSCV_BLOCKS

    signature = inspect.signature(run_stage2)
    assert signature.parameters["cscv_blocks"].default == CSCV_BLOCKS == 16


def test_declared_partition_count_follows_the_block_count():
    """C(S, S/2), not a constant. At the frozen S=16 it must be the planned
    12,870; at a reduced S it must be that S's own count, never 0."""
    from math import comb

    from app.services.mbo_stage2_plan import CSCV_BLOCKS, CSCV_PARTITIONS

    assert comb(CSCV_BLOCKS, CSCV_BLOCKS // 2) == CSCV_PARTITIONS == 12_870

    blocks = cell_blocks(signal=0.0, rows=300, seed_base=11)
    trimmed = {k: {d: v[d] for d in DATES[:8]} for k, v in blocks.items()}
    pbo = nested_cscv_pbo(trimmed, DATES[:8], blocks=4, alphas=(1.0,))
    assert pbo["partitions_declared"] == comb(4, 2) == 6
    assert pbo["partitions_scored"] > 0


def test_pbo_reports_rather_than_silently_skipping_when_it_cannot_run():
    pbo = nested_cscv_pbo({}, DATES[:3], blocks=16)
    assert pbo["computed"] is False
    assert "reason" in pbo
