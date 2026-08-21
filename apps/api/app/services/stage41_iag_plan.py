"""Stage 4.1: the frozen IAG-v1 plan.

An isolated public information event occurs. Aggressive trading develops in one
direction. Displayed liquidity on the *impacted* side is consumed and fails to
replenish. The hypothesis is that information is still being assimilated.

Everything this module declares was fixed before any economic outcome existed,
and the design document it hashes is the binding statement. The module exists so
the executor cannot quietly hold a different opinion from the document: if the
document moves, the hash refuses.

Nothing here consumes a trial. The ledger reads 531 before Stage 4.1 and 531
after it; the single Stage-4.2 reveal moves it to 532.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STAGE41_PLAN_VERSION = "tier1_stage41_iag_v1"
STAGE41_AMENDMENT = "r1"

DESIGN_RELATIVE_PATH = Path("docs") / "2026-08-21-stage41-iag-v1-design.md"
DESIGN_JSON_RELATIVE_PATH = (
    Path("reports") / "tier1_stage41_design" / "v1" / "stage41_iag_v1_design.json"
)
EXPECTED_DESIGN_SHA256 = (
    "c8c85f4f4e89882c1ae01ac7f8fa301be6a11f02fc4d23128f66e4ff4d586f8b"
)
EXPECTED_DESIGN_JSON_SHA256 = (
    "4b632562022aaa1df1e617f56456cbf477df0fe67c1740198d17fe1bff0884c9"
)

REPORT_RELATIVE_DIR = Path("reports") / "tier1_stage41_iag" / "v1"
SELECTION_FILENAME = "stage41_selected_specification.json"
DIAGNOSTIC_FILENAME = "stage41_diagnostic.json"
RESULTS_FILENAME = "stage41_results.json"

EFFECTIVE_TRIALS_BEFORE = 531
EFFECTIVE_TRIALS_AFTER_DESIGN = 531
EFFECTIVE_TRIALS_AFTER_REVEAL = 532

NANOS_PER_SECOND = 1_000_000_000
NANOS_PER_MINUTE = 60 * NANOS_PER_SECOND


# ---------------------------------------------------------------------------
# Population
# ---------------------------------------------------------------------------

CERTIFIED_SYMBOLS: tuple[str, ...] = (
    "AAPL",
    "AMD",
    "CMCSA",
    "CSCO",
    "INTC",
    "MSFT",
    "NVDA",
    "TSLA",
)
CERTIFIED_SESSION_COUNT = 20
CANDIDATE_EVENT_COUNT = 502
QUIET_PERIOD_MINUTES = 60

# Deliberately absent. Stage 3.6 selected on the initial post-news move and
# found +0.41 bps gross; the state, not the jump, is the hypothesis here.
POST_NEWS_SHOCK_THRESHOLD = None


# ---------------------------------------------------------------------------
# Observation window
# ---------------------------------------------------------------------------

OBSERVATION_SECONDS = 120
OBSERVATION_NS = OBSERVATION_SECONDS * NANOS_PER_SECOND

PRIMARY_CADENCE = "200ev"
CONFIRMING_CADENCE = "50ev"
REQUIRED_CADENCES: tuple[str, ...] = (CONFIRMING_CADENCE, PRIMARY_CADENCE)

# Below these the window cannot support a four-quarter persistence test or a
# stable depth trough. Estimated density is ~96 rows at 200ev and ~384 at 50ev,
# so these floors are roughly a fifth of expectation -- loose enough not to
# discard ordinary events, tight enough to refuse a window that barely exists.
MIN_ROWS_PRIMARY = 20
MIN_ROWS_CONFIRMING = 40

PERSISTENCE_QUARTERS = 4
PERSISTENCE_QUARTER_SECONDS = OBSERVATION_SECONDS // PERSISTENCE_QUARTERS
MIN_AGREEING_QUARTERS = 3

LONG = 1
SHORT = -1


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

BASELINE_TILE_SECONDS = OBSERVATION_SECONDS
BASELINE_TILE_NS = OBSERVATION_NS
MIN_BASELINE_TILES = 500

LOW_PERCENTILE = 25.0
HIGH_PERCENTILE = 75.0
PERCENTILE_LEVELS: tuple[float, float] = (LOW_PERCENTILE, HIGH_PERCENTILE)


# ---------------------------------------------------------------------------
# Local lambda
# ---------------------------------------------------------------------------

LAMBDA_MIN_DENOMINATOR_SHARES = 100
LAMBDA_SHARE_SCALE = 1000  # report per 1,000 shares
LAMBDA_WINSORIZATION = None  # rank thresholds are robust; clipping adds a knob


# ---------------------------------------------------------------------------
# Specifications
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Specification:
    """One fully frozen qualification rule."""

    name: str
    depletion_percentile: float
    recovery_threshold: float
    min_supporting: int


SPEC_PRIMARY = Specification(
    name="IAG_v1_PRIMARY",
    depletion_percentile=25.0,
    recovery_threshold=0.25,
    min_supporting=2,
)

SPEC_FALLBACK = Specification(
    name="IAG_v1_FALLBACK",
    depletion_percentile=50.0,
    recovery_threshold=0.50,
    min_supporting=2,
)

SPECIFICATIONS: tuple[Specification, ...] = (SPEC_PRIMARY, SPEC_FALLBACK)

# The absorption disqualifier does not vary between specifications: it is not a
# supply knob, it is the statement that an absorbing market is not an
# assimilation gap.
ABSORPTION_DISQUALIFY_PERCENTILE = HIGH_PERCENTILE

MIN_EVENTS = 100
MIN_SESSIONS = 15


# ---------------------------------------------------------------------------
# Economic test (declared, not run here)
# ---------------------------------------------------------------------------

PRIMARY_HORIZON_MINUTES = 15
PRIMARY_HORIZON_NS = PRIMARY_HORIZON_MINUTES * NANOS_PER_MINUTE
SECONDARY_HORIZON_MINUTES: tuple[int, ...] = (5, 30)

# 8 bps net + a 4 bps execution allowance. Stage 3.6 measured 1.773816 bps of
# execution cost and 0.013895 of fees; doubling that for states selected *for*
# depleted liquidity and rounding up gives 4.0. Predeclared requirement, not a
# forecast.
DESIRED_NET_BPS = 8.0
EXECUTION_ALLOWANCE_BPS = 4.0
PRIMARY_GROSS_HURDLE_BPS = 12.0
STRETCH_GROSS_HURDLE_BPS = 19.0

T_HURDLE = 3.0
CONFIDENCE_LEVEL = 0.95

VERDICT_DETECTED = "IAG_gross_mechanism_detected"
VERDICT_NO_MECHANISM = "no_IAG_mechanism"
VERDICT_INSUFFICIENT = "insufficient_executable_or_statistical_sample"
VERDICTS: tuple[str, ...] = (
    VERDICT_DETECTED,
    VERDICT_NO_MECHANISM,
    VERDICT_INSUFFICIENT,
)


# ---------------------------------------------------------------------------
# Feature semantics registry
# ---------------------------------------------------------------------------
#
# Task 1 of the brief turned up five features whose names promise more than the
# implementation delivers. Recording that as prose in a document nobody reruns
# would not stop a later reader from using them; recording it here, as a
# refusal the executor consults, does.

KIND_COUNTER = "counter_resets_per_window"
KIND_SNAPSHOT = "snapshot_state_at_row"
KIND_RATIO = "ratio_within_window"


@dataclass(frozen=True, slots=True)
class FeatureSemantics:
    """What one Stage-1 feature actually measures."""

    name: str
    kind: str
    directional: bool
    aggregation: str
    note: str


# Only these may be read as carrying a side.
DIRECTIONAL_FEATURES: tuple[FeatureSemantics, ...] = (
    FeatureSemantics(
        "signed_trade_volume",
        KIND_COUNTER,
        True,
        "sum",
        "buy_aggressor_volume - sell_aggressor_volume. Positive is buy pressure. "
        "Resets each window, so summing over an interval reconstructs the "
        "interval total exactly.",
    ),
    FeatureSemantics(
        "buy_aggressor_volume",
        KIND_COUNTER,
        True,
        "sum",
        "Volume where the trade named the bid as aggressor.",
    ),
    FeatureSemantics(
        "sell_aggressor_volume",
        KIND_COUNTER,
        True,
        "sum",
        "Volume where the trade named the ask as aggressor.",
    ),
    FeatureSemantics(
        "aggressor_imbalance",
        KIND_RATIO,
        True,
        "not_aggregated",
        "signed / classified volume within one window. Not summable.",
    ),
    FeatureSemantics(
        "ask_depth_10",
        KIND_SNAPSHOT,
        True,
        "first_last_min",
        "Cumulative displayed size over the top 10 ask levels at this row.",
    ),
    FeatureSemantics(
        "bid_depth_10",
        KIND_SNAPSHOT,
        True,
        "first_last_min",
        "Cumulative displayed size over the top 10 bid levels at this row.",
    ),
)

# Present, useful, but carrying no side. Usable as regime descriptors only.
NON_DIRECTIONAL_FEATURES: tuple[FeatureSemantics, ...] = (
    FeatureSemantics(
        "midpoint",
        KIND_SNAPSHOT,
        False,
        "first_last",
        "(best_bid + best_ask) / 2 at this row. Null when the book is one-sided.",
    ),
    FeatureSemantics(
        "spread_bps",
        KIND_SNAPSHOT,
        False,
        "last",
        "Touch spread in basis points of the midpoint at this row.",
    ),
    FeatureSemantics(
        "absorption_ratio",
        KIND_RATIO,
        False,
        "volume_weighted",
        "execution_volume_without_price_move / execution_volume. High means "
        "executions left the midpoint unchanged. Unclassifiable groups count as "
        "executed but never as absorbed, so the ratio is biased DOWN -- which "
        "makes a disqualifier built on it fire less often, not more.",
    ),
    FeatureSemantics(
        "execution_intensity",
        KIND_RATIO,
        False,
        "mean",
        "execution_count / window_seconds. Already a rate.",
    ),
    FeatureSemantics(
        "cancel_volume_ratio",
        KIND_RATIO,
        False,
        "mean",
        "cancel_volume / add_volume within the window. Side-agnostic: describes "
        "a stressed book regime, never directional withdrawal.",
    ),
    FeatureSemantics(
        "execution_volume",
        KIND_COUNTER,
        False,
        "sum",
        "Volume executed in the window, settled at the native event boundary.",
    ),
    FeatureSemantics(
        "execution_volume_without_price_move",
        KIND_COUNTER,
        False,
        "sum",
        "Absorbed execution volume; numerator of absorption_ratio.",
    ),
)

# Forbidden as evidence of *directional* depletion or replenishment. Every one
# of these accumulates both book sides into a single counter.
SIDE_AGNOSTIC_FORBIDDEN_FOR_DIRECTION: tuple[str, ...] = (
    "queue_depletion_events",
    "touch_replenishment_volume",
    "touch_replenishment_events",
    "refill_after_execution_volume",
    "depletion_followed_by_quote_move",
    "cancel_add_ratio",
    "cancel_volume_ratio",
    "mean_touch_depth",
    "add_count",
    "add_volume",
    "cancel_count",
    "cancel_volume",
)

# Identically zero across all 19,484,064 Stage-1 rows.
UNUSABLE_FEATURES: tuple[str, ...] = ("modify_count",)

FEATURE_INDEX: dict[str, FeatureSemantics] = {
    entry.name: entry
    for entry in (*DIRECTIONAL_FEATURES, *NON_DIRECTIONAL_FEATURES)
}

# Columns the executor reads from the frozen feature parquet. Nothing else.
REQUIRED_FEATURE_COLUMNS: tuple[str, ...] = (
    "feature_available_ts_recv",
    "signed_trade_volume",
    "ask_depth_10",
    "bid_depth_10",
    "midpoint",
    "spread_bps",
    "absorption_ratio",
    "execution_intensity",
    "cancel_volume_ratio",
    "execution_volume",
)


def sha256_of(path: Path) -> str:
    """The hash of a file's bytes, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_frozen_design(repo_root: Path) -> dict[str, Any]:
    """Verify the design document and its machine-readable twin.

    Both are hashed. The document is the binding statement and the JSON is what
    a program can read; if they could drift apart, the program would be running
    a specification nobody approved.
    """
    verified: dict[str, Any] = {}
    for label, relative, expected in (
        ("design", DESIGN_RELATIVE_PATH, EXPECTED_DESIGN_SHA256),
        ("design_json", DESIGN_JSON_RELATIVE_PATH, EXPECTED_DESIGN_JSON_SHA256),
    ):
        path = repo_root / relative
        if not path.is_file():
            raise ValueError(f"the frozen Stage-4.1 {label} is missing at {path}")
        observed = sha256_of(path)
        if observed != expected:
            raise ValueError(
                f"the Stage-4.1 {label} has changed: {observed} != {expected}. "
                "The specification is frozen; a different document is a "
                "different experiment."
            )
        verified[label] = {"path": str(path), "sha256": observed}
    return verified


def assert_directional_use_is_permitted(feature: str) -> FeatureSemantics:
    """Refuse a side-agnostic feature being read as directional evidence.

    ``refill_after_execution_volume`` is the one that would slip past a review:
    it consults the event's side when deciding whether to increment, so it looks
    side-aware, but it accumulates into a single counter. Using it directionally
    is wrong in a way no test on its values would reveal.
    """
    if feature in UNUSABLE_FEATURES:
        raise ValueError(
            f"{feature} is identically zero across the certified batch and "
            "carries no information"
        )
    if feature in SIDE_AGNOSTIC_FORBIDDEN_FOR_DIRECTION:
        raise ValueError(
            f"{feature} accumulates both book sides into one counter, so it "
            "cannot evidence directional depletion or directional "
            "replenishment. IAG-v1 uses the side-separated depth ladders "
            "(ask_depth_10 / bid_depth_10) for that."
        )
    entry = FEATURE_INDEX.get(feature)
    if entry is None:
        raise ValueError(
            f"{feature} has no declared Stage-4.1 semantics; declare what it "
            "measures before using it"
        )
    if not entry.directional:
        raise ValueError(
            f"{feature} is a regime descriptor, not a directional measurement: "
            f"{entry.note}"
        )
    return entry


def impacted_depth_column(direction: int) -> str:
    """The side the informed flow is consuming.

    Buy pressure lifts offers, so the ask ladder is the impacted side; sell
    pressure hits bids.
    """
    if direction == LONG:
        return "ask_depth_10"
    if direction == SHORT:
        return "bid_depth_10"
    raise ValueError(
        f"direction {direction!r} is neither +1 nor -1; there is no impacted "
        "side without an unambiguous direction"
    )


def statistical_plan() -> dict[str, Any]:
    """The declared plan, emitted before anything is measured."""
    return {
        "stage41_plan_version": STAGE41_PLAN_VERSION,
        "amendment": STAGE41_AMENDMENT,
        "contains_strategy_outcome": False,
        "contains_post_decision_return": False,
        "contains_pnl": False,
        "effective_trials_before": EFFECTIVE_TRIALS_BEFORE,
        "effective_trials_after": EFFECTIVE_TRIALS_AFTER_DESIGN,
        "stage_4_2_reveal_moves_ledger_to": EFFECTIVE_TRIALS_AFTER_REVEAL,
        "authorizes_paper_or_live": False,
        "population": {
            "symbols": list(CERTIFIED_SYMBOLS),
            "sessions": CERTIFIED_SESSION_COUNT,
            "candidate_events": CANDIDATE_EVENT_COUNT,
            "quiet_period_minutes": QUIET_PERIOD_MINUTES,
            "post_news_shock_threshold": POST_NEWS_SHOCK_THRESHOLD,
        },
        "observation_window": {
            "seconds": OBSERVATION_SECONDS,
            "cadences": list(REQUIRED_CADENCES),
            "min_rows_200ev": MIN_ROWS_PRIMARY,
            "min_rows_50ev": MIN_ROWS_CONFIRMING,
        },
        "direction": {
            "statistic": "sum of signed_trade_volume over window rows",
            "cadences_must_agree": list(REQUIRED_CADENCES),
            "persistence_quarters": PERSISTENCE_QUARTERS,
            "min_agreeing_quarters": MIN_AGREEING_QUARTERS,
        },
        "baseline": {
            "tile_seconds": BASELINE_TILE_SECONDS,
            "min_tiles": MIN_BASELINE_TILES,
            "percentile_levels": list(PERCENTILE_LEVELS),
        },
        "specifications": [
            {
                "name": spec.name,
                "depletion_percentile": spec.depletion_percentile,
                "recovery_threshold": spec.recovery_threshold,
                "min_supporting": spec.min_supporting,
            }
            for spec in SPECIFICATIONS
        ],
        "selection_rule": (
            "PRIMARY if its counts clear both floors; else FALLBACK if its "
            "counts clear both floors; else insufficient sample and no "
            "economic run. Counts only -- no outcome enters the ladder."
        ),
        "sample_floors": {"min_events": MIN_EVENTS, "min_sessions": MIN_SESSIONS},
        "economic_test": {
            "primary_horizon_minutes": PRIMARY_HORIZON_MINUTES,
            "secondary_diagnostic_horizons": list(SECONDARY_HORIZON_MINUTES),
            "outcome_name": "gross_directional_midpoint_displacement_bps",
            "is_pnl": False,
            "secondary_may_rescue_primary": False,
        },
        "hurdle": {
            "desired_net_bps": DESIRED_NET_BPS,
            "execution_allowance_bps": EXECUTION_ALLOWANCE_BPS,
            "primary_gross_hurdle_bps": PRIMARY_GROSS_HURDLE_BPS,
            "stretch_gross_hurdle_bps": STRETCH_GROSS_HURDLE_BPS,
            "t_hurdle": T_HURDLE,
            "is_expected_return": False,
        },
        "verdicts": list(VERDICTS),
        "feature_semantics": {
            "directional": [f.name for f in DIRECTIONAL_FEATURES],
            "regime_descriptors": [f.name for f in NON_DIRECTIONAL_FEATURES],
            "forbidden_as_directional": list(SIDE_AGNOSTIC_FORBIDDEN_FOR_DIRECTION),
            "unusable": list(UNUSABLE_FEATURES),
        },
    }
