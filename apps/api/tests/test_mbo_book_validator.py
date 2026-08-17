"""Tier-1 MBO book reconstruction: synthetic semantics plus a real-file check.

The synthetic sequences pin each Databento MBO action against the semantics
published in their reference implementations. The integration test replays the
real CMCSA file when it is present and skips when it is not, so the suite runs
on a laptop and means something on the box that holds the data.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services.mbo_book_validator import (
    CANCEL_EXCEEDS_RESTING_SIZE,
    CROSSED_BOOK,
    DUPLICATE_ORDER_ADD,
    F_BAD_TS_RECV,
    F_LAST,
    F_MAYBE_BAD_BOOK,
    F_SNAPSHOT,
    F_TOB,
    FIXED_PRICE_SCALE,
    FLAG_MAYBE_BAD_BOOK,
    INIT_FORMAL_SNAPSHOT,
    INIT_KNOWN_EMPTY_CLEAR,
    INIT_UNKNOWN,
    INVALID_SIDE_FOR_ACTION,
    LOCKED_BOOK,
    MODIFY_CHANGED_SIDE,
    NEGATIVE_OR_UNDEFINED_SIZE,
    SEQUENCE_REGRESSION,
    TS_EVENT_REGRESSION,
    UNCERTIFIED_INITIALIZATION,
    UNDEF_ORDER_SIZE,
    UNDEF_PRICE,
    UNKNOWN_ORDER_CANCEL,
    UNKNOWN_ORDER_FILL,
    UNKNOWN_ORDER_MODIFY,
    MboEvent,
    assert_constants_match_databento,
    replay,
    validation_report,
)

PX = FIXED_PRICE_SCALE  # 1.00 in fixed-point


def ev(
    action,
    side="B",
    price=0,
    size=0,
    order_id=0,
    *,
    seq=None,
    ts=None,
    flags=F_LAST,
) -> MboEvent:
    """One event. `seq`/`ts` default to a monotonically increasing counter."""
    ev.counter = getattr(ev, "counter", 0) + 1
    return MboEvent(
        ts_event=ev.counter * 1_000_000 if ts is None else ts,
        action=action,
        side=side,
        price=price,
        size=size,
        order_id=order_id,
        flags=flags,
        sequence=ev.counter if seq is None else seq,
    )


def run(events):
    return replay(events)


def opened(events):
    """Prepend the XNAS session opening: a sequence-0 `R` clear.

    Tests that assert on `certified` or `clean` need a session whose starting
    state was established, because an unknown initialization now fails both.
    Real files open exactly this way, so this makes the fixtures more faithful
    rather than less.
    """
    opening = MboEvent(
        ts_event=0,
        action="R",
        side="N",
        price=0,
        size=0,
        order_id=0,
        flags=F_BAD_TS_RECV,
        sequence=0,
    )
    return [opening, *events]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_local_constants_match_databento_exactly():
    """Mirrored constants must not drift from the package that defines them."""
    checks = assert_constants_match_databento()
    assert all(checks.values()), [k for k, v in checks.items() if not v]


# ---------------------------------------------------------------------------
# Add / Cancel / Modify / Clear
# ---------------------------------------------------------------------------


def test_add_builds_levels_and_bbo():
    book, state, _ = run(
        [
            ev("A", "B", 100 * PX, 500, 1),
            ev("A", "B", 99 * PX, 300, 2),
            ev("A", "A", 101 * PX, 400, 3),
            ev("A", "A", 102 * PX, 200, 4),
        ]
    )
    assert book.best_bid().price == 100 * PX
    assert book.best_bid().size == 500
    assert book.best_ask().price == 101 * PX
    assert book.order_count() == 4
    assert book.level_counts() == {"bid_levels": 2, "ask_levels": 2}
    assert state.records == 4


def test_multiple_orders_aggregate_into_one_level():
    book, _, _ = run(
        [
            ev("A", "B", 100 * PX, 500, 1),
            ev("A", "B", 100 * PX, 250, 2),
            ev("A", "B", 100 * PX, 125, 3),
        ]
    )
    level = book.best_bid()
    assert level.size == 875
    assert level.count == 3


def test_partial_cancel_reduces_size_and_keeps_the_order():
    book, _, _ = run(
        [ev("A", "B", 100 * PX, 500, 1), ev("C", "B", 100 * PX, 200, 1)]
    )
    assert book.best_bid().size == 300
    assert book.order_count() == 1


def test_full_cancel_removes_the_order_and_empties_the_level():
    book, _, _ = run(
        [ev("A", "B", 100 * PX, 500, 1), ev("C", "B", 100 * PX, 500, 1)]
    )
    assert book.best_bid() is None
    assert book.order_count() == 0
    assert book.level_counts()["bid_levels"] == 0


def test_modify_carries_the_new_absolute_size_not_a_delta():
    """M sets size; C subtracts it. Confusing the two silently halves the book."""
    book, _, _ = run(
        [ev("A", "B", 100 * PX, 500, 1), ev("M", "B", 100 * PX, 300, 1)]
    )
    assert book.best_bid().size == 300


def test_modify_to_a_new_price_moves_the_order_and_loses_priority():
    book, _, _ = run(
        [
            ev("A", "B", 100 * PX, 500, 1),
            ev("A", "B", 99 * PX, 100, 2),
            ev("M", "B", 99 * PX, 500, 1),
        ]
    )
    assert book.best_bid().price == 99 * PX
    assert book.best_bid().size == 600
    assert book.level_counts()["bid_levels"] == 1
    # Order 1 moved to the back of the 99 level, behind order 2.
    bucket = book.bids[99 * PX]
    assert [o.order_id for o in bucket] == [2, 1]


def test_modify_that_increases_size_loses_priority_at_the_same_price():
    book, _, _ = run(
        [
            ev("A", "B", 100 * PX, 100, 1),
            ev("A", "B", 100 * PX, 100, 2),
            ev("M", "B", 100 * PX, 400, 1),
        ]
    )
    assert [o.order_id for o in book.bids[100 * PX]] == [2, 1]
    assert book.best_bid().size == 500


def test_modify_that_decreases_size_keeps_priority():
    book, _, _ = run(
        [
            ev("A", "B", 100 * PX, 500, 1),
            ev("A", "B", 100 * PX, 100, 2),
            ev("M", "B", 100 * PX, 200, 1),
        ]
    )
    assert [o.order_id for o in book.bids[100 * PX]] == [1, 2]


def test_clear_removes_every_resting_order():
    book, state, _ = run(
        [
            ev("A", "B", 100 * PX, 500, 1),
            ev("A", "A", 101 * PX, 500, 2),
            ev("R", "N", 0, 0, 0),
        ]
    )
    assert book.order_count() == 0
    assert book.best_bid() is None and book.best_ask() is None
    assert state.clears == 1


# ---------------------------------------------------------------------------
# The rule most likely to be got wrong
# ---------------------------------------------------------------------------


def test_trade_and_fill_do_not_change_the_book():
    """Databento: fills are always accompanied by a cancel that updates the book.

    Applying F to the book as well would double-count every execution.
    """
    book, _, _ = run(
        [
            ev("A", "B", 100 * PX, 500, 1),
            ev("T", "B", 100 * PX, 200, 0),
            ev("F", "B", 100 * PX, 200, 1),
        ]
    )
    assert book.best_bid().size == 500, "T/F must not touch the book"


def test_the_accompanying_cancel_is_what_reduces_the_book():
    book, _, _ = run(
        [
            ev("A", "B", 100 * PX, 500, 1),
            ev("F", "B", 100 * PX, 200, 1),
            ev("C", "B", 100 * PX, 200, 1),
        ]
    )
    assert book.best_bid().size == 300


def test_none_action_is_inert():
    book, _, _ = run([ev("A", "B", 100 * PX, 500, 1), ev("N", "N", 0, 0, 0)])
    assert book.best_bid().size == 500


# ---------------------------------------------------------------------------
# Integrity violations
# ---------------------------------------------------------------------------


def test_unknown_order_cancel_is_reported_and_does_not_corrupt_the_book():
    book, state, violations = run(
        [ev("A", "B", 100 * PX, 500, 1), ev("C", "B", 100 * PX, 100, 999)]
    )
    assert state.violations[UNKNOWN_ORDER_CANCEL] == 1
    assert book.best_bid().size == 500
    assert violations[0].order_id == 999


def test_unknown_order_modify_is_reported_and_treated_as_an_add():
    book, state, _ = run([ev("M", "B", 100 * PX, 250, 42)])
    assert state.violations[UNKNOWN_ORDER_MODIFY] == 1
    # Databento's reference treats an unknown modify as an add; we follow it so
    # the book stays usable, and say that we had to.
    assert book.best_bid().size == 250


def test_unknown_order_fill_is_reported_even_though_the_book_is_unchanged():
    book, state, _ = run([ev("A", "B", 100 * PX, 500, 1), ev("F", "B", 100 * PX, 10, 777)])
    assert state.violations[UNKNOWN_ORDER_FILL] == 1
    assert book.best_bid().size == 500


def test_duplicate_add_is_reported_and_replaces_rather_than_double_counts():
    book, state, _ = run(
        [ev("A", "B", 100 * PX, 500, 1), ev("A", "B", 100 * PX, 300, 1)]
    )
    assert state.violations[DUPLICATE_ORDER_ADD] == 1
    assert book.best_bid().size == 300
    assert book.best_bid().count == 1


def test_cancel_larger_than_resting_size_is_reported_and_clamped_at_zero():
    """A negative resting size is not a market state."""
    book, state, _ = run(
        [ev("A", "B", 100 * PX, 100, 1), ev("C", "B", 100 * PX, 400, 1)]
    )
    assert state.violations[CANCEL_EXCEEDS_RESTING_SIZE] == 1
    assert book.order_count() == 0
    assert book.best_bid() is None


def test_undefined_size_is_reported_and_the_event_is_not_applied():
    book, state, _ = run([ev("A", "B", 100 * PX, UNDEF_ORDER_SIZE, 1)])
    assert state.violations[NEGATIVE_OR_UNDEFINED_SIZE] == 1
    assert book.order_count() == 0


def test_side_none_on_a_book_changing_action_is_reported():
    book, state, _ = run([ev("A", "N", 100 * PX, 100, 1)])
    assert state.violations[INVALID_SIDE_FOR_ACTION] == 1
    assert book.order_count() == 0


def test_modify_that_changes_side_is_reported():
    book, state, _ = run(
        [ev("A", "B", 100 * PX, 500, 1), ev("M", "A", 101 * PX, 500, 1)]
    )
    assert state.violations[MODIFY_CHANGED_SIDE] == 1
    assert book.best_bid() is None
    assert book.best_ask().price == 101 * PX


def test_crossed_book_is_reported():
    _, state, _ = run(
        [ev("A", "B", 102 * PX, 100, 1), ev("A", "A", 101 * PX, 100, 2)]
    )
    assert state.violations[CROSSED_BOOK] == 1
    assert state.crossed_events == 1


def test_locked_book_is_recorded_but_not_fatal():
    """Bid == ask happens legitimately; it is reported, not treated as a defect."""
    book, state, violations = run(
        opened([ev("A", "B", 100 * PX, 100, 1), ev("A", "A", 100 * PX, 100, 2)])
    )
    assert state.violations[LOCKED_BOOK] == 1
    report = validation_report(book, state, violations, source="synthetic")
    assert report["integrity"]["clean"] is True
    assert LOCKED_BOOK not in report["integrity"]["fatal_violation_counts"]


def test_sequence_regression_is_reported():
    _, state, _ = run(
        [
            ev("A", "B", 100 * PX, 100, 1, seq=10, ts=1_000),
            ev("A", "B", 99 * PX, 100, 2, seq=4, ts=2_000),
        ]
    )
    assert state.violations[SEQUENCE_REGRESSION] == 1


def test_ts_event_regression_is_reported():
    _, state, _ = run(
        [
            ev("A", "B", 100 * PX, 100, 1, seq=1, ts=5_000),
            ev("A", "B", 99 * PX, 100, 2, seq=2, ts=4_000),
        ]
    )
    assert state.violations[TS_EVENT_REGRESSION] == 1


# ---------------------------------------------------------------------------
# F_LAST and snapshot mechanics
# ---------------------------------------------------------------------------


def test_crossed_check_runs_only_at_event_boundaries():
    """A multi-record event may cross transiently; only F_LAST state counts.

    Same events twice: with the boundary rule the transient cross is invisible,
    without it, it is reported. This is what stops a real run drowning in
    thousands of false crossed-book findings.
    """
    events = [
        ev("A", "A", 101 * PX, 100, 1, flags=F_LAST),
        # One venue event, three records: the new bid crosses the old ask until
        # the ask is pulled in the same event.
        ev("A", "B", 102 * PX, 100, 2, flags=0),
        ev("C", "A", 101 * PX, 100, 1, flags=0),
        ev("A", "A", 103 * PX, 100, 3, flags=F_LAST),
    ]
    _, boundary_state, _ = replay(events, check_crossed_on_last_only=True)
    assert boundary_state.violations[CROSSED_BOOK] == 0

    ev.counter = 0
    events_again = [
        ev("A", "A", 101 * PX, 100, 1, flags=F_LAST),
        ev("A", "B", 102 * PX, 100, 2, flags=0),
        ev("C", "A", 101 * PX, 100, 1, flags=0),
        ev("A", "A", 103 * PX, 100, 3, flags=F_LAST),
    ]
    _, naive_state, _ = replay(events_again, check_crossed_on_last_only=False)
    assert naive_state.violations[CROSSED_BOOK] == 1


def test_snapshot_preamble_is_counted_separately_from_live_traffic():
    """R + adds carrying F_SNAPSHOT rebuild state; they are not new orders."""
    book, state, _ = run(
        [
            ev("R", "N", 0, 0, 0, flags=F_SNAPSHOT),
            ev("A", "B", 100 * PX, 500, 1, flags=F_SNAPSHOT),
            ev("A", "A", 101 * PX, 400, 2, flags=F_SNAPSHOT | F_LAST),
            ev("A", "B", 99 * PX, 100, 3, flags=F_LAST),
        ]
    )
    assert state.snapshot_records == 3
    assert state.snapshot_clears == 1
    assert state.snapshot_adds == 2
    assert book.order_count() == 3
    assert book.best_bid().price == 100 * PX


def test_snapshot_records_are_exempt_from_sequence_monotonicity():
    """Snapshot records carry the snapshot's own timestamps and legitimately
    precede the live stream."""
    _, state, _ = run(
        [
            ev("R", "N", 0, 0, 0, seq=9_999, ts=9_999, flags=F_SNAPSHOT),
            ev("A", "B", 100 * PX, 500, 1, seq=9_999, ts=9_999, flags=F_SNAPSHOT),
            ev("A", "B", 99 * PX, 100, 2, seq=1, ts=1, flags=F_LAST),
        ]
    )
    assert state.violations[SEQUENCE_REGRESSION] == 0
    assert state.violations[TS_EVENT_REGRESSION] == 0


def test_top_of_book_add_replaces_the_whole_side():
    book, _, _ = run(
        [
            ev("A", "B", 100 * PX, 500, 1, flags=F_TOB | F_LAST),
            ev("A", "B", 101 * PX, 300, 2, flags=F_TOB | F_LAST),
        ]
    )
    assert book.level_counts()["bid_levels"] == 1
    assert book.best_bid().price == 101 * PX
    # TOB levels are a level, not an order count.
    assert book.best_bid().count == 0


def test_undef_price_with_tob_clears_that_side():
    book, _, _ = run(
        [
            ev("A", "B", 100 * PX, 500, 1, flags=F_TOB | F_LAST),
            ev("A", "A", 101 * PX, 500, 2, flags=F_TOB | F_LAST),
            ev("A", "B", UNDEF_PRICE, 0, 3, flags=F_TOB | F_LAST),
        ]
    )
    assert book.best_bid() is None
    assert book.best_ask() is not None


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


def test_report_exposes_bbo_depth_and_order_counts():
    book, state, violations = run(
        opened([
            ev("A", "B", 100 * PX, 500, 1),
            ev("A", "B", 99 * PX, 300, 2),
            ev("A", "A", 101 * PX, 400, 3),
            ev("A", "A", 102 * PX, 200, 4),
        ])
    )
    report = validation_report(book, state, violations, source="synthetic", depth_levels=5)
    final = report["final_book"]
    assert final["best_bid"]["price_display"] == 100.0
    assert final["best_ask"]["price_display"] == 101.0
    assert final["spread_display"] == 1.0
    assert final["resting_orders"] == 4
    assert final["bid_levels"] == 2 and final["ask_levels"] == 2
    assert [level["price_display"] for level in final["depth"]["bids"]] == [100.0, 99.0]
    assert [level["price_display"] for level in final["depth"]["asks"]] == [101.0, 102.0]
    assert report["integrity"]["clean"] is True


def test_report_marks_fatal_violations_as_not_clean():
    book, state, violations = run(
        [ev("A", "B", 102 * PX, 100, 1), ev("A", "A", 101 * PX, 100, 2)]
    )
    report = validation_report(book, state, violations, source="synthetic")
    assert report["integrity"]["clean"] is False
    assert report["integrity"]["fatal_violation_counts"][CROSSED_BOOK] == 1


# ---------------------------------------------------------------------------
# The adapter that reads real databento records
# ---------------------------------------------------------------------------


def test_from_dbn_reads_genuine_databento_records():
    """Built from real MBOMsg objects, so field names and enum-to-str
    conversion are pinned against the package rather than assumed."""
    import databento_dbn as dbn

    records = [
        dbn.MBOMsg(
            publisher_id=2,
            instrument_id=42,
            ts_event=1_000,
            order_id=7,
            price=100 * PX,
            size=500,
            action=dbn.Action.ADD,
            side=dbn.Side.BID,
            flags=F_LAST,
            ts_recv=1_001,
            sequence=1,
            channel_id=0,
            ts_in_delta=0,
        ),
        dbn.MBOMsg(
            publisher_id=2,
            instrument_id=42,
            ts_event=2_000,
            order_id=8,
            price=101 * PX,
            size=300,
            action=dbn.Action.ADD,
            side=dbn.Side.ASK,
            flags=F_LAST,
            ts_recv=2_001,
            sequence=2,
            channel_id=0,
            ts_in_delta=0,
        ),
        dbn.MBOMsg(
            publisher_id=2,
            instrument_id=42,
            ts_event=3_000,
            order_id=7,
            price=100 * PX,
            size=200,
            action=dbn.Action.CANCEL,
            side=dbn.Side.BID,
            flags=F_LAST,
            ts_recv=3_001,
            sequence=3,
            channel_id=0,
            ts_in_delta=0,
        ),
    ]
    events = [MboEvent.from_dbn(record) for record in records]
    assert [e.action for e in events] == ["A", "A", "C"]
    assert [e.side for e in events] == ["B", "A", "B"]
    assert [e.price for e in events] == [100 * PX, 101 * PX, 100 * PX]
    assert [e.sequence for e in events] == [1, 2, 3]

    book, state, _ = replay(events)
    assert book.best_bid().size == 300
    assert book.best_ask().size == 300
    assert state.records == 3
    assert state.violations[UNKNOWN_ORDER_CANCEL] == 0
    assert list(state.instrument_ids) == [42]
    assert list(state.publisher_ids) == [2]


# ---------------------------------------------------------------------------
# Initialization: was the starting state ever established?
# ---------------------------------------------------------------------------


def test_formal_snapshot_initialization_is_certified():
    book, state, violations = run(
        [
            ev("R", "N", 0, 0, 0, flags=F_SNAPSHOT),
            ev("A", "B", 100 * PX, 500, 1, flags=F_SNAPSHOT),
            ev("A", "A", 101 * PX, 300, 2, flags=F_SNAPSHOT | F_LAST),
            ev("A", "B", 99 * PX, 100, 3),
        ]
    )
    report = validation_report(book, state, violations, source="synthetic")
    init = report["initialization"]
    assert init["mode"] == INIT_FORMAL_SNAPSHOT
    assert init["certified"] is True
    assert init["records_before_initialization"] == 0
    assert report["integrity"]["certified"] is True


def test_known_empty_sequence_zero_clear_is_certified_and_not_called_a_snapshot():
    """The real XNAS shape: record 0 is `sequence=0 action=R side=N order_id=0`
    with flags=8 (F_BAD_TS_RECV) and no F_SNAPSHOT.

    It is a certified start -- the book provably began empty -- but it must not
    be relabelled a snapshot. "We were given the state" and "there was no state
    to give" are different guarantees.
    """
    book, state, violations = run(
        [
            ev("R", "N", 0, 0, 0, seq=0, ts=0, flags=F_BAD_TS_RECV),
            ev("A", "B", 100 * PX, 500, 1),
            ev("A", "A", 101 * PX, 300, 2),
        ]
    )
    report = validation_report(book, state, violations, source="synthetic")
    init = report["initialization"]
    assert init["mode"] == INIT_KNOWN_EMPTY_CLEAR
    assert init["certified"] is True
    assert init["first_action"] == "R"
    assert init["first_sequence"] == 0
    assert init["first_flags"] == F_BAD_TS_RECV
    assert init["records_before_initialization"] == 0
    # The distinction is preserved on both sides.
    assert report["snapshot"]["snapshot_present"] is False
    assert report["snapshot"]["snapshot_records"] == 0
    assert report["integrity"]["certified"] is True


def test_unknown_initialization_fails_certification():
    """A replay that starts on an order mutation is building on unseen state."""
    book, state, violations = run(
        [ev("A", "B", 100 * PX, 500, 1), ev("A", "A", 101 * PX, 300, 2)]
    )
    report = validation_report(book, state, violations, source="synthetic")
    init = report["initialization"]
    assert init["mode"] == INIT_UNKNOWN
    assert init["certified"] is False
    assert init["first_action"] == "A"
    assert report["integrity"]["certified"] is False
    assert report["integrity"]["clean"] is False
    assert "initialization" in report["integrity"]["uncertified_reason"]
    assert report["integrity"]["violation_counts"][UNCERTIFIED_INITIALIZATION] == 1


def test_a_clear_arriving_late_is_not_a_known_empty_start():
    """An `R` at index 3 may be clearing state we never saw."""
    book, state, violations = run(
        [
            ev("A", "B", 100 * PX, 500, 1),
            ev("A", "A", 101 * PX, 300, 2),
            ev("C", "B", 100 * PX, 500, 1),
            ev("R", "N", 0, 0, 0),
        ]
    )
    report = validation_report(book, state, violations, source="synthetic")
    assert report["initialization"]["mode"] == INIT_UNKNOWN
    assert report["initialization"]["records_before_initialization"] == 0


def test_book_neutral_records_before_the_clear_are_scanned_past():
    """T/F/N mutate nothing, so they do not spoil a known-empty start...

    ...but they are counted, so a near-miss is visible rather than inferred.
    """
    book, state, violations = run(
        [
            ev("N", "N", 0, 0, 0),
            ev("T", "B", 100 * PX, 10, 0),
            ev("R", "N", 0, 0, 0),
            ev("A", "B", 100 * PX, 500, 1),
        ]
    )
    report = validation_report(book, state, violations, source="synthetic")
    init = report["initialization"]
    # Strict rule: the clear was not record 0, so this is not a known-empty start.
    assert init["mode"] == INIT_UNKNOWN
    assert init["records_before_initialization"] == 2


# ---------------------------------------------------------------------------
# Fidelity audit: channel gaps, clock quality, action coverage, sequence ties
# ---------------------------------------------------------------------------


def test_maybe_bad_book_withdraws_certification():
    """Databento defines F_MAYBE_BAD_BOOK as an unrecoverable channel gap.

    Internal consistency after such a gap says nothing about the updates that
    never arrived, so one occurrence is enough to withdraw certification -- and
    the book must still finish replaying so the rest of the session is readable.
    """
    book, state, violations = run(
        opened([
            ev("A", "B", 100 * PX, 500, 1),
            ev("A", "A", 101 * PX, 300, 2, flags=F_LAST | F_MAYBE_BAD_BOOK),
            ev("A", "B", 99 * PX, 200, 3),
        ])
    )
    assert state.init_certified is True, "the flag alone must be what decertifies"
    assert state.maybe_bad_book_records == 1
    assert state.violations[FLAG_MAYBE_BAD_BOOK] == 1

    report = validation_report(book, state, violations, source="synthetic")
    assert report["flags"]["f_maybe_bad_book_records"] == 1
    assert report["integrity"]["certified"] is False
    assert report["integrity"]["clean"] is False
    assert "F_MAYBE_BAD_BOOK" in report["integrity"]["uncertified_reason"]
    # The replay still completed; the gap is a verdict, not a crash.
    assert state.records == 4  # the opening clear plus three adds
    assert book.order_count() == 3


def test_a_clean_session_is_certified():
    book, state, violations = run(
        opened([ev("A", "B", 100 * PX, 500, 1), ev("A", "A", 101 * PX, 300, 2)])
    )
    report = validation_report(book, state, violations, source="synthetic")
    assert report["integrity"]["certified"] is True
    assert report["integrity"]["uncertified_reason"] is None
    assert report["flags"]["f_maybe_bad_book_records"] == 0


def test_bad_ts_recv_is_split_snapshot_versus_live_and_does_not_decertify():
    """A receive-clock problem is not a book-state error.

    It must not fail certification, but live occurrences have to be visible for
    later latency and timestamp-quality work.
    """
    book, state, violations = run(
        [
            ev("R", "N", 0, 0, 0, flags=F_SNAPSHOT | F_BAD_TS_RECV),
            ev("A", "B", 100 * PX, 500, 1, flags=F_SNAPSHOT | F_LAST | F_BAD_TS_RECV),
            ev("A", "A", 101 * PX, 300, 2, flags=F_LAST | F_BAD_TS_RECV),
            ev("A", "A", 102 * PX, 100, 3),
        ]
    )
    assert state.bad_ts_recv_snapshot_records == 2
    assert state.bad_ts_recv_live_records == 1

    report = validation_report(book, state, violations, source="synthetic")
    flags = report["flags"]
    assert flags["f_bad_ts_recv_records"] == 3
    assert flags["f_bad_ts_recv_snapshot_records"] == 2
    assert flags["f_bad_ts_recv_live_records"] == 1
    assert report["integrity"]["certified"] is True, "clock quality is not book correctness"
    assert report["integrity"]["clean"] is True


def test_report_lists_every_action_including_the_absent_ones():
    """XNAS normalizes an order replace as C(old id) + A(new id), so few or no
    `M` records is expected. A zero has to be visible to say so."""
    book, state, violations = run(
        [
            ev("A", "B", 100 * PX, 500, 1),
            ev("C", "B", 100 * PX, 100, 1),
            ev("T", "B", 100 * PX, 50, 0),
        ]
    )
    report = validation_report(book, state, violations, source="synthetic")
    by_action = report["replay"]["by_action"]
    assert set(by_action) == set("ACFMNRT")
    assert by_action["A"] == 1 and by_action["C"] == 1 and by_action["T"] == 1
    assert by_action["M"] == 0 and by_action["F"] == 0
    assert by_action["R"] == 0 and by_action["N"] == 0
    # Generic M support is unchanged; it simply did not occur here.
    assert report["replay"]["by_action_observed"] == {"A": 1, "C": 1, "T": 1}


def test_replace_normalized_as_cancel_plus_add_reconstructs_correctly():
    """The XNAS replace shape, end to end, without any `M` record."""
    book, state, _ = run(
        [
            ev("A", "B", 100 * PX, 500, 111),
            ev("C", "B", 100 * PX, 500, 111),
            ev("A", "B", 101 * PX, 500, 222),
        ]
    )
    assert state.by_action["M"] == 0
    assert book.order_count() == 1
    assert book.best_bid().price == 101 * PX
    assert 111 not in book.orders
    assert 222 in book.orders


def test_equal_sequence_numbers_within_one_native_event_are_not_regressions():
    """Records normalized from the same TotalView message share a sequence.

    Only a true backward movement is a regression; flagging ties would report
    every multi-record event as broken.
    """
    _, state, _ = run(
        [
            ev("A", "B", 100 * PX, 500, 1, seq=7, ts=1_000, flags=0),
            ev("C", "B", 100 * PX, 500, 1, seq=7, ts=1_000, flags=0),
            ev("A", "B", 101 * PX, 500, 2, seq=7, ts=1_000, flags=F_LAST),
            ev("A", "A", 102 * PX, 100, 3, seq=8, ts=1_001, flags=F_LAST),
        ]
    )
    assert state.violations[SEQUENCE_REGRESSION] == 0
    assert state.violations[TS_EVENT_REGRESSION] == 0


def test_strictly_backward_sequence_is_still_a_regression():
    _, state, _ = run(
        [
            ev("A", "B", 100 * PX, 500, 1, seq=7, ts=1_000),
            ev("A", "B", 99 * PX, 500, 2, seq=6, ts=1_000),
        ]
    )
    assert state.violations[SEQUENCE_REGRESSION] == 1


def test_violation_counts_are_uncapped_while_samples_are_bounded():
    """Counts must not saturate at the sample limit.

    Deriving counts from the retained sample -- which an earlier version did --
    reports a session with thousands of failures as having exactly the sample
    size, which reads as untidy rather than broken.
    """
    events = [ev("C", "B", 100 * PX, 1, 900_000 + i) for i in range(50)]
    _, state, violations = replay(events, max_recorded_violations=5)
    assert state.violations[UNKNOWN_ORDER_CANCEL] == 50
    assert len(violations) == 5


def test_report_marks_the_sample_as_truncated_when_it_is():
    events = [ev("C", "B", 100 * PX, 1, 900_000 + i) for i in range(50)]
    book, state, violations = replay(events, max_recorded_violations=5)
    report = validation_report(book, state, violations, source="synthetic")
    assert report["integrity"]["violation_counts"][UNKNOWN_ORDER_CANCEL] == 50
    assert report["integrity"]["sample_truncated"] is True
    assert len(report["integrity"]["sample_violations"]) == 5


def test_iter_dbn_events_skips_non_mbo_records(monkeypatch):
    """A DBN file carries metadata and symbol mappings alongside MBO records.

    Feeding those to the book would raise on a missing attribute mid-replay,
    which on a multi-million-record file means finding out an hour in.
    """
    import databento as db

    from app.services import mbo_book_validator as validator

    class NotAnMboRecord:
        stype_out_symbol = "CMCSA"

    mbo = db.MBOMsg(
        publisher_id=2,
        instrument_id=42,
        ts_event=1_000,
        order_id=7,
        price=100 * PX,
        size=500,
        action=db.Action.ADD,
        side=db.Side.BID,
        flags=F_LAST,
        ts_recv=1_001,
        sequence=1,
        channel_id=0,
        ts_in_delta=0,
    )

    class FakeStore:
        @staticmethod
        def from_file(path):
            return [NotAnMboRecord(), mbo, NotAnMboRecord()]

    monkeypatch.setattr(db, "DBNStore", FakeStore)
    events = list(validator.iter_dbn_events("ignored.dbn.zst"))
    assert len(events) == 1
    assert events[0].order_id == 7


# ---------------------------------------------------------------------------
# Integration: the real CMCSA file
# ---------------------------------------------------------------------------

CMCSA_FILE = "xnas-itch-20250626.mbo.CMCSA.0000.dbn.zst"


def _find_cmcsa_file() -> Path | None:
    override = os.environ.get("KEFTRADE_MBO_TEST_FILE")
    if override and Path(override).is_file():
        return Path(override)
    roots = [
        Path(__file__).resolve().parents[3] / "data" / "databento",
        Path(__file__).resolve().parents[3],
        Path("/opt/keftrade/data/databento"),
        Path("/opt/keftrade"),
    ]
    for root in roots:
        candidate = root / CMCSA_FILE
        if candidate.is_file():
            return candidate
    return None


@pytest.mark.skipif(
    _find_cmcsa_file() is None,
    reason=f"{CMCSA_FILE} not present; set KEFTRADE_MBO_TEST_FILE to run",
)
def test_real_cmcsa_session_reconstructs_without_fatal_violations():
    from app.services.mbo_book_validator import validate_dbn_file

    path = _find_cmcsa_file()
    report = validate_dbn_file(str(path))

    replay_stats = report["replay"]
    assert replay_stats["records"] > 100_000, "a full CMCSA session should be large"
    assert set(replay_stats["by_action"]) <= set("ACMRTFN")
    # One symbol, one venue.
    assert len(replay_stats["instrument_ids"]) == 1
    assert len(replay_stats["publisher_ids"]) == 1
    # Sequences and timestamps must advance across the session.
    assert replay_stats["last_sequence"] >= replay_stats["first_sequence"]
    assert replay_stats["last_ts_event"] > replay_stats["first_ts_event"]

    integrity = report["integrity"]
    assert integrity["violation_counts"][SEQUENCE_REGRESSION] == 0
    assert integrity["violation_counts"][TS_EVENT_REGRESSION] == 0
    assert integrity["violation_counts"][CROSSED_BOOK] == 0
    assert integrity["violation_counts"][CANCEL_EXCEEDS_RESTING_SIZE] == 0
    assert integrity["clean"] is True, integrity["fatal_violation_counts"]

    final = report["final_book"]
    if final["best_bid"] and final["best_ask"]:
        assert final["best_bid"]["price"] < final["best_ask"]["price"]
    assert report["peak_resting_orders"] > 0
