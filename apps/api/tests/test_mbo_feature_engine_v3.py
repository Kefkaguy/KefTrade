"""Stage 1 v3: absorption is a property of the native event, not of `F`.

`F` is book-neutral under the certified XNAS normalization -- the resting-book
change arrives on the companion `C`/`M` record of the same native event. v2
classified absorption on the `F` record itself, so it compared a midpoint with
itself and every execution came out "absorbed". The all-160 diagnostic confirmed
it: 8,315,861 finite absorption_ratio values, all exactly 1.0.

v3 settles each native event at its `F_LAST`, comparing the midpoint before the
group's first record against the midpoint after its last.
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


def test_the_engine_is_v3_and_v2_is_recorded_as_superseded():
    assert FEATURE_ENGINE_VERSION == "tier1_mbo_feature_engine_v3"
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
    assert FEATURE_SEMANTICS_HASH != (
        "4aaeb9cb6d6700524d7fb065036612376d482a5cdff47d555d42c8a895c62551"
    )
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
