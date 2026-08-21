"""Stage 4.1 IAG-v1 executor: qualification, selection, and the gated reveal.

Two halves, deliberately separated. Everything up to and including specification
selection is **outcome-blind**: it reads the observation window `[t0, t0+120s]`
and nothing later, so `diagnose` can exercise all of it. The economic reveal
lives in exactly one function, ``gross_directional_displacement_bps``, which is
the only place a midpoint after ``t_obs_end`` is ever read.

That separation is the whole governance story. A structural test asserts the
qualification path never calls the reveal, so the boundary is checkable rather
than merely intended.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from app.services.stage41_iag_plan import (
    ABSORPTION_DISQUALIFY_PERCENTILE,
    BASELINE_TILE_NS,
    CONFIRMING_CADENCE,
    HIGH_PERCENTILE,
    LAMBDA_MIN_DENOMINATOR_SHARES,
    LAMBDA_SHARE_SCALE,
    LONG,
    MIN_AGREEING_QUARTERS,
    MIN_BASELINE_TILES,
    MIN_EVENTS,
    MIN_ROWS_CONFIRMING,
    MIN_ROWS_PRIMARY,
    MIN_SESSIONS,
    OBSERVATION_NS,
    PERSISTENCE_QUARTERS,
    PRIMARY_CADENCE,
    PRIMARY_GROSS_HURDLE_BPS,
    REQUIRED_FEATURE_COLUMNS,
    SHORT,
    SPEC_FALLBACK,
    SPEC_PRIMARY,
    T_HURDLE,
    VERDICT_DETECTED,
    VERDICT_INSUFFICIENT,
    VERDICT_NO_MECHANISM,
    Specification,
    impacted_depth_column,
    sha256_of,
)

STAGE41_EXECUTOR_VERSION = "tier1_stage41_iag_executor_v1"

BPS = 10_000.0

# Why an event produced no qualifying state. Counted, never silently dropped:
# the distribution of refusals is the most informative outcome-blind output
# this stage has.
FAIL_THIN_WINDOW = "insufficient_rows_in_observation_window"
FAIL_AMBIGUOUS_DIRECTION = "ambiguous_or_non_persistent_direction"
FAIL_THIN_BASELINE = "insufficient_causal_baseline"
FAIL_NO_DEPLETION = "impacted_side_not_depleted"
FAIL_REPLENISHED = "impacted_side_replenished"
FAIL_ABSORBED = "market_absorbing_not_assimilating"
FAIL_SUPPORT = "too_few_supporting_stress_conditions"

FAILURE_REASONS: tuple[str, ...] = (
    FAIL_THIN_WINDOW,
    FAIL_AMBIGUOUS_DIRECTION,
    FAIL_THIN_BASELINE,
    FAIL_NO_DEPLETION,
    FAIL_REPLENISHED,
    FAIL_ABSORBED,
    FAIL_SUPPORT,
)


# ---------------------------------------------------------------------------
# Window reduction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WindowStats:
    """One 120-second interval reduced to the statistics IAG-v1 needs.

    The same reduction serves an event window and a baseline tile, so an event
    is always compared against like-for-like. Counters are summed because they
    reset per emission; snapshots are taken first/last/min because summing them
    would be meaningless.
    """

    rows: int
    net_flow: float
    first_ts: int
    last_ts: int
    ask_depth_first: float
    ask_depth_last: float
    ask_depth_min: float
    bid_depth_first: float
    bid_depth_last: float
    bid_depth_min: float
    midpoint_first: float | None
    midpoint_last: float | None
    spread_bps_last: float | None
    absorption_ratio: float | None
    execution_intensity: float | None
    cancel_volume_ratio: float | None

    def depth_first(self, direction: int) -> float:
        return self.ask_depth_first if direction == LONG else self.bid_depth_first

    def depth_last(self, direction: int) -> float:
        return self.ask_depth_last if direction == LONG else self.bid_depth_last

    def depth_min(self, direction: int) -> float:
        return self.ask_depth_min if direction == LONG else self.bid_depth_min


def _finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def reduce_window(columns: dict[str, np.ndarray], order: np.ndarray) -> WindowStats:
    """Reduce a set of feature rows, ordered by the availability clock.

    ``order`` fixes the sequence explicitly rather than trusting file order, so
    "first" and "last" mean earliest and latest *available*, which is what the
    causality argument depends on.
    """
    available = columns["feature_available_ts_recv"][order]
    flow = columns["signed_trade_volume"][order]
    ask = columns["ask_depth_10"][order].astype(float)
    bid = columns["bid_depth_10"][order].astype(float)
    mid = columns["midpoint"][order].astype(float)
    spread = columns["spread_bps"][order].astype(float)
    absorption = columns["absorption_ratio"][order].astype(float)
    intensity = columns["execution_intensity"][order].astype(float)
    cancels = columns["cancel_volume_ratio"][order].astype(float)
    volume = columns["execution_volume"][order].astype(float)

    finite_mid = np.isfinite(mid)
    # Volume-weighted, because absorption is a property of executed volume and
    # an unweighted mean would let a quiet window outvote a busy one.
    weighted = np.isfinite(absorption) & (volume > 0)
    absorption_value = (
        float(np.sum(absorption[weighted] * volume[weighted]) / np.sum(volume[weighted]))
        if weighted.any()
        else None
    )

    def _last_finite(values: np.ndarray) -> float | None:
        finite = np.isfinite(values)
        return float(values[finite][-1]) if finite.any() else None

    def _mean_finite(values: np.ndarray) -> float | None:
        finite = np.isfinite(values)
        return float(values[finite].mean()) if finite.any() else None

    return WindowStats(
        rows=len(order),
        net_flow=float(np.sum(flow)),
        first_ts=int(available[0]),
        last_ts=int(available[-1]),
        ask_depth_first=float(ask[0]),
        ask_depth_last=float(ask[-1]),
        ask_depth_min=float(np.min(ask)),
        bid_depth_first=float(bid[0]),
        bid_depth_last=float(bid[-1]),
        bid_depth_min=float(np.min(bid)),
        midpoint_first=float(mid[finite_mid][0]) if finite_mid.any() else None,
        midpoint_last=float(mid[finite_mid][-1]) if finite_mid.any() else None,
        spread_bps_last=_last_finite(spread),
        absorption_ratio=absorption_value,
        execution_intensity=_mean_finite(intensity),
        cancel_volume_ratio=_mean_finite(cancels),
    )


def window_rows(
    columns: dict[str, np.ndarray], *, start_ns: int, end_ns: int
) -> np.ndarray:
    """Indices of rows available inside ``[start_ns, end_ns]``, in clock order.

    Both ends inclusive, as the design writes them, and sorted by availability
    so an out-of-order file cannot change which row counts as first or last.
    """
    available = columns["feature_available_ts_recv"]
    inside = np.flatnonzero((available >= start_ns) & (available <= end_ns))
    if inside.size == 0:
        return inside
    return inside[np.argsort(available[inside], kind="stable")]


# ---------------------------------------------------------------------------
# Direction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DirectionVerdict:
    """Whether persistent aggressive flow named a side, and why not if it did not."""

    direction: int | None
    net_flow_primary: float
    net_flow_confirming: float
    agreeing_quarters: int
    quarter_signs: tuple[int, ...]
    reason: str | None

    @property
    def is_unambiguous(self) -> bool:
        return self.direction is not None


def _sign(value: float) -> int:
    if value > 0:
        return LONG
    if value < 0:
        return SHORT
    return 0


def resolve_direction(
    primary: dict[str, np.ndarray],
    confirming: dict[str, np.ndarray],
    *,
    t0_ns: int,
    t_obs_end_ns: int,
) -> DirectionVerdict:
    """Direction from post-news order flow, never from price and never from news.

    Three independent clauses, all of which must hold. Net flow must name a side
    on both certified cadences and they must agree, and the side must persist
    across the window rather than arriving in one burst -- a single large print
    in an otherwise balanced two minutes is not "persistent aggressive flow",
    and net-sign alone cannot tell the two apart.
    """
    primary_rows = window_rows(primary, start_ns=t0_ns, end_ns=t_obs_end_ns)
    confirming_rows = window_rows(confirming, start_ns=t0_ns, end_ns=t_obs_end_ns)

    net_primary = float(np.sum(primary["signed_trade_volume"][primary_rows]))
    net_confirming = float(np.sum(confirming["signed_trade_volume"][confirming_rows]))
    sign_primary = _sign(net_primary)
    sign_confirming = _sign(net_confirming)

    quarter_signs = _quarter_signs(primary, t0_ns=t0_ns, t_obs_end_ns=t_obs_end_ns)

    if sign_primary == 0 or sign_confirming == 0:
        return DirectionVerdict(
            None, net_primary, net_confirming, 0, quarter_signs, "zero_net_flow"
        )
    if sign_primary != sign_confirming:
        return DirectionVerdict(
            None, net_primary, net_confirming, 0, quarter_signs, "cadence_disagreement"
        )

    agreeing = sum(1 for s in quarter_signs if s == sign_primary)
    if agreeing < MIN_AGREEING_QUARTERS:
        return DirectionVerdict(
            None,
            net_primary,
            net_confirming,
            agreeing,
            quarter_signs,
            "not_persistent",
        )
    return DirectionVerdict(
        sign_primary, net_primary, net_confirming, agreeing, quarter_signs, None
    )


def _quarter_signs(
    primary: dict[str, np.ndarray], *, t0_ns: int, t_obs_end_ns: int
) -> tuple[int, ...]:
    """Net flow sign in each of the four 30-second quarters.

    A quarter with no flow, or with exactly balanced flow, returns 0 and does
    not agree with anything -- silence is not confirmation.
    """
    span = t_obs_end_ns - t0_ns
    edge = span // PERSISTENCE_QUARTERS
    signs: list[int] = []
    for index in range(PERSISTENCE_QUARTERS):
        start = t0_ns + index * edge
        # Last quarter closes on t_obs_end inclusive; earlier ones are
        # half-open, so no row is counted in two quarters.
        end = t_obs_end_ns if index == PERSISTENCE_QUARTERS - 1 else start + edge - 1
        rows = window_rows(primary, start_ns=start, end_ns=end)
        signs.append(_sign(float(np.sum(primary["signed_trade_volume"][rows]))))
    return tuple(signs)


# ---------------------------------------------------------------------------
# Local lambda
# ---------------------------------------------------------------------------


def local_lambda(stats: WindowStats, direction: int) -> float | None:
    """Directional price displacement per 1,000 shares of same-direction flow.

    Closed window: both midpoints come from rows available no later than
    ``t_obs_end``. Nothing after the observation cutoff enters this, which is
    what keeps it a state variable rather than a peek at the outcome.

    Fail closed on every degenerate case. An undefined lambda leaves supporting
    condition S3 unsatisfied; it never disqualifies the event by itself, because
    "we could not measure impact" is not evidence that impact was low.
    """
    if stats.midpoint_first is None or stats.midpoint_last is None:
        return None
    if stats.midpoint_first <= 0:
        return None

    denominator = direction * stats.net_flow
    if denominator < LAMBDA_MIN_DENOMINATOR_SHARES:
        return None

    displacement_bps = (
        direction
        * (stats.midpoint_last - stats.midpoint_first)
        / stats.midpoint_first
        * BPS
    )
    value = LAMBDA_SHARE_SCALE * displacement_bps / denominator
    return value if math.isfinite(value) else None


# ---------------------------------------------------------------------------
# Causal baseline
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Baseline:
    """Per-symbol distributions of the qualification statistics, prior-only.

    Built by tiling prior data into 120-second windows and reducing each with
    the same function an event window uses. A distribution assembled any other
    way would not be a distribution of the thing being scored.
    """

    symbol: str
    tiles: int
    samples: dict[str, np.ndarray]

    def percentile_of(self, statistic: str, value: float | None) -> float | None:
        """Where ``value`` sits in the prior distribution, as a percentile."""
        if value is None or not math.isfinite(value):
            return None
        sample = self.samples.get(statistic)
        if sample is None or sample.size == 0:
            return None
        return float(np.searchsorted(sample, value, side="right") / sample.size * 100.0)

    @property
    def is_sufficient(self) -> bool:
        return self.tiles >= MIN_BASELINE_TILES

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "tiles": self.tiles,
            "sufficient": self.is_sufficient,
            "min_tiles_required": MIN_BASELINE_TILES,
            "statistics": sorted(self.samples),
        }


BASELINE_STATISTICS: tuple[str, ...] = (
    "ask_depth_last",
    "bid_depth_last",
    "spread_bps_last",
    "absorption_ratio",
    "execution_intensity",
    "cancel_volume_ratio",
    "lambda_long",
    "lambda_short",
)


def build_baseline(
    symbol: str, tiles: Sequence[WindowStats]
) -> Baseline:
    """Sort each statistic once, so percentile lookup is a binary search."""
    collected: dict[str, list[float]] = {name: [] for name in BASELINE_STATISTICS}
    for tile in tiles:
        collected["ask_depth_last"].append(tile.ask_depth_last)
        collected["bid_depth_last"].append(tile.bid_depth_last)
        for name, value in (
            ("spread_bps_last", tile.spread_bps_last),
            ("absorption_ratio", tile.absorption_ratio),
            ("execution_intensity", tile.execution_intensity),
            ("cancel_volume_ratio", tile.cancel_volume_ratio),
            ("lambda_long", local_lambda(tile, LONG)),
            ("lambda_short", local_lambda(tile, SHORT)),
        ):
            if value is not None and math.isfinite(value):
                collected[name].append(float(value))
    return Baseline(
        symbol=symbol,
        tiles=len(tiles),
        samples={
            name: np.sort(np.asarray(values, dtype=float))
            for name, values in collected.items()
        },
    )


def tile_prior_window(
    columns: dict[str, np.ndarray], *, start_ns: int, end_ns: int
) -> list[WindowStats]:
    """Cut ``[start_ns, end_ns)`` into non-overlapping 120-second tiles.

    Tiles carrying fewer rows than an event window would need are dropped, so
    the baseline describes windows comparable to the one being scored rather
    than a mixture of dense and near-empty intervals.
    """
    tiles: list[WindowStats] = []
    edge = start_ns
    while edge + BASELINE_TILE_NS <= end_ns:
        order = window_rows(columns, start_ns=edge, end_ns=edge + BASELINE_TILE_NS - 1)
        if order.size >= MIN_ROWS_PRIMARY:
            tiles.append(reduce_window(columns, order))
        edge += BASELINE_TILE_NS
    return tiles


# ---------------------------------------------------------------------------
# Qualification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EventState:
    """Everything IAG-v1 measures about one event. No outcome, by construction."""

    symbol: str
    session_date: str
    story_id: str
    t0_ns: int
    t_obs_end_ns: int
    rows_primary: int
    rows_confirming: int
    direction: int | None
    direction_reason: str | None
    agreeing_quarters: int
    net_flow_primary: float
    net_flow_confirming: float
    depth_percentile: float | None
    recovery: float | None
    absorption_percentile: float | None
    lambda_value: float | None
    lambda_percentile: float | None
    spread_percentile: float | None
    intensity_percentile: float | None
    cancel_percentile: float | None
    baseline_tiles: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "session_date": self.session_date,
            "story_id": self.story_id,
            "rows_200ev": self.rows_primary,
            "rows_50ev": self.rows_confirming,
            "direction": self.direction,
            "direction_reason": self.direction_reason,
            "agreeing_quarters": self.agreeing_quarters,
            "depth_percentile": self.depth_percentile,
            "recovery": self.recovery,
            "absorption_percentile": self.absorption_percentile,
            "lambda_percentile": self.lambda_percentile,
            "spread_percentile": self.spread_percentile,
            "intensity_percentile": self.intensity_percentile,
            "cancel_percentile": self.cancel_percentile,
            "baseline_tiles": self.baseline_tiles,
        }


def measure_event(
    *,
    symbol: str,
    session_date: str,
    story_id: str,
    t0_ns: int,
    primary: dict[str, np.ndarray],
    confirming: dict[str, np.ndarray],
    baseline: Baseline,
) -> EventState:
    """Reduce one event to its state. Reads nothing after ``t_obs_end``."""
    t_obs_end_ns = t0_ns + OBSERVATION_NS
    primary_order = window_rows(primary, start_ns=t0_ns, end_ns=t_obs_end_ns)
    confirming_order = window_rows(confirming, start_ns=t0_ns, end_ns=t_obs_end_ns)

    verdict = resolve_direction(
        primary, confirming, t0_ns=t0_ns, t_obs_end_ns=t_obs_end_ns
    )

    depth_percentile = recovery = lambda_value = lambda_percentile = None
    absorption_percentile = spread_percentile = None
    intensity_percentile = cancel_percentile = None

    if primary_order.size:
        stats = reduce_window(primary, primary_order)
        absorption_percentile = baseline.percentile_of(
            "absorption_ratio", stats.absorption_ratio
        )
        spread_percentile = baseline.percentile_of(
            "spread_bps_last", stats.spread_bps_last
        )
        intensity_percentile = baseline.percentile_of(
            "execution_intensity", stats.execution_intensity
        )
        cancel_percentile = baseline.percentile_of(
            "cancel_volume_ratio", stats.cancel_volume_ratio
        )
        if verdict.direction is not None:
            column = impacted_depth_column(verdict.direction)
            depth_percentile = baseline.percentile_of(
                "ask_depth_last" if column == "ask_depth_10" else "bid_depth_last",
                stats.depth_last(verdict.direction),
            )
            recovery = _recovery(stats, verdict.direction)
            lambda_value = local_lambda(stats, verdict.direction)
            lambda_percentile = baseline.percentile_of(
                "lambda_long" if verdict.direction == LONG else "lambda_short",
                lambda_value,
            )

    return EventState(
        symbol=symbol,
        session_date=session_date,
        story_id=story_id,
        t0_ns=t0_ns,
        t_obs_end_ns=t_obs_end_ns,
        rows_primary=int(primary_order.size),
        rows_confirming=int(confirming_order.size),
        direction=verdict.direction,
        direction_reason=verdict.reason,
        agreeing_quarters=verdict.agreeing_quarters,
        net_flow_primary=verdict.net_flow_primary,
        net_flow_confirming=verdict.net_flow_confirming,
        depth_percentile=depth_percentile,
        recovery=recovery,
        absorption_percentile=absorption_percentile,
        lambda_value=lambda_value,
        lambda_percentile=lambda_percentile,
        spread_percentile=spread_percentile,
        intensity_percentile=intensity_percentile,
        cancel_percentile=cancel_percentile,
        baseline_tiles=baseline.tiles,
    )


def _recovery(stats: WindowStats, direction: int) -> float:
    """How much of the impacted side's drawdown was refilled by window end.

    Returns 1.0 when the side never drew down, which correctly fails the weak-
    replenishment test: liquidity that never fell cannot have failed to return.
    """
    first = stats.depth_first(direction)
    last = stats.depth_last(direction)
    trough = stats.depth_min(direction)
    if first <= trough:
        return 1.0
    return (last - trough) / (first - trough)


def qualifies(state: EventState, spec: Specification) -> tuple[bool, str | None, int]:
    """Apply one frozen specification. Returns (qualified, failure reason, support)."""
    if state.rows_primary < MIN_ROWS_PRIMARY or state.rows_confirming < MIN_ROWS_CONFIRMING:
        return False, FAIL_THIN_WINDOW, 0
    if state.baseline_tiles < MIN_BASELINE_TILES:
        return False, FAIL_THIN_BASELINE, 0
    if state.direction is None:
        return False, FAIL_AMBIGUOUS_DIRECTION, 0

    # M2 -- the impacted side is depleted relative to its own history.
    if state.depth_percentile is None or state.depth_percentile > spec.depletion_percentile:
        return False, FAIL_NO_DEPLETION, 0
    # M3 -- and it did not come back.
    if state.recovery is None or state.recovery > spec.recovery_threshold:
        return False, FAIL_REPLENISHED, 0
    # M4 -- an absorbing market is the opposite of an assimilation gap.
    if (
        state.absorption_percentile is not None
        and state.absorption_percentile >= ABSORPTION_DISQUALIFY_PERCENTILE
    ):
        return False, FAIL_ABSORBED, 0

    support = supporting_count(state)
    if support < spec.min_supporting:
        return False, FAIL_SUPPORT, support
    return True, None, support


def supporting_count(state: EventState) -> int:
    """How many of the four stress conditions hold.

    S1 is general cancellation / liquidity-withdrawal stress. It describes a
    stressed book regime, not directional withdrawal: the counters behind it
    accumulate both sides, so calling it directional would be false.
    """
    conditions = (
        state.cancel_percentile,  # S1 general withdrawal stress
        state.spread_percentile,  # S2 spread stress
        state.lambda_percentile,  # S3 elevated local impact
        state.intensity_percentile,  # S4 event intensity
    )
    return sum(
        1 for value in conditions if value is not None and value >= HIGH_PERCENTILE
    )


# ---------------------------------------------------------------------------
# Specification selection -- counts only
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpecificationCounts:
    """Outcome-blind eligibility of one specification."""

    name: str
    events: int
    sessions: int
    symbols: int
    failures: dict[str, int]

    @property
    def clears_floors(self) -> bool:
        return self.events >= MIN_EVENTS and self.sessions >= MIN_SESSIONS

    def as_dict(self) -> dict[str, Any]:
        return {
            "specification": self.name,
            "eligible_events": self.events,
            "distinct_sessions": self.sessions,
            "distinct_symbols": self.symbols,
            "clears_floors": self.clears_floors,
            "min_events": MIN_EVENTS,
            "min_sessions": MIN_SESSIONS,
            "failure_reasons": dict(sorted(self.failures.items())),
        }


def count_specification(
    states: Sequence[EventState], spec: Specification
) -> SpecificationCounts:
    """Eligibility counts for one specification. No outcome is read."""
    events: list[EventState] = []
    failures: dict[str, int] = {reason: 0 for reason in FAILURE_REASONS}
    for state in states:
        ok, reason, _support = qualifies(state, spec)
        if ok:
            events.append(state)
        elif reason is not None:
            failures[reason] = failures.get(reason, 0) + 1
    return SpecificationCounts(
        name=spec.name,
        events=len(events),
        sessions=len({e.session_date for e in events}),
        symbols=len({e.symbol for e in events}),
        failures=failures,
    )


def select_specification(
    states: Sequence[EventState],
) -> dict[str, Any]:
    """The frozen deterministic ladder. Counts only; no outcome enters it.

    PRIMARY is evaluated first. FALLBACK is evaluated **only** if PRIMARY misses
    a floor, and if it also misses, no economic run happens at all. Exactly one
    specification can ever reach Stage 4.2, which is what keeps this a single
    primary trial.
    """
    primary = count_specification(states, SPEC_PRIMARY)
    if primary.clears_floors:
        return {
            "selected_specification": SPEC_PRIMARY.name,
            "selection_basis": "PRIMARY cleared both floors",
            "primary": primary.as_dict(),
            "fallback": None,
            "fallback_evaluated": False,
            "economic_run_authorized": True,
            "verdict_if_no_run": None,
        }

    fallback = count_specification(states, SPEC_FALLBACK)
    if fallback.clears_floors:
        return {
            "selected_specification": SPEC_FALLBACK.name,
            "selection_basis": (
                "PRIMARY missed a declared floor; FALLBACK cleared both"
            ),
            "primary": primary.as_dict(),
            "fallback": fallback.as_dict(),
            "fallback_evaluated": True,
            "economic_run_authorized": True,
            "verdict_if_no_run": None,
        }

    return {
        "selected_specification": None,
        "selection_basis": "neither specification cleared both declared floors",
        "primary": primary.as_dict(),
        "fallback": fallback.as_dict(),
        "fallback_evaluated": True,
        "economic_run_authorized": False,
        "verdict_if_no_run": VERDICT_INSUFFICIENT,
    }


def specification_by_name(name: str) -> Specification:
    for spec in (SPEC_PRIMARY, SPEC_FALLBACK):
        if spec.name == name:
            return spec
    raise ValueError(
        f"{name!r} is not a declared Stage-4.1 specification; only "
        f"{SPEC_PRIMARY.name} and {SPEC_FALLBACK.name} exist"
    )


def write_selection(payload: dict[str, Any], path: Path) -> str:
    """Persist the selection and return its hash.

    The run re-verifies this. A selection that could be edited between diagnose
    and run would let the specification change after its counts were known,
    which is the one thing the ladder exists to prevent.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return sha256_of(path)


def read_selection(path: Path, *, expected_sha256: str | None = None) -> dict[str, Any]:
    """Re-read the persisted selection, refusing if it moved."""
    if not path.is_file():
        raise ValueError(
            f"no persisted Stage-4.1 specification selection at {path}. Run "
            "diagnose first: the economic run may only evaluate a specification "
            "that was selected from outcome-blind counts."
        )
    observed = sha256_of(path)
    if expected_sha256 is not None and observed != expected_sha256:
        raise ValueError(
            f"the persisted specification selection has changed: {observed} != "
            f"{expected_sha256}. The selection is frozen once diagnose records it."
        )
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# THE ECONOMIC REVEAL -- the only function that reads past t_obs_end
# ---------------------------------------------------------------------------


def gross_directional_displacement_bps(
    *,
    direction: int,
    midpoint_at_decision: float,
    midpoint_at_horizon: float,
) -> float:
    """Gross directional midpoint displacement, in basis points.

    ``D * (mid(t_horizon) - mid(t_decision)) / mid(t_decision) * 10_000``.

    This is **not** P&L and not executable profit: no spread is crossed, no fee
    is charged, no fill is modelled and no position is sized. It is the raw
    displacement the direction pointed at.

    Deliberately not called "abnormal": no benchmark-adjustment or market-model
    formula is frozen for Stage 4.1, so there is no baseline against which
    anything here could be called abnormal.

    This is the single function in the module that touches a price after
    ``t_obs_end``. Everything the qualification path needs is available before
    it, and a structural test asserts the qualification path never calls it.
    """
    if midpoint_at_decision <= 0:
        raise ValueError(
            "the decision midpoint is not positive; the displacement is "
            "undefined and will not be guessed"
        )
    if direction not in (LONG, SHORT):
        raise ValueError(f"direction {direction!r} is neither +1 nor -1")
    return (
        direction
        * (midpoint_at_horizon - midpoint_at_decision)
        / midpoint_at_decision
        * BPS
    )


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


def session_clustered_inference(
    displacements: Sequence[float], sessions: Sequence[str]
) -> dict[str, Any]:
    """Day-clustered mean, t and 95% interval.

    Events cluster by trading day -- one piece of news moves a stock for the
    rest of the session -- so treating events as independent would overstate
    precision by roughly the square root of the events-per-day.
    """
    from app.services.mbo_stage3_executor import clustered_t

    if len(displacements) != len(sessions):
        raise ValueError("displacements and sessions must align one-to-one")
    if not displacements:
        raise ValueError("no displacements to summarise")

    values = np.asarray(displacements, dtype=float)

    # ``clustered_t`` takes ONE observation per session -- collapsing first is
    # what makes it clustered. Passing event-level values would treat every
    # event as independent and overstate precision by roughly the square root
    # of the events per day.
    by_session: dict[str, list[float]] = {}
    for value, session in zip(values, sessions, strict=True):
        by_session.setdefault(session, []).append(float(value))
    distinct = sorted(by_session)
    session_means = np.asarray(
        [float(np.mean(by_session[session])) for session in distinct], dtype=float
    )

    statistic, p_value = clustered_t(list(session_means))

    session_mean = float(session_means.mean())
    half_width: float | None = None
    if session_means.size >= 2:
        standard_error = float(
            session_means.std(ddof=1) / math.sqrt(session_means.size)
        )
        half_width = _t_critical(session_means.size - 1) * standard_error

    return {
        "events": int(values.size),
        "distinct_sessions": len(distinct),
        # The headline the hurdle judges: mean over eligible events, matching
        # how Stage 3.6 reported its own mean.
        "mean_gross_bps": float(values.mean()),
        "median_gross_bps": float(np.median(values)),
        # What the t and the interval actually describe.
        "session_mean_gross_bps": session_mean,
        "session_clustered_t": float(statistic) if statistic is not None else None,
        "session_clustered_p": float(p_value) if p_value is not None else None,
        "ci95_low_bps": session_mean - half_width if half_width is not None else None,
        "ci95_high_bps": session_mean + half_width if half_width is not None else None,
        "ci95_describes": "session_mean_gross_bps",
        "clustering": "trading_session",
        "events_per_session": {s: len(v) for s, v in sorted(by_session.items())},
    }


def _t_critical(degrees_of_freedom: int) -> float:
    """Two-sided 95% critical value. Small table, no SciPy dependency."""
    table = {
        1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
        8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160,
        14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093,
        20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
        26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
    }
    if degrees_of_freedom < 1:
        raise ValueError("at least two sessions are needed for an interval")
    return table.get(degrees_of_freedom, 1.960)


def decide_verdict(inference: dict[str, Any]) -> dict[str, Any]:
    """The frozen pass/fail rule, applied to the primary horizon only."""
    events = inference["events"]
    sessions = inference["distinct_sessions"]
    if events < MIN_EVENTS or sessions < MIN_SESSIONS:
        return {
            "verdict": VERDICT_INSUFFICIENT,
            "because": (
                f"{events} events over {sessions} sessions is below the declared "
                f"floor of {MIN_EVENTS} events and {MIN_SESSIONS} sessions"
            ),
            "authorizes": None,
        }
    statistic = inference["session_clustered_t"]
    if statistic is None:
        # Fewer than two sessions, or a session-mean sequence with no dispersion.
        # "We could not compute significance" is not significance.
        return {
            "verdict": VERDICT_NO_MECHANISM,
            "because": (
                "the session-clustered t is undefined, so the displacement "
                "cannot be distinguished from zero"
            ),
            "authorizes": None,
        }
    meets_size = inference["mean_gross_bps"] >= PRIMARY_GROSS_HURDLE_BPS
    meets_t = statistic >= T_HURDLE
    if meets_size and meets_t:
        return {
            "verdict": VERDICT_DETECTED,
            "because": (
                f"mean gross displacement cleared the {PRIMARY_GROSS_HURDLE_BPS} bps "
                f"hurdle with clustered t >= {T_HURDLE}"
            ),
            "authorizes": "stage_4_3_execution_simulation_only",
        }
    return {
        "verdict": VERDICT_NO_MECHANISM,
        "because": (
            f"hurdle {PRIMARY_GROSS_HURDLE_BPS} bps met: {meets_size}; "
            f"t >= {T_HURDLE} met: {meets_t}"
        ),
        "authorizes": None,
    }


def load_feature_columns(path: Path) -> dict[str, np.ndarray]:
    """Read only the columns IAG-v1 declares. Nothing else is loaded."""
    import pyarrow.parquet as pq

    table = pq.read_table(path, columns=list(REQUIRED_FEATURE_COLUMNS))
    return {
        name: np.asarray(table.column(name).to_numpy(zero_copy_only=False))
        for name in REQUIRED_FEATURE_COLUMNS
    }


def feature_path(features_dir: Path, symbol: str, session_date: str, cadence: str) -> Path:
    return features_dir / cadence / f"{symbol}_{session_date}.{cadence}.parquet"


__all__ = [
    "CONFIRMING_CADENCE",
    "FAILURE_REASONS",
    "PRIMARY_CADENCE",
    "STAGE41_EXECUTOR_VERSION",
    "Baseline",
    "DirectionVerdict",
    "EventState",
    "SpecificationCounts",
    "WindowStats",
    "build_baseline",
    "count_specification",
    "decide_verdict",
    "feature_path",
    "gross_directional_displacement_bps",
    "load_feature_columns",
    "local_lambda",
    "measure_event",
    "qualifies",
    "read_selection",
    "reduce_window",
    "resolve_direction",
    "select_specification",
    "session_clustered_inference",
    "specification_by_name",
    "supporting_count",
    "tile_prior_window",
    "window_rows",
    "write_selection",
]
