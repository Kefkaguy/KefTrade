"""Stage 2A: the frozen statistical plan for the Tier-1 prediction test.

Declared **before any predictive outcome is viewed**. That ordering is the whole
value of this module: a plan written after seeing which horizon worked is not a
plan, and a multiplicity count assembled after choosing what to report is not a
correction.

This module computes no result. It declares the design, counts the cells the
design commits to, and carries the project's accumulated search exposure
forward. Nothing here reads a feature or a label value.

## What was already spent

The multiplicity ledger **does not reset** because Tier-1 is a new dataset
family. It is the same research programme asking the same question with better
inputs, and the candle work, the gap experiment, the order-flow factors, the
news and sector studies and the Stage-0 probe all consumed exposure against the
same eventual decision. Reusing a fresh dataset for a fourteenth idea is a
fourteen-idea problem.

`PRIOR_EFFECTIVE_TRIALS` is a declared **floor**, not an estimate to be revised
downward.
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.services.intraday_hypotheses import (
    DECLARED_OBSERVATION_DISPERSION_BPS,
    MINIMUM_TRADEABLE_NET_BPS,
    REQUIRED_T_STATISTIC,
)
from app.services.mbo_feature_engine import (
    ABSORPTION_FEATURES,
    AGGRESSIVE_FLOW_FEATURES,
    BOOK_STATE_FEATURES,
    CADENCES,
    FEATURE_SEMANTICS_HASH,
    FEATURE_VOCABULARY,
    LIFECYCLE_FEATURES,
    NORMALIZED_FEATURES,
    PRESSURE_FEATURES,
)
from app.services.mbo_label_engine import (
    HORIZON_NAMES,
    LABEL_DEFINITION_HASH,
    LABEL_ENGINE_VERSION,
)

STAGE2_PLAN_VERSION = "tier1_stage2_plan_v1"

# Carried forward from the prior programme. A floor: the true exposure is at
# least this, never less.
PRIOR_EFFECTIVE_TRIALS = 508
PRIOR_EXPOSURE_SOURCES: tuple[str, ...] = (
    "candle-only gap experiment (six predeclared factors, retired)",
    "order-flow / premarket / sector factor families",
    "news-reaction and sector lead-lag studies",
    "intraday alpha-map cell grid",
    "Stage 0 Alpaca L1 microstructure probe",
)

# ---------------------------------------------------------------------------
# The declared grid
# ---------------------------------------------------------------------------
#
# Every feature in the frozen vocabulary, at every cadence, against every
# horizon. Declared in full rather than pre-screened: choosing a subset now
# would either be arbitrary or -- worse -- informed by a peek at the outcomes.
# The cost of that honesty is a large multiplicity, which is counted below
# rather than hidden.

PREDICTOR_FEATURES: tuple[str, ...] = FEATURE_VOCABULARY

# Stage 1 already ships prior-only normalized variants of four features, so no
# further transform is applied here. Adding a transform family would multiply
# the grid without adding a hypothesis.
TRANSFORMS: tuple[str, ...] = ("identity",)

CADENCE_NAMES: tuple[str, ...] = tuple(c.name for c in CADENCES)

SPLIT_FRACTIONS: tuple[float, float, float] = (0.50, 0.30, 0.20)
SPLIT_NAMES: tuple[str, ...] = ("discovery", "validation", "confirmation")

# One embargo unit is the longest label horizon, so no training row's label can
# overlap the first test row's feature window.
EMBARGO_HORIZON = "60s"

MONOTONICITY_MINIMUM = 0.70
PBO_AUTHORIZATION_CEILING = 0.50
BH_FALSE_DISCOVERY_RATE = 0.10
BLOCK_BOOTSTRAP_RESAMPLES = 2_000
CSCV_PARTITIONS = 16


def declared_cell_count() -> dict[str, Any]:
    """Exactly how many looks the design commits to, before any are taken."""
    features = len(PREDICTOR_FEATURES)
    transforms = len(TRANSFORMS)
    cadences = len(CADENCE_NAMES)
    horizons = len(HORIZON_NAMES)

    feature_cells = features * transforms * cadences * horizons
    # One nested price-only-versus-price-plus-L3 comparison per (cadence,
    # horizon). These are the tests that actually answer "is there incremental
    # information", and they are trials too.
    incremental_tests = cadences * horizons
    baseline_fits = cadences * horizons

    declared = feature_cells + incremental_tests
    return {
        "features": features,
        "transforms": transforms,
        "cadences": cadences,
        "horizons": horizons,
        "feature_cells": feature_cells,
        "incremental_information_tests": incremental_tests,
        "price_only_baseline_fits": baseline_fits,
        "declared_trials_this_stage": declared,
        "prior_effective_trials": PRIOR_EFFECTIVE_TRIALS,
        "cumulative_effective_trials": PRIOR_EFFECTIVE_TRIALS + declared,
        "ledger_resets": False,
        "note": (
            "Baseline fits are counted separately from the trial total because a "
            "baseline is not a hypothesis about the book -- but each incremental "
            "test against one is."
        ),
    }


def statistical_plan() -> dict[str, Any]:
    """The frozen plan. No result, no data, no threshold fitted to an outcome."""
    counts = declared_cell_count()
    return {
        "stage2_plan_version": STAGE2_PLAN_VERSION,
        "label_engine_version": LABEL_ENGINE_VERSION,
        "label_definition_hash": LABEL_DEFINITION_HASH,
        "feature_semantics_hash": FEATURE_SEMANTICS_HASH,
        "declared_before_any_outcome_viewed": True,
        "contains_predictive_result": False,
        "splits": {
            "kind": "chronological",
            "fractions": dict(zip(SPLIT_NAMES, SPLIT_FRACTIONS, strict=True)),
            "unit": "symbol-day, ordered by session date",
            "embargo": (
                f"one {EMBARGO_HORIZON} horizon of wall-clock time is dropped at each "
                "split boundary, so no training label can overlap a test feature "
                "window"
            ),
            "confirmation_is_single_use": True,
            "rule": (
                "Splits are fixed by date before measurement. A boundary moved after "
                "seeing a result is a new declaration and counts again."
            ),
        },
        "transformations": {
            "allowed": "expanding / prior-only only",
            "forbidden": (
                "full-sample means, variances, quantiles, winsorization bounds, or "
                "any statistic computed over data that includes the observation "
                "being transformed"
            ),
            "already_frozen_in_stage1": list(NORMALIZED_FEATURES),
        },
        "baseline": {
            "name": "price_only",
            "inputs": (
                "lagged midpoint returns and tick signs from the same cadence, "
                "prior-only"
            ),
            "purpose": (
                "Short-horizon midpoint changes mean-revert on their own. A book "
                "feature that only recovers bid-ask bounce has added nothing, and "
                "without a baseline it would look like a finding."
            ),
        },
        "incremental_test": {
            "form": "nested comparison per (cadence, horizon)",
            "reported": "increment in out-of-sample skill over the baseline",
            "not_reported_alone": "the level of skill, which the baseline can supply",
        },
        "inference": {
            "clustering": (
                "by session and by symbol. Adjacent snapshots are near-duplicates: "
                "19.5 M rows are not 19.5 M degrees of freedom, and every cell must "
                "report an effective N alongside its raw N."
            ),
            "block_bootstrap": {
                "unit": "symbol-day blocks",
                "resamples": BLOCK_BOOTSTRAP_RESAMPLES,
                "reason": "preserves within-session dependence that an iid resample destroys",
            },
            "multiplicity": {
                "method": "Benjamini-Hochberg",
                "false_discovery_rate": BH_FALSE_DISCOVERY_RATE,
                "applied_across": "every declared cell in the run, not the reported subset",
            },
            "monotonicity": {
                "minimum": MONOTONICITY_MINIMUM,
                "applies_to": "ordinal feature-response testing (bucketed features)",
                "reason": (
                    "a relationship that pays only in one interior bucket is "
                    "describing a handful of observations, not a relationship"
                ),
            },
            "overfitting": {
                "method": "PBO via CSCV",
                "partitions": CSCV_PARTITIONS,
                "authorization_ceiling": PBO_AUTHORIZATION_CEILING,
                "rule": (
                    "PBO above 0.5 authorizes no strategy from the grid, regardless "
                    "of any individual cell's t-statistic. A grid always produces a "
                    "best cell; its t says nothing about how many it beat."
                ),
            },
        },
        "economic_gate": {
            "minimum_tradeable_net_bps": MINIMUM_TRADEABLE_NET_BPS,
            "required_t_statistic": REQUIRED_T_STATISTIC,
            "declared_dispersion_bps_30m": DECLARED_OBSERVATION_DISPERSION_BPS,
            "note": (
                "Statistical significance is not the bar. Stage 3 applies cost and "
                "latency; Stage 2 does not pre-authorize anything."
            ),
        },
        "multiplicity_ledger": {
            "prior_effective_trials": PRIOR_EFFECTIVE_TRIALS,
            "prior_exposure_sources": list(PRIOR_EXPOSURE_SOURCES),
            "resets_for_new_dataset_family": False,
            "reason": (
                "Tier-1 is better input to the same question, not a new question. "
                "Exposure accumulated against the same eventual decision."
            ),
            **{
                key: counts[key]
                for key in (
                    "declared_trials_this_stage",
                    "cumulative_effective_trials",
                )
            },
        },
        "grid": counts,
        "feature_groups": {
            "book_state": len(BOOK_STATE_FEATURES),
            "pressure": len(PRESSURE_FEATURES),
            "order_lifecycle": len(LIFECYCLE_FEATURES),
            "aggressive_flow": len(AGGRESSIVE_FLOW_FEATURES),
            "absorption_resilience": len(ABSORPTION_FEATURES),
            "prior_only_normalized": len(NORMALIZED_FEATURES),
        },
        "horizons": list(HORIZON_NAMES),
        "cadences": list(CADENCE_NAMES),
        "prohibited": [
            "horizon substitution or nearest-horizon selection after results",
            "adding, dropping or renaming a horizon after results",
            "re-splitting after seeing a split's outcome",
            "reporting a subset of cells while correcting for that subset",
            "threshold selection inside Stage 2",
            "any transformation using data at or after the observation",
        ],
    }


PLAN_HASH = hashlib.sha256(
    "\n".join(
        (
            STAGE2_PLAN_VERSION,
            LABEL_DEFINITION_HASH,
            FEATURE_SEMANTICS_HASH,
            *PREDICTOR_FEATURES,
            *TRANSFORMS,
            *CADENCE_NAMES,
            *HORIZON_NAMES,
            *SPLIT_NAMES,
            str(SPLIT_FRACTIONS),
            EMBARGO_HORIZON,
            str(MONOTONICITY_MINIMUM),
            str(PBO_AUTHORIZATION_CEILING),
            str(BH_FALSE_DISCOVERY_RATE),
            str(PRIOR_EFFECTIVE_TRIALS),
        )
    ).encode("utf-8")
).hexdigest()
