"""Stage 4.1 IAG-v2: the frozen raw-MBO plan.

IAG-v1 failed at measurement, not at economics: its 120-second windows held a
median of 8 emitted feature rows against a floor of 20. v2 measures the same
information state at the raw event resolution the venue actually published.

The economic specification is unchanged. What changes is where the numbers come
from -- and, because raw ``A``/``C``/``M`` records carry a validated resting
side, v2 can measure directional liquidity that v1's aggregated counters
provably could not.

Nothing here consumes a trial. The ledger reads 531 before and 531 after; only
the Stage-4.2 reveal would move it to 532.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STAGE41_V2_PLAN_VERSION = "tier1_stage41_iag_v2_raw_mbo"

DESIGN_RELATIVE_PATH = Path("docs") / "2026-08-21-stage41-iag-v2-raw-mbo-design.md"
DESIGN_JSON_RELATIVE_PATH = (
    Path("reports") / "tier1_stage41_design" / "v2" / "stage41_iag_v2_design.json"
)
EXPECTED_DESIGN_SHA256 = (
    "bb7a3954fc6facae0275690292541b9ce6df09f22a78a51702a221c20aa5be36"
)
EXPECTED_DESIGN_JSON_SHA256 = (
    "84575be7a5c0b421636107c780b88cf2771bb785bac0ff0b3ddac36429e0dd5c"
)

REPORT_RELATIVE_DIR = Path("reports") / "tier1_stage41_iag_v2" / "v1"
SELECTION_FILENAME = "stage41_v2_selected_specification.json"
DIAGNOSTIC_FILENAME = "stage41_v2_diagnostic.json"
RESULTS_FILENAME = "stage41_v2_results.json"
PROBE_FILENAME = "stage41_v2_timing_probe.json"

EFFECTIVE_TRIALS_BEFORE = 531
EFFECTIVE_TRIALS_AFTER_DESIGN = 531
EFFECTIVE_TRIALS_AFTER_REVEAL = 532

NANOS_PER_SECOND = 1_000_000_000
NANOS_PER_MINUTE = 60 * NANOS_PER_SECOND


# ---------------------------------------------------------------------------
# IAG-v1, formally retired
# ---------------------------------------------------------------------------
#
# Recorded here so a later reader cannot mistake v2 for a confirmation of v1.
# The June-2025 sample has been used before; v2 is exploratory.

IAG_V1_VERDICT = "insufficient_executable_or_statistical_sample"
IAG_V1_CAUSE = "feature_resolution_mismatch_insufficient_qualifying_supply"
IAG_V1_ECONOMIC_OUTCOME_VIEWED = False
IAG_V2_IS_CONFIRMATION_OF_V1 = False


# ---------------------------------------------------------------------------
# Population -- unchanged from v1
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
QUIET_PERIOD_NS = QUIET_PERIOD_MINUTES * NANOS_PER_MINUTE

POST_NEWS_SHOCK_THRESHOLD = None


# ---------------------------------------------------------------------------
# Observation window -- unchanged from v1
# ---------------------------------------------------------------------------

OBSERVATION_SECONDS = 120
OBSERVATION_NS = OBSERVATION_SECONDS * NANOS_PER_SECOND

PERSISTENCE_QUARTERS = 4
PERSISTENCE_QUARTER_SECONDS = OBSERVATION_SECONDS // PERSISTENCE_QUARTERS
PERSISTENCE_QUARTER_NS = PERSISTENCE_QUARTER_SECONDS * NANOS_PER_SECOND
MIN_AGREEING_QUARTERS = 3

LONG = 1
SHORT = -1

# The whole point of v2. v1 required >=20 emitted 200ev rows and >=40 50ev rows;
# those belonged to the feature-cadence representation and caused the resolution
# mismatch. No raw-record or trade minimum replaces them: the only count-like
# requirement is three signable trades, which the 3-of-4 persistence rule
# already implies and which is therefore not a separate, tunable gate.
MIN_ROWS_REQUIREMENT_REMOVED = True
MIN_RAW_RECORD_REQUIREMENT = None
MIN_TRADE_REQUIREMENT = None

# Depth ladder, exactly as frozen in Stage 1.
DEPTH_LEVELS = 10


# ---------------------------------------------------------------------------
# Raw-record semantics
# ---------------------------------------------------------------------------
#
# Read from mbo_book_validator and mbo_feature_engine, not inferred from field
# names. The trade/fill distinction is the one that matters: Stage-1 v1 signed
# both and was wrong twice over -- double counted, and inverted, because a
# fill's side is the resting side.

TRADE_SIDE_MEANS = "aggressor"
FILL_SIDE_MEANS = "resting_side_opposite_of_aggressor"
FILL_IS_SIGNED = False
ACM_SIDE_MEANS = "resting_order_side"

# Any record in a native-event group containing a T or an F is execution-driven.
# Counting its book update as a voluntary cancellation would count one execution
# twice: once as aggressive flow and again as a withdrawal.
EXECUTION_GROUP_MARKER_ACTIONS: tuple[str, ...] = ("T", "F")

# Side-agnostic Stage-1 outputs that may never stand in for directional
# evidence. v1 had no alternative; v2 does.
FORBIDDEN_AS_DIRECTIONAL_EVIDENCE: tuple[str, ...] = (
    "queue_depletion_events",
    "touch_replenishment_volume",
    "touch_replenishment_events",
    "refill_after_execution_volume",
    "depletion_followed_by_quote_move",
    "cancel_add_ratio",
    "cancel_volume_ratio",
    "mean_touch_depth",
)


# ---------------------------------------------------------------------------
# Raw quality gates -- replacing v1's row floors
# ---------------------------------------------------------------------------

GATE_INITIALIZATION = "uncertified_initialization"
GATE_COVERAGE = "incomplete_raw_observation_coverage"
GATE_TIMING = "uncertifiable_timing_flag_in_window"
GATE_RECONSTRUCTION = "fatal_reconstruction_defect"
GATE_ONE_SIDED = "one_sided_or_missing_book_state"
GATE_NO_COHERENT_STATE = "no_coherent_flast_state_in_window"

QUALITY_GATES: tuple[str, ...] = (
    GATE_INITIALIZATION,
    GATE_COVERAGE,
    GATE_TIMING,
    GATE_RECONSTRUCTION,
    GATE_ONE_SIDED,
    GATE_NO_COHERENT_STATE,
)

CERTIFIED_INITIALIZATIONS: tuple[str, ...] = ("formal_snapshot", "known_empty_clear")


# ---------------------------------------------------------------------------
# Baseline -- unchanged from v1
# ---------------------------------------------------------------------------

BASELINE_TILE_SECONDS = OBSERVATION_SECONDS
BASELINE_TILE_NS = OBSERVATION_NS
MIN_BASELINE_TILES = 500

LOW_PERCENTILE = 25.0
HIGH_PERCENTILE = 75.0
FALLBACK_PERCENTILE = 50.0
PERCENTILE_LEVELS: tuple[float, float] = (LOW_PERCENTILE, HIGH_PERCENTILE)


# ---------------------------------------------------------------------------
# Local lambda -- unchanged from v1
# ---------------------------------------------------------------------------

LAMBDA_MIN_DENOMINATOR_SHARES = 100
LAMBDA_SHARE_SCALE = 1000
LAMBDA_WINSORIZATION = None


# ---------------------------------------------------------------------------
# Specifications and the deterministic ladder -- unchanged from v1
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Specification:
    """One fully frozen qualification rule."""

    name: str
    depletion_percentile: float
    recovery_threshold: float
    min_supporting: int


SPEC_PRIMARY = Specification(
    name="IAG_v2_PRIMARY",
    depletion_percentile=LOW_PERCENTILE,
    recovery_threshold=0.25,
    min_supporting=2,
)

SPEC_FALLBACK = Specification(
    name="IAG_v2_FALLBACK",
    depletion_percentile=FALLBACK_PERCENTILE,
    recovery_threshold=0.50,
    min_supporting=2,
)

SPECIFICATIONS: tuple[Specification, ...] = (SPEC_PRIMARY, SPEC_FALLBACK)

ABSORPTION_DISQUALIFY_PERCENTILE = HIGH_PERCENTILE

MIN_EVENTS = 100
MIN_SESSIONS = 15


# ---------------------------------------------------------------------------
# Economic test -- declared, never run from this stage
# ---------------------------------------------------------------------------

PRIMARY_HORIZON_MINUTES = 15
PRIMARY_HORIZON_NS = PRIMARY_HORIZON_MINUTES * NANOS_PER_MINUTE
SECONDARY_HORIZON_MINUTES: tuple[int, ...] = (5, 30)

DESIRED_NET_BPS = 8.0
EXECUTION_ALLOWANCE_BPS = 4.0
PRIMARY_GROSS_HURDLE_BPS = 12.0
STRETCH_GROSS_HURDLE_BPS = 19.0

T_HURDLE = 3.0

VERDICT_DETECTED = "IAG_gross_mechanism_detected"
VERDICT_NO_MECHANISM = "no_IAG_mechanism"
VERDICT_INSUFFICIENT = "insufficient_executable_or_statistical_sample"
VERDICTS: tuple[str, ...] = (
    VERDICT_DETECTED,
    VERDICT_NO_MECHANISM,
    VERDICT_INSUFFICIENT,
)


def sha256_of(path: Path) -> str:
    """The hash of a file's bytes, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_frozen_design(repo_root: Path) -> dict[str, Any]:
    """Verify the design document and its machine-readable twin."""
    verified: dict[str, Any] = {}
    for label, relative, expected in (
        ("design", DESIGN_RELATIVE_PATH, EXPECTED_DESIGN_SHA256),
        ("design_json", DESIGN_JSON_RELATIVE_PATH, EXPECTED_DESIGN_JSON_SHA256),
    ):
        path = repo_root / relative
        if not path.is_file():
            raise ValueError(f"the frozen Stage-4.1 v2 {label} is missing at {path}")
        observed = sha256_of(path)
        if observed != expected:
            raise ValueError(
                f"the Stage-4.1 v2 {label} has changed: {observed} != {expected}. "
                "The specification is frozen; a different document is a "
                "different experiment."
            )
        verified[label] = {"path": str(path), "sha256": observed}
    return verified


def impacted_side(direction: int) -> str:
    """The book side the informed flow is consuming.

    Buy pressure lifts offers, so the ask ladder is impacted; sell pressure hits
    bids. Returns the Databento side code so it can be compared against a
    record's own ``side`` without a second mapping.
    """
    if direction == LONG:
        return "A"
    if direction == SHORT:
        return "B"
    raise ValueError(
        f"direction {direction!r} is neither +1 nor -1; there is no impacted "
        "side without an unambiguous direction"
    )


def assert_not_side_agnostic(feature: str) -> None:
    """Refuse a Stage-1 side-agnostic counter standing in for directional evidence."""
    if feature in FORBIDDEN_AS_DIRECTIONAL_EVIDENCE:
        raise ValueError(
            f"{feature} accumulates both book sides into one counter and cannot "
            "evidence directional depletion or replenishment. IAG-v2 measures "
            "the impacted side directly from the reconstructed book."
        )


def statistical_plan() -> dict[str, Any]:
    """The declared plan, emitted before anything is measured."""
    return {
        "stage41_v2_plan_version": STAGE41_V2_PLAN_VERSION,
        "contains_strategy_outcome": False,
        "contains_post_decision_return": False,
        "contains_pnl": False,
        "effective_trials_before": EFFECTIVE_TRIALS_BEFORE,
        "effective_trials_after": EFFECTIVE_TRIALS_AFTER_DESIGN,
        "stage_4_2_reveal_would_move_ledger_to": EFFECTIVE_TRIALS_AFTER_REVEAL,
        "authorizes_paper_or_live": False,
        "iag_v1_retirement": {
            "verdict": IAG_V1_VERDICT,
            "cause": IAG_V1_CAUSE,
            "economic_outcome_viewed": IAG_V1_ECONOMIC_OUTCOME_VIEWED,
            "v2_is_confirmation_of_v1": IAG_V2_IS_CONFIRMATION_OF_V1,
            "v2_class": "new_exploratory_measurement_specification",
        },
        "population": {
            "symbols": list(CERTIFIED_SYMBOLS),
            "sessions": CERTIFIED_SESSION_COUNT,
            "candidate_events": CANDIDATE_EVENT_COUNT,
            "quiet_period_minutes": QUIET_PERIOD_MINUTES,
            "post_news_shock_threshold": POST_NEWS_SHOCK_THRESHOLD,
        },
        "observation_window": {
            "seconds": OBSERVATION_SECONDS,
            "persistence_quarters": PERSISTENCE_QUARTERS,
            "quarter_seconds": PERSISTENCE_QUARTER_SECONDS,
            "min_agreeing_quarters": MIN_AGREEING_QUARTERS,
            "min_raw_record_requirement": MIN_RAW_RECORD_REQUIREMENT,
            "min_trade_requirement": MIN_TRADE_REQUIREMENT,
        },
        "state_selection_rule": {
            "S_of_t": "state at the latest coherent F_LAST with ts_recv <= t",
            "nearest_in_time": False,
            "state_after_t_permitted": False,
            "fail_closed_when_undefined_or_one_sided": True,
        },
        "raw_semantics": {
            "trade_side_means": TRADE_SIDE_MEANS,
            "fill_side_means": FILL_SIDE_MEANS,
            "fill_is_signed": FILL_IS_SIGNED,
            "acm_side_means": ACM_SIDE_MEANS,
            "execution_group_marker_actions": list(EXECUTION_GROUP_MARKER_ACTIONS),
            "depth_levels": DEPTH_LEVELS,
        },
        "quality_gates": list(QUALITY_GATES),
        "certified_initializations": list(CERTIFIED_INITIALIZATIONS),
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
        "forbidden_as_directional_evidence": list(FORBIDDEN_AS_DIRECTIONAL_EVIDENCE),
    }
