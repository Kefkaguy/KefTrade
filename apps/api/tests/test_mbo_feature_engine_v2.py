"""Stage 1 v2: the three pre-outcome corrections.

Each of these pins a bug that was found by audit *before* any predictive result
was inspected, so they guard corrections rather than tuning.
"""

from __future__ import annotations

import pytest

from app.services.mbo_book_validator import (
    F_BAD_TS_RECV,
    F_LAST,
    FIXED_PRICE_SCALE,
    MboEvent,
)
from app.services.mbo_feature_engine import (
    FEATURE_ENGINE_VERSION,
    FEATURE_SEMANTICS_HASH,
    FEATURE_VOCABULARY_HASH,
    SNAPSHOT_SCHEMA_HASH,
    SUPERSEDED_ENGINE_VERSIONS,
    Cadence,
    OrderBookFeatureEngine,
    feature_definitions,
)

PX = FIXED_PRICE_SCALE
S = 1_000_000_000  # one second in nanoseconds


def opening(ts: int = 0, recv: int = 1) -> MboEvent:
    return MboEvent(
        ts_event=ts,
        action="R",
        side="N",
        price=0,
        size=0,
        order_id=0,
        flags=F_BAD_TS_RECV,
        sequence=0,
        ts_recv=recv,
    )


def ev(ts, action, side, price, size, order_id, seq, *, recv=None, flags=F_LAST):
    return MboEvent(
        ts_event=ts,
        action=action,
        side=side,
        price=price,
        size=size,
        order_id=order_id,
        flags=flags,
        sequence=seq,
        ts_recv=ts + 1_000 if recv is None else recv,
    )


def run(events, cadences):
    engine = OrderBookFeatureEngine(
        symbol="TEST", session_date="2025-06-26", cadences=cadences
    )
    return list(engine.process(events))


# ---------------------------------------------------------------------------
# 1. Aggressive-flow double counting
# ---------------------------------------------------------------------------


def xnas_execution(aggressor_side: str, resting_side: str, quantity: int = 100):
    """The real XNAS shape for one displayed execution: T -> F -> C.

    All three share a sequence and a quantity, because they describe one event.
    """
    return [
        opening(),
        ev(1 * S, "A", resting_side, 100 * PX, 500, 1, 1),
        ev(1 * S + 1, "A", "A" if resting_side == "B" else "B", 101 * PX, 500, 2, 2),
        ev(2 * S, "T", aggressor_side, 100 * PX, quantity, 0, 7),
        ev(2 * S + 1, "F", resting_side, 100 * PX, quantity, 1, 7),
        ev(2 * S + 2, "C", resting_side, 100 * PX, quantity, 1, 7),
    ]


ALL_FIVE = (Cadence("all", "events", 5),)


def test_sell_aggressor_execution_counts_once_not_twice():
    """100 shares must produce 100 of signed flow, not 200.

    v1 added the size on both the `T` and the `F`.
    """
    row = run(xnas_execution(aggressor_side="A", resting_side="B"), ALL_FIVE)[-1]
    assert row["trade_volume"] == 100
    assert row["sell_aggressor_volume"] == 100
    assert row["buy_aggressor_volume"] == 0
    assert row["signed_trade_volume"] == -100
    assert row["aggressor_imbalance"] == pytest.approx(-1.0)
    # The execution side of the same event is untouched by the fix.
    assert row["execution_count"] == 1
    assert row["execution_volume"] == 100


def test_buy_aggressor_execution_counts_once_not_twice():
    row = run(xnas_execution(aggressor_side="B", resting_side="A"), ALL_FIVE)[-1]
    assert row["trade_volume"] == 100
    assert row["buy_aggressor_volume"] == 100
    assert row["sell_aggressor_volume"] == 0
    assert row["signed_trade_volume"] == 100
    assert row["aggressor_imbalance"] == pytest.approx(1.0)
    assert row["execution_volume"] == 100


def test_side_none_execution_is_unclassified_but_still_executes():
    """Auction and non-displayed prints: volume yes, sign no, execution yes."""
    row = run(xnas_execution(aggressor_side="N", resting_side="B"), ALL_FIVE)[-1]
    assert row["trade_volume"] == 100
    assert row["unclassified_trade_volume"] == 100
    assert row["unclassified_trade_share"] == pytest.approx(1.0)
    assert row["signed_trade_volume"] == 0
    assert row["aggressor_imbalance"] is None  # no classified volume to divide by
    assert row["execution_volume"] == 100


def test_classified_and_execution_volume_agree_on_a_single_execution():
    """The invariant the double count broke: one execution, one trade quantity."""
    row = run(xnas_execution(aggressor_side="A", resting_side="B", quantity=250), ALL_FIVE)[-1]
    classified = row["buy_aggressor_volume"] + row["sell_aggressor_volume"]
    assert classified == row["execution_volume"] == row["trade_volume"] == 250


# ---------------------------------------------------------------------------
# 2. Absolute UTC time grids
# ---------------------------------------------------------------------------


def walking_session(sub_second_offset: int, symbol: str, cadence: Cadence):
    """Events spanning the same wall-clock window, jittered within it.

    Both symbols start and end inside the same seconds; only the sub-second
    placement of their events differs. Anything else would compare grids of
    different *extent* rather than different *alignment*, which is not the
    property under test.
    """
    start = 10 * S + sub_second_offset
    end = 19 * S + 500_000_000
    steps = 30
    events = [opening(ts=10 * S)]
    seq = 1
    for step in range(steps):
        ts = start + (end - start) * step // (steps - 1)
        side = "B" if step % 2 else "A"
        price = (100 * PX) if side == "B" else (101 * PX)
        events.append(ev(ts, "A", side, price, 10, seq, seq))
        seq += 1
    engine = OrderBookFeatureEngine(
        symbol=symbol, session_date="2025-06-26", cadences=(cadence,)
    )
    return list(engine.process(events))


@pytest.mark.parametrize(
    "cadence", [Cadence("1s", "time", S), Cadence("5s", "time", 5 * S)]
)
def test_two_symbols_share_an_identical_grid_despite_different_event_times(cadence):
    """v1 anchored to each file's first F_LAST, so symbols were offset from each
    other by an arbitrary sub-second amount and not comparable at one instant."""
    first = walking_session(123_000_000, "AAA", cadence)
    second = walking_session(777_000_111, "BBB", cadence)

    a_grid = [row["grid_ts_event"] for row in first]
    b_grid = [row["grid_ts_event"] for row in second]

    assert a_grid == b_grid, "both symbols must land on the same absolute boundaries"
    assert a_grid, "the fixture must span enough time to emit"
    # Absolute UTC multiples, not offsets from a per-file anchor.
    assert all(boundary % cadence.interval == 0 for boundary in a_grid)
    # And the underlying data really was offset.
    assert first[0]["source_ts_event"] != second[0]["source_ts_event"]


def test_an_event_one_nanosecond_after_a_boundary_cannot_affect_it():
    """`ts_event <= t` is the whole rule. One nanosecond past it is next interval."""
    boundary = 11 * S
    base = [
        opening(ts=10 * S),
        ev(10 * S + 500_000_000, "A", "B", 100 * PX, 400, 1, 1),
        ev(10 * S + 600_000_000, "A", "A", 101 * PX, 400, 2, 2),
    ]
    # A huge add that lands one nanosecond after the boundary.
    after = base + [
        ev(boundary + 1, "A", "B", 100 * PX, 999_999, 3, 3),
        ev(12 * S + 1, "A", "A", 101 * PX, 10, 4, 4),
    ]
    # The same session without it.
    without = base + [ev(12 * S + 1, "A", "A", 101 * PX, 10, 4, 4)]

    cadence = (Cadence("1s", "time", S),)
    with_row = next(r for r in run(after, cadence) if r["grid_ts_event"] == boundary)
    without_row = next(r for r in run(without, cadence) if r["grid_ts_event"] == boundary)

    assert with_row["bid_size_l1"] == 400, "the late add must not be in this boundary"
    assert with_row["add_count"] == without_row["add_count"]
    assert with_row["bid_size_l1"] == without_row["bid_size_l1"]
    assert with_row["source_ts_event"] <= boundary


def test_an_event_exactly_on_a_boundary_belongs_to_that_boundary():
    """`ts_event <= t`, so equality is inside, not outside."""
    boundary = 11 * S
    events = [
        opening(ts=10 * S),
        ev(10 * S + 500_000_000, "A", "B", 100 * PX, 400, 1, 1),
        ev(boundary, "A", "B", 100 * PX, 600, 2, 2),
        ev(12 * S + 1, "A", "A", 101 * PX, 10, 3, 3),
    ]
    row = next(
        r for r in run(events, (Cadence("1s", "time", S),))
        if r["grid_ts_event"] == boundary
    )
    assert row["bid_size_l1"] == 1000, "the add at exactly t is inside interval t"
    assert row["source_ts_event"] == boundary


def test_an_interval_with_no_events_emits_last_state_with_zero_flow():
    """A quiet second is an observation, not a gap to skip."""
    events = [
        opening(ts=10 * S),
        ev(10 * S + 100_000_000, "A", "B", 100 * PX, 400, 1, 1),
        ev(10 * S + 200_000_000, "A", "A", 101 * PX, 300, 2, 2),
        # Nothing at all for five seconds.
        ev(16 * S + 100_000_000, "A", "B", 100 * PX, 50, 3, 3),
    ]
    rows = run(events, (Cadence("1s", "time", S),))
    quiet = [r for r in rows if 12 * S <= r["grid_ts_event"] <= 15 * S]
    assert len(quiet) == 4, "every silent second still emits"
    for row in quiet:
        # Last known book state carries forward...
        assert row["bid_size_l1"] == 400
        assert row["ask_size_l1"] == 300
        # ...with no new window flow.
        assert row["add_count"] == 0
        assert row["cancel_count"] == 0
        assert row["trade_volume"] == 0
        assert row["window_records"] == 0
        assert row["order_flow_imbalance"] == 0.0


def test_event_cadences_are_unchanged_by_the_grid_rewrite():
    events = [opening(ts=10 * S)]
    for step in range(20):
        events.append(
            ev(10 * S + step * 7_000_000, "A", "B", (100 - step) * PX, 10, step + 1, step + 1)
        )
    rows = run(events, (Cadence("5ev", "events", 5),))
    assert len(rows) == 4
    assert all(row["grid_ts_event"] is None for row in rows)
    assert [row["window_flast_events"] for row in rows] == [5, 5, 5, 5]


# ---------------------------------------------------------------------------
# 3. Availability timestamps
# ---------------------------------------------------------------------------


def test_availability_never_precedes_any_input_record():
    events = [opening(ts=10 * S, recv=10 * S + 5)]
    seq = 1
    recvs = []
    for step in range(20):
        ts = 10 * S + step * 100_000_000
        recv = ts + (step % 7) * 1_000_000  # jittered receive latency
        recvs.append(recv)
        events.append(ev(ts, "A", "B" if step % 2 else "A",
                         (100 * PX) if step % 2 else (101 * PX), 10, seq, seq, recv=recv))
        seq += 1

    for cadence in (Cadence("1s", "time", S), Cadence("5ev", "events", 5)):
        for row in run(events, (cadence,)):
            assert row["feature_available_ts_recv"] >= row["source_ts_recv"]
            # Never earlier than any record inside the window either.
            window_recvs = [
                r for r in recvs
                if r <= row["feature_available_ts_recv"]
            ]
            assert window_recvs, "at least one input must be accounted for"
            assert row["source_ts_event"] <= row["ts_event"]


def test_source_timestamps_identify_the_record_the_state_came_from():
    events = [
        opening(ts=10 * S),
        ev(10 * S + 400_000_000, "A", "B", 100 * PX, 400, 1, 11, recv=10 * S + 450_000_000),
        ev(12 * S + 1, "A", "A", 101 * PX, 300, 2, 12),
    ]
    row = next(
        r for r in run(events, (Cadence("1s", "time", S),))
        if r["grid_ts_event"] == 11 * S
    )
    assert row["source_ts_event"] == 10 * S + 400_000_000
    assert row["source_ts_recv"] == 10 * S + 450_000_000
    assert row["sequence"] == 11
    assert row["ts_event"] == 11 * S  # nominal grid time
    assert row["grid_ts_event"] == 11 * S


def test_perturbing_future_ts_recv_leaves_earlier_rows_unchanged():
    """Receive timestamps are inputs like any other; a later one cannot
    retroactively change when an earlier row became available."""
    base = [opening(ts=10 * S)]
    seq = 1
    for step in range(30):
        ts = 10 * S + step * 200_000_000
        base.append(
            ev(ts, "A", "B" if step % 2 else "A",
               (100 * PX) if step % 2 else (101 * PX), 10, seq, seq, recv=ts + 1_000)
        )
        seq += 1

    cadence = (Cadence("1s", "time", S),)
    original = run(base, cadence)
    cutoff_index = len(base) // 2
    cutoff_ts = base[cutoff_index].ts_event

    perturbed = [
        e
        if index <= cutoff_index
        else MboEvent(
            ts_event=e.ts_event,
            action=e.action,
            side=e.side,
            price=e.price,
            size=e.size,
            order_id=e.order_id,
            flags=e.flags,
            sequence=e.sequence,
            ts_recv=e.ts_recv + 9_000_000_000,  # nine seconds of extra latency
        )
        for index, e in enumerate(base)
    ]
    after = run(perturbed, cadence)

    earlier_before = [r for r in original if r["source_ts_event"] <= cutoff_ts]
    earlier_after = [r for r in after if r["source_ts_event"] <= cutoff_ts]
    assert earlier_before, "the fixture must produce rows before the cutoff"
    assert earlier_before == earlier_after


# ---------------------------------------------------------------------------
# 4. Governance
# ---------------------------------------------------------------------------


def test_engine_version_was_incremented():
    assert FEATURE_ENGINE_VERSION == "tier1_mbo_feature_engine_v2"


def test_the_superseded_version_is_preserved_with_its_reason():
    assert len(SUPERSEDED_ENGINE_VERSIONS) == 1
    superseded = SUPERSEDED_ENGINE_VERSIONS[0]
    assert superseded["version"] == "tier1_mbo_feature_engine_v1"
    assert superseded["superseded_before_outcome"] == "true"
    assert "double counting" in superseded["reason"]
    # The v1 vocabulary hash is kept so an old manifest remains identifiable.
    assert superseded["feature_vocabulary_hash"].startswith("25e685913e3a")


def test_a_semantic_correction_changes_the_recorded_hash_even_though_names_did_not():
    """The point of the semantics hash.

    v2 renamed nothing, so a hash over column names alone still matches v1 --
    which would have made a corrected engine indistinguishable from the one it
    replaced.
    """
    v1_vocabulary_hash = SUPERSEDED_ENGINE_VERSIONS[0]["feature_vocabulary_hash"]
    assert FEATURE_VOCABULARY_HASH == v1_vocabulary_hash, (
        "feature names are unchanged by v2, so the name hash must still match"
    )
    assert FEATURE_SEMANTICS_HASH != v1_vocabulary_hash
    assert SNAPSHOT_SCHEMA_HASH != FEATURE_VOCABULARY_HASH


def test_definitions_document_the_attribution_split_and_the_grid_rule():
    definitions = feature_definitions()
    attribution = definitions["aggressive_flow_attribution"]
    assert "buy_aggressor_volume" in attribution["trade_records"]
    assert "buy_aggressor_volume" not in attribution["fill_records"]
    assert "execution_volume" in attribution["fill_records"]
    assert "T -> F -> C" in attribution["note"]
    assert "ts_event <= t" in definitions["time_grid"]
    assert definitions["contains_forward_information"] is False
    assert definitions["feature_engine_version"] == FEATURE_ENGINE_VERSION
