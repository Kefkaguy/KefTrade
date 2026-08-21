"""Stage 4.1 IAG-v2 executor: one streaming pass over raw MBO.

The scanner walks a symbol-day's records exactly once, maintains a reconstructed
``MboBook``, and captures state **only at completed ``F_LAST`` records**. Because
the pass proceeds in non-decreasing ``ts_recv``, the state-selection rule falls
out of the traversal: ``S(t)`` is simply the last coherent state recorded when
the first record with ``ts_recv > t`` arrives. It is frozen at that instant and
never revisited, so a later state cannot leak backwards into an earlier reading.

The economic reveal lives in one function,
``gross_directional_displacement_bps``, and it is the only place a midpoint after
``t_obs_end`` is ever read. A structural test asserts the qualification path
never calls it.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app.services.mbo_book_validator import (
    ACTION_ADD,
    ACTION_CANCEL,
    ACTION_FILL,
    ACTION_MODIFY,
    ACTION_TRADE,
    F_BAD_TS_RECV,
    F_LAST,
    F_MAYBE_BAD_BOOK,
    F_SNAPSHOT,
    FIXED_PRICE_SCALE,
    SIDE_ASK,
    SIDE_BID,
    MboBook,
)
from app.services.stage41_iag_v2_plan import (
    ABSORPTION_DISQUALIFY_PERCENTILE,
    BASELINE_TILE_NS,
    DEPTH_LEVELS,
    GATE_COVERAGE,
    GATE_NO_COHERENT_STATE,
    GATE_ONE_SIDED,
    GATE_TIMING,
    HIGH_PERCENTILE,
    LAMBDA_MIN_DENOMINATOR_SHARES,
    LAMBDA_SHARE_SCALE,
    LONG,
    MIN_AGREEING_QUARTERS,
    MIN_BASELINE_TILES,
    MIN_EVENTS,
    MIN_SESSIONS,
    OBSERVATION_NS,
    PERSISTENCE_QUARTER_NS,
    PERSISTENCE_QUARTERS,
    PRIMARY_GROSS_HURDLE_BPS,
    SHORT,
    SPEC_FALLBACK,
    SPEC_PRIMARY,
    T_HURDLE,
    VERDICT_DETECTED,
    VERDICT_INSUFFICIENT,
    VERDICT_NO_MECHANISM,
    Specification,
    impacted_side,
    sha256_of,
)

STAGE41_V2_EXECUTOR_VERSION = "tier1_stage41_iag_v2_executor_v1"

BPS = 10_000.0

FAIL_GATE = "raw_quality_gate"
FAIL_AMBIGUOUS_DIRECTION = "ambiguous_or_non_persistent_direction"
FAIL_THIN_BASELINE = "insufficient_causal_baseline"
FAIL_NO_DEPLETION = "impacted_side_not_depleted"
FAIL_REPLENISHED = "impacted_side_replenished"
FAIL_ABSORBED = "market_absorbing_not_assimilating"
FAIL_SUPPORT = "too_few_supporting_stress_conditions"

FAILURE_REASONS: tuple[str, ...] = (
    FAIL_GATE,
    FAIL_AMBIGUOUS_DIRECTION,
    FAIL_THIN_BASELINE,
    FAIL_NO_DEPLETION,
    FAIL_REPLENISHED,
    FAIL_ABSORBED,
    FAIL_SUPPORT,
)

AMBIGUITY_ZERO_FLOW = "zero_net_flow"
AMBIGUITY_NOT_PERSISTENT = "not_persistent"
AMBIGUITY_NO_SIGNABLE_TRADE = "no_signable_trade"


# ---------------------------------------------------------------------------
# Coherent state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoherentState:
    """The book as it stood at one completed ``F_LAST``.

    Captured nowhere else. Inside a native event the touch is transient, and
    Stage 1 learned twice what reading it there costs.
    """

    ts_recv: int
    bid_depth: float
    ask_depth: float
    midpoint: float | None
    spread_bps: float | None

    @property
    def is_two_sided(self) -> bool:
        return self.midpoint is not None

    def depth_for(self, side: str) -> float:
        return self.ask_depth if side == SIDE_ASK else self.bid_depth


def capture_state(book: MboBook, ts_recv: int) -> CoherentState:
    """Read the top-10 ladders and the touch from a settled book.

    Sums the sorted level structures directly rather than going through
    ``book.depth()``. That helper builds a ``PriceLevel`` and then a dict --
    including a float division and a ``round`` for display -- for each of the
    twenty levels, and this is called at every coherent boundary across ~562
    million records, where it is the single dominant cost of the whole pass.

    The quantity is identical: summed displayed size over the first ten price
    levels of each side, exactly as the frozen ``{bid,ask}_depth_10`` define it.
    """
    bids = book.bids
    asks = book.asks

    bid_depth = 0
    if bids:
        for orders in bids.values()[-DEPTH_LEVELS:]:
            for order in orders:
                bid_depth += order.size
    ask_depth = 0
    if asks:
        for orders in asks.values()[:DEPTH_LEVELS]:
            for order in orders:
                ask_depth += order.size

    midpoint = spread_bps = None
    if bids and asks:
        bid_price = bids.keys()[-1] / FIXED_PRICE_SCALE
        ask_price = asks.keys()[0] / FIXED_PRICE_SCALE
        mid = (bid_price + ask_price) / 2.0
        if mid > 0:
            midpoint = mid
            spread_bps = (ask_price - bid_price) / mid * BPS

    return CoherentState(
        ts_recv=ts_recv,
        bid_depth=float(bid_depth),
        ask_depth=float(ask_depth),
        midpoint=midpoint,
        spread_bps=spread_bps,
    )


# ---------------------------------------------------------------------------
# Interval accumulator -- serves an event window and a baseline tile alike
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class IntervalAccumulator:
    """Bounded state for one 120-second interval.

    Both sides of every directional quantity are accumulated, because the
    impacted side is not known until the direction is resolved at interval
    close. Two counters per side is the price of not needing a second pass.
    """

    start_ns: int
    end_ns: int
    anchor: CoherentState | None = None  # S(start) -- may predate the interval
    final: CoherentState | None = None  # latest coherent state inside it

    records: int = 0
    signable_trades: int = 0
    unsignable_trades: int = 0
    coherent_states: int = 0

    buy_shares: int = 0
    sell_shares: int = 0
    quarter_buy: list[int] = field(default_factory=lambda: [0] * PERSISTENCE_QUARTERS)
    quarter_sell: list[int] = field(default_factory=lambda: [0] * PERSISTENCE_QUARTERS)

    # Genuine (non-execution, non-snapshot) book pressure, per side.
    withdrawn: dict[str, int] = field(default_factory=lambda: {SIDE_BID: 0, SIDE_ASK: 0})
    added: dict[str, int] = field(default_factory=lambda: {SIDE_BID: 0, SIDE_ASK: 0})

    # Depth trough per side, and additions observed since that trough was set.
    min_depth: dict[str, float | None] = field(
        default_factory=lambda: {SIDE_BID: None, SIDE_ASK: None}
    )
    added_since_min: dict[str, int] = field(
        default_factory=lambda: {SIDE_BID: 0, SIDE_ASK: 0}
    )

    execution_count: int = 0
    execution_volume: int = 0
    absorbed_volume: int = 0

    timing_flagged: bool = False

    def quarter_of(self, ts_recv: int) -> int:
        """Which 30-second quarter a record falls in.

        First three half-open, last closed on ``end_ns``, so no record is
        counted twice and the final instant is not discarded.
        """
        offset = ts_recv - self.start_ns
        index = int(offset // PERSISTENCE_QUARTER_NS)
        return min(index, PERSISTENCE_QUARTERS - 1)

    @property
    def net_flow(self) -> int:
        return self.buy_shares - self.sell_shares


# ---------------------------------------------------------------------------
# Reduced interval -- what survives after the pass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntervalStats:
    """One interval reduced. Identical shape for events and baseline tiles."""

    start_ns: int
    records: int
    signable_trades: int
    unsignable_trades: int
    coherent_states: int
    buy_shares: int
    sell_shares: int
    quarter_signs: tuple[int, ...]
    anchor_bid_depth: float | None
    anchor_ask_depth: float | None
    anchor_midpoint: float | None
    final_bid_depth: float | None
    final_ask_depth: float | None
    final_midpoint: float | None
    final_spread_bps: float | None
    min_bid_depth: float | None
    min_ask_depth: float | None
    withdrawn_bid: int
    withdrawn_ask: int
    added_bid: int
    added_ask: int
    added_since_min_bid: int
    added_since_min_ask: int
    execution_count: int
    execution_volume: int
    absorbed_volume: int
    timing_flagged: bool

    @property
    def net_flow(self) -> int:
        return self.buy_shares - self.sell_shares

    def anchor_depth(self, side: str) -> float | None:
        return self.anchor_ask_depth if side == SIDE_ASK else self.anchor_bid_depth

    def final_depth(self, side: str) -> float | None:
        return self.final_ask_depth if side == SIDE_ASK else self.final_bid_depth

    def min_depth(self, side: str) -> float | None:
        return self.min_ask_depth if side == SIDE_ASK else self.min_bid_depth

    def withdrawn(self, side: str) -> int:
        return self.withdrawn_ask if side == SIDE_ASK else self.withdrawn_bid

    def added(self, side: str) -> int:
        return self.added_ask if side == SIDE_ASK else self.added_bid

    def added_since_min(self, side: str) -> int:
        return (
            self.added_since_min_ask if side == SIDE_ASK else self.added_since_min_bid
        )

    @property
    def absorption_ratio(self) -> float | None:
        if self.execution_volume <= 0:
            return None
        return self.absorbed_volume / self.execution_volume

    @property
    def execution_intensity(self) -> float:
        return self.execution_count / (OBSERVATION_NS / 1e9)

    def withdrawal_pressure(self, side: str) -> float | None:
        total = self.withdrawn(side) + self.added(side)
        return self.withdrawn(side) / total if total > 0 else None


def reduce_interval(accumulator: IntervalAccumulator) -> IntervalStats:
    """Freeze an accumulator into an immutable reduction."""
    signs = []
    for index in range(PERSISTENCE_QUARTERS):
        net = accumulator.quarter_buy[index] - accumulator.quarter_sell[index]
        signs.append(_sign(net))
    anchor = accumulator.anchor
    final = accumulator.final
    return IntervalStats(
        start_ns=accumulator.start_ns,
        records=accumulator.records,
        signable_trades=accumulator.signable_trades,
        unsignable_trades=accumulator.unsignable_trades,
        coherent_states=accumulator.coherent_states,
        buy_shares=accumulator.buy_shares,
        sell_shares=accumulator.sell_shares,
        quarter_signs=tuple(signs),
        anchor_bid_depth=anchor.bid_depth if anchor else None,
        anchor_ask_depth=anchor.ask_depth if anchor else None,
        anchor_midpoint=anchor.midpoint if anchor else None,
        final_bid_depth=final.bid_depth if final else None,
        final_ask_depth=final.ask_depth if final else None,
        final_midpoint=final.midpoint if final else None,
        final_spread_bps=final.spread_bps if final else None,
        min_bid_depth=accumulator.min_depth[SIDE_BID],
        min_ask_depth=accumulator.min_depth[SIDE_ASK],
        withdrawn_bid=accumulator.withdrawn[SIDE_BID],
        withdrawn_ask=accumulator.withdrawn[SIDE_ASK],
        added_bid=accumulator.added[SIDE_BID],
        added_ask=accumulator.added[SIDE_ASK],
        added_since_min_bid=accumulator.added_since_min[SIDE_BID],
        added_since_min_ask=accumulator.added_since_min[SIDE_ASK],
        execution_count=accumulator.execution_count,
        execution_volume=accumulator.execution_volume,
        absorbed_volume=accumulator.absorbed_volume,
        timing_flagged=accumulator.timing_flagged,
    )


def _sign(value: float) -> int:
    if value > 0:
        return LONG
    if value < 0:
        return SHORT
    return 0


# ---------------------------------------------------------------------------
# The single-pass scanner
# ---------------------------------------------------------------------------


class SessionScanner:
    """One replay of one symbol-day, serving every interval it contains.

    The 60-minute quiet period against a 120-second window guarantees that two
    events for the same symbol never overlap, so a single "active window"
    pointer suffices -- no interval tree, no per-event replay.

    Book mutation happens on every record. Depth is read only at completed
    ``F_LAST`` states, and only into the intervals that are actually open.
    """

    def __init__(self, event_starts: Sequence[int]) -> None:
        self.event_starts = sorted(event_starts)
        # The single-pointer design is only sound because the 60-minute quiet
        # period keeps windows apart. Assert it rather than assume it: two
        # overlapping windows would silently share an anchor.
        for earlier, later in zip(self.event_starts, self.event_starts[1:], strict=False):
            if later - earlier <= OBSERVATION_NS:
                raise ValueError(
                    f"event windows overlap ({earlier} and {later} are "
                    f"{later - earlier} ns apart, window is {OBSERVATION_NS} ns). "
                    "The quiet period should make this impossible; the scanner "
                    "will not measure overlapping windows with one pointer."
                )
        self.book = MboBook()
        self.latest_state: CoherentState | None = None

        self._next_event = 0
        self._active: IntervalAccumulator | None = None
        self.event_intervals: dict[int, IntervalStats] = {}
        self.event_gate_failures: dict[int, str] = {}

        self._tile: IntervalAccumulator | None = None
        self.tiles: list[IntervalStats] = []

        # Native-event group state, delimited by F_LAST.
        self._group_records: list[Any] = []
        self._group_has_execution = False
        self._group_pre_midpoint: float | None = None
        self._group_execution_count = 0
        self._group_execution_volume = 0

        self.records = 0
        self.first_ts_recv: int | None = None
        self.last_ts_recv: int | None = None
        self.out_of_order_records = 0

    # -- book snapshots used for MODIFY decomposition -----------------------

    def _resting(self, order_id: int):
        return self.book.orders.get(order_id)

    # -- the pass ----------------------------------------------------------

    def run(self, events: Iterable[Any]) -> None:
        for event in events:
            ts = int(event.ts_recv)
            if self.first_ts_recv is None:
                self.first_ts_recv = ts
            elif ts < self.last_ts_recv:
                # The whole state-selection rule rests on the pass proceeding in
                # non-decreasing receive order: "the latest coherent state so
                # far" is only S(t) if nothing earlier is still to come. An
                # out-of-order file would silently anchor windows to the wrong
                # state, so it is refused rather than measured.
                self.out_of_order_records += 1
                raise ValueError(
                    f"raw stream is out of receive order at record {self.records}: "
                    f"ts_recv {ts} follows {self.last_ts_recv}. Stage 4.1 v2 will "
                    "not measure a stream whose ordering it cannot rely on."
                )
            self.last_ts_recv = ts
            self.records += 1

            # 1. Everything strictly before this record is now final, so any
            #    boundary the record has passed can be frozen against the state
            #    as it stood at or before that boundary.
            self._advance_boundaries(ts)

            # 2. Capture the pre-image a MODIFY/CANCEL needs, before mutation.
            pressure = self._classify_book_pressure(event)

            # 3. Native-event group bookkeeping.
            if not self._group_records:
                self._group_pre_midpoint = (
                    self.latest_state.midpoint if self.latest_state else None
                )
                self._group_has_execution = False
                self._group_execution_count = 0
                self._group_execution_volume = 0
            self._group_records.append((event, pressure))
            if event.action in (ACTION_TRADE, ACTION_FILL):
                self._group_has_execution = True
            if event.action == ACTION_FILL:
                self._group_execution_count += 1
                self._group_execution_volume += int(event.size or 0)

            # 4. Mutate.
            self.book.apply(event)

            # 5. Record census and signed aggression. T records only, never F.
            for interval in self._open_intervals(ts):
                interval.records += 1
            self._accumulate_trade(event, ts)

            # 6. Settle at the coherent boundary.
            if event.flags & F_LAST:
                self._settle_group(ts)

        self._advance_boundaries(None)

    # -- boundaries --------------------------------------------------------

    def _advance_boundaries(self, ts: int | None) -> None:
        """Open and close intervals the incoming record has moved past.

        Called *before* the record is applied, so ``self.latest_state`` still
        reflects only records with ``ts_recv`` strictly less than ``ts``. Any
        boundary ``b < ts`` therefore has ``S(b) == self.latest_state`` at this
        instant, which is exactly the state-selection rule.
        """
        end_of_stream = ts is None

        # -- baseline tiles, contiguous and absolute --------------------------
        if self._tile is not None and (end_of_stream or ts > self._tile.end_ns):
            self._close_tile()
        if not end_of_stream and self._tile is None:
            start = (ts // BASELINE_TILE_NS) * BASELINE_TILE_NS
            self._tile = IntervalAccumulator(
                start_ns=start,
                end_ns=start + BASELINE_TILE_NS - 1,
                # S(tile_start): may legitimately come from an earlier tile --
                # it is the state as it stood when this tile opened.
                anchor=self.latest_state,
            )

        # -- the one active event window -------------------------------------
        if self._active is not None and (end_of_stream or ts > self._active.end_ns):
            # If the stream ended before the window did, the window was never
            # fully observed and no amount of state can stand in for that.
            covered = (
                self.last_ts_recv is not None
                and self.last_ts_recv >= self._active.end_ns
            )
            self._close_event(covered=covered)

        if end_of_stream:
            return

        # Open the next window on the first record at or after its t0, so the
        # anchor frozen above is S(t0) with no in-window record applied yet.
        if (
            self._active is None
            and self._next_event < len(self.event_starts)
            and ts >= self.event_starts[self._next_event]
        ):
            start = self.event_starts[self._next_event]
            self._next_event += 1
            interval = IntervalAccumulator(
                start_ns=start,
                end_ns=start + OBSERVATION_NS,
                anchor=self.latest_state,
            )
            if ts > interval.end_ns:
                # The stream spans the window but carries no record inside it.
                # That is an empty window, not missing coverage, and the
                # coherent-state gate is the honest way to say so.
                self.event_intervals[start] = reduce_interval(interval)
                return
            self._active = interval

    def _close_tile(self) -> None:
        tile = self._tile
        self._tile = None
        if tile is None:
            return
        # A tile holding no coherent state has no endpoint; borrowing one from
        # the previous tile would report one tile's liquidity as another's.
        if tile.final is not None:
            self.tiles.append(reduce_interval(tile))

    def _close_event(self, *, covered: bool = True) -> None:
        active = self._active
        self._active = None
        if active is None:
            return
        self.event_intervals[active.start_ns] = reduce_interval(active)
        if not covered:
            self.event_gate_failures[active.start_ns] = GATE_COVERAGE

    # -- per-record accounting ---------------------------------------------

    def _classify_book_pressure(self, event: Any) -> tuple[str, int] | None:
        """Decompose a book-mutating record into withdrawal or addition.

        Called *before* the book is mutated, because a MODIFY is only
        interpretable against the order it is modifying. Snapshot records are
        book state, not order events, and are excluded here.
        """
        if event.flags & F_SNAPSHOT:
            return None
        action = event.action
        side = event.side
        if side not in (SIDE_BID, SIDE_ASK):
            return None
        size = int(event.size or 0)

        if action == ACTION_ADD:
            return ("add", size)
        if action == ACTION_CANCEL:
            return ("withdraw", size)
        if action == ACTION_MODIFY:
            order = self._resting(int(event.order_id or 0))
            if order is None:
                # Databento's reference treats an unknown modify as an add.
                return ("add", size)
            if order.price != event.price:
                # Detach and reinsert: the old resting quantity leaves its price.
                return ("withdraw", int(order.size))
            if size > order.size:
                return ("add", size - int(order.size))
            if size < order.size:
                return ("withdraw", int(order.size) - size)
        return None

    def _accumulate_trade(self, event: Any, ts: int) -> None:
        """Sign aggression from ``T`` records only.

        ``T.side`` names the aggressor. ``F.side`` names the resting side -- the
        opposite -- and signing it would be wrong twice over: inverted, and
        already carried by the ``T``.
        """
        if event.action != ACTION_TRADE:
            return
        size = int(event.size or 0)
        for interval in self._open_intervals(ts):
            if event.side == SIDE_BID:
                interval.buy_shares += size
                interval.quarter_buy[interval.quarter_of(ts)] += size
                interval.signable_trades += 1
            elif event.side == SIDE_ASK:
                interval.sell_shares += size
                interval.quarter_sell[interval.quarter_of(ts)] += size
                interval.signable_trades += 1
            else:
                interval.unsignable_trades += 1

    def _open_intervals(self, ts: int) -> list[IntervalAccumulator]:
        open_intervals: list[IntervalAccumulator] = []
        if self._tile is not None and self._tile.start_ns <= ts <= self._tile.end_ns:
            open_intervals.append(self._tile)
        if (
            self._active is not None
            and self._active.start_ns <= ts <= self._active.end_ns
        ):
            open_intervals.append(self._active)
        return open_intervals

    def _settle_group(self, ts: int) -> None:
        """Close the native event and read the coherent state it produced."""
        records = self._group_records
        self._group_records = []

        state = capture_state(self.book, ts)
        self.latest_state = state

        absorbed = (
            self._group_pre_midpoint is not None
            and state.midpoint is not None
            and self._group_pre_midpoint == state.midpoint
        )

        for interval in self._open_intervals(ts):
            interval.coherent_states += 1
            if interval.final is None or ts >= interval.final.ts_recv:
                interval.final = state

            # Execution volume and absorption, classified at the boundary.
            interval.execution_count += self._group_execution_count
            interval.execution_volume += self._group_execution_volume
            if absorbed:
                interval.absorbed_volume += self._group_execution_volume

            # Genuine book pressure only. A group carrying an execution has its
            # C/M records caused by that execution, and counting them as
            # voluntary withdrawal would count one execution twice.
            if not self._group_has_execution:
                for record, pressure in records:
                    if pressure is None:
                        continue
                    kind, size = pressure
                    side = record.side
                    if kind == "withdraw":
                        interval.withdrawn[side] += size
                    else:
                        interval.added[side] += size
                        interval.added_since_min[side] += size

            if record_flags_uncertifiable(records):
                interval.timing_flagged = True

            # Depth trough, per side. A new trough resets the replenishment
            # counter: what came back before the low is not replenishment.
            for side in (SIDE_BID, SIDE_ASK):
                depth = state.depth_for(side)
                current = interval.min_depth[side]
                if current is None or depth < current:
                    interval.min_depth[side] = depth
                    interval.added_since_min[side] = 0

        self._group_pre_midpoint = None
        self._group_has_execution = False
        self._group_execution_count = 0
        self._group_execution_volume = 0


def record_flags_uncertifiable(records: Sequence[tuple[Any, Any]]) -> bool:
    """Did any record in this group carry a flag we refuse to stand behind?"""
    for record, _pressure in records:
        if record.flags & (F_BAD_TS_RECV | F_MAYBE_BAD_BOOK):
            return True
    return False


# ---------------------------------------------------------------------------
# Direction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DirectionVerdict:
    direction: int | None
    net_flow: int
    agreeing_quarters: int
    quarter_signs: tuple[int, ...]
    reason: str | None


def resolve_direction(stats: IntervalStats) -> DirectionVerdict:
    """One raw measurement. No cadence agreement, because there is one measurement."""
    if stats.signable_trades == 0:
        return DirectionVerdict(
            None, 0, 0, stats.quarter_signs, AMBIGUITY_NO_SIGNABLE_TRADE
        )
    net = stats.net_flow
    direction = _sign(net)
    if direction == 0:
        return DirectionVerdict(
            None, net, 0, stats.quarter_signs, AMBIGUITY_ZERO_FLOW
        )
    agreeing = sum(1 for s in stats.quarter_signs if s == direction)
    if agreeing < MIN_AGREEING_QUARTERS:
        return DirectionVerdict(
            None, net, agreeing, stats.quarter_signs, AMBIGUITY_NOT_PERSISTENT
        )
    return DirectionVerdict(direction, net, agreeing, stats.quarter_signs, None)


# ---------------------------------------------------------------------------
# Local lambda
# ---------------------------------------------------------------------------


def local_lambda(stats: IntervalStats, direction: int) -> float | None:
    """Directional displacement per 1,000 aggressive shares, closed window.

    Both midpoints obey the state-selection rule: the anchor is ``S(t0)`` and
    the final is the latest coherent state at or before ``t_obs_end``. Nothing
    after the cutoff enters, which is what keeps this a state variable.
    """
    mid_start = stats.anchor_midpoint
    mid_end = stats.final_midpoint
    if mid_start is None or mid_end is None or mid_start <= 0:
        return None
    denominator = direction * stats.net_flow
    if denominator < LAMBDA_MIN_DENOMINATOR_SHARES:
        return None
    displacement_bps = direction * (mid_end - mid_start) / mid_start * BPS
    value = LAMBDA_SHARE_SCALE * displacement_bps / denominator
    return value if math.isfinite(value) else None


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

BASELINE_STATISTICS: tuple[str, ...] = (
    "final_ask_depth",
    "final_bid_depth",
    "final_spread_bps",
    "absorption_ratio",
    "execution_intensity",
    "withdrawal_pressure_ask",
    "withdrawal_pressure_bid",
    "lambda_long",
    "lambda_short",
)


@dataclass(slots=True)
class Baseline:
    """Per-symbol prior-only distributions, sorted once for binary search."""

    symbol: str
    tiles: int
    samples: dict[str, np.ndarray]

    def percentile_of(self, statistic: str, value: float | None) -> float | None:
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
        }


def build_baseline(symbol: str, tiles: Sequence[IntervalStats]) -> Baseline:
    collected: dict[str, list[float]] = {name: [] for name in BASELINE_STATISTICS}
    for tile in tiles:
        for name, value in (
            ("final_ask_depth", tile.final_ask_depth),
            ("final_bid_depth", tile.final_bid_depth),
            ("final_spread_bps", tile.final_spread_bps),
            ("absorption_ratio", tile.absorption_ratio),
            ("execution_intensity", tile.execution_intensity),
            ("withdrawal_pressure_ask", tile.withdrawal_pressure(SIDE_ASK)),
            ("withdrawal_pressure_bid", tile.withdrawal_pressure(SIDE_BID)),
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


# ---------------------------------------------------------------------------
# Event state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EventState:
    """Everything IAG-v2 measures about one event. No outcome, by construction."""

    symbol: str
    session_date: str
    story_id: str
    t0_ns: int
    t_obs_end_ns: int
    gate_failure: str | None
    records: int
    signable_trades: int
    unsignable_trades: int
    coherent_states: int
    direction: int | None
    direction_reason: str | None
    agreeing_quarters: int
    quarter_signs: tuple[int, ...]
    net_flow: int
    depth_ref: float | None
    depth_min: float | None
    depth_end: float | None
    depletion_ratio: float | None
    recovery_ratio: float | None
    replenishment_ratio: float | None
    depth_percentile: float | None
    absorption_percentile: float | None
    lambda_value: float | None
    lambda_percentile: float | None
    spread_percentile: float | None
    intensity_percentile: float | None
    withdrawal_percentile: float | None
    baseline_tiles: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "session_date": self.session_date,
            "story_id": self.story_id,
            "gate_failure": self.gate_failure,
            "records": self.records,
            "signable_trades": self.signable_trades,
            "coherent_states": self.coherent_states,
            "direction": self.direction,
            "direction_reason": self.direction_reason,
            "agreeing_quarters": self.agreeing_quarters,
            "depth_percentile": self.depth_percentile,
            "recovery_ratio": self.recovery_ratio,
            "depletion_ratio": self.depletion_ratio,
            "absorption_percentile": self.absorption_percentile,
            "lambda_percentile": self.lambda_percentile,
            "spread_percentile": self.spread_percentile,
            "intensity_percentile": self.intensity_percentile,
            "withdrawal_percentile": self.withdrawal_percentile,
            "baseline_tiles": self.baseline_tiles,
        }


def measure_event(
    *,
    symbol: str,
    session_date: str,
    story_id: str,
    t0_ns: int,
    stats: IntervalStats | None,
    baseline: Baseline,
    gate_failure: str | None = None,
) -> EventState:
    """Reduce one event to its state. Reads nothing after ``t_obs_end``."""
    blank = EventState(
        symbol=symbol, session_date=session_date, story_id=story_id,
        t0_ns=t0_ns, t_obs_end_ns=t0_ns + OBSERVATION_NS,
        gate_failure=gate_failure or GATE_COVERAGE,
        records=0, signable_trades=0, unsignable_trades=0, coherent_states=0,
        direction=None, direction_reason=None, agreeing_quarters=0,
        quarter_signs=(0,) * PERSISTENCE_QUARTERS, net_flow=0,
        depth_ref=None, depth_min=None, depth_end=None,
        depletion_ratio=None, recovery_ratio=None, replenishment_ratio=None,
        depth_percentile=None, absorption_percentile=None,
        lambda_value=None, lambda_percentile=None, spread_percentile=None,
        intensity_percentile=None, withdrawal_percentile=None,
        baseline_tiles=baseline.tiles,
    )
    if stats is None:
        return blank

    failure = gate_failure
    if failure is None:
        failure = _gate_failure(stats)

    verdict = resolve_direction(stats)
    absorption_pct = baseline.percentile_of("absorption_ratio", stats.absorption_ratio)
    spread_pct = baseline.percentile_of("final_spread_bps", stats.final_spread_bps)
    intensity_pct = baseline.percentile_of(
        "execution_intensity", stats.execution_intensity
    )

    depth_ref = depth_min = depth_end = None
    depletion = recovery = replenishment = None
    depth_pct = lambda_value = lambda_pct = withdrawal_pct = None

    if verdict.direction is not None:
        side = impacted_side(verdict.direction)
        depth_ref = stats.anchor_depth(side)
        depth_min = stats.min_depth(side)
        depth_end = stats.final_depth(side)
        if depth_ref is not None and depth_min is not None and depth_ref > 0:
            depletion = depth_min / depth_ref
        if (
            depth_ref is not None
            and depth_min is not None
            and depth_end is not None
        ):
            recovery = (
                (depth_end - depth_min) / (depth_ref - depth_min)
                if depth_ref > depth_min
                else 1.0
            )
            replenishment = stats.added_since_min(side) / max(depth_ref - depth_min, 1.0)
        depth_pct = baseline.percentile_of(
            "final_ask_depth" if side == SIDE_ASK else "final_bid_depth", depth_end
        )
        lambda_value = local_lambda(stats, verdict.direction)
        lambda_pct = baseline.percentile_of(
            "lambda_long" if verdict.direction == LONG else "lambda_short",
            lambda_value,
        )
        withdrawal_pct = baseline.percentile_of(
            "withdrawal_pressure_ask" if side == SIDE_ASK else "withdrawal_pressure_bid",
            stats.withdrawal_pressure(side),
        )

    return EventState(
        symbol=symbol, session_date=session_date, story_id=story_id,
        t0_ns=t0_ns, t_obs_end_ns=t0_ns + OBSERVATION_NS,
        gate_failure=failure,
        records=stats.records,
        signable_trades=stats.signable_trades,
        unsignable_trades=stats.unsignable_trades,
        coherent_states=stats.coherent_states,
        direction=verdict.direction,
        direction_reason=verdict.reason,
        agreeing_quarters=verdict.agreeing_quarters,
        quarter_signs=stats.quarter_signs,
        net_flow=stats.net_flow,
        depth_ref=depth_ref, depth_min=depth_min, depth_end=depth_end,
        depletion_ratio=depletion, recovery_ratio=recovery,
        replenishment_ratio=replenishment,
        depth_percentile=depth_pct,
        absorption_percentile=absorption_pct,
        lambda_value=lambda_value, lambda_percentile=lambda_pct,
        spread_percentile=spread_pct, intensity_percentile=intensity_pct,
        withdrawal_percentile=withdrawal_pct,
        baseline_tiles=baseline.tiles,
    )


def _gate_failure(stats: IntervalStats) -> str | None:
    """The frozen structural gates. No record or trade count among them."""
    if stats.timing_flagged:
        return GATE_TIMING
    if stats.coherent_states == 0:
        return GATE_NO_COHERENT_STATE
    if stats.anchor_midpoint is None or stats.final_midpoint is None:
        return GATE_ONE_SIDED
    return None


# ---------------------------------------------------------------------------
# Qualification
# ---------------------------------------------------------------------------


def supporting_count(state: EventState) -> int:
    """How many of the four stress conditions hold.

    S1 is now genuinely directional: raw ``A``/``C``/``M`` carry a validated
    resting side, which v1's aggregated counters did not.
    """
    conditions = (
        state.withdrawal_percentile,
        state.spread_percentile,
        state.lambda_percentile,
        state.intensity_percentile,
    )
    return sum(1 for v in conditions if v is not None and v >= HIGH_PERCENTILE)


def qualifies(state: EventState, spec: Specification) -> tuple[bool, str | None, int]:
    """Apply one frozen specification."""
    if state.gate_failure is not None:
        return False, FAIL_GATE, 0
    if state.baseline_tiles < MIN_BASELINE_TILES:
        return False, FAIL_THIN_BASELINE, 0
    if state.direction is None:
        return False, FAIL_AMBIGUOUS_DIRECTION, 0
    if state.depth_percentile is None or state.depth_percentile > spec.depletion_percentile:
        return False, FAIL_NO_DEPLETION, 0
    if state.recovery_ratio is None or state.recovery_ratio > spec.recovery_threshold:
        return False, FAIL_REPLENISHED, 0
    if (
        state.absorption_percentile is not None
        and state.absorption_percentile >= ABSORPTION_DISQUALIFY_PERCENTILE
    ):
        return False, FAIL_ABSORBED, 0
    support = supporting_count(state)
    if support < spec.min_supporting:
        return False, FAIL_SUPPORT, support
    return True, None, support


# ---------------------------------------------------------------------------
# Selection ladder -- counts only
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpecificationCounts:
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


def select_specification(states: Sequence[EventState]) -> dict[str, Any]:
    """The frozen deterministic ladder. Counts only; no outcome enters it."""
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
            "selection_basis": "PRIMARY missed a declared floor; FALLBACK cleared both",
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
        f"{name!r} is not a declared Stage-4.1 v2 specification; only "
        f"{SPEC_PRIMARY.name} and {SPEC_FALLBACK.name} exist"
    )


def write_selection(payload: dict[str, Any], path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return sha256_of(path)


def read_selection(path: Path, *, expected_sha256: str | None = None) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(
            f"no persisted Stage-4.1 v2 specification selection at {path}. Run "
            "diagnose first: the economic run may only evaluate a specification "
            "selected from outcome-blind counts."
        )
    observed = sha256_of(path)
    if expected_sha256 is not None and observed != expected_sha256:
        raise ValueError(
            f"the persisted specification selection has changed: {observed} != "
            f"{expected_sha256}. The selection is frozen once diagnose records it."
        )
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# THE ECONOMIC REVEAL -- the only function reading past t_obs_end
# ---------------------------------------------------------------------------


def gross_directional_displacement_bps(
    *, direction: int, midpoint_at_decision: float, midpoint_at_horizon: float
) -> float:
    """Gross directional midpoint displacement, in basis points.

    ``D * (mid(t_horizon) - mid(t_decision)) / mid(t_decision) * 10_000``.

    **Not P&L and not executable profit**: no spread is crossed, no fee charged,
    no fill modelled, no position sized. Deliberately not called "abnormal" --
    no benchmark-adjustment formula is frozen, so there is no baseline against
    which anything here could be abnormal.

    This is the single function that touches a price after ``t_obs_end``, and a
    structural test asserts the qualification path never calls it.
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
# Inference and verdict
# ---------------------------------------------------------------------------


def session_clustered_inference(
    displacements: Sequence[float], sessions: Sequence[str]
) -> dict[str, Any]:
    """Day-clustered mean, t and interval, reusing Stage 3's own statistic."""
    from app.services.mbo_stage3_executor import clustered_t

    if len(displacements) != len(sessions):
        raise ValueError("displacements and sessions must align one-to-one")
    if not displacements:
        raise ValueError("no displacements to summarise")

    values = np.asarray(displacements, dtype=float)
    by_session: dict[str, list[float]] = {}
    for value, session in zip(values, sessions, strict=True):
        by_session.setdefault(session, []).append(float(value))
    distinct = sorted(by_session)
    session_means = np.asarray(
        [float(np.mean(by_session[s])) for s in distinct], dtype=float
    )
    statistic, p_value = clustered_t(list(session_means))

    session_mean = float(session_means.mean())
    half_width: float | None = None
    if session_means.size >= 2:
        standard_error = float(session_means.std(ddof=1) / math.sqrt(session_means.size))
        half_width = _t_critical(session_means.size - 1) * standard_error

    return {
        "events": int(values.size),
        "distinct_sessions": len(distinct),
        "mean_gross_bps": float(values.mean()),
        "median_gross_bps": float(np.median(values)),
        "session_mean_gross_bps": session_mean,
        "session_clustered_t": float(statistic) if statistic is not None else None,
        "session_clustered_p": float(p_value) if p_value is not None else None,
        "ci95_low_bps": session_mean - half_width if half_width is not None else None,
        "ci95_high_bps": session_mean + half_width if half_width is not None else None,
        "ci95_describes": "session_mean_gross_bps",
        "clustering": "trading_session",
    }


def _t_critical(degrees_of_freedom: int) -> float:
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
                f"mean gross displacement cleared the {PRIMARY_GROSS_HURDLE_BPS} "
                f"bps hurdle with clustered t >= {T_HURDLE}"
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


__all__ = [
    "AMBIGUITY_NOT_PERSISTENT",
    "AMBIGUITY_NO_SIGNABLE_TRADE",
    "AMBIGUITY_ZERO_FLOW",
    "FAILURE_REASONS",
    "STAGE41_V2_EXECUTOR_VERSION",
    "Baseline",
    "CoherentState",
    "DirectionVerdict",
    "EventState",
    "IntervalAccumulator",
    "IntervalStats",
    "SessionScanner",
    "SpecificationCounts",
    "build_baseline",
    "capture_state",
    "count_specification",
    "decide_verdict",
    "gross_directional_displacement_bps",
    "local_lambda",
    "measure_event",
    "qualifies",
    "read_selection",
    "reduce_interval",
    "resolve_direction",
    "select_specification",
    "session_clustered_inference",
    "specification_by_name",
    "supporting_count",
    "write_selection",
]
