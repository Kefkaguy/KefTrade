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

## Causality

Every snapshot is emitted at a completed `F_LAST` boundary and describes only
events at or before it. Three specific disciplines:

* **Windowed counters cover `(previous snapshot of this cadence, now]`.** They
  are reset on emission, so a window can never reach forward.
* **Normalization is prior-only and session-local.** Expanding mean and
  variance are computed from snapshots *strictly before* the current one,
  within the same symbol-day. A statistic that needed the whole session would
  leak the session's future into its own past.
* **Nothing is carried across symbol-days.** Each file starts cold.

## The polarity trap

Databento's `side` means opposite things on `T` and `F`, and signing both the
same way inverts every aggressor feature:

| Action | `side=B` | Signed aggressive flow |
|---|---|---|
| `T` Trade | the trade aggressor was a **buyer** | **+** |
| `F` Fill | a resting **buy** order was filled | **−** (a seller hit it) |

`side=N` is never signed. Databento list the cases: auctions, trades against
non-displayed orders, implied orders, off-exchange prints, and sources that do
not disseminate a side. Those still count toward volume and are reported as
unclassified, exactly as the Stage 0 trade-flow work did -- dividing signed flow
by total rather than by classified volume would drag every window toward zero in
proportion to how many prints happened to be unsignable.

## Raw primitives are preserved

Every ratio is stored beside the counts it came from, so any derived value can
be recomputed and audited. `cancel_add_ratio` without `cancel_count` and
`add_count` is an assertion; with them it is a measurement.
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

FEATURE_ENGINE_VERSION = "tier1_mbo_feature_engine_v1"

# Depth is reported at these fixed level counts. Declared, not tuned.
DEPTH_LEVELS: tuple[int, ...] = (1, 5, 10)

# Prior-only normalization is withheld below this many prior observations
# rather than computed from a handful and presented as a z-score.
MIN_PRIOR_OBSERVATIONS = 30


# ---------------------------------------------------------------------------
# Sampling cadences
# ---------------------------------------------------------------------------


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
#
# Frozen 2026-08-16, before any predictive outcome was inspected. Adding,
# removing or renaming a column changes FEATURE_VOCABULARY_HASH, which is
# recorded in every manifest -- so a vocabulary that moved after results were
# seen is visible rather than deniable.

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
    "ts_event",
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

FEATURE_VOCABULARY_HASH = hashlib.sha256(
    "\n".join(FEATURE_VOCABULARY).encode("utf-8")
).hexdigest()

# Features whose value is a window aggregate rather than an instantaneous book
# reading. Recorded so a consumer cannot mistake one for the other.
WINDOWED_FEATURES: frozenset[str] = frozenset(
    (*LIFECYCLE_FEATURES, *AGGRESSIVE_FLOW_FEATURES, *ABSORPTION_FEATURES)
) | {"order_flow_imbalance", "order_flow_imbalance_normalized", "mean_touch_depth"}


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


# ---------------------------------------------------------------------------
# Prior-only normalization
# ---------------------------------------------------------------------------


class ExpandingNormalizer:
    """Welford mean/variance over **strictly prior** observations.

    ``value`` is normalized against the statistics of everything seen before
    it, then folded in. Folding first would let an observation normalize
    against itself, which is a small leak that flatters exactly the extreme
    readings a later stage would care about.
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


# ---------------------------------------------------------------------------
# Window accumulator
# ---------------------------------------------------------------------------


@dataclass
class WindowAccumulator:
    """Everything that happened since this cadence last emitted.

    Reset on emission, so the window is ``(previous snapshot, now]`` and cannot
    reach forward by construction.
    """

    records: int = 0
    flast_events: int = 0
    start_ts: int | None = None

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

    def reset(self, ts: int) -> None:
        for name, default in (
            ("records", 0),
            ("flast_events", 0),
            ("add_count", 0),
            ("add_volume", 0),
            ("cancel_count", 0),
            ("cancel_volume", 0),
            ("modify_count", 0),
            ("execution_count", 0),
            ("execution_volume", 0),
            ("trade_count", 0),
            ("trade_volume", 0),
            ("buy_aggressor_volume", 0),
            ("sell_aggressor_volume", 0),
            ("unclassified_trade_volume", 0),
            ("ofi", 0.0),
            ("touch_depth_sum", 0.0),
            ("touch_depth_samples", 0),
            ("touch_replenishment_volume", 0),
            ("touch_replenishment_events", 0),
            ("queue_depletion_events", 0),
            ("depletion_followed_by_quote_move", 0),
            ("best_bid_changes", 0),
            ("best_ask_changes", 0),
            ("flast_with_unchanged_touch", 0),
            ("executions_without_price_move", 0),
            ("execution_volume_without_price_move", 0),
            ("refill_after_execution_volume", 0),
        ):
            setattr(self, name, default)
        self.start_ts = ts


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _TouchState:
    bid_price: int | None = None
    ask_price: int | None = None
    bid_size: int = 0
    ask_size: int = 0


class OrderBookFeatureEngine:
    """Replays MBO events, maintaining a book and emitting causal snapshots.

    The book is the *same* ``MboBook`` the Tier-1 validator uses -- the gate
    that passed 160/160 validated this exact reconstruction, so the features
    are built on the artefact that was certified rather than a second
    implementation that might disagree with it.
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
        self._next_time_boundary: dict[str, int | None] = {
            c.name: None for c in cadences if c.kind == "time"
        }
        self._normalizers: dict[str, dict[str, ExpandingNormalizer]] = {
            c.name: {name: ExpandingNormalizer() for name in NORMALIZED_FEATURES}
            for c in cadences
        }
        self._touch = _TouchState()
        self._flast_index = 0
        self._records = 0
        # Set when the touch empties, cleared when the price then moves, so
        # "depletion followed by a quote move" is a sequence rather than a
        # coincidence of two counters in the same window.
        self._pending_depletion: dict[str, int | None] = {"B": None, "A": None}
        self._execution_seen_at_touch: dict[str, int] = {"B": 0, "A": 0}

    # -- per-event bookkeeping --------------------------------------------

    def _observe_pre_state(self) -> _TouchState:
        bid = self.book.best_bid()
        ask = self.book.best_ask()
        return _TouchState(
            bid_price=bid.price if bid else None,
            ask_price=ask.price if ask else None,
            bid_size=bid.size if bid else 0,
            ask_size=ask.size if ask else 0,
        )

    def _accumulate(self, event: MboEvent, before: _TouchState, after: _TouchState) -> None:
        action = event.action
        size = int(event.size) if event.size is not None else 0
        mid_before = _midpoint(before)
        mid_after = _midpoint(after)
        price_moved = mid_before is not None and mid_after is not None and mid_before != mid_after

        # Cont-Kukanov-Stoikov e_n over the touch, computed on the same kernel
        # the validator uses, but from L3 state rather than a vendor NBBO.
        e_n = _cks_touch_contribution(before, after)

        for cadence in self.cadences:
            window = self._windows[cadence.name]
            window.records += 1
            if window.start_ts is None:
                window.start_ts = event.ts_event
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
                window.execution_count += 1
                window.execution_volume += size
                if not price_moved:
                    window.executions_without_price_move += 1
                    window.execution_volume_without_price_move += size
                # A fill names the resting side; the aggressor is the opposite.
                if event.side == SIDE_BID:
                    window.sell_aggressor_volume += size
                elif event.side == SIDE_ASK:
                    window.buy_aggressor_volume += size
            elif action == ACTION_TRADE:
                window.trade_count += 1
                window.trade_volume += size
                # A trade names the aggressor directly.
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

        # Depletion is a book transition, tracked once rather than per cadence.
        for side, before_size, after_size, before_px, after_px in (
            ("B", before.bid_size, after.bid_size, before.bid_price, after.bid_price),
            ("A", before.ask_size, after.ask_size, before.ask_price, after.ask_price),
        ):
            if before_size > 0 and after_size == 0:
                for cadence in self.cadences:
                    self._windows[cadence.name].queue_depletion_events += 1
                self._pending_depletion[side] = before_px
            elif self._pending_depletion[side] is not None and after_px != self._pending_depletion[side]:
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

    def _due(self, cadence: Cadence, event: MboEvent) -> bool:
        if cadence.kind == "events":
            return self._windows[cadence.name].flast_events >= cadence.interval
        boundary = self._next_time_boundary[cadence.name]
        if boundary is None:
            # Anchor the grid to the first F_LAST seen, so a session that opens
            # at an arbitrary nanosecond is not sampled on a fictional clock.
            self._next_time_boundary[cadence.name] = event.ts_event + cadence.interval
            return False
        return event.ts_event >= boundary

    def _snapshot(self, cadence: Cadence, event: MboEvent) -> dict[str, Any]:
        window = self._windows[cadence.name]
        bid = self.book.best_bid()
        ask = self.book.best_ask()
        depth = self.book.depth(max(DEPTH_LEVELS))

        best_bid_price = bid.price if bid else None
        best_ask_price = ask.price if ask else None
        bid_size = bid.size if bid else 0
        ask_size = ask.size if ask else 0
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
        spread_bps = (
            spread / midpoint * 10_000 if spread is not None and midpoint else None
        )
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
        window_ns = (
            event.ts_event - window.start_ts if window.start_ts is not None else 0
        )
        window_seconds = window_ns / 1e9 if window_ns else None

        classified_volume = window.buy_aggressor_volume + window.sell_aggressor_volume
        total_trade_volume = window.trade_volume

        row: dict[str, Any] = {
            "symbol": self.symbol,
            "session_date": self.session_date,
            "cadence": cadence.name,
            "sequence_index": self._emitted[cadence.name],
            "ts_event": event.ts_event,
            "sequence": event.sequence,
            "flast_index": self._flast_index,
            "window_ns": window_ns,
            "window_flast_events": window.flast_events,
            "window_records": window.records,
            # -- book state
            "best_bid_price": best_bid_price,
            "best_ask_price": best_ask_price,
            "spread": spread,
            "spread_bps": spread_bps,
            "midpoint": midpoint,
            "bid_size_l1": bid_size,
            "ask_size_l1": ask_size,
            "bid_order_count_l1": bid.count if bid else 0,
            "ask_order_count_l1": ask.count if ask else 0,
            "bid_depth_5": _cumulative(depth["bids"], 5, "size"),
            "ask_depth_5": _cumulative(depth["asks"], 5, "size"),
            "bid_depth_10": _cumulative(depth["bids"], 10, "size"),
            "ask_depth_10": _cumulative(depth["asks"], 10, "size"),
            "bid_order_count_5": _cumulative(depth["bids"], 5, "count"),
            "ask_order_count_5": _cumulative(depth["asks"], 5, "count"),
            "bid_levels": self.book.level_counts()["bid_levels"],
            "ask_levels": self.book.level_counts()["ask_levels"],
            "resting_orders": self.book.order_count(),
            # -- pressure
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
            # -- lifecycle
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
            # -- aggressive flow
            "trade_count": window.trade_count,
            "trade_volume": total_trade_volume,
            "buy_aggressor_volume": window.buy_aggressor_volume,
            "sell_aggressor_volume": window.sell_aggressor_volume,
            "unclassified_trade_volume": window.unclassified_trade_volume,
            "unclassified_trade_share": _safe_ratio(
                window.unclassified_trade_volume, total_trade_volume
            ),
            "signed_trade_volume": window.buy_aggressor_volume - window.sell_aggressor_volume,
            # Over *classified* volume only. Dividing by total would drag the
            # imbalance toward zero in proportion to unsignable prints.
            "aggressor_imbalance": _safe_ratio(
                window.buy_aggressor_volume - window.sell_aggressor_volume,
                classified_volume,
            ),
            "execution_intensity": (
                window.execution_count / window_seconds if window_seconds else None
            ),
            # -- absorption / resilience
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
        return row

    # -- driver ------------------------------------------------------------

    def process(self, events: Iterable[MboEvent]) -> Iterator[dict[str, Any]]:
        """Yield snapshots as the stream is consumed. Never materializes it."""
        for event in events:
            self._records += 1
            before = self._observe_pre_state()
            self.book.apply(event)
            after = self._observe_pre_state()
            self._accumulate(event, before, after)
            self._touch = after

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

            for cadence in self.cadences:
                if self._due(cadence, event):
                    yield self._snapshot(cadence, event)
                    self._windows[cadence.name].reset(event.ts_event)
                    if cadence.kind == "time":
                        boundary = self._next_time_boundary[cadence.name]
                        assert boundary is not None
                        # Step to the first boundary strictly after this event,
                        # so a gap in the stream skips grid points instead of
                        # emitting a burst of empty windows.
                        while boundary <= event.ts_event:
                            boundary += cadence.interval
                        self._next_time_boundary[cadence.name] = boundary


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
        "causality": (
            "Every snapshot is emitted at a completed F_LAST boundary and uses only "
            "events at or before it. Windowed features cover (previous snapshot of "
            "the same cadence, now]. Normalization is prior-only and session-local."
        ),
        "contains_forward_information": False,
    }
