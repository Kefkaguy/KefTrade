"""Tier-1 MBO book reconstruction and state-integrity validator.

Stage 0 retired Alpaca L1: its NBBO reports one venue's slice of a best price
that is usually tied between venues, so 45.224% of gross order-flow imbalance
was venue rotation rather than order flow.  Tier 1 replaces that feed with a
real single-venue book -- Nasdaq TotalView-ITCH via Databento, order-level.

This module does one thing: **replay MBO events into an order-ID keyed book and
report whether the reconstruction is internally consistent.**  It contains no
feature, no forward return, no prediction test, no Alpha Map cell, no strategy
and no threshold.  Whether the book is faithfully reconstructible is a
prerequisite question, and answering it first is the same discipline that made
Stage 0 cheap to abandon.

## Event semantics

Taken from Databento's own reference implementations rather than inferred:

| Action | Effect on the book |
|---|---|
| `A` Add | Insert a new order at its price level |
| `C` Cancel | Remove **some size** from a resting order; remove it at zero |
| `M` Modify | Set the order's **new absolute** price and/or size |
| `R` Clear | Remove every resting order |
| `T` Trade | An aggressing order traded. **No book change.** |
| `F` Fill | A resting order was filled. **No book change.** |
| `N` None | No book change; may still carry flags |

The `T`/`F` rule is the one most likely to be got wrong, and Databento states
the reason plainly: fills are always accompanied by a cancel or modify that
does update the book.  Applying `F` to the book as well would double-count
every execution.

Priority rules on `M`: changing price, or *increasing* size, loses queue
priority.  Decreasing size keeps it.

## Two details that decide whether the numbers mean anything

**`F_LAST`.** One venue event can normalize into several records.  The book is
only in a coherent state after a record carrying `F_LAST`, so every book-level
integrity check -- crossed book above all -- is evaluated there and nowhere
else.  Checking mid-event would report a transient half-applied book as a
violation and bury the real ones.

**Snapshots.** For MBO data from 2024-02-10 onward, Databento starts a session
with an `R` clear carrying `F_SNAPSHOT`, followed by `A` records (also
`F_SNAPSHOT`) reinserting each resting order in priority order.  Those adds are
book state, not new order events, so they are counted separately: a session
whose snapshot is missing and one whose snapshot is empty are different facts.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from sortedcontainers import SortedDict

MBO_VALIDATOR_VERSION = "tier1_mbo_book_validator_v1"

# Mirrors of the databento_dbn constants, so the pure replay core can be
# exercised without the package installed.  `assert_constants_match_databento`
# pins them against the real thing whenever it is importable.
FIXED_PRICE_SCALE = 1_000_000_000
UNDEF_PRICE = 9_223_372_036_854_775_807
UNDEF_ORDER_SIZE = 4_294_967_295

F_MAYBE_BAD_BOOK = 4
F_BAD_TS_RECV = 8
F_MBP = 16
F_SNAPSHOT = 32
F_TOB = 64
F_LAST = 128

ACTION_ADD = "A"
ACTION_CANCEL = "C"
ACTION_MODIFY = "M"
ACTION_CLEAR = "R"
ACTION_TRADE = "T"
ACTION_FILL = "F"
ACTION_NONE = "N"
BOOK_NEUTRAL_ACTIONS = frozenset({ACTION_TRADE, ACTION_FILL, ACTION_NONE})
KNOWN_ACTIONS = frozenset(
    {ACTION_ADD, ACTION_CANCEL, ACTION_MODIFY, ACTION_CLEAR, *BOOK_NEUTRAL_ACTIONS}
)

SIDE_ASK = "A"
SIDE_BID = "B"
SIDE_NONE = "N"

# ---------------------------------------------------------------------------
# Violation vocabulary.  Fixed strings, because a report is only comparable
# across runs if the categories cannot drift.
# ---------------------------------------------------------------------------

UNKNOWN_ORDER_CANCEL = "unknown_order_cancel"
UNKNOWN_ORDER_MODIFY = "unknown_order_modify"
UNKNOWN_ORDER_FILL = "unknown_order_fill"
DUPLICATE_ORDER_ADD = "duplicate_order_add"
CANCEL_EXCEEDS_RESTING_SIZE = "cancel_exceeds_resting_size"
NEGATIVE_OR_UNDEFINED_SIZE = "negative_or_undefined_size"
CROSSED_BOOK = "crossed_book"
LOCKED_BOOK = "locked_book"
SEQUENCE_REGRESSION = "sequence_regression"
TS_EVENT_REGRESSION = "ts_event_regression"
MODIFY_CHANGED_SIDE = "modify_changed_side"
INVALID_SIDE_FOR_ACTION = "invalid_side_for_action"
UNKNOWN_ACTION = "unknown_action"
UNDEF_PRICE_WITHOUT_TOB = "undef_price_without_tob"
SNAPSHOT_AFTER_SESSION_START = "snapshot_after_session_start"
FLAG_MAYBE_BAD_BOOK = "flag_maybe_bad_book"
FLAG_BAD_TS_RECV = "flag_bad_ts_recv"
# Raised once per file when the starting state was never established. See the
# initialization modes below.
UNCERTIFIED_INITIALIZATION = "uncertified_initialization"

VIOLATION_KINDS = (
    UNKNOWN_ORDER_CANCEL,
    UNKNOWN_ORDER_MODIFY,
    UNKNOWN_ORDER_FILL,
    DUPLICATE_ORDER_ADD,
    CANCEL_EXCEEDS_RESTING_SIZE,
    NEGATIVE_OR_UNDEFINED_SIZE,
    CROSSED_BOOK,
    LOCKED_BOOK,
    SEQUENCE_REGRESSION,
    TS_EVENT_REGRESSION,
    MODIFY_CHANGED_SIDE,
    INVALID_SIDE_FOR_ACTION,
    UNKNOWN_ACTION,
    UNDEF_PRICE_WITHOUT_TOB,
    SNAPSHOT_AFTER_SESSION_START,
    FLAG_MAYBE_BAD_BOOK,
    FLAG_BAD_TS_RECV,
    UNCERTIFIED_INITIALIZATION,
)

# A locked book (bid == ask) is legal on Nasdaq across venues and common at the
# open; it is recorded but is not a defect.  A crossed book on a *single* venue
# is a reconstruction failure.
NON_FATAL_VIOLATIONS = frozenset({LOCKED_BOOK, FLAG_BAD_TS_RECV})

# ---------------------------------------------------------------------------
# Initialization modes.
#
# A reconstruction is only meaningful if the state it started from is known.
# There are two ways that can be true and one way it cannot:
#
#   formal_snapshot   -- Databento marked the opening records F_SNAPSHOT, so the
#                        book was explicitly rebuilt from published state.
#   known_empty_clear -- the very first record is an `R` clear with no snapshot
#                        flag, so the book provably started empty.  This is what
#                        XNAS full-session files do: record 0 is
#                        `sequence=0 action=R side=N order_id=0`.
#   unknown           -- neither.  The replay began mid-book against state we
#                        never saw, and every count downstream is suspect.
#
# The first two are deliberately kept distinct.  A sequence-0 clear is *not* a
# snapshot: no orders were published, the book was simply empty.  Relabelling it
# would erase the difference between "we were given the state" and "there was no
# state to give", which are different guarantees.
# ---------------------------------------------------------------------------

INIT_FORMAL_SNAPSHOT = "formal_snapshot"
INIT_KNOWN_EMPTY_CLEAR = "known_empty_clear"
INIT_UNKNOWN = "unknown"
CERTIFIED_INIT_MODES = frozenset({INIT_FORMAL_SNAPSHOT, INIT_KNOWN_EMPTY_CLEAR})
INIT_MODES = (INIT_FORMAL_SNAPSHOT, INIT_KNOWN_EMPTY_CLEAR, INIT_UNKNOWN)


@dataclass(slots=True)
class MboEvent:
    """One MBO record, decoupled from databento's record type.

    Keeping the replay core independent of ``databento_dbn`` means the
    synthetic tests exercise exactly the code the real file exercises.
    """

    ts_event: int
    action: str
    side: str
    price: int
    size: int
    order_id: int
    flags: int
    sequence: int
    instrument_id: int = 0
    publisher_id: int = 0
    ts_recv: int = 0

    @classmethod
    def from_dbn(cls, record: Any) -> MboEvent:
        return cls(
            ts_event=int(record.ts_event),
            action=str(record.action),
            side=str(record.side),
            price=int(record.price),
            size=int(record.size),
            order_id=int(record.order_id),
            flags=int(record.flags),
            sequence=int(record.sequence),
            instrument_id=int(getattr(record, "instrument_id", 0) or 0),
            publisher_id=int(getattr(record, "publisher_id", 0) or 0),
            ts_recv=int(getattr(record, "ts_recv", 0) or 0),
        )


@dataclass(slots=True)
class RestingOrder:
    order_id: int
    side: str
    price: int
    size: int
    ts_event: int
    sequence: int
    is_top_of_book: bool = False


@dataclass(slots=True)
class PriceLevel:
    price: int
    size: int
    count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "price": self.price,
            "price_display": round(self.price / FIXED_PRICE_SCALE, 6),
            "size": self.size,
            "count": self.count,
        }


@dataclass(slots=True)
class Violation:
    kind: str
    sequence: int
    ts_event: int
    order_id: int
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "sequence": self.sequence,
            "ts_event": self.ts_event,
            "order_id": self.order_id,
            "detail": self.detail,
        }


class MboBook:
    """An order-ID keyed single-venue book, plus the levels it implies.

    Deviates from Databento's reference implementation in exactly one respect,
    and deliberately: where the reference asserts, this **records and
    continues**.  An assertion turns the first anomaly into a crash and hides
    every later one, which is the opposite of what a validator is for.
    """

    __slots__ = ("_counts", "_max_violations", "_violations", "asks", "bids", "orders")

    def __init__(self, *, max_recorded_violations: int = 200) -> None:
        self.orders: dict[int, RestingOrder] = {}
        self.bids: SortedDict[int, list[RestingOrder]] = SortedDict()
        self.asks: SortedDict[int, list[RestingOrder]] = SortedDict()
        self._violations: list[Violation] = []
        self._counts: Counter = Counter()
        self._max_violations = max_recorded_violations

    # -- reporting ---------------------------------------------------------

    def record(self, kind: str, event: MboEvent, detail: str) -> None:
        """Count every occurrence; retain a bounded sample of the detail.

        The count and the sample are deliberately separate.  Deriving counts
        from the retained list -- which an earlier version did -- caps them at
        the sample size, so a session with 40,000 crossed books reports 200 and
        looks merely untidy instead of broken.  Samples are bounded because a
        multi-million-record session should not be able to exhaust memory
        through its own error path.
        """
        self._counts[kind] += 1
        if len(self._violations) < self._max_violations:
            self._violations.append(
                Violation(
                    kind=kind,
                    sequence=event.sequence,
                    ts_event=event.ts_event,
                    order_id=event.order_id,
                    detail=detail,
                )
            )

    @property
    def violations(self) -> list[Violation]:
        """A bounded sample, for reading. Not a source of counts."""
        return list(self._violations)

    @property
    def counts(self) -> Counter:
        """Complete, uncapped occurrence counts by violation kind."""
        return Counter(self._counts)

    # -- inspection --------------------------------------------------------

    def _levels(self, side: str) -> SortedDict:
        if side == SIDE_ASK:
            return self.asks
        if side == SIDE_BID:
            return self.bids
        raise ValueError(f"invalid side {side!r}")

    @staticmethod
    def _to_level(price: int, orders: list[RestingOrder]) -> PriceLevel:
        return PriceLevel(
            price=price,
            size=sum(order.size for order in orders),
            # Top-of-book synthetic orders are a level, not an order count, so
            # they are excluded exactly as Databento's reference does.
            count=sum(1 for order in orders if not order.is_top_of_book),
        )

    def best_bid(self) -> PriceLevel | None:
        if not self.bids:
            return None
        price, orders = self.bids.peekitem(-1)
        return self._to_level(price, orders)

    def best_ask(self) -> PriceLevel | None:
        if not self.asks:
            return None
        price, orders = self.asks.peekitem(0)
        return self._to_level(price, orders)

    def depth(self, levels: int = 10) -> dict[str, list[dict[str, Any]]]:
        bids = [
            self._to_level(price, orders).as_dict()
            for price, orders in reversed(self.bids.items()[-levels:])
        ]
        asks = [
            self._to_level(price, orders).as_dict()
            for price, orders in self.asks.items()[:levels]
        ]
        return {"bids": bids, "asks": asks}

    def order_count(self) -> int:
        return len(self.orders)

    def level_counts(self) -> dict[str, int]:
        return {"bid_levels": len(self.bids), "ask_levels": len(self.asks)}

    def resting_size(self) -> dict[str, int]:
        return {
            "bid_size": sum(order.size for order in self.orders.values() if order.side == SIDE_BID),
            "ask_size": sum(order.size for order in self.orders.values() if order.side == SIDE_ASK),
        }

    # -- mutation ----------------------------------------------------------

    def clear(self) -> None:
        self.orders.clear()
        self.bids.clear()
        self.asks.clear()

    def _insert(self, order: RestingOrder) -> None:
        levels = self._levels(order.side)
        bucket = levels.get(order.price)
        if bucket is None:
            bucket = []
            levels[order.price] = bucket
        bucket.append(order)
        self.orders[order.order_id] = order

    def _detach(self, order: RestingOrder) -> None:
        levels = self._levels(order.side)
        bucket = levels.get(order.price)
        if bucket is not None:
            try:
                bucket.remove(order)
            except ValueError:
                pass
            if not bucket:
                levels.pop(order.price, None)

    def apply(self, event: MboEvent) -> None:
        """Apply one record.  Book-level checks are the replayer's job."""
        action = event.action

        if action not in KNOWN_ACTIONS:
            self.record(UNKNOWN_ACTION, event, f"action={action!r}")
            return

        if action == ACTION_FILL and event.order_id and event.order_id not in self.orders:
            # A fill does not change the book, but naming an order that is not
            # resting means the book and the feed disagree about what exists.
            self.record(
                UNKNOWN_ORDER_FILL, event, f"fill referenced absent order {event.order_id}"
            )

        if action in BOOK_NEUTRAL_ACTIONS:
            return

        if action == ACTION_CLEAR:
            self.clear()
            return

        if event.side not in (SIDE_ASK, SIDE_BID):
            self.record(
                INVALID_SIDE_FOR_ACTION, event, f"action={action} with side={event.side!r}"
            )
            return

        if event.size == UNDEF_ORDER_SIZE or event.size < 0:
            self.record(NEGATIVE_OR_UNDEFINED_SIZE, event, f"size={event.size}")
            return

        is_tob = bool(event.flags & F_TOB)
        if event.price == UNDEF_PRICE:
            if not is_tob:
                self.record(UNDEF_PRICE_WITHOUT_TOB, event, f"action={action}")
                return
            # Top-of-book publisher signalling that the side has no depth.
            for order in [o for o in self.orders.values() if o.side == event.side]:
                self._detach(order)
                self.orders.pop(order.order_id, None)
            self._levels(event.side).clear()
            return

        if action == ACTION_ADD:
            self._apply_add(event, is_tob=is_tob)
        elif action == ACTION_CANCEL:
            self._apply_cancel(event)
        elif action == ACTION_MODIFY:
            self._apply_modify(event, is_tob=is_tob)

    def _apply_add(self, event: MboEvent, *, is_tob: bool) -> None:
        if is_tob:
            # Top-of-book normalization: the add replaces the whole side.
            for order in [o for o in self.orders.values() if o.side == event.side]:
                self.orders.pop(order.order_id, None)
            self._levels(event.side).clear()
        elif event.order_id in self.orders:
            existing = self.orders[event.order_id]
            self.record(
                DUPLICATE_ORDER_ADD,
                event,
                f"order {event.order_id} already resting at {existing.price} size {existing.size}",
            )
            # Replace rather than double-count: keeping both would corrupt
            # every level statistic downstream of this point.
            self._detach(existing)
            self.orders.pop(event.order_id, None)
        self._insert(
            RestingOrder(
                order_id=event.order_id,
                side=event.side,
                price=event.price,
                size=event.size,
                ts_event=event.ts_event,
                sequence=event.sequence,
                is_top_of_book=is_tob,
            )
        )

    def _apply_cancel(self, event: MboEvent) -> None:
        order = self.orders.get(event.order_id)
        if order is None:
            self.record(
                UNKNOWN_ORDER_CANCEL, event, f"cancel for absent order {event.order_id}"
            )
            return
        if event.size > order.size:
            self.record(
                CANCEL_EXCEEDS_RESTING_SIZE,
                event,
                f"cancel {event.size} against resting {order.size}",
            )
            # Clamp at zero. A negative resting size is not a market state.
            order.size = 0
        else:
            order.size -= event.size
        if order.size == 0:
            self._detach(order)
            self.orders.pop(event.order_id, None)

    def _apply_modify(self, event: MboEvent, *, is_tob: bool) -> None:
        order = self.orders.get(event.order_id)
        if order is None:
            self.record(
                UNKNOWN_ORDER_MODIFY, event, f"modify for absent order {event.order_id}"
            )
            # Databento's reference treats an unknown modify as an add; follow
            # it so the book stays usable, and report that we had to.
            self._apply_add(event, is_tob=is_tob)
            return
        if order.side != event.side:
            self.record(
                MODIFY_CHANGED_SIDE, event, f"{order.side} -> {event.side}"
            )
            self._detach(order)
            self.orders.pop(event.order_id, None)
            self._apply_add(event, is_tob=is_tob)
            return

        loses_priority = order.price != event.price or event.size > order.size
        if order.price != event.price:
            self._detach(order)
            order.price = event.price
            order.size = event.size
            order.ts_event = event.ts_event
            order.sequence = event.sequence
            self._insert(order)
            return
        if loses_priority:
            # Same price, larger size: goes to the back of its own level.
            bucket = self._levels(order.side).get(order.price)
            if bucket is not None:
                try:
                    bucket.remove(order)
                except ValueError:
                    pass
                bucket.append(order)
            order.ts_event = event.ts_event
            order.sequence = event.sequence
        order.size = event.size
        if order.size == 0:
            self._detach(order)
            self.orders.pop(event.order_id, None)


@dataclass
class ReplayState:
    """Counters accumulated across a replay."""

    records: int = 0
    by_action: Counter = field(default_factory=Counter)
    by_side: Counter = field(default_factory=Counter)
    snapshot_records: int = 0
    snapshot_adds: int = 0
    snapshot_clears: int = 0
    clears: int = 0
    last_records: int = 0
    tob_records: int = 0
    mbp_records: int = 0
    violations: Counter = field(default_factory=Counter)
    first_ts_event: int | None = None
    last_ts_event: int | None = None
    first_sequence: int | None = None
    last_sequence: int | None = None
    instrument_ids: set[int] = field(default_factory=set)
    publisher_ids: set[int] = field(default_factory=set)
    max_order_count: int = 0
    crossed_events: int = 0
    locked_events: int = 0
    book_states_checked: int = 0
    # Databento defines F_MAYBE_BAD_BOOK as an unrecoverable channel gap: the
    # book may be missing updates it will never receive. Counted on its own so
    # certification can turn on it directly rather than on a derived total.
    maybe_bad_book_records: int = 0
    # F_BAD_TS_RECV is a receive-clock problem, not a book-state error. Split
    # because a snapshot-only occurrence says nothing about the live stream,
    # while live occurrences are exactly what later latency work needs to know.
    bad_ts_recv_snapshot_records: int = 0
    bad_ts_recv_live_records: int = 0
    # How the book's starting state was established. See INIT_* above.
    init_mode: str = INIT_UNKNOWN
    init_first_action: str | None = None
    init_first_sequence: int | None = None
    init_first_flags: int | None = None
    init_records_before: int = 0
    init_resolved: bool = False

    @property
    def init_certified(self) -> bool:
        return self.init_mode in CERTIFIED_INIT_MODES


def replay(
    events: Iterable[MboEvent],
    *,
    max_recorded_violations: int = 200,
    check_crossed_on_last_only: bool = True,
) -> tuple[MboBook, ReplayState, list[Violation]]:
    """Replay MBO events into a book and collect integrity findings.

    ``check_crossed_on_last_only`` exists to be turned **off** in tests, to
    demonstrate that it is what suppresses false crossed-book reports rather
    than the book happening never to cross mid-event.
    """
    book = MboBook(max_recorded_violations=max_recorded_violations)
    state = ReplayState()
    session_started = False
    last_live_sequence: int | None = None
    last_live_ts_event: int | None = None

    for event in events:
        state.records += 1
        state.by_action[event.action] += 1
        state.by_side[event.side] += 1
        state.instrument_ids.add(event.instrument_id)
        state.publisher_ids.add(event.publisher_id)

        flags = event.flags
        is_snapshot = bool(flags & F_SNAPSHOT)
        is_last = bool(flags & F_LAST)

        # Resolve how this file's starting state was established, once, from
        # the leading records. Book-neutral actions (T/F/N) are scanned past
        # rather than treated as initialization, because they mutate nothing.
        if not state.init_resolved:
            index = state.records - 1
            if state.records == 1:
                state.init_first_action = event.action
                state.init_first_sequence = event.sequence
                state.init_first_flags = event.flags
            if is_snapshot:
                state.init_mode = INIT_FORMAL_SNAPSHOT
                state.init_records_before = index
                state.init_resolved = True
            elif event.action == ACTION_CLEAR:
                # A clear proves the book is empty only if nothing preceded it.
                # Anywhere but record 0 it may be clearing state we never saw.
                state.init_mode = (
                    INIT_KNOWN_EMPTY_CLEAR if index == 0 else INIT_UNKNOWN
                )
                state.init_records_before = index
                state.init_resolved = True
            elif event.action in (ACTION_ADD, ACTION_CANCEL, ACTION_MODIFY):
                # An order mutation before any clear or snapshot: the replay is
                # building on state it was never given.
                state.init_mode = INIT_UNKNOWN
                state.init_records_before = index
                state.init_resolved = True
        if is_snapshot:
            state.snapshot_records += 1
            if event.action == ACTION_ADD:
                state.snapshot_adds += 1
            elif event.action == ACTION_CLEAR:
                state.snapshot_clears += 1
            if session_started:
                book.record(
                    SNAPSHOT_AFTER_SESSION_START,
                    event,
                    "snapshot record arrived after non-snapshot traffic",
                )
        else:
            session_started = True
        if is_last:
            state.last_records += 1
        if flags & F_TOB:
            state.tob_records += 1
        if flags & F_MBP:
            state.mbp_records += 1
        if flags & F_MAYBE_BAD_BOOK:
            # An unrecoverable channel gap. The reconstruction downstream of
            # this record may be missing updates that will never arrive, so no
            # amount of internal consistency afterwards makes the session
            # trustworthy. Certification fails on a single occurrence.
            state.maybe_bad_book_records += 1
            book.record(
                FLAG_MAYBE_BAD_BOOK,
                event,
                "F_MAYBE_BAD_BOOK set by publisher: unrecoverable channel gap",
            )
        if flags & F_BAD_TS_RECV:
            where = "snapshot" if is_snapshot else "live"
            if is_snapshot:
                state.bad_ts_recv_snapshot_records += 1
            else:
                state.bad_ts_recv_live_records += 1
            book.record(
                FLAG_BAD_TS_RECV, event, f"F_BAD_TS_RECV set by publisher ({where} record)"
            )

        if event.action == ACTION_CLEAR:
            state.clears += 1

        # Monotonicity, measured over the live stream only. Snapshot records
        # carry the snapshot's own generation timestamp and sequence, which
        # legitimately sit outside the live ordering -- so they are neither
        # checked nor allowed to become the baseline the next record is checked
        # against. Letting them set the baseline reports the first live record
        # as a regression against the snapshot, which is not a defect in the
        # feed.
        if not is_snapshot:
            if last_live_sequence is not None and event.sequence < last_live_sequence:
                book.record(
                    SEQUENCE_REGRESSION,
                    event,
                    f"sequence {event.sequence} < previous {last_live_sequence}",
                )
            if last_live_ts_event is not None and event.ts_event < last_live_ts_event:
                book.record(
                    TS_EVENT_REGRESSION,
                    event,
                    f"ts_event {event.ts_event} < previous {last_live_ts_event}",
                )
            last_live_sequence = event.sequence
            last_live_ts_event = event.ts_event

        # Reporting bounds cover every record, snapshot included.
        if state.first_ts_event is None:
            state.first_ts_event = event.ts_event
            state.first_sequence = event.sequence
        state.last_ts_event = event.ts_event
        state.last_sequence = event.sequence

        book.apply(event)

        state.max_order_count = max(state.max_order_count, book.order_count())

        # The book is only coherent at an event boundary.
        if is_last or not check_crossed_on_last_only:
            state.book_states_checked += 1
            bid = book.best_bid()
            ask = book.best_ask()
            if bid is not None and ask is not None:
                if bid.price > ask.price:
                    state.crossed_events += 1
                    book.record(
                        CROSSED_BOOK,
                        event,
                        f"bid {bid.price} > ask {ask.price}",
                    )
                elif bid.price == ask.price:
                    state.locked_events += 1
                    book.record(LOCKED_BOOK, event, f"bid == ask == {bid.price}")

    # An unresolved initialization means the stream held nothing but
    # book-neutral records, which is not a starting state either.
    if not state.init_resolved:
        state.init_mode = INIT_UNKNOWN
        state.init_records_before = state.records

    # Counts come from the book's uncapped counter, never from the bounded
    # sample list.
    state.violations = book.counts
    if not state.init_certified:
        state.violations[UNCERTIFIED_INITIALIZATION] = 1
    return book, state, book.violations


def iter_dbn_events(path: str) -> Iterator[MboEvent]:
    """Stream MBO records from a DBN(.zst) file without materializing them.

    A single symbol-session of a liquid name is millions of records; the Stage 0
    quote work already established that holding one is how this runs out of
    memory.
    """
    import databento as db

    store = db.DBNStore.from_file(path)
    for record in store:
        if getattr(record, "action", None) is None or not hasattr(record, "order_id"):
            continue  # metadata, symbol mappings, error records
        yield MboEvent.from_dbn(record)


def _uncertified_reason(state: ReplayState) -> str | None:
    """Why certification was withdrawn, most decisive cause first."""
    reasons: list[str] = []
    if state.maybe_bad_book_records:
        reasons.append(
            "F_MAYBE_BAD_BOOK: Databento reported an unrecoverable channel gap on "
            f"{state.maybe_bad_book_records} record(s); the book may be missing "
            "updates that will never arrive"
        )
    if not state.init_certified:
        reasons.append(
            f"initialization mode {state.init_mode!r}: the replay began from a "
            "state that was never established, so the reconstruction is "
            f"building on unseen orders (first action {state.init_first_action!r}, "
            f"{state.init_records_before} record(s) before initialization)"
        )
    return "; ".join(reasons) or None


def validation_report(
    book: MboBook,
    state: ReplayState,
    violations: list[Violation],
    *,
    source: str,
    depth_levels: int = 10,
) -> dict[str, Any]:
    """The deliverable: what was replayed, what the book looks like, what broke."""
    fatal = {
        kind: count
        for kind, count in state.violations.items()
        if kind not in NON_FATAL_VIOLATIONS and count
    }
    bid = book.best_bid()
    ask = book.best_ask()
    spread = (ask.price - bid.price) if (bid and ask) else None
    # Every action, including the ones that did not occur. A zero is a finding:
    # Nasdaq TotalView normalizes an order replace as C(old id) + A(new id), so
    # few or no `M` records is expected rather than evidence of a parsing bug.
    # Printing only the observed keys makes that impossible to see.
    by_action = {action: state.by_action.get(action, 0) for action in sorted(KNOWN_ACTIONS)}
    return {
        "validator_version": MBO_VALIDATOR_VERSION,
        "source": source,
        "replay": {
            "records": state.records,
            "by_action": by_action,
            "by_action_observed": dict(sorted(state.by_action.items())),
            "by_side": dict(sorted(state.by_side.items())),
            "instrument_ids": sorted(state.instrument_ids),
            "publisher_ids": sorted(state.publisher_ids),
            "first_ts_event": state.first_ts_event,
            "last_ts_event": state.last_ts_event,
            "first_sequence": state.first_sequence,
            "last_sequence": state.last_sequence,
        },
        "snapshot": {
            "snapshot_records": state.snapshot_records,
            "snapshot_clears": state.snapshot_clears,
            "snapshot_adds": state.snapshot_adds,
            "total_clears": state.clears,
            "snapshot_present": state.snapshot_records > 0,
        },
        "initialization": {
            "mode": state.init_mode,
            "certified": state.init_certified,
            "first_action": state.init_first_action,
            "first_sequence": state.init_first_sequence,
            "first_flags": state.init_first_flags,
            "records_before_initialization": state.init_records_before,
        },
        "flags": {
            "f_last_records": state.last_records,
            "f_tob_records": state.tob_records,
            "f_mbp_records": state.mbp_records,
            "book_states_checked": state.book_states_checked,
            # An unrecoverable channel gap. Non-zero means the reconstruction
            # is missing updates it will never receive.
            "f_maybe_bad_book_records": state.maybe_bad_book_records,
            # A receive-clock problem, not a book-state error. Split so that a
            # snapshot-only occurrence -- which says nothing about the live
            # stream -- is distinguishable from live occurrences, which are
            # what later latency and timestamp-quality work needs.
            "f_bad_ts_recv_records": (
                state.bad_ts_recv_snapshot_records + state.bad_ts_recv_live_records
            ),
            "f_bad_ts_recv_snapshot_records": state.bad_ts_recv_snapshot_records,
            "f_bad_ts_recv_live_records": state.bad_ts_recv_live_records,
        },
        "final_book": {
            "best_bid": bid.as_dict() if bid else None,
            "best_ask": ask.as_dict() if ask else None,
            "spread": spread,
            "spread_display": (
                round(spread / FIXED_PRICE_SCALE, 6) if spread is not None else None
            ),
            "resting_orders": book.order_count(),
            **book.level_counts(),
            **book.resting_size(),
            "depth": book.depth(depth_levels),
        },
        "peak_resting_orders": state.max_order_count,
        "integrity": {
            "violation_counts": {
                kind: state.violations.get(kind, 0) for kind in VIOLATION_KINDS
            },
            "fatal_violation_counts": fatal,
            "crossed_book_events": state.crossed_events,
            "locked_book_events": state.locked_events,
            "clean": not fatal,
            # Certification is a stricter statement than `clean`: a single
            # F_MAYBE_BAD_BOOK record withdraws it on its own, because internal
            # consistency after an unrecoverable gap says nothing about the
            # updates that never arrived. `clean` already fails on it -- this
            # names *why* rather than leaving a reader to infer it from a
            # violation table.
            "certified": (
                (not fatal)
                and state.maybe_bad_book_records == 0
                and state.init_certified
            ),
            "uncertified_reason": _uncertified_reason(state),
            "sample_violations": [item.as_dict() for item in violations[:25]],
            "sample_truncated": len(violations) < sum(state.violations.values()),
        },
    }


def validate_dbn_file(path: str, *, depth_levels: int = 10) -> dict[str, Any]:
    """Replay one DBN file end to end and return its validation report."""
    book, state, violations = replay(iter_dbn_events(path))
    return validation_report(book, state, violations, source=path, depth_levels=depth_levels)


def assert_constants_match_databento() -> dict[str, bool]:
    """Pin the local mirrors against ``databento_dbn`` when it is installed."""
    import databento_dbn as dbn

    checks = {
        "FIXED_PRICE_SCALE": FIXED_PRICE_SCALE == dbn.FIXED_PRICE_SCALE,
        "UNDEF_PRICE": UNDEF_PRICE == dbn.UNDEF_PRICE,
        "UNDEF_ORDER_SIZE": UNDEF_ORDER_SIZE == dbn.UNDEF_ORDER_SIZE,
        "F_LAST": F_LAST == dbn.F_LAST,
        "F_TOB": F_TOB == dbn.F_TOB,
        "F_SNAPSHOT": F_SNAPSHOT == dbn.F_SNAPSHOT,
        "F_MBP": F_MBP == dbn.F_MBP,
        "F_BAD_TS_RECV": F_BAD_TS_RECV == dbn.F_BAD_TS_RECV,
        "F_MAYBE_BAD_BOOK": F_MAYBE_BAD_BOOK == dbn.F_MAYBE_BAD_BOOK,
        "ACTION_ADD": ACTION_ADD == str(dbn.Action.ADD),
        "ACTION_CANCEL": ACTION_CANCEL == str(dbn.Action.CANCEL),
        "ACTION_MODIFY": ACTION_MODIFY == str(dbn.Action.MODIFY),
        "ACTION_CLEAR": ACTION_CLEAR == str(dbn.Action.CLEAR),
        "ACTION_TRADE": ACTION_TRADE == str(dbn.Action.TRADE),
        "ACTION_FILL": ACTION_FILL == str(dbn.Action.FILL),
        "ACTION_NONE": ACTION_NONE == str(dbn.Action.NONE),
        "SIDE_ASK": SIDE_ASK == str(dbn.Side.ASK),
        "SIDE_BID": SIDE_BID == str(dbn.Side.BID),
        "SIDE_NONE": SIDE_NONE == str(dbn.Side.NONE),
    }
    return checks
