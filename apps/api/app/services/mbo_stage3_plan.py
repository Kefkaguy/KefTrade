"""Stage 3: the frozen economic-viability plan.

Declared **before any economic outcome is visible**, and -- because Stage 2B has
not yet been run -- before even the identity of the survivors is known. That
ordering is deliberate. A rule written after seeing which cells survived, or
after seeing what costs did to them, is not a rule; it is a result.

Stage 2 asked whether the L3 block carries incremental predictive information.
Stage 3 asks the only question that matters afterwards: **is any of it
harvestable once you must decide late, act later still, cross the spread, and
pay the fee schedule?**

## What Stage 3 may not do

No Stage-1 feature changes. No Stage-2 cell, label, horizon or model changes. No
refitting of anything -- the coefficients are the frozen Stage-2 confirmation
fit and are used as they stand. No new signal is constructed, no feature is
selected, no threshold is searched against an economic outcome.

## The trading rule, frozen here

A prediction is not a trade. Turning one into the other requires choices, and
every one of them is fixed in this module before an outcome exists:

* **Primary rule -- the cost hurdle.** Take the trade only when the model's own
  predicted edge exceeds the round-trip cost that is *observable at decision
  time*: the prevailing half-spread on both legs plus the frozen per-share fee
  schedule. This is not a tuned threshold. It is the break-even the market is
  quoting at that instant, and it uses no information about what happens next.

* **Secondary rule -- the discovery decile.** Take the trade when the absolute
  prediction is in the top decile of absolute predictions *measured on the
  discovery dates only*. Declared here so a second rule exists without being
  invented later; counted as a secondary family, never promoted.

The primary economic question -- "does a survivor remain positive at 250 ms
after realistic costs?" -- is answered by the **primary rule at the 250 ms rung
only**. That is one test per survivor. Everything else in the ladder is
descriptive context, reported in full and corrected as a secondary family.

## Latency is a causal constraint, not a haircut

Two distinct instants, and nothing may confuse them:

* ``t_decision`` = ``feature_available_ts_recv``. The snapshot's inputs are not
  all knowable before this, so no decision may occur before it.
* ``t_arrival`` = ``t_decision + latency``. The order reaches the book here, and
  the fill may use **only** book state at or after this instant.

The midpoint drift between those two instants is not a modelling nuisance. It is
the adverse selection the strategy actually suffers, and it is measured and
reported separately rather than folded into slippage.
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.services.mbo_stage2_plan import (
    PLAN_DESIGN_HASH as STAGE2_PLAN_DESIGN_HASH,
)
from app.services.mbo_stage2_plan import (
    PLAN_HASH as STAGE2_PLAN_HASH,
)
from app.services.mbo_stage2_plan import (
    PRIOR_EFFECTIVE_TRIALS as STAGE2_PRIOR_EFFECTIVE_TRIALS,
)

STAGE3_PLAN_VERSION = "tier1_stage3_economics_v1"

NANOS_PER_MILLISECOND = 1_000_000
NANOS_PER_SECOND = 1_000_000_000

# ---------------------------------------------------------------------------
# The latency ladder
# ---------------------------------------------------------------------------

LATENCY_RUNGS: tuple[tuple[str, int], ...] = (
    ("50ms", 50 * NANOS_PER_MILLISECOND),
    ("250ms", 250 * NANOS_PER_MILLISECOND),
    ("1s", 1 * NANOS_PER_SECOND),
)
PRIMARY_LATENCY = "250ms"

# ---------------------------------------------------------------------------
# The trading rule
# ---------------------------------------------------------------------------

PRIMARY_RULE = "cost_hurdle"
SECONDARY_RULE = "discovery_decile"
TRADING_RULES: tuple[str, ...] = (PRIMARY_RULE, SECONDARY_RULE)

# Measured on discovery dates only, so it can never be chosen against a Stage-3
# economic outcome.
DISCOVERY_DECILE_QUANTILE = 0.90

TRADE_SIZE_SHARES = 100

# A marketable order walks the book, but not without limit: a trade that cannot
# fill inside this many price levels is recorded as unfilled rather than assumed
# to have executed somewhere worse.
MAX_BOOK_LEVELS_WALKED = 10

# ---------------------------------------------------------------------------
# The fee schedule -- frozen constants from public schedules, not estimates
# tuned to make anything work
# ---------------------------------------------------------------------------

FEE_SCHEDULE: dict[str, Any] = {
    "venue": "XNAS",
    "liquidity_role": "taker on both legs",
    "why_taker": (
        "the horizons are seconds. A passive entry that does not fill is not a "
        "cheaper version of this strategy, it is a different strategy with its "
        "own fill-probability model. Taking is the honest cost of acting on a "
        "second-scale signal."
    ),
    "nasdaq_taker_fee_usd_per_share": 0.0030,
    "sec_section_31_usd_per_million_sold": 27.80,
    "finra_taf_usd_per_share_sold": 0.000166,
    "finra_taf_cap_usd_per_trade": 8.30,
    "clearing_usd_per_share": 0.0002,
    "declared_before_any_economic_outcome": "true",
    "rate_caveat": (
        "the SEC Section 31 rate is reset periodically. The value here is the "
        "declared constant for this run; a re-run against a different rate must "
        "say so rather than silently re-price."
    ),
}

# ---------------------------------------------------------------------------
# What counts as a pass
# ---------------------------------------------------------------------------

ECONOMIC_GATES: dict[str, Any] = {
    "primary_question": (
        "does the survivor remain economically positive at 250 ms after "
        "realistic costs?"
    ),
    "primary_statistic": "mean net return in basis points per trade",
    "must_be_positive": True,
    "inference": (
        "session-clustered t over per-session-date mean net bps, one "
        "observation per session date, exactly as Stage 2"
    ),
    "t_hurdle": 3.0,
    "false_discovery_rate": 0.10,
    "primary_family": "one test per survivor, at the 250 ms rung, primary rule",
    "secondary_families": (
        "the 50 ms and 1 s rungs, and the discovery-decile rule; reported in "
        "full, corrected separately, never promoted to the primary answer"
    ),
    "minimum_trades_for_inference": 100,
    "minimum_session_dates": 4,
    "what_a_pass_authorizes": (
        "a paper-trading deployment proposal for review. It authorizes no live "
        "order, no capital, and no real money."
    ),
}

# ---------------------------------------------------------------------------
# Multiplicity
# ---------------------------------------------------------------------------

# Stage 2 spent its own 14 cells on top of the 508 lifetime floor.
PRIOR_EFFECTIVE_TRIALS = STAGE2_PRIOR_EFFECTIVE_TRIALS + 14

MEASUREMENTS: tuple[str, ...] = (
    "gross_return_bps",
    "spread_paid_bps",
    "entry_price",
    "exit_price",
    "slippage_bps",
    "adverse_selection_bps",
    "fees_bps",
    "net_return_bps",
    "win_rate",
    "trade_count",
    "average_holding_ns",
    "displayed_liquidity_shares",
    "capacity_shares",
)

REPORT_BREAKDOWNS: tuple[str, ...] = ("by_symbol", "by_session_date", "by_latency")

FORBIDDEN: tuple[str, ...] = (
    "refitting any Stage-2 model or re-selecting alpha",
    "changing any Stage-1 feature, Stage-2 cell, label or horizon",
    "constructing a new signal or selecting features",
    "searching a trading threshold against an economic outcome",
    "promoting a secondary rung or rule to the primary answer",
    "placing live or paper orders",
)

PLAN_DESIGN_ELEMENTS: tuple[str, ...] = (
    STAGE3_PLAN_VERSION,
    *(f"latency:{name}:{ns}" for name, ns in LATENCY_RUNGS),
    f"primary_latency={PRIMARY_LATENCY}",
    *(f"rule:{rule}" for rule in TRADING_RULES),
    f"primary_rule={PRIMARY_RULE}",
    f"decile={DISCOVERY_DECILE_QUANTILE}",
    f"trade_size={TRADE_SIZE_SHARES}",
    f"max_levels={MAX_BOOK_LEVELS_WALKED}",
    "taker_fee=0.0030",
    "sec_fee=27.80",
    "taf=0.000166",
    "clearing=0.0002",
    "t_hurdle=3.0",
    "fdr=0.10",
    "min_trades=100",
    f"prior_effective_trials={PRIOR_EFFECTIVE_TRIALS}",
    "authorizes=paper_proposal_only;not=live;not=capital",
)

# The design, independent of which cells turn out to survive. This value is
# identical whether Stage 3 is planned before or after Stage 2B runs, which is
# what proves the rules were not written around the survivors.
PLAN_DESIGN_HASH = hashlib.sha256(
    "\n".join(PLAN_DESIGN_ELEMENTS).encode("utf-8")
).hexdigest()


def statistical_plan() -> dict[str, Any]:
    """The declared plan, for writing next to the results."""
    return {
        "stage3_plan_version": STAGE3_PLAN_VERSION,
        "declared_before_any_economic_outcome": True,
        "declared_before_survivors_were_known": True,
        "contains_economic_result": False,
        "stage2_plan_hash": STAGE2_PLAN_HASH,
        "stage2_plan_design_hash": STAGE2_PLAN_DESIGN_HASH,
        "plan_design_hash": PLAN_DESIGN_HASH,
        "latency_ladder": [
            {"name": name, "nanoseconds": ns} for name, ns in LATENCY_RUNGS
        ],
        "primary_latency": PRIMARY_LATENCY,
        "causality": {
            "decision_instant": "feature_available_ts_recv",
            "arrival_instant": "decision + latency",
            "fill_information": "book state at or after arrival only",
        },
        "trading_rules": {
            "primary": {
                "name": PRIMARY_RULE,
                "definition": (
                    "trade only when |predicted bps| exceeds the round-trip cost "
                    "observable at decision time: half-spread on entry plus "
                    "half-spread on exit plus the frozen per-share fees"
                ),
                "why_not_a_tuned_threshold": (
                    "the hurdle is quoted by the market at the decision instant "
                    "and uses no information about what happens afterwards"
                ),
            },
            "secondary": {
                "name": SECONDARY_RULE,
                "definition": (
                    f"trade when |predicted bps| is at or above the "
                    f"{DISCOVERY_DECILE_QUANTILE:.0%} quantile of |predicted bps| "
                    "measured on the discovery dates only"
                ),
            },
        },
        "fee_schedule": dict(FEE_SCHEDULE),
        "gates": dict(ECONOMIC_GATES),
        "measurements": list(MEASUREMENTS),
        "report_breakdowns": list(REPORT_BREAKDOWNS),
        "multiplicity": {
            "prior_effective_trials": PRIOR_EFFECTIVE_TRIALS,
            "note": (
                "lifetime exposure carries forward; it does not reset because "
                "this is a new stage"
            ),
        },
        "forbidden": list(FORBIDDEN),
    }
