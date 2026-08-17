"""Stage 2A v3: the final frozen, executable Stage-2 prediction plan.

Declared **before any feature-label relationship is visible**. Every choice that
could otherwise be made after seeing results is fixed here, exactly, including
the model, the scaling, the score, the test statistic, the pass criteria and the
overfitting procedure. An unspecified choice is a degree of freedom, and a
degree of freedom exercised after the fact is not a decision -- it is a result.

## What v3 corrected

**1. The primary target is the raw event-time return.** v2 residualized the
target against the equal-weighted cross-sectional mean of the eight symbols at
each instant. That rule is *undefined* for the event cadences: 1s and 5s grids
are cross-symbol aligned by construction, but 50ev and 200ev clocks advance on
each symbol's own event count and are asynchronous, so "the same instant across
eight symbols" does not exist there. A quorum rule cannot rescue a
same-instant definition that has no instants.

The dependence residualization was meant to control is already handled: whole
session-date splits keep all eight symbols on the same side of every boundary,
and inference is one observation per session date, so eight symbols never enter
as eight independent sessions. And the primary question is *absolute* future
price predictability beyond a price-only baseline, which is what the raw return
measures.

No residualized-target secondary family is declared. If one is wanted later it
must be declared separately and counted separately.

**2. PBO is nested, not a flattened configuration set.** v2 defined the CSCV
configuration set as "14 cells + 5 alpha values", which is wrong: alpha is a
hyperparameter *inside* each cell, not an independent configuration competing
with them. The configuration set is the **14 cells**. Alpha is selected inside
each partition's in-sample half, per cell, from the frozen candidate set.

## Lifetime exposure is not the BH family

* **Lifetime effective trials** -- everything this programme has spent against
  the same eventual decision. Carries the 508 floor and only grows.
* **The BH family** -- the 14 primary cells of this run.

Correcting 14 cells as though they were 522 would be as wrong as the reverse.

## What passing Stage 2 authorizes

The final four dates are an **internal** single-use confirmation gate. Clearing
every Stage-2 gate authorizes exactly two things: Stage-3 economic, cost and
latency testing, and the acquisition or use of a larger, completely untouched
external confirmation sample. It authorizes **no real-money deployment**.
"""

from __future__ import annotations

import hashlib
from math import comb
from typing import Any

from app.services.intraday_hypotheses import (
    MINIMUM_TRADEABLE_NET_BPS,
    REQUIRED_T_STATISTIC,
)
from app.services.mbo_feature_engine import (
    FEATURE_SEMANTICS_HASH,
    FEATURE_VOCABULARY,
    NORMALIZED_FEATURES,
)
from app.services.mbo_label_engine import (
    LABEL_DEFINITION_HASH,
    LABEL_ENGINE_VERSION,
)

STAGE2_PLAN_VERSION = "tier1_stage2_plan_v3"

SUPERSEDED_PLAN_VERSIONS: tuple[dict[str, str], ...] = (
    {
        "version": "tier1_stage2_plan_v1",
        "commit": "f3289c9701ea8c7d431d941a60b20b5cf447c548",
        "superseded_before_outcome": "true",
        "reason": (
            "declared a 1,652-cell individual-feature grid, treating 59 sensors as "
            "59 independent strategies; split by fraction rather than whole "
            "session-date blocks; left the executable model unspecified; and "
            "conflated lifetime exposure with the within-run BH family."
        ),
    },
    {
        "version": "tier1_stage2_plan_v2",
        "commit": "a28b322971c4e2f476069c302f9b6d915e67f7b0",
        "superseded_before_outcome": "true",
        "reason": (
            "residualized the primary target cross-sectionally at each instant, a "
            "rule undefined for the asynchronous 50ev/200ev cadences; and defined "
            "the CSCV configuration set as 14 cells plus 5 alpha values, treating a "
            "within-cell hyperparameter as an independent configuration."
        ),
    },
)

PRIOR_EFFECTIVE_TRIALS = 508
PRIOR_EXPOSURE_SOURCES: tuple[str, ...] = (
    "candle-only gap experiment (six predeclared factors, retired)",
    "order-flow / premarket / sector factor families",
    "news-reaction and sector lead-lag studies",
    "intraday alpha-map cell grid",
    "Stage 0 Alpaca L1 microstructure probe",
)

# ---------------------------------------------------------------------------
# The primary hypothesis grid: 14 block-level cells
# ---------------------------------------------------------------------------

PRIMARY_CELLS: tuple[tuple[str, str], ...] = (
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

BH_FAMILY_SIZE = len(PRIMARY_CELLS)

SPLIT_DATE_BLOCKS: tuple[tuple[str, int], ...] = (
    ("discovery", 10),
    ("validation", 6),
    ("confirmation", 4),
)
TOTAL_SESSION_DATES = sum(count for _, count in SPLIT_DATE_BLOCKS)
EMBARGO_HORIZON = "60s"

PRIMARY_TARGET = "return_bps"
PRICE_ONLY_LAGS: tuple[int, ...] = (1, 2, 3, 5, 10)
RIDGE_ALPHAS: tuple[float, ...] = (0.01, 0.1, 1.0, 10.0, 100.0)

# CSCV geometry.
CSCV_BLOCKS = 16
CSCV_IN_SAMPLE_BLOCKS = CSCV_BLOCKS // 2
CSCV_PARTITIONS = comb(CSCV_BLOCKS, CSCV_IN_SAMPLE_BLOCKS)
PBO_AUTHORIZATION_CEILING = 0.50
BH_FALSE_DISCOVERY_RATE = 0.10
BLOCK_BOOTSTRAP_RESAMPLES = 2000

# Design width: 5 lagged returns + 5 signs + 59 features + intercept.
DESIGN_WIDTH = 2 * len(PRICE_ONLY_LAGS) + len(FEATURE_VOCABULARY) + 1


MODEL_SPEC: dict[str, Any] = {
    "primary_target": {
        "column": PRIMARY_TARGET,
        "definition": (
            "the cell horizon's signed midpoint return in basis points, taken "
            "directly from the wide event-time label table, used only where that "
            "horizon's status is 'ok'"
        ),
        "residualized": False,
        "rows_excluded": "any row whose horizon status is not 'ok'; never imputed",
        "why_not_residualized": (
            "A same-instant cross-sectional mean is undefined for the 50ev and "
            "200ev cadences, whose clocks advance on each symbol's own event count "
            "and are therefore asynchronous across symbols; a quorum rule cannot "
            "rescue a definition that has no shared instants. The cross-symbol "
            "dependence it was meant to control is already handled by whole "
            "session-date splits and per-date inference. And the primary question "
            "is absolute future-price predictability beyond a price-only baseline, "
            "which the raw return measures directly."
        ),
        "residualized_secondary_family_declared": False,
        "note_on_later_declaration": (
            "If a residualized-target family is wanted later it must be declared "
            "separately and counted separately; it is not part of this run."
        ),
    },
    "price_only_baseline": {
        "inputs": (
            f"lagged own-cadence midpoint log-returns at lags {list(PRICE_ONLY_LAGS)} "
            "plus the sign of each, computed within the symbol-day and prior-only"
        ),
        "estimator": "ordinary least squares, no regularization, intercept fitted",
        "purpose": (
            "short-horizon midpoint changes mean-revert unaided; a book feature "
            "that only recovers bid-ask bounce has added nothing"
        ),
    },
    "l3_model": {
        "estimator": "ridge regression",
        "inputs": "the price-only lags PLUS all 59 frozen Stage-1 features",
        "form": (
            "nested -- the L3 model is the baseline's inputs augmented, never a "
            "separate fit"
        ),
        "design_width": DESIGN_WIDTH,
        "reason": (
            "ridge, because 59 correlated sensors under OLS is a variance problem, "
            "and the hypothesis is about the block rather than about which sensor "
            "wins"
        ),
    },
    "scaling": {
        "rule": (
            "per (symbol, cadence), expanding-window standardization using strictly "
            "prior observations within the symbol-day; withheld below 30 priors"
        ),
        "forbidden": "any statistic computed over data at or after the observation",
        "partition_independent": True,
        "consequence": (
            "Because standardization looks only within a symbol-day and only "
            "backwards, the design matrix does not depend on how dates are split. "
            "That is what makes per-date Gram matrices additive, and therefore what "
            "makes the nested CSCV below computationally feasible."
        ),
        "already_frozen_in_stage1": list(NORMALIZED_FEATURES),
    },
    "cross_sectional_residualization": {
        "applied": False,
        "reason": "see primary_target.why_not_residualized",
    },
    "hyperparameter_rule": {
        "parameter": "ridge alpha",
        "candidates": list(RIDGE_ALPHAS),
        "chronological_path": (
            "selected inside the DISCOVERY block only, by leave-one-date-out "
            "cross-validation over the 10 discovery dates, maximizing mean "
            "out-of-fold delta_R2; then frozen and reused unchanged for validation "
            "and confirmation"
        ),
        "leakage_rule": (
            "No validation or confirmation date may enter any alpha choice on the "
            "ordinary chronological path, directly or through a fold boundary."
        ),
        "inside_cscv": (
            "re-selected independently within each CSCV partition's in-sample half; "
            "see pbo.implementation"
        ),
        "forbidden": "re-tuning on validation or confirmation for any reason",
    },
    "out_of_sample_score": {
        "primary": "out-of-sample R^2 of the raw return_bps target",
        "incremental_statistic": "delta_R2 = R2(l3_model) - R2(price_only_baseline)",
        "reported": (
            "delta_R2 with its interval; the level of R2 is reported beside it but "
            "is not the test"
        ),
    },
    "inference": {
        "test_statistic": (
            "session-clustered t on the per-session-date delta_R2, one observation "
            "per session date, so 19.5 M rows cannot masquerade as 19.5 M degrees "
            "of freedom"
        ),
        "effective_n": "reported beside raw N for every cell",
        "block_bootstrap": {
            "unit": "whole session dates (all eight symbols together)",
            "resamples": BLOCK_BOOTSTRAP_RESAMPLES,
            "statistic": "delta_R2",
            "interval": "two-sided 95% percentile",
        },
    },
    "bh_family": {
        "members": "the 14 primary cells of this run",
        "size": BH_FAMILY_SIZE,
        "false_discovery_rate": BH_FALSE_DISCOVERY_RATE,
        "note": "lifetime exposure is tracked separately and is not the BH denominator",
    },
    "pass_criteria": {
        "discovery": (
            "delta_R2 > 0 with session-clustered t >= 3.0 and a bootstrap lower "
            "bound above 0; failure here ends the cell"
        ),
        "validation": (
            "same sign, delta_R2 > 0, session-clustered t >= 3.0, survives BH across "
            "the 14-cell family, and the point estimate is at least half the "
            "discovery estimate -- a validation estimate that collapses is a "
            "discovery artefact"
        ),
        "confirmation": (
            "single use, run once, only for cells that passed validation; same sign "
            "and delta_R2 > 0 with a bootstrap lower bound above 0. No re-run, no "
            "re-split, no second look"
        ),
        "monotonicity": {
            "minimum": 0.70,
            "applies_to": (
                "the later, separately counted feature-decomposition stage where "
                "ordinal bucketed feature-response testing applies; the block-level "
                "primary test is not ordinal and monotonicity does not gate it"
            ),
        },
    },
    "pbo": {
        "method": "nested CSCV (combinatorially symmetric cross-validation)",
        "configuration_set": (
            "the 14 primary cells. Alpha is a hyperparameter inside a cell, not a "
            "configuration competing with cells, so it does not enter the set."
        ),
        "configuration_count": BH_FAMILY_SIZE,
        "implementation": (
            f"Session dates are divided into S={CSCV_BLOCKS} contiguous blocks. For "
            f"each of the C({CSCV_BLOCKS},{CSCV_IN_SAMPLE_BLOCKS})="
            f"{CSCV_PARTITIONS:,} balanced partitions into an in-sample half and an "
            "out-of-sample half, and for each of the 14 cells independently: "
            "(1) select alpha using ONLY that partition's in-sample dates, from the "
            "frozen candidate set, by leave-one-block-out CV over the in-sample "
            "blocks; (2) fit the cell with that alpha on the full in-sample half; "
            "(3) score in-sample and out-of-sample delta_R2. Then select the cell "
            "with the best in-sample delta_R2 and record its rank among the 14 "
            "out-of-sample scores. PBO is the fraction of partitions whose "
            "in-sample-selected cell ranks in the bottom half out of sample."
        ),
        "alpha_is_nested_not_flattened": True,
        "performance_metric": "delta_R2 of the raw return_bps target",
        "authorization_ceiling": PBO_AUTHORIZATION_CEILING,
        "rule": (
            "PBO above 0.50 authorizes no strategy from the grid regardless of any "
            "individual cell's t-statistic. A grid always produces a best cell; its "
            "t says nothing about how many it beat."
        ),
        "feasibility": {
            "assessed_before_outcomes": True,
            "feasible": True,
            "why": (
                "Standardization is prior-only within symbol-day, so the design "
                "matrix is partition-independent and per-date Gram matrices "
                "(X'X, X'y, y'y, n) are additive. Any partition's fit is a sum of "
                "precomputed per-date blocks followed by one Cholesky solve, so no "
                "row-level data is revisited per partition."
            ),
            "precomputed_blocks": BH_FAMILY_SIZE * TOTAL_SESSION_DATES,
            "design_width": DESIGN_WIDTH,
            "solves_per_cell_partition": (
                len(RIDGE_ALPHAS) * CSCV_IN_SAMPLE_BLOCKS + len(RIDGE_ALPHAS) + 1
            ),
            "total_solves": (
                CSCV_PARTITIONS
                * BH_FAMILY_SIZE
                * (len(RIDGE_ALPHAS) * CSCV_IN_SAMPLE_BLOCKS + len(RIDGE_ALPHAS) + 1)
            ),
            # Corrected after building the executor. The 3.2 figure counted only
            # the Cholesky solves and omitted Gram assembly, which dominates:
            # 12,870 partitions x 14 cells x 8 blocks of 70x70 accumulation. The
            # figure below is measured end-to-end over all 12,870 partitions.
            "measured_single_core_minutes": 6.91,
            "superseded_estimate_single_core_minutes": 3.2,
            "resident_memory_mb": 11.1,
            "if_infeasible": (
                "PBO would have been removed as an authorization statistic before "
                "outcomes rather than computed in a flattened, invalid form. It was "
                "measured as feasible, so it stays."
            ),
        },
    },
}


def authorization_scope() -> dict[str, Any]:
    """Exactly what clearing every Stage-2 gate does and does not permit."""
    return {
        "confirmation_block": {
            "session_dates": 4,
            "kind": "internal single-use confirmation gate",
            "single_use": True,
            "is_an_external_sample": False,
        },
        "passing_stage2_authorizes": [
            "Stage-3 economic, cost and latency testing",
            (
                "acquisition or use of a larger, completely untouched external "
                "confirmation sample"
            ),
        ],
        "passing_stage2_does_not_authorize": [
            "real-money deployment",
            "any live capital allocation",
            "treating the 4-date internal gate as an external validation",
        ],
        "reason": (
            "Four internal dates drawn from the same frozen 160-symbol-day batch "
            "cannot establish out-of-sample behaviour on data the programme has "
            "never touched. They are the last internal check, not evidence about "
            "the world."
        ),
    }


def multiplicity_accounting() -> dict[str, Any]:
    return {
        "bh_family": {
            "description": "primary cells declared in this run; the BH denominator",
            "size": BH_FAMILY_SIZE,
            "false_discovery_rate": BH_FALSE_DISCOVERY_RATE,
            "cells": [{"cadence": c, "horizon": h} for c, h in PRIMARY_CELLS],
        },
        "hyperparameter_looks": {
            "ridge_alpha_candidates": len(RIDGE_ALPHAS),
            "treated_as": "a hyperparameter nested inside each cell",
            "counted_as_pbo_configurations": False,
            "counted_in_bh_family": False,
            "handled_by": (
                "re-selection inside each CSCV partition's in-sample half, so the "
                "cost of tuning is absorbed into the PBO estimate rather than "
                "counted as extra configurations competing with the cells"
            ),
        },
        "lifetime_exposure_ledger": {
            "description": (
                "everything the programme has spent against the same eventual "
                "decision; bookkeeping, not the BH denominator"
            ),
            "prior_effective_trials": PRIOR_EFFECTIVE_TRIALS,
            "prior_exposure_sources": list(PRIOR_EXPOSURE_SOURCES),
            "added_this_stage": BH_FAMILY_SIZE,
            "lifetime_effective_trials": PRIOR_EFFECTIVE_TRIALS + BH_FAMILY_SIZE,
            "resets_for_new_dataset_family": False,
            "reason": "Tier-1 is better input to the same question, not a new question",
        },
        "deferred_stages_not_counted_here": {
            "feature_decomposition": (
                "individual-feature attribution runs only if the block-level "
                "hypothesis survives, and is declared and counted separately then"
            ),
            "residualized_target_family": (
                "not declared; if wanted later it is a separate declaration with its "
                "own count"
            ),
            "declared_now": False,
        },
    }


def statistical_plan() -> dict[str, Any]:
    return {
        "stage2_plan_version": STAGE2_PLAN_VERSION,
        "label_engine_version": LABEL_ENGINE_VERSION,
        "label_definition_hash": LABEL_DEFINITION_HASH,
        "feature_semantics_hash": FEATURE_SEMANTICS_HASH,
        "superseded_plan_versions": [dict(e) for e in SUPERSEDED_PLAN_VERSIONS],
        "declared_before_any_outcome_viewed": True,
        "contains_predictive_result": False,
        "primary_hypothesis": (
            "The complete frozen Stage-1 L3 feature block carries incremental "
            "predictive information about the raw forward midpoint return beyond a "
            "price-only baseline."
        ),
        "primary_grid": {
            "form": "block-level, one cell per admissible (cadence, horizon) pair",
            "cells": [{"cadence": c, "horizon": h} for c, h in PRIMARY_CELLS],
            "count": BH_FAMILY_SIZE,
            "individual_feature_ranking_in_primary_run": False,
            "pairing_rule": (
                "time horizons on time cadences, change horizons on event cadences; "
                "a mismatched clock tests the mismatch, not the book"
            ),
            "features_in_block": len(FEATURE_VOCABULARY),
        },
        "splits": {
            "kind": "chronological, whole session-date blocks",
            "blocks": [
                {"name": name, "session_dates": count}
                for name, count in SPLIT_DATE_BLOCKS
            ],
            "total_session_dates": TOTAL_SESSION_DATES,
            "all_symbols_move_together": True,
            "date_never_split_across_sets": True,
            "reason": (
                "two symbols from the same session are not independent; splitting a "
                "date leaks the day's regime across the boundary"
            ),
            "embargo": (
                f"whole-date blocks already exceed the longest label "
                f"({EMBARGO_HORIZON}), so no additional embargo period is required "
                "and none is applied"
            ),
            "confirmation_is_single_use": True,
        },
        "model": MODEL_SPEC,
        "multiplicity": multiplicity_accounting(),
        "authorization_scope": authorization_scope(),
        "economic_gate": {
            "minimum_tradeable_net_bps": MINIMUM_TRADEABLE_NET_BPS,
            "required_t_statistic": REQUIRED_T_STATISTIC,
            "note": (
                "Statistical significance is not the bar. Stage 3 applies cost and "
                "latency; Stage 2 pre-authorizes nothing."
            ),
        },
        "prohibited": [
            "horizon substitution or nearest-horizon selection after results",
            "adding, dropping or renaming a horizon or cell after results",
            "re-splitting, or moving a date between blocks, after results",
            "re-tuning ridge alpha outside the discovery block on the chronological path",
            "letting any validation or confirmation date enter an alpha choice",
            "flattening alpha into the PBO configuration set",
            "individual-feature ranking inside the primary run",
            "residualizing the primary target",
            "reporting a subset of cells while correcting for that subset",
            "threshold selection inside Stage 2",
            "any transformation using data at or after the observation",
            "re-running confirmation for any reason",
            "treating the internal confirmation block as an external sample",
            "deploying real money on the strength of Stage 2 alone",
        ],
    }


# The DESIGN, with no reference to the artefacts it is declared over. A Stage-1
# semantic correction must not be able to move this value: if it does, the plan
# itself changed and that is a new trial, not a rebinding.
PLAN_DESIGN_ELEMENTS: tuple[str, ...] = (
    STAGE2_PLAN_VERSION,
    *(f"{c}:{h}" for c, h in PRIMARY_CELLS),
    *(f"{name}:{count}" for name, count in SPLIT_DATE_BLOCKS),
    PRIMARY_TARGET,
    "residualized=False",
    str(PRICE_ONLY_LAGS),
    MODEL_SPEC["l3_model"]["estimator"],
    str(RIDGE_ALPHAS),
    MODEL_SPEC["out_of_sample_score"]["incremental_statistic"],
    MODEL_SPEC["pbo"]["method"],
    f"cscv_blocks={CSCV_BLOCKS}",
    f"cscv_configurations={BH_FAMILY_SIZE}",
    str(PBO_AUTHORIZATION_CEILING),
    str(BH_FAMILY_SIZE),
    str(PRIOR_EFFECTIVE_TRIALS),
    "authorizes=stage3+external_sample;not=real_money",
)

PLAN_DESIGN_HASH = hashlib.sha256(
    "\n".join(PLAN_DESIGN_ELEMENTS).encode("utf-8")
).hexdigest()

# The design PLUS the artefacts it is declared over. This moves when Stage-1
# semantics are corrected, and it is supposed to.
PLAN_HASH = hashlib.sha256(
    "\n".join(
        (
            PLAN_DESIGN_ELEMENTS[0],
            LABEL_DEFINITION_HASH,
            FEATURE_SEMANTICS_HASH,
            *PLAN_DESIGN_ELEMENTS[1:],
        )
    ).encode("utf-8")
).hexdigest()

SUPERSEDED_PLAN_HASHES: tuple[dict[str, str], ...] = (
    {
        "plan_hash": (
            "e575428229bc5324fe74ca1593213a7acc39c879bf46eaac77bb1921d8430a25"
        ),
        "superseded_before_outcome": "true",
        "design_changed": "false",
        "declared_over_feature_semantics": (
            "7f613b06e8ba25bc45947c1ea6d3558e4508f73e37d6ef09736ba91d2d3933eb"
        ),
        "reason": (
            "rebound from feature-engine v3 to v4 for the queue_persistence "
            "coherent-state correction. PLAN_DESIGN_HASH is unchanged across "
            "both rebindings, which is the proof that no design element moved."
        ),
    },
    {
        "plan_hash": (
            "ba51ccba12caf6969bbb0da84ff4cffa956361c56d5ea7bf77b453893331ca6e"
        ),
        "superseded_before_outcome": "true",
        "design_changed": "false",
        "declared_over_feature_semantics": (
            "4aaeb9cb6d6700524d7fb065036612376d482a5cdff47d555d42c8a895c62551"
        ),
        "reason": (
            "rebound from feature-engine v2 to v3 after the absorption semantics "
            "correction. Not one design element moved -- the cells, the split, "
            "the target, the estimator, the alpha set, the statistic, the PBO "
            "rule, the ceiling, the family size and the prior-trial ledger are "
            "all unchanged, which PLAN_DESIGN_HASH proves. Only the identity of "
            "the feature artefact the plan is declared over changed."
        ),
    },
)
