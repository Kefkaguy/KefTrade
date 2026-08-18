"""Stage 3.5: the frozen execution-timing mechanism plan.

## What this is, and what it is not

Stage 3 asked whether the L3 block supports a standalone market-taking strategy.
**That question is closed and its verdict stands untouched.** Nothing here
reinterprets it, and a positive result here would not rescue it.

Stage 3.5 asks a different question, of a kind that does not require the signal
to pay for a round trip:

> Given an order that **already has to execute** for reasons of its own, can the
> four frozen predictors reduce implementation shortfall by choosing between
> immediate marketable execution and a bounded delay?

This is an execution optimizer. The order exists whether or not the model does;
the only decision is *when* to send it. A signal too small to overcome a
round-trip spread can still be large enough to say "not in the next 300
milliseconds" -- those are genuinely different bars, and the second is lower.

## Why the parent side is synthetic and balanced

At every eligible prediction the study evaluates **both** a required BUY and a
required SELL. That is not a convenience: it is what makes the parent order
*exogenous*. If the side were chosen by the model, this would be a directional
strategy wearing an execution costume, and the savings would be indistinguishable
from Stage-3 alpha. Pairing the two sides makes the side structurally
independent of the prediction by construction, and no rule anywhere in this
module may choose one.

A structural consequence, which the report must not obscure: for any given
prediction **exactly one** of the two sides delays. Predicted up delays the
sell; predicted down delays the buy. So balanced-parent-flow savings are
necessarily half the delayed-side savings, and reporting only the delayed side
would double the apparent benefit of a mechanism that in practice sees both
sides of the flow.

## Exploratory status, stated plainly

These twenty June-2025 dates have already produced Stage-2 and Stage-3 outcomes
that were viewed. Reusing them cannot yield confirmatory evidence, whatever the
statistics say. Stage 3.5 is therefore **mechanism-development evidence only**,
and the strongest thing it can authorize is an external, untouched
execution-timing confirmation experiment. It cannot authorize paper trading, it
cannot authorize live trading, and it cannot retroactively rescue Stage 3.
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.services.mbo_stage3_plan import (
    FROZEN_SURVIVORS,
    PRIMARY_FEE_SCHEDULE,
    SURVIVOR_HASH,
)
from app.services.mbo_stage3_plan import (
    PLAN_DESIGN_HASH as STAGE3_PLAN_DESIGN_HASH,
)
from app.services.mbo_stage3_plan import (
    PRIOR_EFFECTIVE_TRIALS as STAGE3_PRIOR_EFFECTIVE_TRIALS,
)

STAGE35_PLAN_VERSION = "tier1_stage35_execution_timing_v2"

SUPERSEDED_PLAN_VERSIONS: tuple[dict[str, str], ...] = (
    {
        "version": "tier1_stage35_execution_timing_v1",
        "plan_design_hash": (
            "ab0d42679cbedf6ac6b23706766ad16896e7d86413162b8f66e42cd3153c9fa7"
        ),
        "superseded_before_any_execution_outcome": "true",
        "reason": (
            "measurement defects, all pre-outcome. The send instant was not "
            "clamped to the decision, so a target timestamp earlier than the "
            "prediction would have sent a 'delayed' order before the prediction "
            "existed. Comparability required a timed fill for BOTH sides when "
            "only one side delays, so observations could be excluded on future "
            "liquidity the policy never uses -- and that exclusion is not random, "
            "because future thinness correlates with the dynamics being studied. "
            "Dollar savings and the Section-31 notional multiplied fixed-point "
            "price units by share counts without dividing by the price scale. "
            "delayed_fraction reported 1.0 for a mechanism that delays one of the "
            "two parent orders in each pair. The exact-zero prediction had no "
            "declared rule. Arrivals past the end of the certified stream could "
            "be served from BookReplay's final snapshot as though tradable."
        ),
    },
)

NANOS_PER_MILLISECOND = 1_000_000
NANOS_PER_SECOND = 1_000_000_000

# ---------------------------------------------------------------------------
# The predictors: exactly Stage 2's four survivors, unchanged
# ---------------------------------------------------------------------------

FROZEN_CELLS: tuple[str, ...] = tuple(FROZEN_SURVIVORS)
CELL_COUNT = len(FROZEN_CELLS)
CELL_HASH = SURVIVOR_HASH

MODEL_REUSE: dict[str, Any] = {
    "cells": list(FROZEN_CELLS),
    "model": "the frozen Stage-2 ridge fit, coefficients reproduced not refitted",
    "alpha": "the alpha Stage 2 recorded per cell; never re-selected",
    "features": "the frozen Stage-1 v4 vocabulary, unchanged",
    "refitting_against_execution_outcomes": False,
    "why_no_refit": (
        "a fit adjusted to improve execution savings would be a new model "
        "selected on this study's own outcome, and the study would then be "
        "measuring its own tuning"
    ),
}

# ---------------------------------------------------------------------------
# Row-level out-of-sample chronology
# ---------------------------------------------------------------------------
#
# Stage 3 evaluated only the four confirmation dates, because the confirmation
# fit was trained on the other sixteen. Stage 3.5 wants prediction rows across
# all twenty dates, so each block gets a fit that never saw the date being
# predicted.

CHRONOLOGY: dict[str, Any] = {
    "discovery": "leave-one-discovery-date-out: fit on the other 9 discovery dates",
    "validation": "fit on all 10 discovery dates",
    "confirmation": "fit on the 16 discovery + validation dates",
    "invariant": "the date being predicted is never in its own training set",
    "recorded": "the exact training dates are recorded per block and per date",
    "why": (
        "a prediction scored on a date its own fit was trained on is not a "
        "prediction. Leave-one-out inside discovery is the cheapest construction "
        "that keeps every one of the twenty dates usable without that happening."
    ),
}

# ---------------------------------------------------------------------------
# The synthetic required order
# ---------------------------------------------------------------------------

PARENT_ORDER: dict[str, Any] = {
    "construction": "one required BUY and one required SELL at every eligible prediction",
    "size_shares": 100,
    "side_chosen_by_model": False,
    "why_balanced": (
        "pairing both sides makes the parent side structurally independent of "
        "the prediction. A model-chosen side would make this a directional "
        "strategy in an execution costume."
    ),
    "exactly_one_side_delays": (
        "predicted up delays the sell, predicted down delays the buy; so "
        "balanced-flow savings are necessarily half the delayed-side savings"
    ),
}

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

LATENCY_NS = 250 * NANOS_PER_MILLISECOND
DELAY_DEADLINE_NS = 750 * NANOS_PER_MILLISECOND
MAX_ARRIVAL_NS = DELAY_DEADLINE_NS + LATENCY_NS  # decision + 1 second

TIMING: dict[str, Any] = {
    "decision_instant": "source_feature_available_ts_recv",
    "baseline_arrival": "decision + 250ms",
    "delay_deadline_send": "decision + 750ms",
    "timed_send": "min(max(target_available_ts_recv, decision), deadline_send)",
    "clamped_at_decision": (
        "a target timestamp earlier than the decision would otherwise send a "
        "'delayed' order before the prediction existed"
    ),
    "invariant": "decision <= timed_send <= decision + 750ms",
    "timed_arrival": "timed_send + 250ms",
    "max_arrival": "decision + 1s",
    "trigger": (
        "the frozen Stage-2 event target -- next_change or next_2_changes -- "
        "whose availability instant is an online-observable trigger"
    ),
    "no_future_information": (
        "the trigger is the arrival of an event, not its outcome. Neither the "
        "future price nor the label return decides anything: waiting until the "
        "midpoint next moves is a thing a participant can actually do."
    ),
    "unresolved_target": "if the target has not resolved by the deadline, send at the deadline",
}

# ---------------------------------------------------------------------------
# The policy -- sign only
# ---------------------------------------------------------------------------

POLICY: dict[str, Any] = {
    "rule": "sign of the prediction only; no magnitude threshold anywhere",
    "buy_predicted_up": "execute immediately",
    "sell_predicted_down": "execute immediately",
    "buy_predicted_down": "delay",
    "sell_predicted_up": "delay",
    "why_sign_only": (
        "a magnitude threshold is a free parameter, and a free parameter chosen "
        "against an execution outcome is a search. The sign is the model's own "
        "output with nothing added."
    ),
    "no_threshold_search": True,
    "no_delay_search": True,
    "no_latency_search": True,
}

# ---------------------------------------------------------------------------
# The exact tie
# ---------------------------------------------------------------------------
#
# Declared before outcomes, and deliberately not a magnitude threshold: this is
# what to do when the model expresses no direction at all.

ZERO_PREDICTION_RULE: dict[str, Any] = {
    "condition": "predicted_bps == 0.0 exactly",
    "action": "no timing preference; neither parent side delays",
    "classification": "no_direction_zero_prediction",
    "counted": True,
    "why_not_treated_as_down": (
        "classifying an exact tie as predicted-down would invent a direction the "
        "model did not express, and would do so asymmetrically -- always in "
        "favour of delaying the buy"
    ),
    "is_a_magnitude_threshold": False,
}

# ---------------------------------------------------------------------------
# Certified coverage
# ---------------------------------------------------------------------------

COVERAGE_RULE: dict[str, Any] = {
    "requirement": (
        "every instant a pair queries -- decision, baseline arrival and timed "
        "arrival -- must lie inside the receive-time span the certified file "
        "actually covers"
    ),
    "outside_coverage": "the pair is refused and counted, never filled",
    "why": (
        "the inherited BookReplay answers instants past the last record by "
        "snapshotting the final book. That is right for its own purpose and "
        "wrong here: a delayed order arriving after the stream ends would fill "
        "against a book that no longer exists, and the fill would look "
        "completely ordinary."
    ),
    "not_relied_upon": "BookReplay's post-EOF final snapshot is never a tradable state",
}

# ---------------------------------------------------------------------------
# The outcome
# ---------------------------------------------------------------------------

PRIMARY_METRIC = "balanced_parent_flow_savings_bps"

OUTCOME: dict[str, Any] = {
    "buy_savings_bps": "(baseline_fill - timed_fill) / decision_midpoint * 10000",
    "sell_savings_bps": "(timed_fill - baseline_fill) / decision_midpoint * 10000",
    "sign_convention": "positive means the L3 timing improved the required order",
    "delayed_side_savings": "the savings on the side that actually delayed",
    "balanced_parent_flow_savings": (
        "the mean over the required BUY and the required SELL, where the "
        "non-delayed side has exactly zero timing improvement by construction"
    ),
    "primary_scientific_metric": PRIMARY_METRIC,
    "why_balanced_is_primary": (
        "a desk that only ever executes the side the model happens to favour is "
        "not executing parent flow; it is trading. Balanced flow is the honest "
        "denominator."
    ),
}

DECOMPOSITION: tuple[str, ...] = (
    "total_savings_bps",
    "midpoint_timing_benefit_bps",
    "book_walk_benefit_bps",
    "dollar_savings_per_100_shares",
    "buy_savings_bps",
    "sell_savings_bps",
    "by_symbol",
    "by_session_date",
    "comparable_pair_count",
    "pairs_with_a_delay_fraction",
    "parent_orders_delayed_fraction",
    "target_triggered_delays",
    "deadline_triggered_delays",
    "displayed_liquidity_shares",
    "levels_walked",
)

DECOMPOSITION_IDENTITY = (
    "total_savings = midpoint_timing_benefit + book_walk_benefit, exactly, for "
    "both sides; asserted rather than assumed"
)

# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------

FEES: dict[str, Any] = {
    "round_trip_costs_apply": False,
    "why": (
        "the parent order executes either way. There is no exit leg to pay for, "
        "and inventing one would manufacture a cost this mechanism does not "
        "incur. Common one-way fees are identical under both policies and are "
        "not strategy alpha."
    ),
    "price_dependent_difference_reported_separately": True,
    "june_2025_expectation": (
        "Section 31 was $0.00 per million across the whole window and FINRA TAF "
        "is per share rather than per dollar, so the expected price-dependent "
        "fee difference between the two policies is exactly zero. It is computed "
        "anyway and reported, because an expectation is not a measurement."
    ),
    "schedule": PRIMARY_FEE_SCHEDULE["name"],
}

# ---------------------------------------------------------------------------
# Comparability
# ---------------------------------------------------------------------------

COMPARABILITY: dict[str, Any] = {
    "rule": (
        "a paired observation is comparable when both baseline executions and "
        "the TIMED execution OF THE DELAYED SIDE can be evaluated under the "
        "frozen fill rules"
    ),
    "non_delayed_side": (
        "executes at the baseline instant under both policies, so its policy "
        "fill literally reuses its baseline fill and its future liquidity is "
        "never queried"
    ),
    "why_not_require_the_unused_leg": (
        "gating inclusion on a market state the policy never touches would "
        "exclude observations for an irrelevant reason -- and not at random, "
        "since future thinness correlates with exactly the book dynamics this "
        "mechanism claims to exploit"
    ),
    "asymmetric_failures_are_recorded": True,
    "why": (
        "dropping the cases where only one leg fills would select on execution "
        "difficulty, and execution difficulty is correlated with exactly the "
        "book states this mechanism claims to exploit"
    ),
}

# ---------------------------------------------------------------------------
# Inference and the screen
# ---------------------------------------------------------------------------

BH_FALSE_DISCOVERY_RATE = 0.10
T_HURDLE = 3.0
MIN_SESSION_DATES = 10
MIN_COMPARABLE_PAIRS = 1_000

MECHANISM_SCREEN: dict[str, Any] = {
    "family": "the four frozen cells, and nothing else",
    "statistic": "session-date mean balanced-flow savings, then clustered t",
    "mean_balanced_flow_savings_bps": "> 0",
    "clustered_t": f">= {T_HURDLE}",
    "benjamini_hochberg_q": f"<= {BH_FALSE_DISCOVERY_RATE}",
    "minimum_session_dates_with_comparable_observations": MIN_SESSION_DATES,
    "minimum_comparable_paired_observations": MIN_COMPARABLE_PAIRS,
    "what_a_pass_authorizes": (
        "an external, untouched execution-timing confirmation experiment. "
        "Nothing else."
    ),
    "what_a_pass_does_not_authorize": (
        "paper trading, live trading, capital, or any reinterpretation of the "
        "closed Stage-3 verdict"
    ),
    "if_no_cell_passes": (
        "close this execution-timing mechanism and move to passive-fill or "
        "order-flow-toxicity research. Do not tune thresholds, delays or "
        "latencies against these outcomes -- that would convert a negative "
        "result into a search."
    ),
}

# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------

PRIOR_EFFECTIVE_TRIALS = STAGE3_PRIOR_EFFECTIVE_TRIALS + 4  # Stage 3 spent four
NEW_EXPLORATORY_SPECIFICATIONS = 4

GOVERNANCE: dict[str, Any] = {
    "family_status": "a new mechanism family opened AFTER Stage-3 outcomes were viewed",
    "evidence_class": "exploratory mechanism development",
    "confirmatory": False,
    "why_not_confirmatory": (
        "these twenty dates have already produced viewed outcomes. No statistic "
        "computed on them can be confirmatory, however it is corrected."
    ),
    "prior_effective_trials": PRIOR_EFFECTIVE_TRIALS,
    "adds_to_ledger_when_outcomes_are_viewed": NEW_EXPLORATORY_SPECIFICATIONS,
    "stage3_verdict": "closed and unaltered; not reinterpreted by this study",
}

FORBIDDEN: tuple[str, ...] = (
    "refitting any model against execution outcomes",
    "choosing the parent-order side with the model",
    "any magnitude threshold on the prediction",
    "searching the delay, the deadline or the latency",
    "filtering symbols",
    "promoting a result after outcomes are viewed",
    "authorizing paper or live trading from this study",
    "reinterpreting or reopening the Stage-3 verdict",
)

PLAN_DESIGN_ELEMENTS: tuple[str, ...] = (
    STAGE35_PLAN_VERSION,
    f"cells={CELL_HASH}",
    "model=frozen_stage2_reproduced_not_refitted",
    "chronology=lodo_discovery|discovery10_validation|discovery16_confirmation",
    "parent=balanced_buy_and_sell_100_shares",
    "policy=sign_only",
    f"latency_ns={LATENCY_NS}",
    f"delay_deadline_ns={DELAY_DEADLINE_NS}",
    f"max_arrival_ns={MAX_ARRIVAL_NS}",
    "trigger=frozen_stage2_target_available_ts_recv",
    f"primary_metric={PRIMARY_METRIC}",
    "decomposition=midpoint+book_walk",
    "fees=one_way_no_round_trip",
    "comparability=both_legs_certifiable",
    f"t_hurdle={T_HURDLE}",
    f"fdr={BH_FALSE_DISCOVERY_RATE}",
    f"min_session_dates={MIN_SESSION_DATES}",
    f"min_comparable_pairs={MIN_COMPARABLE_PAIRS}",
    f"prior_effective_trials={PRIOR_EFFECTIVE_TRIALS}",
    "evidence=exploratory_mechanism_development",
    "timed_send_clamped_to_decision=true",
    "comparability=baseline_both_sides+timed_delayed_side_only",
    "dollar_units=divided_by_price_scale",
    "delay_reporting=pairs_1.0_parent_orders_0.5",
    "zero_prediction=no_direction_no_delay",
    "coverage=arrivals_must_be_inside_certified_receive_span",
    "authorizes=external_confirmation_experiment_only;not=paper;not=live",
)

PLAN_DESIGN_HASH = hashlib.sha256(
    "\n".join(PLAN_DESIGN_ELEMENTS).encode("utf-8")
).hexdigest()


def statistical_plan() -> dict[str, Any]:
    """The declared plan, for writing next to the results."""
    return {
        "stage35_plan_version": STAGE35_PLAN_VERSION,
        "plan_design_hash": PLAN_DESIGN_HASH,
        "stage3_plan_design_hash": STAGE3_PLAN_DESIGN_HASH,
        "contains_execution_outcome": False,
        "research_question": (
            "can the four frozen L3 predictors reduce implementation shortfall "
            "for an exogenous order that already needs to execute, by choosing "
            "between immediate marketable execution and a bounded delay?"
        ),
        "is_a_directional_strategy": False,
        "frozen_cells": list(FROZEN_CELLS),
        "cell_hash": CELL_HASH,
        "model_reuse": dict(MODEL_REUSE),
        "chronology": dict(CHRONOLOGY),
        "parent_order": dict(PARENT_ORDER),
        "timing": dict(TIMING),
        "policy": dict(POLICY),
        "outcome": dict(OUTCOME),
        "decomposition": list(DECOMPOSITION),
        "decomposition_identity": DECOMPOSITION_IDENTITY,
        "fees": dict(FEES),
        "comparability": dict(COMPARABILITY),
        "zero_prediction_rule": dict(ZERO_PREDICTION_RULE),
        "coverage_rule": dict(COVERAGE_RULE),
        "superseded_plan_versions": [dict(e) for e in SUPERSEDED_PLAN_VERSIONS],
        "mechanism_screen": dict(MECHANISM_SCREEN),
        "governance": dict(GOVERNANCE),
        "forbidden": list(FORBIDDEN),
    }
