"""Stage 1 leakage tests: no snapshot may depend on anything after it.

The strongest available check is truncation invariance. If a snapshot computed
from the whole session differs from the same snapshot computed from a stream cut
off immediately after its own event, then something downstream reached backwards
-- which is the only way a feature can see the future.

This is the Stage-1 analogue of `perturb_future_candles`, and it is stated two
ways on purpose: cut the future off, and separately, replace it with something
different. A bug that survives one framing rarely survives both.
"""

from __future__ import annotations

import random

import pytest

from app.services.mbo_book_validator import (
    F_BAD_TS_RECV,
    F_LAST,
    FIXED_PRICE_SCALE,
    MboEvent,
)
from app.services.mbo_feature_engine import (
    CADENCES,
    FEATURE_VOCABULARY,
    MIN_PRIOR_OBSERVATIONS,
    ExpandingNormalizer,
    OrderBookFeatureEngine,
    feature_definitions,
)

PX = FIXED_PRICE_SCALE


def session_events(seed: int = 7, count: int = 600) -> list[MboEvent]:
    """A deterministic pseudo-session with adds, cancels, trades and fills."""
    rng = random.Random(seed)
    events: list[MboEvent] = [
        MboEvent(
            ts_event=0,
            action="R",
            side="N",
            price=0,
            size=0,
            order_id=0,
            flags=F_BAD_TS_RECV,
            sequence=0,
        )
    ]
    live: list[tuple[int, str, int]] = []
    ts = 1_000_000
    # A drifting reference price and a variable inside-spread, so the touch
    # actually moves. A fixture pinned at a constant one-tick spread leaves
    # spread-based statistics with zero variance and silently untested.
    reference = 34 * PX
    for index in range(1, count + 1):
        ts += rng.randint(1_000_000, 40_000_000)
        if rng.random() < 0.15:
            reference += rng.choice((-2, -1, 1, 2)) * 10**7
        half_spread = rng.choice((1, 1, 1, 2, 3)) * 10**7
        roll = rng.random()
        if roll < 0.55 or not live:
            side = "B" if rng.random() < 0.5 else "A"
            offset = rng.randint(0, 6) * 10**7
            price = (
                (reference - half_spread - offset)
                if side == "B"
                else (reference + half_spread + offset)
            )
            size = rng.randint(1, 500)
            events.append(
                MboEvent(
                    ts_event=ts,
                    action="A",
                    side=side,
                    price=price,
                    size=size,
                    order_id=index,
                    flags=F_LAST,
                    sequence=index,
                )
            )
            live.append((index, side, price))
        elif roll < 0.8:
            order_id, side, price = live.pop(rng.randrange(len(live)))
            events.append(
                MboEvent(
                    ts_event=ts,
                    action="C",
                    side=side,
                    price=price,
                    size=10**9,  # clamped by the book; a full cancel
                    order_id=order_id,
                    flags=F_LAST,
                    sequence=index,
                )
            )
        elif roll < 0.92:
            side = "B" if rng.random() < 0.5 else "A"
            events.append(
                MboEvent(
                    ts_event=ts,
                    action="T",
                    side=side,
                    price=reference,
                    size=rng.randint(1, 200),
                    order_id=0,
                    flags=F_LAST,
                    sequence=index,
                )
            )
        else:
            order_id, side, price = live[rng.randrange(len(live))]
            events.append(
                MboEvent(
                    ts_event=ts,
                    action="F",
                    side=side,
                    price=price,
                    size=rng.randint(1, 50),
                    order_id=order_id,
                    flags=F_LAST,
                    sequence=index,
                )
            )
    return events


def run(events):
    engine = OrderBookFeatureEngine(symbol="TEST", session_date="2025-06-26")
    return list(engine.process(events))


def index_of_event(events, snapshot) -> int:
    """The last event index that may be kept for this snapshot to still emit.

    Event cadences emit *on* their source event, so the cut is that event.

    Time cadences emit at a grid boundary, which only becomes final once an
    event arrives with `ts_event > boundary`. That trigger event contributes
    nothing to the snapshot -- boundaries are flushed before it is applied --
    so including it is causally sound, and excluding it would simply prevent
    the boundary from being emitted at all.
    """
    grid_ts = snapshot["grid_ts_event"]
    if grid_ts is None:
        for position, event in enumerate(events):
            if (
                event.ts_event == snapshot["source_ts_event"]
                and event.sequence == snapshot["sequence"]
            ):
                return position
        raise AssertionError("event-cadence snapshot has no source event")
    for position, event in enumerate(events):
        if event.ts_event > grid_ts:
            return position
    return len(events) - 1


# ---------------------------------------------------------------------------
# Truncation invariance
# ---------------------------------------------------------------------------


def test_snapshots_are_unchanged_when_the_future_is_cut_off():
    """The definitive causality check.

    Every snapshot from the full session must be byte-identical to the same
    snapshot produced from a stream that ends at its own event.
    """
    events = session_events()
    full = run(events)
    assert len(full) > 8, "the fixture must produce enough snapshots to be meaningful"

    for snapshot in full:
        cut = index_of_event(events, snapshot)
        truncated = run(events[: cut + 1])
        matching = [
            row
            for row in truncated
            if row["cadence"] == snapshot["cadence"]
            and row["sequence_index"] == snapshot["sequence_index"]
        ]
        assert matching, f"snapshot {snapshot['cadence']}#{snapshot['sequence_index']} vanished"
        assert matching[0] == snapshot, (
            f"{snapshot['cadence']}#{snapshot['sequence_index']} changed when the "
            "future was removed"
        )


def test_snapshots_are_unchanged_when_the_future_is_replaced():
    """Perturb everything after a snapshot; the snapshot must not move."""
    events = session_events()
    full = run(events)
    target = full[len(full) // 2]
    cut = index_of_event(events, target)

    perturbed = list(events[: cut + 1])
    rng = random.Random(99)
    for event in events[cut + 1 :]:
        perturbed.append(
            MboEvent(
                ts_event=event.ts_event + rng.randint(1, 5_000_000),
                action=event.action,
                side=event.side,
                price=event.price + rng.randint(-3, 3) * 10**7,
                size=max(1, event.size + rng.randint(-40, 40)),
                order_id=event.order_id,
                flags=event.flags,
                sequence=event.sequence,
            )
        )

    after = run(perturbed)
    matching = next(
        row
        for row in after
        if row["cadence"] == target["cadence"]
        and row["sequence_index"] == target["sequence_index"]
    )
    assert matching == target


def test_truncation_invariance_holds_once_z_scores_are_populated():
    """The short fixture never reaches MIN_PRIOR_OBSERVATIONS, so no z-score is
    populated and the normalizer escapes the invariance check above.

    This runs a session long enough for z-scores to exist, then truncates at
    snapshots that actually carry them. An expanding statistic is the most
    plausible place for a future value to leak backwards, so it has to be
    covered explicitly rather than by implication.
    """
    events = session_events(seed=11, count=6_000)
    full = run(events)
    with_z = [row for row in full if row["spread_bps_z"] is not None]
    assert len(with_z) > 20, "fixture must populate z-scores to test them"

    for snapshot in with_z[:: max(1, len(with_z) // 12)]:
        cut = index_of_event(events, snapshot)
        truncated = run(events[: cut + 1])
        matching = next(
            row
            for row in truncated
            if row["cadence"] == snapshot["cadence"]
            and row["sequence_index"] == snapshot["sequence_index"]
        )
        assert matching["spread_bps_z"] == snapshot["spread_bps_z"]
        assert matching == snapshot


def test_every_snapshot_precedes_the_events_it_excludes():
    """Ordering sanity: a snapshot's window never starts after it ends."""
    events = session_events()
    for snapshot in run(events):
        assert snapshot["window_ns"] >= 0
        assert snapshot["window_records"] >= 1
        assert snapshot["flast_index"] >= 1


# ---------------------------------------------------------------------------
# Prior-only normalization
# ---------------------------------------------------------------------------


def test_normalizer_excludes_the_current_observation():
    """A value must not be normalized against statistics that include itself."""
    normalizer = ExpandingNormalizer()
    for _ in range(MIN_PRIOR_OBSERVATIONS):
        assert normalizer.normalize_then_update(10.0) is None

    # Prior history is constant, so its variance is zero and no z is defined.
    assert normalizer.normalize_then_update(10.0) is None

    spread = ExpandingNormalizer()
    for value in range(MIN_PRIOR_OBSERVATIONS + 1):
        spread.normalize_then_update(float(value))
    # A large jump must read as extreme against the prior spread, which it
    # could not if it had already been folded into the mean.
    z = spread.normalize_then_update(1_000.0)
    assert z is not None and z > 5


def test_normalizer_withholds_below_the_minimum_history():
    normalizer = ExpandingNormalizer()
    results = [normalizer.normalize_then_update(float(i)) for i in range(MIN_PRIOR_OBSERVATIONS)]
    assert all(value is None for value in results)
    assert normalizer.count == MIN_PRIOR_OBSERVATIONS


def test_normalization_is_session_local():
    """Two identical sessions produce identical z-scores.

    Nothing carries across symbol-days, so a session cannot be normalized
    against a neighbour it would not have seen.
    """
    events = session_events()
    first = run(events)
    second = run(events)
    assert first == second
    engine = OrderBookFeatureEngine(symbol="OTHER", session_date="2025-06-27")
    third = list(engine.process(events))
    assert [row["spread_bps_z"] for row in third] == [row["spread_bps_z"] for row in first]


# ---------------------------------------------------------------------------
# Vocabulary contains nothing forward-looking
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "banned",
    ["forward", "future", "return", "label", "target", "pnl", "profit", "alpha", "signal"],
)
def test_no_feature_name_suggests_an_outcome(banned):
    offenders = [name for name in FEATURE_VOCABULARY if banned in name.lower()]
    assert offenders == [], f"Stage 1 must not carry outcome-shaped columns: {offenders}"


def test_definitions_declare_no_forward_information():
    definitions = feature_definitions()
    assert definitions["contains_forward_information"] is False
    assert definitions["feature_count"] == len(FEATURE_VOCABULARY)


def test_every_cadence_is_declared_not_discovered():
    """Cadences are fixed in code, not chosen per run."""
    assert [c.name for c in CADENCES] == ["1s", "5s", "50ev", "200ev"]
