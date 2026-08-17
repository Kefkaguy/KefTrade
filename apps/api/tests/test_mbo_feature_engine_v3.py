"""Stage 1 v3 and v4: coherent native-event semantics.

Both corrections are the same mistake in two places -- a quantity declared over
*completed* book states was computed from the transient before/after of whichever
normalized record happened to carry the flag.

`F` is book-neutral under the certified XNAS normalization -- the resting-book
change arrives on the companion `C`/`M` record of the same native event. v2
classified absorption on the `F` record itself, so it compared a midpoint with
itself and every execution came out "absorbed". The all-160 diagnostic confirmed
it: 8,315,861 finite absorption_ratio values, all exactly 1.0.

v3 settles each native event at its `F_LAST`, comparing the midpoint before the
group's first record against the midpoint after its last.

v4 does the same for `queue_persistence`, which is declared as the share of the
window's `F_LAST` states where neither touch price moved, but was computed from
the final record's own before/after -- and which also compared two absent touch
prices as equal, so a one-sided book counted as persistent.
"""

from __future__ import annotations

from app.services.mbo_book_validator import F_LAST, FIXED_PRICE_SCALE, MboEvent
from app.services.mbo_feature_engine import (
    FEATURE_ENGINE_VERSION,
    FEATURE_SEMANTICS_HASH,
    FEATURE_VOCABULARY,
    FEATURE_VOCABULARY_HASH,
    SNAPSHOT_SCHEMA_HASH,
    SUPERSEDED_ENGINE_VERSIONS,
    Cadence,
    OrderBookFeatureEngine,
)

PX = FIXED_PRICE_SCALE
S = 1_000_000_000
ONE_SECOND = (Cadence("1s", "time", S),)


def ev(ts, action, side, price, size, order_id, seq, *, flags=0):
    return MboEvent(
        ts_event=ts,
        action=action,
        side=side,
        price=price,
        size=size,
        order_id=order_id,
        flags=flags,
        sequence=seq,
        ts_recv=ts + 1_000,
    )


def run(events, cadences=ONE_SECOND):
    engine = OrderBookFeatureEngine(
        symbol="TEST", session_date="2025-06-26", cadences=cadences
    )
    return list(engine.process(events))


def book(ts_start=0, *, bid=100 * PX, ask=101 * PX, size=500, seq=1):
    """Two-sided opening book: one resting bid, one resting ask."""
    return [
        ev(ts_start, "A", "B", bid, size, 1, seq),
        ev(ts_start, "A", "A", ask, size, 2, seq + 1, flags=F_LAST),
    ]


def settled(rows):
    """The window in which the native event was settled.

    Window accumulators reset each cadence interval, so the executions appear in
    the snapshot covering their own second, not in the final one.
    """
    executing = [r for r in rows if r["execution_count"]]
    assert len(executing) <= 1, "these fixtures put every execution in one window"
    return executing[0] if executing else rows[-1]


# ---------------------------------------------------------------------------
# The T -> F -> C native event
# ---------------------------------------------------------------------------


def test_execution_leaving_the_midpoint_unchanged_is_absorbed():
    """A partial fill of a resting bid: the level survives, the midpoint holds."""
    events = [
        *book(),
        # One native event: trade, fill of the resting bid, cancel of the filled
        # quantity. F_LAST only on the last record.
        ev(S, "T", "A", 100 * PX, 100, 0, 10),
        ev(S, "F", "B", 100 * PX, 100, 1, 10),
        ev(S, "C", "B", 100 * PX, 100, 1, 10, flags=F_LAST),
        *[ev(2 * S + i, "A", "B", 99 * PX, 10, 50 + i, 20 + i, flags=F_LAST) for i in range(2)],
    ]
    row = settled(run(events))
    assert row["execution_count"] == 1
    assert row["execution_volume"] == 100
    assert row["executions_without_price_move"] == 1
    assert row["execution_volume_without_price_move"] == 100
    assert row["absorption_ratio"] == 1.0


def test_execution_that_moves_the_midpoint_is_not_absorbed():
    """A full sweep of the resting bid: the level dies, the midpoint moves."""
    events = [
        *book(),
        ev(S, "T", "A", 100 * PX, 500, 0, 10),
        ev(S, "F", "B", 100 * PX, 500, 1, 10),
        ev(S, "C", "B", 100 * PX, 500, 1, 10),
        # A new, lower bid arrives in the same native event.
        ev(S, "A", "B", 98 * PX, 300, 3, 10, flags=F_LAST),
        *[ev(2 * S + i, "A", "A", 102 * PX, 10, 60 + i, 20 + i, flags=F_LAST) for i in range(2)],
    ]
    row = settled(run(events))
    assert row["execution_count"] == 1
    assert row["execution_volume"] == 500
    assert row["executions_without_price_move"] == 0
    assert row["execution_volume_without_price_move"] == 0
    assert row["absorption_ratio"] == 0.0


def test_the_v2_bug_is_actually_gone():
    """v2 read the midpoint across the book-neutral F record, so it could only
    ever answer 'unchanged'. The sweep above must not return 1.0."""
    events = [
        *book(),
        ev(S, "T", "A", 100 * PX, 500, 0, 10),
        ev(S, "F", "B", 100 * PX, 500, 1, 10),
        ev(S, "C", "B", 100 * PX, 500, 1, 10),
        ev(S, "A", "B", 98 * PX, 300, 3, 10, flags=F_LAST),
        *[ev(2 * S + i, "A", "A", 102 * PX, 10, 60 + i, 20 + i, flags=F_LAST) for i in range(2)],
    ]
    assert settled(run(events))["absorption_ratio"] != 1.0


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def test_multiple_records_share_one_native_event():
    """Three fills inside one F_LAST group are one settlement, and the midpoint
    is judged once, at the end."""
    events = [
        *book(size=900),
        ev(S, "T", "A", 100 * PX, 300, 0, 10),
        ev(S, "F", "B", 100 * PX, 100, 1, 10),
        ev(S, "F", "B", 100 * PX, 100, 1, 10),
        ev(S, "F", "B", 100 * PX, 100, 1, 10),
        ev(S, "C", "B", 100 * PX, 300, 1, 10, flags=F_LAST),
        *[ev(2 * S + i, "A", "B", 99 * PX, 10, 70 + i, 20 + i, flags=F_LAST) for i in range(2)],
    ]
    row = settled(run(events))
    assert row["execution_count"] == 3
    assert row["execution_volume"] == 300
    # All three belong to one native event, which left the midpoint alone.
    assert row["executions_without_price_move"] == 3
    assert row["execution_volume_without_price_move"] == 300


def test_no_leakage_across_adjacent_flast_groups():
    """Two native events in the same window: an absorbed one immediately
    followed by a moving one. Each must be judged on its own midpoints, and the
    second must not retroactively unclassify the first.

    Both groups carry the same ts_event so no grid flush separates them, which
    is the case where a stale pre-group midpoint would actually leak.
    """
    events = [
        *book(size=900),
        # Group 1: partial fill, midpoint unchanged.
        ev(S, "T", "A", 100 * PX, 100, 0, 10),
        ev(S, "F", "B", 100 * PX, 100, 1, 10),
        ev(S, "C", "B", 100 * PX, 100, 1, 10, flags=F_LAST),
        # Group 2: sweep the remaining 800, midpoint moves to (97 + 101) / 2.
        ev(S, "T", "A", 100 * PX, 800, 0, 11),
        ev(S, "F", "B", 100 * PX, 800, 1, 11),
        ev(S, "C", "B", 100 * PX, 800, 1, 11),
        ev(S, "A", "B", 97 * PX, 400, 4, 11, flags=F_LAST),
        *[ev(2 * S + i, "A", "A", 102 * PX, 10, 80 + i, 20 + i, flags=F_LAST) for i in range(2)],
    ]
    row = settled(run(events))
    assert row["execution_count"] == 2
    assert row["execution_volume"] == 900
    # Only the first group is absorbed.
    assert row["executions_without_price_move"] == 1
    assert row["execution_volume_without_price_move"] == 100
    assert row["absorption_ratio"] == 100 / 900


def test_groups_separated_by_a_grid_flush_are_judged_independently():
    """The same two events one nanosecond apart fall either side of the 1s
    boundary flush, and each window reports only its own."""
    events = [
        *book(size=900),
        ev(S, "T", "A", 100 * PX, 100, 0, 10),
        ev(S, "F", "B", 100 * PX, 100, 1, 10),
        ev(S, "C", "B", 100 * PX, 100, 1, 10, flags=F_LAST),
        ev(S + 1, "T", "A", 100 * PX, 800, 0, 11),
        ev(S + 1, "F", "B", 100 * PX, 800, 1, 11),
        ev(S + 1, "C", "B", 100 * PX, 800, 1, 11),
        ev(S + 1, "A", "B", 97 * PX, 400, 4, 11, flags=F_LAST),
        *[ev(3 * S + i, "A", "A", 102 * PX, 10, 80 + i, 20 + i, flags=F_LAST) for i in range(2)],
    ]
    executing = [r for r in run(events) if r["execution_count"]]
    assert len(executing) == 2
    absorbed, moved = executing
    assert absorbed["execution_volume"] == 100
    assert absorbed["absorption_ratio"] == 1.0
    assert moved["execution_volume"] == 800
    assert moved["absorption_ratio"] == 0.0


def test_a_group_with_no_executions_settles_without_counting_anything():
    events = [
        *book(),
        ev(S, "A", "B", 99 * PX, 200, 5, 10),
        ev(S, "C", "B", 99 * PX, 200, 5, 10, flags=F_LAST),
        *[ev(2 * S + i, "A", "B", 99 * PX, 10, 90 + i, 20 + i, flags=F_LAST) for i in range(2)],
    ]
    row = settled(run(events))
    assert row["execution_count"] == 0
    assert row["absorption_ratio"] is None


# ---------------------------------------------------------------------------
# One-sided books
# ---------------------------------------------------------------------------


def test_a_one_sided_final_book_is_not_counted_as_absorbed():
    """No midpoint exists after the group, so absorption is unknown -- and
    unknown must never be recorded as absorbed."""
    events = [
        *book(),
        # Sweep the bid and add nothing back: the book is ask-only afterwards.
        ev(S, "T", "A", 100 * PX, 500, 0, 10),
        ev(S, "F", "B", 100 * PX, 500, 1, 10),
        ev(S, "C", "B", 100 * PX, 500, 1, 10, flags=F_LAST),
        *[ev(2 * S + i, "A", "A", 103 * PX, 10, 95 + i, 20 + i, flags=F_LAST) for i in range(2)],
    ]
    row = settled(run(events))
    assert row["execution_count"] == 1
    assert row["execution_volume"] == 500
    assert row["executions_without_price_move"] == 0
    assert row["execution_volume_without_price_move"] == 0
    assert row["absorption_ratio"] == 0.0


def test_a_one_sided_opening_book_is_not_counted_as_absorbed():
    """No midpoint exists before the group either."""
    events = [
        ev(0, "A", "B", 100 * PX, 500, 1, 1, flags=F_LAST),
        ev(S, "T", "A", 100 * PX, 100, 0, 10),
        ev(S, "F", "B", 100 * PX, 100, 1, 10),
        ev(S, "C", "B", 100 * PX, 100, 1, 10),
        ev(S, "A", "A", 101 * PX, 500, 2, 10, flags=F_LAST),
        *[ev(2 * S + i, "A", "B", 99 * PX, 10, 96 + i, 20 + i, flags=F_LAST) for i in range(2)],
    ]
    row = settled(run(events))
    assert row["execution_count"] == 1
    assert row["executions_without_price_move"] == 0


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_v2_is_recorded_as_superseded_with_the_diagnostic_that_found_it():
    versions = {entry["version"] for entry in SUPERSEDED_ENGINE_VERSIONS}
    assert "tier1_mbo_feature_engine_v2" in versions
    v2 = next(
        e for e in SUPERSEDED_ENGINE_VERSIONS
        if e["version"] == "tier1_mbo_feature_engine_v2"
    )
    assert v2["superseded_before_outcome"] == "true"
    assert "book-neutral" in v2["reason"]
    assert "8,315,861" in v2["reason"]


def test_the_semantics_hash_moved_but_the_vocabulary_did_not():
    """The correction renamed nothing, which is exactly why a name hash alone is
    not sufficient provenance."""
    assert FEATURE_SEMANTICS_HASH not in {
        "4aaeb9cb6d6700524d7fb065036612376d482a5cdff47d555d42c8a895c62551",  # v2
        "7f613b06e8ba25bc45947c1ea6d3558e4508f73e37d6ef09736ba91d2d3933eb",  # v3
    }
    assert FEATURE_VOCABULARY_HASH == (
        "25e685913e3a3d05248ef6f09ad44e4b0cab91276bf7bd66d2f0d650f06b82a7"
    )
    assert SNAPSHOT_SCHEMA_HASH == (
        "7e19d06b91a2faa6178a767462fe6e1c2b3ad5865c2db2055e82c02dd47185e9"
    )


def test_modify_count_is_still_a_frozen_sensor():
    """Dormant is not the same as removed. The diagnostic found modify_count
    always zero; it stays in the vocabulary and contributes zero."""
    assert "modify_count" in FEATURE_VOCABULARY
    assert len(FEATURE_VOCABULARY) == 59


# ---------------------------------------------------------------------------
# v4: queue_persistence over completed F_LAST states
# ---------------------------------------------------------------------------


def test_touch_moved_earlier_in_the_native_event_is_not_persistent():
    """The correction that motivated v4.

    Inside one native event the touch moves on an early record and the final
    normalized record leaves it alone. Judging the event by that last record's
    own before/after reads "unchanged"; judging it coherently, from the previous
    completed F_LAST to this one, the touch plainly moved.
    """
    events = [
        *book(),  # bid 100, ask 101 -- completed F_LAST #1
        # One native event: the bid is pulled and rebuilt lower, then an
        # unrelated deep add carries the F_LAST without touching the top.
        ev(S, "C", "B", 100 * PX, 500, 1, 10),
        ev(S, "A", "B", 98 * PX, 400, 3, 10),
        ev(S, "A", "B", 90 * PX, 100, 4, 10, flags=F_LAST),
        *[ev(2 * S + i, "A", "A", 105 * PX, 10, 60 + i, 20 + i, flags=F_LAST) for i in range(2)],
    ]
    rows = run(events)
    # The window containing the moving native event must not call it persistent.
    moved = [r for r in rows if r["queue_persistence"] is not None][1]
    assert moved["queue_persistence"] == 0.0


def test_a_genuinely_unchanged_touch_is_persistent():
    """The mirror case: a native event that leaves both touch prices where they
    were must still count as persistence."""
    events = [
        *book(),
        # Deep adds only; the touch is untouched from F_LAST to F_LAST.
        ev(S, "A", "B", 90 * PX, 100, 3, 10),
        ev(S, "A", "A", 110 * PX, 100, 4, 10, flags=F_LAST),
        *[ev(2 * S + i, "A", "B", 91 * PX, 10, 60 + i, 20 + i, flags=F_LAST) for i in range(2)],
    ]
    rows = run(events)
    persistent = [r for r in rows if r["queue_persistence"] is not None][1]
    assert persistent["queue_persistence"] == 1.0


def test_a_touch_that_moves_and_returns_within_one_event_is_persistent():
    """Coherent means coherent in both directions: transient excursions inside a
    native event are invisible from one completed state to the next."""
    events = [
        *book(),
        ev(S, "C", "B", 100 * PX, 500, 1, 10),
        ev(S, "A", "B", 100 * PX, 500, 5, 10, flags=F_LAST),
        *[ev(2 * S + i, "A", "B", 90 * PX, 10, 60 + i, 20 + i, flags=F_LAST) for i in range(2)],
    ]
    rows = run(events)
    restored = [r for r in rows if r["queue_persistence"] is not None][1]
    assert restored["queue_persistence"] == 1.0


def test_a_one_sided_book_is_not_evidence_of_persistence():
    """Two absent touches compared with `==` were equal, so an empty or
    one-sided book counted as persistent. It is absence of evidence."""
    events = [
        ev(0, "A", "B", 100 * PX, 500, 1, 1, flags=F_LAST),  # bid only
        ev(S, "A", "B", 99 * PX, 100, 2, 10, flags=F_LAST),  # still bid only
        ev(2 * S, "A", "B", 98 * PX, 100, 3, 20, flags=F_LAST),
    ]
    rows = run(events)
    for row in rows:
        assert row["queue_persistence"] in (None, 0.0)


def test_the_first_flast_of_the_session_is_not_persistent():
    """There is no previous completed state to persist from."""
    events = [
        ev(0, "A", "B", 100 * PX, 500, 1, 1),
        ev(0, "A", "A", 101 * PX, 500, 2, 2, flags=F_LAST),
        *[ev(2 * S + i, "A", "B", 90 * PX, 10, 60 + i, 20 + i, flags=F_LAST) for i in range(2)],
    ]
    first = run(events)[0]
    assert first["queue_persistence"] == 0.0


def test_the_engine_is_v4_and_v3_is_recorded_as_superseded():
    v3 = next(
        e for e in SUPERSEDED_ENGINE_VERSIONS
        if e["version"] == "tier1_mbo_feature_engine_v3"
    )
    assert FEATURE_ENGINE_VERSION == "tier1_mbo_feature_engine_v4"
    assert v3["superseded_before_outcome"] == "true"
    assert "queue_persistence" in v3["reason"]
    # v3 never produced an artefact, so nothing needs migrating from it.
    assert "none" in v3["datasets_extracted_under_this_version"]


def test_mean_touch_depth_samples_the_coherent_state_once_per_flast():
    """Audit result, pinned.

    `mean_touch_depth` is the other feature declared over F_LAST states. Unlike
    persistence it is a level rather than a transition, and it was already
    sampled from the post-F_LAST book once per completed event -- so it needed
    no correction. This test keeps it that way: the transient sizes inside a
    native event must not enter the average.
    """
    events = [
        *book(size=500),  # touch depth (500 + 500) / 2 = 500
        # One native event whose interior briefly shows a much larger bid, but
        # whose completed state is back to 500 on each side.
        ev(S, "A", "B", 100 * PX, 9_000, 3, 10),
        ev(S, "C", "B", 100 * PX, 9_000, 3, 10, flags=F_LAST),
        *[ev(2 * S + i, "A", "B", 90 * PX, 10, 60 + i, 20 + i, flags=F_LAST) for i in range(2)],
    ]
    rows = run(events)
    settled_window = [r for r in rows if r["window_flast_events"]][1]
    assert settled_window["window_flast_events"] == 1
    # 500, not a blend with the transient 9,000.
    assert settled_window["mean_touch_depth"] == 500.0
