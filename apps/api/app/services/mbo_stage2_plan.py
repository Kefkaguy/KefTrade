"""Stage 2A v2: the frozen, executable Stage-2 prediction plan.

Declared **before any feature-label relationship is visible**. Every choice that
could otherwise be made after seeing results is fixed here, exactly, including
the model, the scaling, the score, the test statistic and the pass criteria. An
unspecified choice is a degree of freedom, and a degree of freedom exercised
after the fact is not a decision -- it is a result.

## What v2 corrected

v1 declared a 1,652-cell grid of individual features against every horizon at
every cadence. That treated 59 sensors as 59 independent strategies and would
have produced a winner ranking whose top cell was mostly selection.

The primary authorization test is now **block-level**: does the complete frozen
L3 feature block carry incremental predictive information beyond a price-only
baseline? Fourteen cells, one per admissible (cadence, horizon) pair. No
individual-feature ranking is computed in the primary run at all. Feature
decomposition is a later, separately counted stage, and only if the block-level
hypothesis survives.

## Lifetime exposure is not the BH family

Two different quantities, conflated in v1:

* **Lifetime effective trials** -- everything this research programme has ever
  spent against the same eventual decision. Carries the 508 floor forward and
  only grows. Used for deflated-Sharpe style corrections and for judging whether
  the programme as a whole has earned a conclusion.
* **The BH family** -- the 14 primary cells declared in *this* run. Multiplicity
  control within a run is applied across the cells of that run.

Correcting 14 cells as though they were 522 would be as wrong as correcting 522
looks as though they were 14. Both numbers are reported.
"""

from __future__ import annotations

import hashlib
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

STAGE2_PLAN_VERSION = "tier1_stage2_plan_v2"

SUPERSEDED_PLAN_VERSIONS: tuple[dict[str, str], ...] = (
    {
        "version": "tier1_stage2_plan_v1",
        "commit": "f3289c9701ea8c7d431d941a60b20b5cf447c548",
        "superseded_before_outcome": "true",
        "reason": (
            "declared a 1,652-cell individual-feature grid, treating 59 sensors as "
            "59 independent strategies and inviting a winner ranking that would be "
            "mostly selection; split by fraction rather than by whole session-date "
            "blocks; left the executable model, scaling, score, test statistic and "
            "pass criteria unspecified; and conflated lifetime exposure with the "
            "within-run BH family."
        ),
        "declared_trials": "1680",
    },
)

# ---------------------------------------------------------------------------
# Lifetime exposure ledger (bookkeeping) vs the BH family (this run)
# ---------------------------------------------------------------------------

PRIOR_EFFECTIVE_TRIALS = 508
PRIOR_EXPOSURE_SOURCES: tuple[str, ...] = (
    "candle-only gap experiment (six predeclared factors, retired)",
    "order-flow / premarket / sector factor families",
    "news-reaction and sector lead-lag studies",
    "intraday alpha-map cell grid",
    "Stage 0 Alpaca L1 microstructure probe",
)

# ---------------------------------------------------------------------------
# C. The primary hypothesis grid: 14 block-level cells
# ---------------------------------------------------------------------------
#
# Time horizons are tested on time cadences and change horizons on event
# cadences. Pairing a 60-second horizon with a 200-event clock, or a
# next-change horizon with a 5-second clock, would test a mismatch between the
# sampling clock and the outcome clock rather than a hypothesis about the book.

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

# ---------------------------------------------------------------------------
# D. Chronological split by complete session-date blocks
# ---------------------------------------------------------------------------
#
# All eight symbols move together. A date is never split across sets: two
# symbols from the same session are not independent, so putting one in training
# and one in test leaks the day's regime across the boundary.

SPLIT_DATE_BLOCKS: tuple[tuple[str, int], ...] = (
    ("discovery", 10),
    ("validation", 6),
    ("confirmation", 4),
)
TOTAL_SESSION_DATES = sum(count for _, count in SPLIT_DATE_BLOCKS)
EMBARGO_HORIZON = "60s"
EMBARGO_DATES = 0  # whole-date blocks already exceed any intraday horizon

# ---------------------------------------------------------------------------
# E. The executable model, frozen
# ---------------------------------------------------------------------------

PRIMARY_TARGET = "return_bps"

PRICE_ONLY_LAGS: tuple[int, ...] = (1, 2, 3, 5, 10)

MODEL_SPEC: dict[str, Any] = {
    "primary_target": {
        "column": PRIMARY_TARGET,
        "definition": (
            "the horizon's signed midpoint return in basis points, from the wide "
            "label table, used only where that horizon's status is 'ok'"
        ),
        "rows_excluded": "any row whose horizon status is not 'ok'; never imputed",
    },
    "price_only_baseline": {
        "inputs": (
            f"lagged own-cadence midpoint log-returns at lags {list(PRICE_ONLY_LAGS)} "
            "plus the sign of each, computed within the symbol-day and prior-only"
        ),
        "estimator": "ordinary least squares, no regularization, intercept fitted",
        "purpose": (
            "short-horizon midpoint changes mean-revert unaided; a book feature that "
            "only recovers bid-ask bounce has added nothing"
        ),
    },
    "l3_model": {
        "estimator": "ridge regression",
        "inputs": "the price-only lags PLUS all 59 frozen Stage-1 features",
        "form": "nested -- the L3 model is the baseline's inputs augmented, never a separate fit",
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
        "already_frozen_in_stage1": list(NORMALIZED_FEATURES),
    },
    "cross_sectional_residualization": {
        "rule": (
            "at each grid instant, subtract the equal-weighted cross-sectional mean "
            "of the target across the eight symbols before fitting; the residual is "
            "the modelled quantity"
        ),
        "reason": (
            "without it a market-wide move fires on eight names at once and reads as "
            "eight independent confirmations of one bet"
        ),
        "applies_to": "the target only; features are not residualized in the primary run",
        "requires": "at least 6 of 8 symbols present at the instant, else the row is dropped",
    },
    "hyperparameter_rule": {
        "parameter": "ridge alpha",
        "candidates": [0.01, 0.1, 1.0, 10.0, 100.0],
        "selection": (
            "chosen inside the DISCOVERY block only, by the same out-of-sample score, "
            "using expanding-origin cross-validation over discovery dates; the chosen "
            "alpha is then frozen and reused unchanged for validation and confirmation"
        ),
        "forbidden": "re-tuning on validation or confirmation for any reason",
    },
    "out_of_sample_score": {
        "primary": "out-of-sample R^2 of the residualized target",
        "incremental_statistic": "delta_R2 = R2(l3_model) - R2(price_only_baseline)",
        "reported": "delta_R2 with its interval; the level of R2 is reported beside it but is not the test",
    },
    "inference": {
        "test_statistic": (
            "session-clustered t on the per-session-date delta_R2, one observation "
            "per session date, so 19.5 M rows cannot masquerade as 19.5 M degrees of "
            "freedom"
        ),
        "effective_n": "reported beside raw N for every cell",
        "block_bootstrap": {
            "unit": "whole session dates (all eight symbols together)",
            "resamples": 2000,
            "statistic": "delta_R2",
            "interval": "two-sided 95% percentile",
        },
    },
    "bh_family": {
        "members": "the 14 primary cells of this run",
        "size": BH_FAMILY_SIZE,
        "false_discovery_rate": 0.10,
        "note": (
            "lifetime exposure is tracked separately and is not the BH denominator"
        ),
    },
    "pass_criteria": {
        "discovery": (
            "delta_R2 > 0 with session-clustered t >= 3.0 and a bootstrap lower bound "
            "above 0; failure here ends the cell"
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
        "method": "CSCV (combinatorially symmetric cross-validation)",
        "implementation": (
            "session dates split into S=16 contiguous blocks; all C(16,8)=12,870 "
            "balanced partitions into in-sample and out-of-sample halves; for each, "
            "select the configuration with the best in-sample performance metric and "
            "record its out-of-sample rank; PBO is the fraction of partitions whose "
            "selected configuration lands in the bottom half out of sample"
        ),
        "configuration_set": (
            "the 14 primary cells plus the 5 ridge-alpha candidates -- the choices "
            "actually made, not a synthetic grid"
        ),
        "performance_metric": "delta_R2 of the residualized target",
        "authorization_ceiling": 0.50,
        "rule": (
            "PBO above 0.50 authorizes no strategy from the grid regardless of any "
            "individual cell's t-statistic. A grid always produces a best cell; its "
            "t says nothing about how many it beat."
        ),
    },
}


def multiplicity_accounting() -> dict[str, Any]:
    """The two distinct counts, kept apart."""
    return {
        "bh_family": {
            "description": "primary cells declared in this run; the BH denominator",
            "size": BH_FAMILY_SIZE,
            "false_discovery_rate": 0.10,
            "cells": [{"cadence": c, "horizon": h} for c, h in PRIMARY_CELLS],
        },
        "hyperparameter_looks": {
            "ridge_alpha_candidates": len(MODEL_SPEC["hyperparameter_rule"]["candidates"]),
            "confined_to": "discovery block only",
            "counted_in_pbo_configuration_set": True,
            "counted_in_bh_family": False,
            "reason": (
                "alpha is selected inside discovery and frozen; it is not a separate "
                "hypothesis about the book, but it is a choice, so PBO sees it"
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
            "reason": (
                "Tier-1 is better input to the same question, not a new question"
            ),
        },
        "deferred_stages_not_counted_here": {
            "feature_decomposition": (
                "individual-feature attribution runs only if the block-level "
                "hypothesis survives, and is declared and counted separately then"
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
            "predictive information about the residualized forward midpoint return "
            "beyond a price-only baseline."
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
                {"name": name, "session_dates": count} for name, count in SPLIT_DATE_BLOCKS
            ],
            "total_session_dates": TOTAL_SESSION_DATES,
            "all_symbols_move_together": True,
            "date_never_split_across_sets": True,
            "reason": (
                "two symbols from the same session are not independent; splitting a "
                "date leaks the day's regime across the boundary"
            ),
            "embargo": (
                f"whole-date blocks already exceed the longest label ({EMBARGO_HORIZON}), "
                "so no additional embargo period is required and none is applied"
            ),
            "confirmation_is_single_use": True,
        },
        "model": MODEL_SPEC,
        "multiplicity": multiplicity_accounting(),
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
            "re-tuning ridge alpha outside the discovery block",
            "individual-feature ranking inside the primary run",
            "reporting a subset of cells while correcting for that subset",
            "threshold selection inside Stage 2",
            "any transformation using data at or after the observation",
            "re-running confirmation for any reason",
        ],
    }


PLAN_HASH = hashlib.sha256(
    "\n".join(
        (
            STAGE2_PLAN_VERSION,
            LABEL_DEFINITION_HASH,
            FEATURE_SEMANTICS_HASH,
            *(f"{c}:{h}" for c, h in PRIMARY_CELLS),
            *(f"{name}:{count}" for name, count in SPLIT_DATE_BLOCKS),
            PRIMARY_TARGET,
            str(PRICE_ONLY_LAGS),
            MODEL_SPEC["l3_model"]["estimator"],
            str(MODEL_SPEC["hyperparameter_rule"]["candidates"]),
            MODEL_SPEC["out_of_sample_score"]["incremental_statistic"],
            MODEL_SPEC["pbo"]["method"],
            str(MODEL_SPEC["pbo"]["authorization_ceiling"]),
            str(BH_FAMILY_SIZE),
            str(PRIOR_EFFECTIVE_TRIALS),
        )
    ).encode("utf-8")
).hexdigest()
