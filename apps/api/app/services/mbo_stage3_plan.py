"""Stage 3: the frozen economic-viability plan.

## Governance, stated accurately

Stage 2B **has** run, on the VPS, and produced four confirmation survivors. They
are named in ``FROZEN_SURVIVORS`` below and bound into every Stage-3 artefact by
``SURVIVOR_HASH``. An earlier draft of this module claimed the plan was declared
before the survivors were known; that was false and has been removed rather than
softened. What is true, and what actually matters, is:

* the Stage-2 survivors are **known**;
* **no Stage-3 economic outcome has been viewed**;
* the Stage-3 rules below are **frozen before any economic outcome exists**.

Knowing which cells survived cannot bias a cost model or a latency ladder. It
could bias a trading rule chosen to flatter them, which is exactly why the rules
are fixed here, in advance, and hashed.

## All four survivors are event-clocked

Every survivor is a ``50ev`` or ``200ev`` cadence paired with a ``next_change``
or ``next_2_changes`` horizon. **None of them has a numeric horizon.** There is
no number of nanoseconds that can stand in for "the next midpoint change",
because when that change happens is itself the thing being measured.

So Stage 3 never converts a horizon into a duration. The exit instant comes from
the frozen Stage-2 label columns -- the exact ``label_ts_event``,
``label_ts_recv``, ``available_ts_recv`` and realized lag that Stage 2 recorded
for that target event -- and nowhere else. ``horizon_ns`` does not exist in this
module, and could not be smuggled in without deleting a test.

## What Stage 3 may not do

No Stage-1 feature changes. No Stage-2 cell, label, horizon or model changes. No
refitting -- the coefficients and alpha are the frozen Stage-2 confirmation fit
and are used as they stand. No new signal, no feature selection, no threshold
searched against an economic outcome.
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

STAGE3_PLAN_VERSION = "tier1_stage3_economics_v3"

SUPERSEDED_PLAN_VERSIONS: tuple[dict[str, str], ...] = (
    {
        "version": "tier1_stage3_economics_v2",
        "plan_design_hash": (
            "874292555a9e136294f36c45a69c402a8448213652cdf9a1aa867638b5529ff3"
        ),
        "superseded_before_any_economic_outcome": "true",
        "reason": (
            "froze a 2026-dated fee schedule for research sessions that are June "
            "2025, which would have priced twenty 2025 sessions against rates "
            "that did not yet exist and charged a non-zero Section 31 fee through "
            "a period in which that rate was $0.00 per million. It also silently "
            "set retail CAT pass-through to zero without a June-2025 customer fee "
            "schedule to support it, and verified the reconstructed Stage-2 fit "
            "against delta_R2 over one aggregated confirmation Gram when Stage 2 "
            "had actually recorded the arithmetic mean of per-date values -- a "
            "different number, so the check could fail on a correct reproduction "
            "and pass on some incorrect ones."
        ),
    },
    {
        "version": "tier1_stage3_economics_v1",
        "plan_design_hash": (
            "f6878f6608002f1363982a4b38e7de719b460e34aa0c371db65aac4a93a83221"
        ),
        "superseded_before_any_economic_outcome": "true",
        "reason": (
            "claimed declared_before_survivors_were_known, which was false -- "
            "Stage 2B had already run on the VPS. It also carried a numeric "
            "horizon_ns, which cannot express next_change or next_2_changes and "
            "would have fabricated a clock for all four survivors; froze a single "
            "fee schedule that charged an exchange remove fee directly to a "
            "retail brokerage account; and had no rule for F_BAD_TS_RECV."
        ),
    },
)

NANOS_PER_MILLISECOND = 1_000_000
NANOS_PER_SECOND = 1_000_000_000

# ---------------------------------------------------------------------------
# The four frozen Stage-2 survivors
# ---------------------------------------------------------------------------

FROZEN_SURVIVORS: tuple[str, ...] = (
    "200ev|next_2_changes",
    "200ev|next_change",
    "50ev|next_2_changes",
    "50ev|next_change",
)
SURVIVOR_COUNT = len(FROZEN_SURVIVORS)
SURVIVOR_HASH = hashlib.sha256("\n".join(FROZEN_SURVIVORS).encode("utf-8")).hexdigest()

# Every survivor is event-clocked. Asserted here so that a future edit adding a
# time-cadence survivor cannot quietly re-enable duration arithmetic.
SURVIVOR_HORIZON_KIND = "changes"
SURVIVOR_HORIZONS: tuple[str, ...] = ("next_change", "next_2_changes")

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
# Event-time execution
# ---------------------------------------------------------------------------

EXECUTION_TIMING: dict[str, Any] = {
    "decision_instant": "feature_available_ts_recv",
    "entry_arrival": "feature_available_ts_recv + latency",
    "exit_signal_instant": (
        "the frozen Stage-2 event-horizon resolution, read from the label "
        "columns <prefix>_label_ts_event / <prefix>_label_ts_recv / "
        "<prefix>_available_ts_recv"
    ),
    "exit_arrival": "<prefix>_available_ts_recv + latency",
    "no_clock_horizon": (
        "next_change and next_2_changes have no duration. Stage 3 never converts "
        "a horizon into nanoseconds and never assumes one."
    ),
    "resolved_before_entry": (
        "if the target event resolves at or before the executable entry arrival, "
        "the candidate is recorded as horizon_resolved_before_entry -- a named "
        "missed opportunity, never a trade and never silently dropped"
    ),
}

# ---------------------------------------------------------------------------
# The trading rule
# ---------------------------------------------------------------------------

PRIMARY_RULE = "cost_hurdle"
SECONDARY_RULE = "discovery_decile"
TRADING_RULES: tuple[str, ...] = (PRIMARY_RULE, SECONDARY_RULE)

DISCOVERY_DECILE_QUANTILE = 0.90
TRADE_SIZE_SHARES = 100
MAX_BOOK_LEVELS_WALKED = 10

# ---------------------------------------------------------------------------
# Fee schedules -- the rates actually in force over the session window
# ---------------------------------------------------------------------------
#
# The research sessions are **June 2025**. An earlier draft froze 2026-dated
# constants, which would have priced twenty 2025 session dates against a
# schedule that did not exist yet. Worse, it carried a non-zero Section 31 rate
# through a period in which that rate was zero.

FEE_SCHEDULE_VERSION = "june-2025-historical-v1"
SESSION_WINDOW_FROM = "2025-06-01"
SESSION_WINDOW_TO = "2025-06-30"

# SEC Section 31: set to $0.00 per million effective 2025-05-14, and therefore
# zero for every June 2025 session. This is a rate that was actually in force,
# not a simplification.
SECTION_31_USD_PER_MILLION = 0.00
SECTION_31_EFFECTIVE_FROM = "2025-05-14"

# FINRA Trading Activity Fee, sale leg only.
FINRA_TAF_USD_PER_SHARE_SOLD = 0.000166
FINRA_TAF_CAP_USD_PER_TRADE = 8.30
FINRA_TAF_EFFECTIVE_FROM = "2025-01-01"

# CAT: a declared value for the stress cases, requiring verification. It is NOT
# used in the primary schedule, and the primary schedule does not claim it was
# zero -- it records that the treatment is unverified. See below.
CAT_USD_PER_SHARE_DECLARED = 0.000022

RATE_VERIFICATION_REQUIRED = True

_VERIFICATION_NOTE = (
    "rates must be confirmed against the schedules in force on each evaluated "
    "June 2025 session date before any result is relied on"
)

PRIMARY_FEE_SCHEDULE: dict[str, Any] = {
    "name": "retail_june_2025",
    "role": "primary",
    "broker": "Alpaca (commission-free US equities)",
    "schedule_version": FEE_SCHEDULE_VERSION,
    "effective_from": SESSION_WINDOW_FROM,
    "effective_to": SESSION_WINDOW_TO,
    "commission_usd_per_share": 0.0,
    "exchange_take_fee_usd_per_share": 0.0,
    "why_no_exchange_fee": (
        "a commission-free retail account is not billed the venue's per-share "
        "remove fee; the broker absorbs it in its routing economics"
    ),
    "sec_section_31_usd_per_million_sold": SECTION_31_USD_PER_MILLION,
    "sec_section_31_effective_from": SECTION_31_EFFECTIVE_FROM,
    "sec_section_31_note": (
        "zero for the whole June 2025 window because the Section 31 rate was set "
        "to $0.00 per million effective 2025-05-14"
    ),
    "finra_taf_usd_per_share_sold": FINRA_TAF_USD_PER_SHARE_SOLD,
    "finra_taf_cap_usd_per_trade": FINRA_TAF_CAP_USD_PER_TRADE,
    "finra_taf_effective_from": FINRA_TAF_EFFECTIVE_FROM,
    "cat_usd_per_share": 0.0,
    "cat_treatment_verified": False,
    "cat_note": (
        "EXCLUDED, NOT PROVEN ZERO. No June-2025 Alpaca customer fee schedule "
        "was available to establish whether CAT was passed through to retail "
        "accounts at that time. Rather than assume the convenient answer, this "
        "schedule excludes CAT and a separately named CAT-inclusive retail "
        "stress case is reported alongside it. If the primary and the CAT stress "
        "disagree about viability, the honest reading is that the question turns "
        "on an unverified fact and must be settled before deployment."
    ),
    "clearing_usd_per_share": 0.0,
    "rates_require_verification": RATE_VERIFICATION_REQUIRED,
    "verification_note": _VERIFICATION_NOTE,
}

RETAIL_CAT_STRESS_FEE_SCHEDULE: dict[str, Any] = {
    **PRIMARY_FEE_SCHEDULE,
    "name": "retail_june_2025_with_cat_passthrough",
    "role": "retail_cat_stress",
    "cat_usd_per_share": CAT_USD_PER_SHARE_DECLARED,
    "cat_treatment_verified": False,
    "cat_note": (
        "the same retail account, priced as though CAT had been passed through "
        "per share. This exists because the June-2025 customer treatment could "
        "not be verified, and it is reported separately rather than blended."
    ),
}

CONSERVATIVE_FEE_SCHEDULE: dict[str, Any] = {
    "name": "direct_member_june_2025_stress",
    "role": "conservative_stress",
    "schedule_version": FEE_SCHEDULE_VERSION,
    "effective_from": SESSION_WINDOW_FROM,
    "effective_to": SESSION_WINDOW_TO,
    "commission_usd_per_share": 0.0,
    "exchange_take_fee_usd_per_share": 0.0030,
    "why_exchange_fee": (
        "a direct member taking liquidity on XNAS pays the standard remove fee; "
        "this is the stress case, not the intended account"
    ),
    "sec_section_31_usd_per_million_sold": SECTION_31_USD_PER_MILLION,
    "sec_section_31_effective_from": SECTION_31_EFFECTIVE_FROM,
    "finra_taf_usd_per_share_sold": FINRA_TAF_USD_PER_SHARE_SOLD,
    "finra_taf_cap_usd_per_trade": FINRA_TAF_CAP_USD_PER_TRADE,
    "finra_taf_effective_from": FINRA_TAF_EFFECTIVE_FROM,
    "cat_usd_per_share": CAT_USD_PER_SHARE_DECLARED,
    "cat_treatment_verified": False,
    "clearing_usd_per_share": 0.0002,
    "rates_require_verification": RATE_VERIFICATION_REQUIRED,
    "verification_note": _VERIFICATION_NOTE,
}

FEE_SCHEDULES: dict[str, dict[str, Any]] = {
    PRIMARY_FEE_SCHEDULE["name"]: PRIMARY_FEE_SCHEDULE,
    RETAIL_CAT_STRESS_FEE_SCHEDULE["name"]: RETAIL_CAT_STRESS_FEE_SCHEDULE,
    CONSERVATIVE_FEE_SCHEDULE["name"]: CONSERVATIVE_FEE_SCHEDULE,
}
PRIMARY_FEE_SCHEDULE_NAME = PRIMARY_FEE_SCHEDULE["name"]


def assert_session_dates_covered(session_dates) -> None:
    """Refuse to price a session the frozen schedule does not cover.

    A fee schedule is only meaningful over the window it was in force. Pricing a
    2026 session against June-2025 rates, or the reverse, is the same class of
    error as using tomorrow's book to fill today's order.
    """
    outside = sorted(
        d for d in session_dates if not SESSION_WINDOW_FROM <= d <= SESSION_WINDOW_TO
    )
    if outside:
        raise ValueError(
            f"session dates outside the frozen fee window "
            f"{SESSION_WINDOW_FROM}..{SESSION_WINDOW_TO}: {outside}"
        )


# ---------------------------------------------------------------------------
# Flagged receive timestamps
# ---------------------------------------------------------------------------

F_BAD_TS_RECV = 8

BAD_TS_RECV_RULE: dict[str, Any] = {
    "flag": "F_BAD_TS_RECV (8)",
    "decision": (
        "a candidate whose timing window [decision_ts, exit_arrival] contains any "
        "record flagged F_BAD_TS_RECV is excluded as uncertifiable_timing"
    ),
    "frozen_before_any_economic_outcome": "true",
    "why_exclusion_and_not_repair": (
        "the whole of Stage 3 is an argument about when things could be known. A "
        "flagged receive timestamp is the venue telling us it does not vouch for "
        "that instant. Substituting ts_event, interpolating, or trusting it "
        "anyway would all be inventing timing, and inventing timing is the one "
        "error that silently converts a losing strategy into a winning one. "
        "Excluding costs sample size, which is visible and survivable; trusting "
        "it costs correctness, which is neither."
    ),
    "excluded_candidates_are_counted": True,
    "never_silently_dropped": True,
}

# ---------------------------------------------------------------------------
# What counts as a pass
# ---------------------------------------------------------------------------

ECONOMIC_GATES: dict[str, Any] = {
    "primary_question": (
        "does a survivor remain economically positive at 250 ms after realistic "
        "costs, on the intended-broker retail schedule?"
    ),
    "primary_statistic": "mean net return in basis points per trade",
    "primary_fee_schedule": PRIMARY_FEE_SCHEDULE_NAME,
    "must_be_positive": True,
    "inference": (
        "session-clustered t over per-session-date mean net bps, one observation "
        "per session date, exactly as Stage 2"
    ),
    "t_hurdle": 3.0,
    "false_discovery_rate": 0.10,
    "primary_family": (
        "one test per frozen survivor, at the 250 ms rung, primary rule, primary "
        "fee schedule -- four tests"
    ),
    "secondary_families": (
        "the 50 ms and 1 s rungs, the discovery-decile rule, the CAT-passthrough "
        "retail stress and the direct-member stress schedule; reported in full, "
        "corrected separately, never promoted to the primary answer"
    ),
    "minimum_trades_for_inference": 100,
    "minimum_session_dates": 4,
    "insufficient_sample_verdict": "not_authorized_insufficient_executable_sample",
    "insufficient_sample_meaning": (
        "fewer than 100 trades or fewer than 4 session dates at the 250 ms "
        "primary rung. This is NOT a negative-return finding and must never be "
        "reported as one: it means the strategy could not be executed often "
        "enough to be measured. It nonetheless fails to authorize Stage 4 or a "
        "paper deployment, because an edge that cannot be executed enough to be "
        "tested cannot be deployed on the strength of that test."
    ),
    "minima_are_frozen": (
        "these minima were declared before any economic outcome and may never be "
        "lowered afterwards; doing so would convert an unmeasurable result into "
        "an authorized one by redefinition"
    ),
    "what_a_pass_authorizes": (
        "a paper-trading deployment proposal for review. It authorizes no live "
        "order, no capital, and no real money."
    ),
}

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
    "realized_lag_ns",
    "displayed_liquidity_shares",
    "capacity_shares",
)

REPORT_BREAKDOWNS: tuple[str, ...] = (
    "by_symbol",
    "by_session_date",
    "by_latency",
    "by_fee_schedule",
)

FORBIDDEN: tuple[str, ...] = (
    "refitting any Stage-2 model or re-selecting alpha",
    "changing any Stage-1 feature, Stage-2 cell, label or horizon",
    "converting an event horizon into a numeric duration",
    "constructing a new signal or selecting features",
    "searching a trading threshold against an economic outcome",
    "promoting a secondary rung, rule or fee schedule to the primary answer",
    "trusting a receive timestamp flagged F_BAD_TS_RECV",
    "placing live or paper orders",
)

PLAN_DESIGN_ELEMENTS: tuple[str, ...] = (
    STAGE3_PLAN_VERSION,
    f"survivors={SURVIVOR_HASH}",
    *(f"latency:{name}:{ns}" for name, ns in LATENCY_RUNGS),
    f"primary_latency={PRIMARY_LATENCY}",
    *(f"rule:{rule}" for rule in TRADING_RULES),
    f"primary_rule={PRIMARY_RULE}",
    f"decile={DISCOVERY_DECILE_QUANTILE}",
    f"trade_size={TRADE_SIZE_SHARES}",
    f"max_levels={MAX_BOOK_LEVELS_WALKED}",
    "exit=event_horizon_resolution_available_ts_recv+latency",
    "no_clock_horizon=true",
    f"fee_schedule_version={FEE_SCHEDULE_VERSION}",
    f"session_window={SESSION_WINDOW_FROM}..{SESSION_WINDOW_TO}",
    f"primary_fee_schedule={PRIMARY_FEE_SCHEDULE_NAME}",
    f"sec_section_31={SECTION_31_USD_PER_MILLION}@{SECTION_31_EFFECTIVE_FROM}",
    (
        f"finra_taf={FINRA_TAF_USD_PER_SHARE_SOLD}"
        f"/cap{FINRA_TAF_CAP_USD_PER_TRADE}@{FINRA_TAF_EFFECTIVE_FROM}"
    ),
    "retail_commission=0.0",
    "retail_exchange_take_fee=0.0",
    f"cat_declared={CAT_USD_PER_SHARE_DECLARED}",
    "retail_cat_treatment=unverified_excluded_with_named_stress",
    "retail_cat_stress_schedule=retail_june_2025_with_cat_passthrough",
    "conservative_fee_schedule=direct_member_june_2025_stress",
    "bad_ts_recv=exclude_uncertifiable",
    "t_hurdle=3.0",
    "fdr=0.10",
    "min_trades=100",
    "min_session_dates=4",
    "insufficient=not_authorized_insufficient_executable_sample",
    f"prior_effective_trials={PRIOR_EFFECTIVE_TRIALS}",
    "authorizes=paper_proposal_only;not=live;not=capital",
)

PLAN_DESIGN_HASH = hashlib.sha256(
    "\n".join(PLAN_DESIGN_ELEMENTS).encode("utf-8")
).hexdigest()


def statistical_plan() -> dict[str, Any]:
    """The declared plan, for writing next to the results."""
    return {
        "stage3_plan_version": STAGE3_PLAN_VERSION,
        "superseded_plan_versions": [dict(e) for e in SUPERSEDED_PLAN_VERSIONS],
        "governance": {
            "stage2_survivors_known": True,
            "stage3_economic_outcome_viewed": False,
            "stage3_rules_frozen_before_economic_outcomes": True,
        },
        "contains_economic_result": False,
        "frozen_survivors": list(FROZEN_SURVIVORS),
        "survivor_count": SURVIVOR_COUNT,
        "survivor_hash": SURVIVOR_HASH,
        "stage2_plan_hash": STAGE2_PLAN_HASH,
        "stage2_plan_design_hash": STAGE2_PLAN_DESIGN_HASH,
        "plan_design_hash": PLAN_DESIGN_HASH,
        "latency_ladder": [
            {"name": name, "nanoseconds": ns} for name, ns in LATENCY_RUNGS
        ],
        "primary_latency": PRIMARY_LATENCY,
        "execution_timing": dict(EXECUTION_TIMING),
        "trading_rules": {
            "primary": {
                "name": PRIMARY_RULE,
                "definition": (
                    "trade only when |predicted bps| exceeds the round-trip cost "
                    "observable at decision time: half-spread on entry plus "
                    "half-spread on exit plus the schedule's per-share fees"
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
        "fee_schedules": {name: dict(s) for name, s in FEE_SCHEDULES.items()},
        "bad_ts_recv_rule": dict(BAD_TS_RECV_RULE),
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
