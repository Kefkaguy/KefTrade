"""Stage 1: Tier-1 order-book state and feature engine.

Turns a reconstructed XNAS L3 book into **causal state variables**, sampled on
fixed clocks. It answers "what did the book look like, and what had just
happened to it" at each sampling instant, using only information available at
that instant.

It does **not** answer whether any of that predicts anything. There is no
forward return, no label, no correlation with future price, no Alpha Map cell,
no strategy, no P/L, no parameter optimization and no threshold selection in
this module or anywhere it is called from. Stage 1 stops before prediction, and
the feature vocabulary is frozen *before* any outcome is inspected so that the
choice of what to measure cannot be contaminated by what turned out to work.

## v2: three pre-outcome corrections

Found by audit before any predictive result was inspected, so these are
corrections to a measurement that was wrong, not tuning toward a result that
was wanted. `SUPERSEDED_ENGINE_VERSIONS` records what they replaced.

**1. Aggressive flow was double counted.** XNAS normalizes one displayed
execution as `T` → `F` → `C`. v1 added the size to buy/sell aggressor volume on
*both* the `T` and the `F`, so a 100-share execution produced 200 shares of
signed flow. The two records describe the same trade from different sides.

The split is now strict:

| Record | Contributes to |
|---|---|
| `T` Trade | `trade_count`, `trade_volume`, buy/sell aggressor volume, `signed_trade_volume`, `aggressor_imbalance`, `unclassified_trade_volume` |
| `F` Fill | `execution_count`, `execution_volume`, absorption, and the refill/lifecycle features |

`F` never touches aggressor volume. Its `side` is still meaningful -- it names
the *resting* side, the opposite of the aggressor -- but the `T` already
carries the trade, so signing the `F` as well counts it twice.

**2. Time grids are absolute UTC, not per-symbol.** v1 anchored the 1s/5s grids
to each file's first `F_LAST`, so two symbols sampled on grids offset from each
other by an arbitrary sub-second amount and were not comparable at the same
instant. Boundaries are now absolute multiples of the interval in UTC
nanoseconds, identical for every symbol.

A time-grid snapshot at boundary `t` may use **only the last completed `F_LAST`
with `ts_event <= t`**. An event at `t + 1ns` belongs strictly to the next
interval and cannot touch it. An interval with no events emits the last known
book state with zero new window-flow primitives, rather than being skipped --
a quiet second is an observation.

**3. Availability timestamps are preserved.** `grid_ts_event`,
`source_ts_event`, `source_ts_recv` and `feature_available_ts_recv` are carried
so a later stage can simulate latency without re-deriving when a row could
first have been known. `feature_available_ts_recv` is the maximum `ts_recv`
over every record the snapshot depended on, so it never precedes an input.

## Causality

* **Windowed counters cover `(previous snapshot of this cadence, now]`.** Reset
  on emission, so a window cannot reach forward by construction.
* **Normalization is prior-only and session-local.** Welford over snapshots
  *strictly before* the current one, within the same symbol-day.
* **Nothing crosses symbol-days.** Each file starts cold.

## Raw primitives are preserved

Every ratio is stored beside the counts it came from. `cancel_add_ratio`
without `cancel_count` and `add_count` is an assertion; with them it is a
measurement.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from app.services.mbo_book_validator import (
    ACTION_ADD,
    ACTION_CANCEL,
    ACTION_CLEAR,
    ACTION_FILL,
    ACTION_MODIFY,
    ACTION_TRADE,
    F_LAST,
    FIXED_PRICE_SCALE,
    MBO_VALIDATOR_VERSION,
    SIDE_ASK,
    SIDE_BID,
    MboBook,
    MboEvent,
)

FEATURE_ENGINE_VERSION = "tier1_mbo_feature_engine_v2"

# Superseded before any predictive outcome was inspected. Kept so a reader of an
# older manifest can tell which semantics produced it.
SUPERSEDED_ENGINE_VERSIONS: tuple[dict[str, str], ...] = (
    {
        "version": "tier1_mbo_feature_engine_v1",
        "superseded_before_outcome": "true",
        "reason": (
            "T and F from one XNAS execution both incremented aggressor volume, "
            "double counting signed aggressive flow; time grids were anchored to "
            "each file's first F_LAST rather than absolute UTC, so symbols were "
            "not comparable at the same instant; availability timestamps were "
            "absent."
        ),
        "feature_vocabulary_hash": (
            "25e685913e3a3d05248ef6f09ad44e4b0cab91276bf7bd66d2f0d650f06b82a7"
        ),
    },
)

DEPTH_LEVELS: tuple[int, ...] = (1, 5, 10)
MIN_PRIOR_OBSERVATIONS = 30


@dataclass(frozen=True, slots=True)
class Cadence:
    """A fixed sampling clock. `kind` is 'time' or 'events'."""

    name: str
    kind: str
    interval: int  # nanoseconds for 'time', F_LAST events for 'events'


CADENCES: tuple[Cadence, ...] = (
    Cadence("1s", "time", 1_000_000_000),
    Cadence("5s", "time", 5_000_000_000),
    Cadence("50ev", "events", 50),
    Cadence("200ev", "events", 200),
)


# ---------------------------------------------------------------------------
# The frozen feature vocabulary
# ---------------------------------------------------------------------------

BOOK_STATE_FEATURES: tuple[str, ...] = (
    "best_bid_price",
    "best_ask_price",
    "spread",
    "spread_bps",
    "midpoint",
    "bid_size_l1",
    "ask_size_l1",
    "bid_order_count_l1",
    "ask_order_count_l1",
    "bid_depth_5",
    "ask_depth_5",
    "bid_depth_10",
    "ask_depth_10",
    "bid_order_count_5",
    "ask_order_count_5",
    "bid_levels",
    "ask_levels",
    "resting_orders",
)

PRESSURE_FEATURES: tuple[str, ...] = (
    "queue_imbalance",
    "normalized_queue_imbalance",
    "microprice",
    "microprice_minus_mid",
    "microprice_minus_mid_bps",
    "order_flow_imbalance",
    "order_flow_imbalance_normalized",
    "mean_touch_depth",
)

LIFECYCLE_FEATURES: tuple[str, ...] = (
    "add_count",
    "add_volume",
    "cancel_count",
    "cancel_volume",
    "modify_count",
    "execution_count",
    "execution_volume",
    "cancel_add_ratio",
    "cancel_volume_ratio",
    "touch_replenishment_volume",
    "touch_replenishment_events",
    "queue_depletion_events",
    "queue_persistence",
    "best_bid_changes",
    "best_ask_changes",
)

AGGRESSIVE_FLOW_FEATURES: tuple[str, ...] = (
    "trade_count",
    "trade_volume",
    "buy_aggressor_volume",
    "sell_aggressor_volume",
    "unclassified_trade_volume",
    "unclassified_trade_share",
    "signed_trade_volume",
    "aggressor_imbalance",
    "execution_intensity",
)

ABSORPTION_FEATURES: tuple[str, ...] = (
    "executions_without_price_move",
    "execution_volume_without_price_move",
    "absorption_ratio",
    "refill_after_execution_volume",
    "depletion_followed_by_quote_move",
)

CONTEXT_COLUMNS: tuple[str, ...] = (
    "symbol",
    "session_date",
    "cadence",
    "sequence_index",
    # Nominal snapshot time: the grid boundary for time cadences, the source
    # event for event cadences.
    "ts_event",
    # Absolute UTC grid boundary; None for event cadences.
    "grid_ts_event",
    # The last completed F_LAST at or before the snapshot instant.
    "source_ts_event",
    "source_ts_recv",
    # Never precedes any record the snapshot depended on.
    "feature_available_ts_recv",
    "sequence",
    "flast_index",
    "window_ns",
    "window_flast_events",
    "window_records",
)

NORMALIZED_FEATURES: tuple[str, ...] = (
    "spread_bps_z",
    "normalized_queue_imbalance_z",
    "order_flow_imbalance_normalized_z",
    "signed_trade_volume_z",
)

FEATURE_VOCABULARY: tuple[str, ...] = (
    *BOOK_STATE_FEATURES,
    *PRESSURE_FEATURES,
    *LIFECYCLE_FEATURES,
    *AGGRESSIVE_FLOW_FEATURES,
    *ABSORPTION_FEATURES,
    *NORMALIZED_FEATURES,
)

SNAPSHOT_COLUMNS: tuple[str, ...] = (*CONTEXT_COLUMNS, *FEATURE_VOCABULARY)

# Names only. Unchanged by v2 -- the correction was to what `T` and `F` mean,
# not to what the columns are called, which is exactly why a name hash alone is
# not sufficient provenance.
FEATURE_VOCABULARY_HASH = hashlib.sha256(
    "\n".join(FEATURE_VOCABULARY).encode("utf-8")
).hexdigest()

# The full written schema, including the v2 provenance columns.
SNAPSHOT_SCHEMA_HASH = hashlib.sha256(
    "\n".join(SNAPSHOT_COLUMNS).encode("utf-8")
).hexdigest()

# Binds the engine version to the vocabulary, so a semantic correction that
# leaves every column name untouched still changes the recorded hash. A hash
# over names alone would have called v1 and v2 identical.
FEATURE_SEMANTICS_HASH = hashlib.sha256(
    "\n".join((FEATURE_ENGINE_VERSION, *SNAPSHOT_COLUMNS)).encode("utf-8")
).hexdigest()

WINDOWED_FEATURES: frozenset[str] = frozenset(
    (*LIFECYCLE_FEATURES, *AGGRESSIVE_FLOW_FEATURES, *ABSORPTION_FEATURES)
) | {"order_flow_imbalance", "order_flow_imbalance_normalized", "mean_touch_depth"}


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


class ExpandingNormalizer:
    """Welford mean/variance over **strictly prior** observations.

    The value is normalized against the statistics of everything before it,
    then folded in. Folding first would let an observation normalize against
    itself -- a small leak that flatters exactly the extreme readings a later
    stage would care about.
    """

    __slots__ = ("_count", "_m2", "_mean")

    def __init__(self) -> None:
        self._count = 0
        self._mean = 0.0
        self._m2 = 0.0

    def normalize_then_update(self, value: float | None) -> float | None:
        if value is None:
            return None
        z: float | None = None
        if self._count >= MIN_PRIOR_OBSERVATIONS:
            variance = self._m2 / (self._count - 1)
            if variance > 0:
                z = (value - self._mean) / (variance**0.5)
        self._count += 1
        delta = value - self._mean
        self._mean += delta / self._count
        self._m2 += delta * (value - self._mean)
        return z

    @property
    def count(self) -> int:
        return self._count


@dataclass
class WindowAccumulator:
    """Everything since this cadence last emitted: ``(previous, now]``."""

    records: int = 0
    flast_events: int = 0
    start_ts: int | None = None
    max_ts_recv: int = 0

    add_count: int = 0
    add_volume: int = 0
    cancel_count: int = 0
    cancel_volume: int = 0
    modify_count: int = 0
    execution_count: int = 0
    execution_volume: int = 0

    trade_count: int = 0
    trade_volume: int = 0
    buy_aggressor_volume: int = 0
    sell_aggressor_volume: int = 0
    unclassified_trade_volume: int = 0

    ofi: float = 0.0
    touch_depth_sum: float = 0.0
    touch_depth_samples: int = 0

    touch_replenishment_volume: int = 0
    touch_replenishment_events: int = 0
    queue_depletion_events: int = 0
    depletion_followed_by_quote_move: int = 0
    best_bid_changes: int = 0
    best_ask_changes: int = 0
    flast_with_unchanged_touch: int = 0

    executions_without_price_move: int = 0
    execution_volume_without_price_move: int = 0
    refill_after_execution_volume: int = 0

    _RESET_FIELDS = (
        "records",
        "flast_events",
        "add_count",
        "add_volume",
        "cancel_count",
        "cancel_volume",
        "modify_count",
        "execution_count",
        "execution_volume",
        "trade_count",
        "trade_volume",
        "buy_aggressor_volume",
        "sell_aggressor_volume",
        "unclassified_trade_volume",
        "touch_replenishment_volume",
        "touch_replenishment_events",
        "queue_depletion_events",
        "depletion_followed_by_quote_move",
        "best_bid_changes",
        "best_ask_changes",
        "flast_with_unchanged_touch",
        "executions_without_price_move",
        "execution_volume_without_price_move",
        "refill_after_execution_volume",
        "touch_depth_samples",
        "max_ts_recv",
    )

    def reset(self, ts: int) -> None:
        for name in self._RESET_FIELDS:
            setattr(self, name, 0)
        self.ofi = 0.0
        self.touch_depth_sum = 0.0
        self.start_ts = ts


@dataclass(slots=True)
class _TouchState:
    bid_price: int | None = None
    ask_price: int | None = None
    bid_size: int = 0
    ask_size: int = 0


@dataclass(slots=True)
class CompletedState:
    """Book readings at a completed ``F_LAST``, with its source timestamps.

    Captured at the boundary rather than read live, because a time-grid
    snapshot at ``t`` must reflect the last completed event at or before ``t``
    -- not whatever the book looks like when the *next* event happens to arrive
    and trigger the emission.
    """

    ts_event: int
    ts_recv: int
    sequence: int
    flast_index: int
    best_bid_price: int | None
    best_ask_price: int | None
    bid_size: int
    ask_size: int
    bid_count: int
    ask_count: int
    bid_depth_5: int
    ask_depth_5: int
    bid_depth_10: int
    ask_depth_10: int
    bid_order_count_5: int
    ask_order_count_5: int
    bid_levels: int
    ask_levels: int
    resting_orders: int


class OrderBookFeatureEngine:
    """Replays MBO events, maintaining a book and emitting causal snapshots.

    Drives the *same* ``MboBook`` the Tier-1 validator uses -- the gate that
    passed 160/160 certified this exact reconstruction, so features are built on
    the artefact that was certified rather than a second implementation that
    might disagree with it.
    """

    def __init__(
        self,
        *,
        symbol: str,
        session_date: str,
        cadences: tuple[Cadence, ...] = CADENCES,
    ) -> None:
        self.symbol = symbol
        self.session_date = session_date
        self.cadences = cadences
        self.book = MboBook(max_recorded_violations=0)
        self._windows = {cadence.name: WindowAccumulator() for cadence in cadences}
        self._emitted = dict.fromkeys((c.name for c in cadences), 0)
        self._next_boundary: dict[str, int | None] = {
            c.name: None for c in cadences if c.kind == "time"
        }
        self._normalizers: dict[str, dict[str, ExpandingNormalizer]] = {
            c.name: {name: ExpandingNormalizer() for name in NORMALIZED_FEATURES}
            for c in cadences
        }
        self._completed: CompletedState | None = None
        self._flast_index = 0
        self._records = 0
        self._pending_depletion: dict[str, int | None] = {"B": None, "A": None}
        self._execution_seen_at_touch: dict[str, int] = {"B": 0, "A": 0}

    # -- book reading ------------------------------------------------------

    def _touch(self) -> _TouchState:
        bid = self.book.best_bid()
        ask = self.book.best_ask()
        return _TouchState(
            bid_price=bid.price if bid else None,
            ask_price=ask.price if ask else None,
            bid_size=bid.size if bid else 0,
            ask_size=ask.size if ask else 0,
        )

    def _capture(self, event: MboEvent) -> CompletedState:
        bid = self.book.best_bid()
        ask = self.book.best_ask()
        depth = self.book.depth(max(DEPTH_LEVELS))
        levels = self.book.level_counts()
        return CompletedState(
            ts_event=event.ts_event,
            ts_recv=event.ts_recv,
            sequence=event.sequence,
            flast_index=self._flast_index,
            best_bid_price=bid.price if bid else None,
            best_ask_price=ask.price if ask else None,
            bid_size=bid.size if bid else 0,
            ask_size=ask.size if ask else 0,
            bid_count=bid.count if bid else 0,
            ask_count=ask.count if ask else 0,
            bid_depth_5=_cumulative(depth["bids"], 5, "size"),
            ask_depth_5=_cumulative(depth["asks"], 5, "size"),
            bid_depth_10=_cumulative(depth["bids"], 10, "size"),
            ask_depth_10=_cumulative(depth["asks"], 10, "size"),
            bid_order_count_5=_cumulative(depth["bids"], 5, "count"),
            ask_order_count_5=_cumulative(depth["asks"], 5, "count"),
            bid_levels=levels["bid_levels"],
            ask_levels=levels["ask_levels"],
            resting_orders=self.book.order_count(),
        )

    # -- per-event bookkeeping --------------------------------------------

    def _accumulate(self, event: MboEvent, before: _TouchState, after: _TouchState) -> None:
        action = event.action
        size = int(event.size) if event.size is not None else 0
        mid_before = _midpoint(before)
        mid_after = _midpoint(after)
        price_moved = (
            mid_before is not None and mid_after is not None and mid_before != mid_after
        )
        e_n = _cks_touch_contribution(before, after)

        for cadence in self.cadences:
            window = self._windows[cadence.name]
            window.records += 1
            if window.start_ts is None:
                window.start_ts = event.ts_event
            window.max_ts_recv = max(window.max_ts_recv, event.ts_recv)
            window.ofi += e_n

            if action == ACTION_ADD:
                window.add_count += 1
                window.add_volume += size
                if _is_at_touch(event, before):
                    window.touch_replenishment_volume += size
                    window.touch_replenishment_events += 1
                    if self._execution_seen_at_touch.get(event.side, 0):
                        window.refill_after_execution_volume += size
            elif action == ACTION_CANCEL:
                window.cancel_count += 1
                window.cancel_volume += size
            elif action == ACTION_MODIFY:
                window.modify_count += 1
            elif action == ACTION_FILL:
                # Executions and absorption only. The accompanying `T` carries
                # the trade; adding this size to aggressor volume as well would
                # count one XNAS execution twice.
                window.execution_count += 1
                window.execution_volume += size
                if not price_moved:
                    window.executions_without_price_move += 1
                    window.execution_volume_without_price_move += size
            elif action == ACTION_TRADE:
                window.trade_count += 1
                window.trade_volume += size
                # The trade names its aggressor directly.
                if event.side == SIDE_BID:
                    window.buy_aggressor_volume += size
                elif event.side == SIDE_ASK:
                    window.sell_aggressor_volume += size
                else:
                    # Auctions, non-displayed, implied, off-exchange. Counted,
                    # never signed.
                    window.unclassified_trade_volume += size

            if before.bid_price != after.bid_price:
                window.best_bid_changes += 1
            if before.ask_price != after.ask_price:
                window.best_ask_changes += 1

        for side, before_size, after_size, before_px, after_px in (
            ("B", before.bid_size, after.bid_size, before.bid_price, after.bid_price),
            ("A", before.ask_size, after.ask_size, before.ask_price, after.ask_price),
        ):
            if before_size > 0 and after_size == 0:
                for cadence in self.cadences:
                    self._windows[cadence.name].queue_depletion_events += 1
                self._pending_depletion[side] = before_px
            elif (
                self._pending_depletion[side] is not None
                and after_px != self._pending_depletion[side]
            ):
                for cadence in self.cadences:
                    self._windows[cadence.name].depletion_followed_by_quote_move += 1
                self._pending_depletion[side] = None

        if action == ACTION_FILL and event.side in (SIDE_BID, SIDE_ASK):
            self._execution_seen_at_touch[event.side] = 1
        if action == ACTION_ADD and event.side in (SIDE_BID, SIDE_ASK):
            self._execution_seen_at_touch[event.side] = 0
        if action == ACTION_CLEAR:
            self._pending_depletion = {"B": None, "A": None}
            self._execution_seen_at_touch = {"B": 0, "A": 0}

    # -- emission ----------------------------------------------------------

    def _snapshot(
        self,
        cadence: Cadence,
        state: CompletedState,
        *,
        nominal_ts: int,
        grid_ts: int | None,
    ) -> dict[str, Any]:
        window = self._windows[cadence.name]
        best_bid_price = state.best_bid_price
        best_ask_price = state.best_ask_price
        bid_size = state.bid_size
        ask_size = state.ask_size

        spread = (
            best_ask_price - best_bid_price
            if best_bid_price is not None and best_ask_price is not None
            else None
        )
        midpoint = (
            (best_bid_price + best_ask_price) / 2
            if best_bid_price is not None and best_ask_price is not None
            else None
        )
        spread_bps = spread / midpoint * 10_000 if spread is not None and midpoint else None
        touch_total = bid_size + ask_size
        queue_imbalance = _safe_ratio(bid_size, touch_total)
        normalized_qi = _safe_ratio(bid_size - ask_size, touch_total)
        microprice = (
            (best_bid_price * ask_size + best_ask_price * bid_size) / touch_total
            if touch_total and best_bid_price is not None and best_ask_price is not None
            else None
        )
        micro_minus_mid = (
            microprice - midpoint if microprice is not None and midpoint is not None else None
        )
        micro_minus_mid_bps = (
            micro_minus_mid / midpoint * 10_000
            if micro_minus_mid is not None and midpoint
            else None
        )
        mean_touch_depth = _safe_ratio(window.touch_depth_sum, window.touch_depth_samples)
        window_ns = nominal_ts - window.start_ts if window.start_ts is not None else 0
        window_seconds = window_ns / 1e9 if window_ns else None
        classified_volume = window.buy_aggressor_volume + window.sell_aggressor_volume

        # Never earlier than any record this snapshot depended on: the window's
        # records, and the state-defining event.
        available_ts_recv = max(window.max_ts_recv, state.ts_recv)

        row: dict[str, Any] = {
            "symbol": self.symbol,
            "session_date": self.session_date,
            "cadence": cadence.name,
            "sequence_index": self._emitted[cadence.name],
            "ts_event": nominal_ts,
            "grid_ts_event": grid_ts,
            "source_ts_event": state.ts_event,
            "source_ts_recv": state.ts_recv,
            "feature_available_ts_recv": available_ts_recv,
            "sequence": state.sequence,
            "flast_index": state.flast_index,
            "window_ns": window_ns,
            "window_flast_events": window.flast_events,
            "window_records": window.records,
            "best_bid_price": best_bid_price,
            "best_ask_price": best_ask_price,
            "spread": spread,
            "spread_bps": spread_bps,
            "midpoint": midpoint,
            "bid_size_l1": bid_size,
            "ask_size_l1": ask_size,
            "bid_order_count_l1": state.bid_count,
            "ask_order_count_l1": state.ask_count,
            "bid_depth_5": state.bid_depth_5,
            "ask_depth_5": state.ask_depth_5,
            "bid_depth_10": state.bid_depth_10,
            "ask_depth_10": state.ask_depth_10,
            "bid_order_count_5": state.bid_order_count_5,
            "ask_order_count_5": state.ask_order_count_5,
            "bid_levels": state.bid_levels,
            "ask_levels": state.ask_levels,
            "resting_orders": state.resting_orders,
            "queue_imbalance": queue_imbalance,
            "normalized_queue_imbalance": normalized_qi,
            "microprice": microprice,
            "microprice_minus_mid": micro_minus_mid,
            "microprice_minus_mid_bps": micro_minus_mid_bps,
            "order_flow_imbalance": window.ofi,
            "order_flow_imbalance_normalized": (
                window.ofi / mean_touch_depth if mean_touch_depth else None
            ),
            "mean_touch_depth": mean_touch_depth,
            "add_count": window.add_count,
            "add_volume": window.add_volume,
            "cancel_count": window.cancel_count,
            "cancel_volume": window.cancel_volume,
            "modify_count": window.modify_count,
            "execution_count": window.execution_count,
            "execution_volume": window.execution_volume,
            "cancel_add_ratio": _safe_ratio(window.cancel_count, window.add_count),
            "cancel_volume_ratio": _safe_ratio(window.cancel_volume, window.add_volume),
            "touch_replenishment_volume": window.touch_replenishment_volume,
            "touch_replenishment_events": window.touch_replenishment_events,
            "queue_depletion_events": window.queue_depletion_events,
            "queue_persistence": _safe_ratio(
                window.flast_with_unchanged_touch, window.flast_events
            ),
            "best_bid_changes": window.best_bid_changes,
            "best_ask_changes": window.best_ask_changes,
            "trade_count": window.trade_count,
            "trade_volume": window.trade_volume,
            "buy_aggressor_volume": window.buy_aggressor_volume,
            "sell_aggressor_volume": window.sell_aggressor_volume,
            "unclassified_trade_volume": window.unclassified_trade_volume,
            "unclassified_trade_share": _safe_ratio(
                window.unclassified_trade_volume, window.trade_volume
            ),
            "signed_trade_volume": window.buy_aggressor_volume - window.sell_aggressor_volume,
            # Over *classified* volume only, so unsignable prints cannot drag
            # the imbalance toward zero.
            "aggressor_imbalance": _safe_ratio(
                window.buy_aggressor_volume - window.sell_aggressor_volume,
                classified_volume,
            ),
            "execution_intensity": (
                window.execution_count / window_seconds if window_seconds else None
            ),
            "executions_without_price_move": window.executions_without_price_move,
            "execution_volume_without_price_move": window.execution_volume_without_price_move,
            "absorption_ratio": _safe_ratio(
                window.execution_volume_without_price_move, window.execution_volume
            ),
            "refill_after_execution_volume": window.refill_after_execution_volume,
            "depletion_followed_by_quote_move": window.depletion_followed_by_quote_move,
        }

        normalizers = self._normalizers[cadence.name]
        row["spread_bps_z"] = normalizers["spread_bps_z"].normalize_then_update(spread_bps)
        row["normalized_queue_imbalance_z"] = normalizers[
            "normalized_queue_imbalance_z"
        ].normalize_then_update(normalized_qi)
        row["order_flow_imbalance_normalized_z"] = normalizers[
            "order_flow_imbalance_normalized_z"
        ].normalize_then_update(row["order_flow_imbalance_normalized"])
        row["signed_trade_volume_z"] = normalizers[
            "signed_trade_volume_z"
        ].normalize_then_update(float(row["signed_trade_volume"]))

        self._emitted[cadence.name] += 1
        self._windows[cadence.name].reset(nominal_ts)
        return row

    def _due_time_boundaries(self, cadence: Cadence, upto_exclusive: int) -> Iterator[int]:
        """Grid boundaries strictly before ``upto_exclusive``, in order.

        Strictly before, because a boundary at exactly ``t`` may still receive
        the event at ``t`` -- ``ts_event <= t`` puts it inside that interval.
        """
        boundary = self._next_boundary[cadence.name]
        if boundary is None:
            return
        while boundary < upto_exclusive:
            yield boundary
            boundary += cadence.interval
        self._next_boundary[cadence.name] = boundary

    def _flush_time_grid(self, upto_exclusive: int) -> Iterator[dict[str, Any]]:
        """Emit every grid boundary that is now final."""
        if self._completed is None:
            return
        for cadence in self.cadences:
            if cadence.kind != "time":
                continue
            for boundary in self._due_time_boundaries(cadence, upto_exclusive):
                # An interval with no events still emits: the last known book
                # state with zero new window flow. A quiet second is an
                # observation, not a gap to skip.
                yield self._snapshot(
                    cadence, self._completed, nominal_ts=boundary, grid_ts=boundary
                )

    # -- driver ------------------------------------------------------------

    def process(self, events: Iterable[MboEvent]) -> Iterator[dict[str, Any]]:
        """Yield snapshots as the stream is consumed. Never materializes it."""
        last_ts: int | None = None
        for event in events:
            # Everything strictly before this event's timestamp is now final.
            yield from self._flush_time_grid(event.ts_event)

            self._records += 1
            before = self._touch()
            self.book.apply(event)
            after = self._touch()
            self._accumulate(event, before, after)
            last_ts = event.ts_event

            if not (event.flags & F_LAST):
                continue

            self._flast_index += 1
            touch_unchanged = (
                before.bid_price == after.bid_price and before.ask_price == after.ask_price
            )
            for cadence in self.cadences:
                window = self._windows[cadence.name]
                window.flast_events += 1
                if touch_unchanged:
                    window.flast_with_unchanged_touch += 1
                window.touch_depth_sum += (after.bid_size + after.ask_size) / 2
                window.touch_depth_samples += 1

            self._completed = self._capture(event)

            # Absolute UTC grid: the first boundary at or after the first
            # completed event. Identical for every symbol, so two files are
            # comparable at the same instant.
            for cadence in self.cadences:
                if cadence.kind == "time" and self._next_boundary[cadence.name] is None:
                    interval = cadence.interval
                    self._next_boundary[cadence.name] = (
                        (event.ts_event + interval - 1) // interval
                    ) * interval

            for cadence in self.cadences:
                if cadence.kind == "events" and (
                    self._windows[cadence.name].flast_events >= cadence.interval
                ):
                    yield self._snapshot(
                        cadence,
                        self._completed,
                        nominal_ts=event.ts_event,
                        grid_ts=None,
                    )

        # Close the grid: boundaries at or before the final event are final.
        if last_ts is not None:
            yield from self._flush_time_grid(last_ts + 1)


def _midpoint(state: _TouchState) -> float | None:
    if state.bid_price is None or state.ask_price is None:
        return None
    return (state.bid_price + state.ask_price) / 2


def _is_at_touch(event: MboEvent, before: _TouchState) -> bool:
    if event.side == SIDE_BID:
        return before.bid_price is not None and event.price >= before.bid_price
    if event.side == SIDE_ASK:
        return before.ask_price is not None and event.price <= before.ask_price
    return False


def _cks_touch_contribution(before: _TouchState, after: _TouchState) -> float:
    """CKS ``e_n`` over the touch, from reconstructed L3 state.

    The same kernel Stage 0 measured on Alpaca's NBBO -- but there the sizes
    were one venue's slice of a tied best price, which is what made 45.224 % of
    it venue rotation. Here both sides come from a single real book, so a size
    change is a liquidity event.
    """
    if None in (before.bid_price, after.bid_price, before.ask_price, after.ask_price):
        return 0.0
    pb0, pb1 = before.bid_price, after.bid_price
    pa0, pa1 = before.ask_price, after.ask_price
    qb0, qb1 = before.bid_size, after.bid_size
    qa0, qa1 = before.ask_size, after.ask_size
    bid = (qb1 if pb1 >= pb0 else 0) - (qb0 if pb1 <= pb0 else 0)
    ask = -((qa1 if pa1 <= pa0 else 0) - (qa0 if pa1 >= pa0 else 0))
    return float(bid + ask)


def _cumulative(levels: list[dict[str, Any]], depth: int, key: str) -> int:
    return sum(int(level[key]) for level in levels[:depth])


def feature_definitions() -> dict[str, Any]:
    """The frozen vocabulary, as data, for the manifest and the report."""
    return {
        "feature_engine_version": FEATURE_ENGINE_VERSION,
        "validator_version": MBO_VALIDATOR_VERSION,
        "feature_vocabulary_hash": FEATURE_VOCABULARY_HASH,
        "snapshot_schema_hash": SNAPSHOT_SCHEMA_HASH,
        "feature_semantics_hash": FEATURE_SEMANTICS_HASH,
        "superseded_engine_versions": [dict(entry) for entry in SUPERSEDED_ENGINE_VERSIONS],
        "feature_count": len(FEATURE_VOCABULARY),
        "groups": {
            "book_state": list(BOOK_STATE_FEATURES),
            "pressure": list(PRESSURE_FEATURES),
            "order_lifecycle": list(LIFECYCLE_FEATURES),
            "aggressive_flow": list(AGGRESSIVE_FLOW_FEATURES),
            "absorption_resilience": list(ABSORPTION_FEATURES),
            "prior_only_normalized": list(NORMALIZED_FEATURES),
        },
        "context_columns": list(CONTEXT_COLUMNS),
        "windowed_features": sorted(WINDOWED_FEATURES),
        "cadences": [
            {"name": c.name, "kind": c.kind, "interval": c.interval} for c in CADENCES
        ],
        "depth_levels": list(DEPTH_LEVELS),
        "min_prior_observations": MIN_PRIOR_OBSERVATIONS,
        "price_scale": FIXED_PRICE_SCALE,
        "aggressive_flow_attribution": {
            "trade_records": [
                "trade_count",
                "trade_volume",
                "buy_aggressor_volume",
                "sell_aggressor_volume",
                "signed_trade_volume",
                "aggressor_imbalance",
                "unclassified_trade_volume",
            ],
            "fill_records": [
                "execution_count",
                "execution_volume",
                "executions_without_price_move",
                "execution_volume_without_price_move",
                "absorption_ratio",
                "refill_after_execution_volume",
            ],
            "note": (
                "XNAS normalizes one displayed execution as T -> F -> C. Fills never "
                "contribute to aggressor volume; the accompanying trade already "
                "carries it."
            ),
        },
        "time_grid": (
            "Absolute UTC multiples of the interval, identical across symbols. A "
            "boundary t uses only the last completed F_LAST with ts_event <= t; an "
            "event at t+1ns belongs to the next interval. Intervals with no events "
            "emit the last known state with zero window flow."
        ),
        "causality": (
            "Every snapshot uses only events at or before its instant. Windowed "
            "features cover (previous snapshot of the same cadence, now]. "
            "Normalization is prior-only and session-local. "
            "feature_available_ts_recv never precedes any input record."
        ),
        "contains_forward_information": False,
    }
