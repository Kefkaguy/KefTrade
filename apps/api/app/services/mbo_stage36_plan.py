"""Stage 3.6: the frozen news-triggered L3 consensus plan.

Every value here is transcribed from
``docs/2026-08-19-stage36-news-l3-consensus-design.md``, which is frozen and
hashed. Nothing in this module chooses anything: if the design is silent on a
point, the correct behaviour is to refuse, not to pick the reading that makes
the experiment easier to pass.

## What is being asked

Stage 3 closed the standalone market-taking question. Stage 3.5 asked whether
the same four predictors could time an order that had to execute anyway. Stage
3.6 asks a third thing:

> Does a *news-triggered state*, combined with agreement across all four frozen
> L3 predictors, identify executable five-minute trades with materially larger
> net expectancy?

The news event supplies the trigger; the four models supply the direction. The
initial 30-second price move supplies **neither** -- it is recorded as a
diagnostic and is forbidden from selecting or sizing anything, because a rule
keyed to it would be a threshold chosen after the shock distribution had been
inspected.

## Why this is exploratory and cannot be otherwise

The June 2025 batch has already produced viewed outcomes at Stage 2, Stage 3 and
Stage 3.5. No statistic computed on it can be confirmatory however it is
corrected. The ledger stands at 530 effective trials; this experiment is one
fresh primary specification and moves it to 531 the moment its economic outcome
is exposed -- pass or fail, because a look is a look.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.services.mbo_stage3_plan import (
    FEE_SCHEDULES,
    MAX_BOOK_LEVELS_WALKED,
    PRIMARY_FEE_SCHEDULE_NAME,
)

STAGE36_PLAN_VERSION = "tier1_stage36_news_l3_consensus_v1"

# ---------------------------------------------------------------------------
# The frozen design document
# ---------------------------------------------------------------------------

DESIGN_RELATIVE_PATH = "docs/2026-08-19-stage36-news-l3-consensus-design.md"
EXPECTED_DESIGN_SHA256 = (
    "9b7ea99ece2b37365d2aaec910b29e2e0f2c74e886099857ae34e2dd38cdf3e4"
)

PREOUTCOME_RELATIVE_DIR = "reports/tier1_stage36_preoutcome/v1"
MANIFEST_FILENAME = "stage36_preoutcome_manifest.json"
EXPECTED_MANIFEST_SHA256 = (
    "7e1aebdb112e70a3b4915848d18c80db36b200f7577b05cfd9915121b8bfda17"
)

# Keyed by the manifest's own ``files`` keys, so a renamed key is a mismatch
# rather than a silently skipped check.
EXPECTED_CSV_SHA256: dict[str, str] = {
    "news_events": "1dddce6aecc3de83eccf441bad0681a4366c2541fa3b1e4ad8a37bbdea59a1b4",
    "shock_census": "543011c691a1e38237c4ec57721c3d4de2e1a03479be7f4a5f6792fb10f26c41",
    "consensus_census": (
        "571eadb42f67e374f35cdcf4f4c9a1898fac44218fdbc158ac88790c0dd6f276"
    ),
}
CSV_FILENAMES: dict[str, str] = {
    "news_events": "stage36_news_events.csv",
    "shock_census": "stage36_30s_shock_census.csv",
    "consensus_census": "stage36_l3_consensus_preoutcome.csv",
}

# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

SYMBOLS: tuple[str, ...] = (
    "AAPL",
    "AMD",
    "CMCSA",
    "CSCO",
    "INTC",
    "MSFT",
    "NVDA",
    "TSLA",
)
EXPECTED_SESSION_COUNT = 20

# ---------------------------------------------------------------------------
# News semantics
# ---------------------------------------------------------------------------

NEWS_TIMESTAMP_FIELD = "known_at"
FORBIDDEN_NEWS_TIMESTAMP_FIELD = "received_at"
STORY_IDENTITY = "COALESCE(content_hash, article_id)"
QUIET_PERIOD_MINUTES = 60
SESSION_WINDOW_ET = ("09:30:00", "15:54:29")

NEWS_RULES: dict[str, Any] = {
    "event_timestamp": NEWS_TIMESTAMP_FIELD,
    "forbidden_timestamp": FORBIDDEN_NEWS_TIMESTAMP_FIELD,
    "why_forbidden": (
        "received_at is later backfill ingestion, not when the market could "
        "have known"
    ),
    "audit": "known_at == updated_at across all 115,613 inspected news rows",
    "story_identity": STORY_IDENTITY,
    "quiet_period_minutes": QUIET_PERIOD_MINUTES,
    "quiet_period_scope": (
        "all previous same-symbol news, including premarket and otherwise "
        "non-tradable stories"
    ),
    "quiet_period_uses_future_price": False,
    "session_window_et": list(SESSION_WINDOW_ET),
}

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

NANOS_PER_MILLISECOND = 1_000_000
NANOS_PER_SECOND = 1_000_000_000

DECISION_DELAY_NS = 30 * NANOS_PER_SECOND          # t0 -> td
LATENCY_NS = 250 * NANOS_PER_MILLISECOND           # request -> arrival
HOLDING_NS = 5 * 60 * NANOS_PER_SECOND             # entry request -> exit request

TIMING: dict[str, Any] = {
    "t0": "known_at",
    "td": "t0 + 30 seconds",
    "entry_request": "td",
    "entry_arrival": "td + 250ms",
    "exit_request": "td + 5 minutes",
    "exit_arrival": "td + 5 minutes + 250ms",
    "arrival_to_arrival_holding": "exactly 5 minutes",
    "decision_delay_ns": DECISION_DELAY_NS,
    "latency_ns": LATENCY_NS,
    "holding_ns": HOLDING_NS,
}

# ---------------------------------------------------------------------------
# The four frozen predictors
# ---------------------------------------------------------------------------

FROZEN_CELLS: tuple[str, ...] = (
    "200ev|next_2_changes",
    "200ev|next_change",
    "50ev|next_2_changes",
    "50ev|next_change",
)
CELL_COUNT = len(FROZEN_CELLS)

PREDICTION_SELECTION: dict[str, Any] = {
    "rule": (
        "for each cell, the LATEST finite prediction whose "
        "feature_available_ts_recv lies in [t0, td]"
    ),
    "stale_before_t0": "forbidden",
    "why": (
        "a prediction formed before the news was knowable is not a reaction to "
        "it, and one formed after td would not have been available at the "
        "decision instant"
    ),
    "reconstruction": "Stage-3.5's exact per-date out-of-sample reconstruction",
    "refit": False,
    "model_selection": (
        "forbidden -- all four cells participate; no previously better cell may "
        "be preferred"
    ),
}

# ---------------------------------------------------------------------------
# Consensus
# ---------------------------------------------------------------------------

CONSENSUS_4_OF_4 = "4_of_4"
CONSENSUS_3_OF_4 = "3_of_4"
CONSENSUS_2_VS_2 = "2_vs_2"
CONSENSUS_INCOMPLETE = "incomplete"

STRONG_CONSENSUS: tuple[str, ...] = (CONSENSUS_4_OF_4, CONSENSUS_3_OF_4)

CONSENSUS_RULES: dict[str, Any] = {
    "requires_all_four_directional": True,
    "4_same_sign": "trade that direction",
    "3_versus_1": "trade the majority direction",
    "2_versus_2": "no trade",
    "any_unavailable": "no trade",
    "any_zero": "no trade",
    "initial_price_direction_selects_direction": False,
    "continuation_versus_reversal": "diagnostic only",
}

# ---------------------------------------------------------------------------
# The frozen pre-outcome counts
# ---------------------------------------------------------------------------

EXPECTED_COUNTS: dict[str, int] = {
    "news_events": 259,
    "shock_rows": 259,
    "consensus_rows": 259,
    CONSENSUS_4_OF_4: 147,
    CONSENSUS_3_OF_4: 21,
    CONSENSUS_2_VS_2: 81,
    CONSENSUS_INCOMPLETE: 10,
    "strong_consensus": 168,
}
MEASURED_EVENTS = EXPECTED_COUNTS["news_events"]
STRONG_CONSENSUS_CANDIDATES = EXPECTED_COUNTS["strong_consensus"]

# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

TRADE_SIZE_SHARES = 100
LONG = 1
SHORT = -1

EXECUTION: dict[str, Any] = {
    "trade_size_shares": TRADE_SIZE_SHARES,
    "both_legs": "marketable",
    "long": {"entry": "consumes asks", "exit": "consumes bids"},
    "short": {"entry": "consumes bids", "exit": "consumes asks"},
    "displayed_liquidity_only": True,
    "max_displayed_levels": MAX_BOOK_LEVELS_WALKED,
    "insufficient_liquidity": "execution failure",
    "never_invent_a_worse_fill": True,
    "book_semantics": "the certified Stage-3 BookReplay and MBO reconstruction",
    "book_visibility": "records with ts_recv <= t only",
}

CERTIFICATION: dict[str, Any] = {
    "raw_source": "resolved through the existing Stage-1 manifests",
    "raw_hash_verified": True,
    "arrivals_inside_receive_coverage": True,
    "bad_ts_recv": "F_BAD_TS_RECV contamination in the timing interval fails closed",
    "stale_eof_book": "forbidden",
}

# ---------------------------------------------------------------------------
# Fees and the economic formula
# ---------------------------------------------------------------------------

PRIMARY_FEE_SCHEDULE = PRIMARY_FEE_SCHEDULE_NAME

FEES: dict[str, Any] = {
    "primary_fee_schedule": PRIMARY_FEE_SCHEDULE,
    "available_schedules": list(FEE_SCHEDULES),
    "new_assumptions_introduced": False,
}

ECONOMIC_FORMULA: dict[str, str] = {
    "realized_return_bps": "s * (exit_fill - entry_fill) / entry_fill * 10000",
    "primary_net_return_bps": "realized_return_bps - primary_fees_bps",
    "s": "+1 for LONG, -1 for SHORT",
}

# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

MIN_EXECUTABLE_TRADES = 100
MIN_SESSIONS = 15
T_HURDLE = 3.0
PRIMARY_TARGET_BPS = 5.0
STRETCH_TARGET_BPS = 8.0

VERDICT_INSUFFICIENT = "not_authorized_insufficient_executable_sample"
VERDICT_SUPPORTED = "news_l3_5bps_mechanism_supported_exploratory"
VERDICT_NO_MECHANISM = "no_5bps_news_l3_mechanism"

GATES: dict[str, Any] = {
    "minimum_executable_trades": MIN_EXECUTABLE_TRADES,
    "minimum_sessions": MIN_SESSIONS,
    "insufficient_verdict": VERDICT_INSUFFICIENT,
    "inference": "clustered by trading session",
    "t_hurdle": T_HURDLE,
    "primary_target_bps": PRIMARY_TARGET_BPS,
    "stretch_target_bps": STRETCH_TARGET_BPS,
    "primary_success_verdict": VERDICT_SUPPORTED,
    "failure_verdict": VERDICT_NO_MECHANISM,
    "primary_specifications": 1,
    "multiple_testing_branches": False,
}

ALLOWED_DIAGNOSTICS: tuple[str, ...] = (
    "gross midpoint return",
    "fill-to-fill return",
    "execution cost",
    "spread/book cost",
    "fees",
    "levels walked",
    "failure counts",
    "per-day net return",
    "per-symbol net return",
    "4/4 versus 3/4",
    "continuation versus reversal",
    "30-second shock bins",
)

FORBIDDEN_POST_OUTCOME_ADAPTATION: tuple[str, ...] = (
    "symbols",
    "observation interval",
    "holding horizon",
    "consensus threshold",
    "shock threshold",
    "latency",
    "quiet period",
    "trade size",
    "fee assumptions",
)

# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------

PRIOR_EFFECTIVE_TRIALS = 530
FRESH_PRIMARY_SPECIFICATIONS = 1
EFFECTIVE_TRIALS_AFTER_OUTCOME = 531

GOVERNANCE: dict[str, Any] = {
    "evidence_class": "exploratory mechanism development",
    "confirmatory": False,
    "why_not_confirmatory": (
        "the June 2025 batch has already produced viewed outcomes at Stage 2, "
        "Stage 3 and Stage 3.5; no statistic computed on it can be confirmatory"
    ),
    "prior_effective_trials": PRIOR_EFFECTIVE_TRIALS,
    "fresh_primary_specifications": FRESH_PRIMARY_SPECIFICATIONS,
    "effective_trials_after_outcome": EFFECTIVE_TRIALS_AFTER_OUTCOME,
    "ledger_advances": "when the economic outcome is exposed, pass or fail",
    "authorizes_paper_or_live": False,
    "a_pass_authorizes": "an untouched external confirmation experiment, only",
}

# The design's own elements, hashed so that a transcription drift in this module
# is detectable independently of the document hash.
PLAN_DESIGN_ELEMENTS: tuple[str, ...] = (
    STAGE36_PLAN_VERSION,
    f"design_sha256={EXPECTED_DESIGN_SHA256}",
    f"manifest_sha256={EXPECTED_MANIFEST_SHA256}",
    *(f"csv:{k}={v}" for k, v in sorted(EXPECTED_CSV_SHA256.items())),
    f"symbols={'+'.join(SYMBOLS)}",
    f"sessions={EXPECTED_SESSION_COUNT}",
    f"news_timestamp={NEWS_TIMESTAMP_FIELD}",
    f"quiet_period_minutes={QUIET_PERIOD_MINUTES}",
    f"session_window={SESSION_WINDOW_ET[0]}-{SESSION_WINDOW_ET[1]}",
    f"decision_delay_ns={DECISION_DELAY_NS}",
    f"latency_ns={LATENCY_NS}",
    f"holding_ns={HOLDING_NS}",
    *(f"cell:{c}" for c in FROZEN_CELLS),
    "consensus=4of4_or_3of4_trade;2v2_no;zero_no;unavailable_no",
    "shock_threshold=none",
    *(f"count:{k}={v}" for k, v in sorted(EXPECTED_COUNTS.items())),
    f"trade_size={TRADE_SIZE_SHARES}",
    f"max_levels={MAX_BOOK_LEVELS_WALKED}",
    f"fee_schedule={PRIMARY_FEE_SCHEDULE}",
    f"min_trades={MIN_EXECUTABLE_TRADES}",
    f"min_sessions={MIN_SESSIONS}",
    f"t_hurdle={T_HURDLE}",
    f"primary_bps={PRIMARY_TARGET_BPS}",
    f"stretch_bps={STRETCH_TARGET_BPS}",
    f"prior_effective_trials={PRIOR_EFFECTIVE_TRIALS}",
    f"effective_trials_after_outcome={EFFECTIVE_TRIALS_AFTER_OUTCOME}",
    "authorizes=external_confirmation_only;not=paper;not=live",
)

PLAN_DESIGN_HASH = hashlib.sha256(
    "\n".join(PLAN_DESIGN_ELEMENTS).encode("utf-8")
).hexdigest()


def sha256_of(path: Path) -> str:
    """Hash a file's exact bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_frozen_design(repo_root: Path) -> dict[str, str]:
    """Refuse to do anything substantive against a design that has moved.

    The document is the specification. Transcribing it into this module does not
    make the module authoritative, so the bytes are checked rather than trusted.
    """
    design = repo_root / DESIGN_RELATIVE_PATH
    if not design.is_file():
        raise ValueError(f"the frozen Stage-3.6 design is missing at {design}")
    observed = sha256_of(design)
    if observed != EXPECTED_DESIGN_SHA256:
        raise ValueError(
            f"the Stage-3.6 design has changed: {observed} != "
            f"{EXPECTED_DESIGN_SHA256}. The specification is frozen; a different "
            "document is a different experiment."
        )
    return {"path": str(design), "sha256": observed}


def statistical_plan() -> dict[str, Any]:
    """The declared plan, carrying no economic outcome."""
    return {
        "stage36_plan_version": STAGE36_PLAN_VERSION,
        "plan_design_hash": PLAN_DESIGN_HASH,
        "design_sha256": EXPECTED_DESIGN_SHA256,
        "manifest_sha256": EXPECTED_MANIFEST_SHA256,
        "contains_economic_outcome": False,
        "research_question": (
            "can a news-triggered state combined with the four frozen L3 "
            "predictors identify executable five-minute long/short trades with "
            "materially larger net expectancy?"
        ),
        "symbols": list(SYMBOLS),
        "expected_session_count": EXPECTED_SESSION_COUNT,
        "news_rules": dict(NEWS_RULES),
        "timing": dict(TIMING),
        "frozen_cells": list(FROZEN_CELLS),
        "prediction_selection": dict(PREDICTION_SELECTION),
        "consensus_rules": dict(CONSENSUS_RULES),
        "expected_counts": dict(EXPECTED_COUNTS),
        "execution": dict(EXECUTION),
        "certification": dict(CERTIFICATION),
        "fees": dict(FEES),
        "economic_formula": dict(ECONOMIC_FORMULA),
        "gates": dict(GATES),
        "allowed_diagnostics": list(ALLOWED_DIAGNOSTICS),
        "forbidden_post_outcome_adaptation": list(FORBIDDEN_POST_OUTCOME_ADAPTATION),
        "governance": dict(GOVERNANCE),
    }
