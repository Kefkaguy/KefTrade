"""Stage 4.1 IAG-v2 raw-MBO tests.

Every scenario is built from synthetic ``MboEvent`` records, so the semantics
that matter -- a trade's side being the aggressor, a fill's being the resting
side, an execution's ``C`` not being a cancellation -- are exercised against the
real ``MboBook`` and the real scanner without needing a DBN file.

Governance boundaries get structural tests. This module's prose necessarily
discusses forward returns, so a keyword search would match the explanation and
prove nothing.
"""

from __future__ import annotations

import ast
import inspect
import itertools
import json

import pytest

from app.services.mbo_book_validator import (
    F_BAD_TS_RECV,
    F_LAST,
    F_MAYBE_BAD_BOOK,
    F_SNAPSHOT,
    FIXED_PRICE_SCALE,
    MboEvent,
)
from app.services.stage41_iag_v2_executor import (
    AMBIGUITY_NO_SIGNABLE_TRADE,
    AMBIGUITY_NOT_PERSISTENT,
    AMBIGUITY_ZERO_FLOW,
    FAIL_ABSORBED,
    FAIL_AMBIGUOUS_DIRECTION,
    FAIL_GATE,
    FAIL_NO_DEPLETION,
    FAIL_REPLENISHED,
    FAIL_SUPPORT,
    FAIL_THIN_BASELINE,
    FAILURE_REASONS,
    EventState,
    SessionScanner,
    build_baseline,
    count_specification,
    decide_verdict,
    gross_directional_displacement_bps,
    local_lambda,
    measure_event,
    qualifies,
    read_selection,
    resolve_direction,
    select_specification,
    session_clustered_inference,
    specification_by_name,
    supporting_count,
    write_selection,
)
from app.services.stage41_iag_v2_plan import (
    CERTIFIED_SYMBOLS,
    EFFECTIVE_TRIALS_AFTER_DESIGN,
    EFFECTIVE_TRIALS_AFTER_REVEAL,
    EFFECTIVE_TRIALS_BEFORE,
    EXPECTED_DESIGN_SHA256,
    FORBIDDEN_AS_DIRECTIONAL_EVIDENCE,
    GATE_COVERAGE,
    GATE_NO_COHERENT_STATE,
    GATE_ONE_SIDED,
    GATE_TIMING,
    LONG,
    MIN_AGREEING_QUARTERS,
    MIN_BASELINE_TILES,
    MIN_EVENTS,
    MIN_RAW_RECORD_REQUIREMENT,
    MIN_SESSIONS,
    MIN_TRADE_REQUIREMENT,
    NANOS_PER_SECOND,
    OBSERVATION_NS,
    OBSERVATION_SECONDS,
    PERSISTENCE_QUARTER_NS,
    PERSISTENCE_QUARTERS,
    PRIMARY_GROSS_HURDLE_BPS,
    PRIMARY_HORIZON_MINUTES,
    QUIET_PERIOD_MINUTES,
    SECONDARY_HORIZON_MINUTES,
    SHORT,
    SPEC_FALLBACK,
    SPEC_PRIMARY,
    T_HURDLE,
    VERDICT_DETECTED,
    VERDICT_INSUFFICIENT,
    VERDICT_NO_MECHANISM,
    assert_frozen_design,
    assert_not_side_agnostic,
    impacted_side,
    statistical_plan,
)

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[3]

T0 = 1_748_872_800_000_000_000  # a clean 120s-tile boundary
T_OBS_END = T0 + OBSERVATION_NS
PRICE = FIXED_PRICE_SCALE  # $1.00 in fixed-point units


# ---------------------------------------------------------------------------
# Synthetic record builders
# ---------------------------------------------------------------------------

_ids = itertools.count(1000)


def ev(action, side, *, ts, price=None, size=0, order_id=None, flags=F_LAST, seq=0):
    """One MBO record. ``F_LAST`` by default so each is its own native event."""
    return MboEvent(
        ts_event=ts,
        action=action,
        side=side,
        price=PRICE * 100 if price is None else price,
        size=size,
        order_id=next(_ids) if order_id is None else order_id,
        flags=flags,
        sequence=seq,
        ts_recv=ts,
    )


def opening_clear(ts):
    """XNAS full-session files open with a clear proving an empty book."""
    return ev("R", "N", ts=ts, size=0, order_id=0, flags=F_LAST)


def two_sided_book(ts, *, bid_size=1000, ask_size=1000, levels=3):
    """A resting book deep enough that depth_10 is meaningful."""
    records = []
    for level in range(levels):
        records.append(
            ev("A", "B", ts=ts, price=PRICE * 100 - (level + 1) * PRICE, size=bid_size)
        )
        records.append(
            ev("A", "A", ts=ts, price=PRICE * 100 + (level + 1) * PRICE, size=ask_size)
        )
    return records


def execution_group(ts, *, aggressor, resting_order_id, size, price=None):
    """The certified XNAS shape: T -> F -> C sharing a sequence.

    Only the last record carries ``F_LAST``; the group is one native event.
    """
    resting = "A" if aggressor == "B" else "B"
    px = PRICE * 100 if price is None else price
    return [
        ev("T", aggressor, ts=ts, price=px, size=size, flags=0, seq=7),
        ev("F", resting, ts=ts, price=px, size=size, order_id=resting_order_id,
           flags=0, seq=7),
        ev("C", resting, ts=ts, price=px, size=size, order_id=resting_order_id,
           flags=F_LAST, seq=7),
    ]


def scan(records, starts=()):
    """Run the scanner over records in receive order.

    The real feed is non-decreasing in ``ts_recv`` and the scanner now refuses
    anything else, so fixtures are sorted here rather than each being written in
    order by hand. ``sorted`` is stable, so records sharing an instant keep the
    order they were built in -- which is what makes a T -> F -> C group survive.
    """
    scanner = SessionScanner(list(starts))
    scanner.run(sorted(records, key=lambda r: r.ts_recv))
    return scanner


# ---------------------------------------------------------------------------
# Frozen design and governance
# ---------------------------------------------------------------------------


def test_the_frozen_design_verifies():
    verified = assert_frozen_design(REPO_ROOT)
    assert verified["design"]["sha256"] == EXPECTED_DESIGN_SHA256
    assert len(verified["design_json"]["sha256"]) == 64


def test_a_modified_design_is_refused(tmp_path):
    import shutil

    (tmp_path / "docs").mkdir()
    (tmp_path / "reports" / "tier1_stage41_design" / "v2").mkdir(parents=True)
    shutil.copy2(
        REPO_ROOT / "docs" / "2026-08-21-stage41-iag-v2-raw-mbo-design.md",
        tmp_path / "docs" / "2026-08-21-stage41-iag-v2-raw-mbo-design.md",
    )
    shutil.copy2(
        REPO_ROOT / "reports/tier1_stage41_design/v2/stage41_iag_v2_design.json",
        tmp_path / "reports/tier1_stage41_design/v2/stage41_iag_v2_design.json",
    )
    target = tmp_path / "docs" / "2026-08-21-stage41-iag-v2-raw-mbo-design.md"
    target.write_text(target.read_text(encoding="utf-8") + "\nsneak\n", encoding="utf-8")
    with pytest.raises(ValueError, match="design has changed"):
        assert_frozen_design(tmp_path)


def test_the_ledger_does_not_move_for_design_or_diagnose():
    assert EFFECTIVE_TRIALS_BEFORE == 531
    assert EFFECTIVE_TRIALS_AFTER_DESIGN == 531
    assert EFFECTIVE_TRIALS_AFTER_REVEAL == 532
    plan = statistical_plan()
    assert plan["effective_trials_before"] == plan["effective_trials_after"] == 531


def test_iag_v1_is_recorded_as_retired_and_not_confirmed():
    plan = statistical_plan()
    retirement = plan["iag_v1_retirement"]
    assert retirement["verdict"] == VERDICT_INSUFFICIENT
    assert retirement["economic_outcome_viewed"] is False
    assert retirement["v2_is_confirmation_of_v1"] is False
    assert retirement["v2_class"] == "new_exploratory_measurement_specification"


def test_the_preserved_parameters_are_unchanged():
    plan = statistical_plan()
    assert plan["observation_window"]["seconds"] == OBSERVATION_SECONDS == 120
    assert plan["observation_window"]["persistence_quarters"] == 4
    assert plan["observation_window"]["min_agreeing_quarters"] == 3
    assert plan["population"]["quiet_period_minutes"] == QUIET_PERIOD_MINUTES == 60
    assert plan["baseline"]["min_tiles"] == MIN_BASELINE_TILES == 500
    assert plan["baseline"]["percentile_levels"] == [25.0, 75.0]
    assert plan["sample_floors"] == {"min_events": 100, "min_sessions": 15}
    assert plan["economic_test"]["primary_horizon_minutes"] == 15
    assert plan["economic_test"]["secondary_diagnostic_horizons"] == [5, 30]
    assert plan["hurdle"]["primary_gross_hurdle_bps"] == 12.0
    assert plan["hurdle"]["t_hurdle"] == 3.0
    assert plan["population"]["symbols"] == list(CERTIFIED_SYMBOLS)


def test_the_v1_row_requirement_is_gone_and_nothing_replaced_it():
    """The whole point of v2. No raw-record or trade minimum stands in for it."""
    assert MIN_RAW_RECORD_REQUIREMENT is None
    assert MIN_TRADE_REQUIREMENT is None
    plan = statistical_plan()
    assert plan["observation_window"]["min_raw_record_requirement"] is None
    assert plan["observation_window"]["min_trade_requirement"] is None


def test_there_is_no_cadence_agreement_requirement():
    plan = statistical_plan()
    assert "cadences_must_agree" not in plan.get("direction", {})
    source = inspect.getsource(resolve_direction)
    assert "50ev" not in source
    assert "200ev" not in source


# ---------------------------------------------------------------------------
# Raw semantics: the aggressor, and the fill that is not one
# ---------------------------------------------------------------------------


def test_a_trade_side_bid_is_a_buy_aggressor():
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records += two_sided_book(T0 - NANOS_PER_SECOND)
    for quarter in range(PERSISTENCE_QUARTERS):
        records.append(
            ev("T", "B", ts=T0 + quarter * PERSISTENCE_QUARTER_NS + 1, size=100)
        )
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, size=1))
    scanner = scan(records, starts=[T0])
    stats = scanner.event_intervals[T0]
    assert stats.buy_shares == 400
    assert stats.sell_shares == 0
    assert stats.signable_trades == 4
    assert resolve_direction(stats).direction == LONG


def test_a_trade_side_ask_is_a_sell_aggressor():
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records += two_sided_book(T0 - NANOS_PER_SECOND)
    for quarter in range(PERSISTENCE_QUARTERS):
        records.append(
            ev("T", "A", ts=T0 + quarter * PERSISTENCE_QUARTER_NS + 1, size=100)
        )
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, size=1))
    stats = scan(records, starts=[T0]).event_intervals[T0]
    assert stats.sell_shares == 400
    assert resolve_direction(stats).direction == SHORT


def test_a_fill_never_contributes_signed_aggression():
    """A fill's side is the RESTING side. Signing it would be wrong twice over:
    inverted, and already carried by the trade."""
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records += two_sided_book(T0 - NANOS_PER_SECOND)
    # Fills only -- no trade records at all.
    for quarter in range(PERSISTENCE_QUARTERS):
        records.append(
            ev("F", "A", ts=T0 + quarter * PERSISTENCE_QUARTER_NS + 1, size=100)
        )
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, size=1))
    stats = scan(records, starts=[T0]).event_intervals[T0]
    assert stats.buy_shares == 0
    assert stats.sell_shares == 0
    assert stats.signable_trades == 0
    assert resolve_direction(stats).reason == AMBIGUITY_NO_SIGNABLE_TRADE


def test_a_trade_with_side_none_is_counted_but_never_signed():
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records += two_sided_book(T0 - NANOS_PER_SECOND)
    records.append(ev("T", "N", ts=T0 + 1, size=500))
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, size=1))
    stats = scan(records, starts=[T0]).event_intervals[T0]
    assert stats.unsignable_trades == 1
    assert stats.signable_trades == 0
    assert stats.net_flow == 0


def test_an_execution_group_is_not_a_voluntary_cancellation():
    """T -> F -> C is one execution. Counting its C as a withdrawal would count
    the same execution twice: once as flow, again as liquidity leaving."""
    resting_id = 5555
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records += two_sided_book(T0 - NANOS_PER_SECOND)
    records.append(
        ev("A", "A", ts=T0 - NANOS_PER_SECOND, price=PRICE * 101, size=800,
           order_id=resting_id)
    )
    records += execution_group(
        T0 + 1, aggressor="B", resting_order_id=resting_id, size=800,
        price=PRICE * 101,
    )
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, size=1))
    stats = scan(records, starts=[T0]).event_intervals[T0]

    assert stats.buy_shares == 800  # the trade was signed
    assert stats.withdrawn_ask == 0  # its cancel was not counted as withdrawal
    assert stats.execution_volume == 800


def test_a_standalone_cancel_is_a_genuine_withdrawal():
    resting_id = 6666
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records += two_sided_book(T0 - NANOS_PER_SECOND)
    records.append(
        ev("A", "A", ts=T0 - NANOS_PER_SECOND, price=PRICE * 101, size=700,
           order_id=resting_id)
    )
    records.append(
        ev("C", "A", ts=T0 + 1, price=PRICE * 101, size=700, order_id=resting_id)
    )
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, size=1))
    stats = scan(records, starts=[T0]).event_intervals[T0]
    assert stats.withdrawn_ask == 700
    assert stats.added_ask == 0


def test_a_genuine_add_is_counted_as_addition():
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records += two_sided_book(T0 - NANOS_PER_SECOND)
    records.append(ev("A", "A", ts=T0 + 1, price=PRICE * 101, size=350))
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, size=1))
    stats = scan(records, starts=[T0]).event_intervals[T0]
    assert stats.added_ask == 350
    assert stats.withdrawn_ask == 0


def test_a_same_price_modify_increase_is_an_addition_of_the_difference():
    resting_id = 7777
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records += two_sided_book(T0 - NANOS_PER_SECOND)
    records.append(
        ev("A", "A", ts=T0 - NANOS_PER_SECOND, price=PRICE * 101, size=200,
           order_id=resting_id)
    )
    records.append(
        ev("M", "A", ts=T0 + 1, price=PRICE * 101, size=500, order_id=resting_id)
    )
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, size=1))
    stats = scan(records, starts=[T0]).event_intervals[T0]
    assert stats.added_ask == 300  # 500 - 200
    assert stats.withdrawn_ask == 0


def test_a_same_price_modify_decrease_is_a_withdrawal_of_the_difference():
    resting_id = 8888
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records += two_sided_book(T0 - NANOS_PER_SECOND)
    records.append(
        ev("A", "A", ts=T0 - NANOS_PER_SECOND, price=PRICE * 101, size=900,
           order_id=resting_id)
    )
    records.append(
        ev("M", "A", ts=T0 + 1, price=PRICE * 101, size=400, order_id=resting_id)
    )
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, size=1))
    stats = scan(records, starts=[T0]).event_intervals[T0]
    assert stats.withdrawn_ask == 500  # 900 - 400
    assert stats.added_ask == 0


def test_a_price_changing_modify_withdraws_the_old_resting_size():
    resting_id = 9999
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records += two_sided_book(T0 - NANOS_PER_SECOND)
    records.append(
        ev("A", "A", ts=T0 - NANOS_PER_SECOND, price=PRICE * 101, size=600,
           order_id=resting_id)
    )
    records.append(
        ev("M", "A", ts=T0 + 1, price=PRICE * 105, size=600, order_id=resting_id)
    )
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, size=1))
    stats = scan(records, starts=[T0]).event_intervals[T0]
    assert stats.withdrawn_ask == 600


def test_snapshot_records_are_book_state_not_order_events():
    records = [opening_clear(T0 - 2 * NANOS_PER_SECOND)]
    records += two_sided_book(T0 - 2 * NANOS_PER_SECOND)
    records.append(
        ev("A", "A", ts=T0 + 1, price=PRICE * 101, size=1000, flags=F_LAST | F_SNAPSHOT)
    )
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, size=1))
    stats = scan(records, starts=[T0]).event_intervals[T0]
    assert stats.added_ask == 0  # snapshot add is state, not an order event


# ---------------------------------------------------------------------------
# Direction and persistence
# ---------------------------------------------------------------------------


def _directional_records(quarter_sizes, side="B"):
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records += two_sided_book(T0 - NANOS_PER_SECOND)
    for index, size in enumerate(quarter_sizes):
        if size == 0:
            continue
        ts = T0 + index * PERSISTENCE_QUARTER_NS + 1
        records.append(
            ev("T", side if size > 0 else ("A" if side == "B" else "B"),
               ts=ts, size=abs(size))
        )
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, size=1))
    return records


def test_four_agreeing_quarters_resolve_the_direction():
    stats = scan(_directional_records([100, 100, 100, 100]), starts=[T0]).event_intervals[T0]
    verdict = resolve_direction(stats)
    assert verdict.direction == LONG
    assert verdict.agreeing_quarters == 4


def test_exactly_three_of_four_quarters_is_enough():
    stats = scan(_directional_records([100, 100, -50, 100]), starts=[T0]).event_intervals[T0]
    verdict = resolve_direction(stats)
    assert verdict.agreeing_quarters == MIN_AGREEING_QUARTERS == 3
    assert verdict.direction == LONG


def test_a_burst_without_persistence_is_ambiguous():
    """Net sign alone cannot tell sustained pressure from one large print."""
    stats = scan(_directional_records([1000, -10, -10, -10]), starts=[T0]).event_intervals[T0]
    verdict = resolve_direction(stats)
    assert verdict.net_flow > 0
    assert verdict.agreeing_quarters == 1
    assert verdict.direction is None
    assert verdict.reason == AMBIGUITY_NOT_PERSISTENT


def test_a_silent_quarter_does_not_agree():
    stats = scan(_directional_records([100, 100, 0, 0]), starts=[T0]).event_intervals[T0]
    verdict = resolve_direction(stats)
    assert verdict.agreeing_quarters == 2
    assert verdict.direction is None


def test_exactly_balanced_flow_is_zero_net_flow():
    stats = scan(_directional_records([100, -100, 100, -100]), starts=[T0]).event_intervals[T0]
    verdict = resolve_direction(stats)
    assert verdict.net_flow == 0
    assert verdict.reason == AMBIGUITY_ZERO_FLOW


def test_the_quarter_boundaries_are_exact_and_non_overlapping():
    """Three half-open quarters and a final closed one, so the instant at
    t_obs_end is counted and nothing is counted twice."""
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records += two_sided_book(T0 - NANOS_PER_SECOND)
    for boundary in (
        T0,
        T0 + PERSISTENCE_QUARTER_NS,
        T0 + 2 * PERSISTENCE_QUARTER_NS,
        T0 + 3 * PERSISTENCE_QUARTER_NS,
        T_OBS_END,
    ):
        records.append(ev("T", "B", ts=boundary, size=10))
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, size=1))
    stats = scan(records, starts=[T0]).event_intervals[T0]
    # 5 trades: one opening each quarter, plus one exactly at t_obs_end which
    # belongs to the final quarter.
    assert stats.signable_trades == 5
    assert stats.buy_shares == 50
    assert stats.quarter_signs == (LONG, LONG, LONG, LONG)


def test_a_record_after_t_obs_end_is_outside_the_window():
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records += two_sided_book(T0 - NANOS_PER_SECOND)
    records.append(ev("T", "B", ts=T_OBS_END, size=10))
    records.append(ev("T", "B", ts=T_OBS_END + 1, size=999_999))
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, size=1))
    stats = scan(records, starts=[T0]).event_intervals[T0]
    assert stats.buy_shares == 10  # the +1ns trade is excluded


def test_the_impacted_side_follows_the_direction():
    assert impacted_side(LONG) == "A"
    assert impacted_side(SHORT) == "B"
    with pytest.raises(ValueError, match="neither"):
        impacted_side(0)


# ---------------------------------------------------------------------------
# State selection: latest coherent F_LAST at or before t
# ---------------------------------------------------------------------------


def test_the_anchor_is_the_latest_state_at_or_before_t0():
    """Never nearest-in-time: a state 1 ns after t0 must not become S(t0)."""
    records = [opening_clear(T0 - 10 * NANOS_PER_SECOND)]
    records += two_sided_book(T0 - 10 * NANOS_PER_SECOND, ask_size=1000)
    # A much larger ask book arrives 1 ns AFTER t0. It must not be the anchor.
    records.append(ev("A", "A", ts=T0 + 1, price=PRICE * 104, size=50_000))
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, size=1))
    stats = scan(records, starts=[T0]).event_intervals[T0]
    assert stats.anchor_ask_depth == 3000  # three levels of 1000, pre-t0
    assert stats.final_ask_depth == 53_000  # the late add is inside the window


def test_a_state_after_t_obs_end_never_becomes_the_final_state():
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records += two_sided_book(T0 - NANOS_PER_SECOND, ask_size=1000)
    # One record inside the window, so the window is not merely empty...
    records.append(ev("T", "B", ts=T0 + NANOS_PER_SECOND, size=10))
    # ...and a large add 1 ns after the cutoff, which must stay invisible.
    records.append(ev("A", "A", ts=T_OBS_END + 1, price=PRICE * 104, size=99_000))
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, size=1))
    stats = scan(records, starts=[T0]).event_intervals[T0]
    assert stats.final_ask_depth == 3000  # the post-cutoff add is invisible


def test_an_absent_anchor_is_never_filled_from_inside_the_window():
    """S(t0) must be undefined when no coherent state precedes t0. Borrowing the
    first in-window state would be "nearest", which the rule forbids, and it
    would silently give the event a reference depth measured *after* the news."""
    records = [
        # The session opens INSIDE the window: nothing precedes t0.
        opening_clear(T0 + NANOS_PER_SECOND),
    ]
    records += two_sided_book(T0 + NANOS_PER_SECOND, ask_size=7777)
    records.append(ev("T", "B", ts=T0 + 2 * NANOS_PER_SECOND, size=10))
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, size=1))
    stats = scan(records, starts=[T0]).event_intervals[T0]

    assert stats.anchor_ask_depth is None
    assert stats.anchor_midpoint is None
    # ...and the in-window state exists, so the only way anchor could be filled
    # is by taking a state from after t0.
    assert stats.final_ask_depth == 7777 * 3

    state = measure_event(
        symbol="AAPL", session_date="2025-06-02", story_id="s", t0_ns=T0,
        stats=stats, baseline=build_baseline("AAPL", []),
    )
    assert state.gate_failure == GATE_ONE_SIDED
    assert local_lambda(stats, LONG) is None  # no start midpoint, no lambda


def test_a_window_with_no_records_is_empty_not_uncovered():
    """The stream spans it; there was simply no activity. Saying "uncovered"
    would blame the data for the market being quiet."""
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records += two_sided_book(T0 - NANOS_PER_SECOND)
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, size=1))
    scanner = scan(records, starts=[T0])
    stats = scanner.event_intervals[T0]
    assert stats.records == 0
    assert stats.coherent_states == 0
    assert T0 not in scanner.event_gate_failures
    state = measure_event(
        symbol="AAPL", session_date="2025-06-02", story_id="s", t0_ns=T0,
        stats=stats, baseline=build_baseline("AAPL", []),
    )
    assert state.gate_failure == GATE_NO_COHERENT_STATE


def test_an_out_of_order_stream_is_refused():
    """The state-selection rule rests on non-decreasing receive order; a file
    that violates it would anchor windows to the wrong state."""
    records = [
        opening_clear(T0 - NANOS_PER_SECOND),
        ev("T", "B", ts=T0 + 10 * NANOS_PER_SECOND, size=10),
        ev("T", "B", ts=T0 + NANOS_PER_SECOND, size=10),
    ]
    scanner = SessionScanner([T0])
    with pytest.raises(ValueError, match="out of receive order"):
        scanner.run(records)


def test_state_is_captured_only_at_completed_native_events():
    """Mid-group the touch is transient. Stage 1 learned this twice."""
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records += two_sided_book(T0 - NANOS_PER_SECOND, ask_size=1000)
    # A three-record native event: two adds then the F_LAST. Only one coherent
    # state should result, not three.
    records.append(ev("A", "A", ts=T0 + 1, price=PRICE * 104, size=100, flags=0, seq=9))
    records.append(ev("A", "A", ts=T0 + 2, price=PRICE * 105, size=100, flags=0, seq=9))
    records.append(
        ev("A", "A", ts=T0 + 3, price=PRICE * 106, size=100, flags=F_LAST, seq=9)
    )
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, size=1))
    stats = scan(records, starts=[T0]).event_intervals[T0]
    assert stats.coherent_states == 1
    assert stats.records == 3


def test_a_one_sided_book_yields_no_midpoint_and_fails_the_gate():
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    # Bids only: no midpoint anywhere.
    records.append(ev("A", "B", ts=T0 - NANOS_PER_SECOND, price=PRICE * 99, size=500))
    records.append(ev("T", "B", ts=T0 + 1, size=10))
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, price=PRICE * 98,
                      size=1))
    scanner = scan(records, starts=[T0])
    stats = scanner.event_intervals[T0]
    assert stats.final_midpoint is None
    state = measure_event(
        symbol="AAPL", session_date="2025-06-02", story_id="s", t0_ns=T0,
        stats=stats, baseline=build_baseline("AAPL", []),
    )
    assert state.gate_failure == GATE_ONE_SIDED


# ---------------------------------------------------------------------------
# Timing and coverage gates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", [F_BAD_TS_RECV, F_MAYBE_BAD_BOOK])
def test_a_flagged_record_in_the_window_fails_closed(flag):
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records += two_sided_book(T0 - NANOS_PER_SECOND)
    records.append(ev("A", "A", ts=T0 + 1, price=PRICE * 104, size=10,
                      flags=F_LAST | flag))
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, size=1))
    stats = scan(records, starts=[T0]).event_intervals[T0]
    assert stats.timing_flagged is True
    state = measure_event(
        symbol="AAPL", session_date="2025-06-02", story_id="s", t0_ns=T0,
        stats=stats, baseline=build_baseline("AAPL", []),
    )
    assert state.gate_failure == GATE_TIMING


def test_a_flag_outside_the_window_does_not_contaminate_it():
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records += two_sided_book(T0 - NANOS_PER_SECOND)
    records.append(ev("T", "B", ts=T0 + 1, size=10))
    records.append(ev("A", "A", ts=T_OBS_END + 1, price=PRICE * 104, size=10,
                      flags=F_LAST | F_BAD_TS_RECV))
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, size=1))
    stats = scan(records, starts=[T0]).event_intervals[T0]
    assert stats.timing_flagged is False


def test_a_stream_ending_before_the_window_closes_is_incomplete_coverage():
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records += two_sided_book(T0 - NANOS_PER_SECOND)
    records.append(ev("T", "B", ts=T0 + 1, size=10))
    # Nothing after t_obs_end: the window was never fully observed.
    scanner = scan(records, starts=[T0])
    assert scanner.event_gate_failures.get(T0) == GATE_COVERAGE


def test_overlapping_event_windows_are_refused_outright():
    """The single-pointer design is only sound because the quiet period keeps
    windows apart. Assert it rather than assume it."""
    with pytest.raises(ValueError, match="event windows overlap"):
        SessionScanner([T0, T0 + OBSERVATION_NS])
    SessionScanner([T0, T0 + OBSERVATION_NS + 1])  # adjacent is fine


# ---------------------------------------------------------------------------
# Depth, M2 and M3
# ---------------------------------------------------------------------------


def _depletion_records(*, ask_start=3000, trough=300, ask_end=400):
    """Ask liquidity consumed and only partly restored."""
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records.append(
        ev("A", "B", ts=T0 - NANOS_PER_SECOND, price=PRICE * 99, size=5000)
    )
    resting = 4242
    records.append(
        ev("A", "A", ts=T0 - NANOS_PER_SECOND, price=PRICE * 101, size=ask_start,
           order_id=resting)
    )
    for quarter in range(PERSISTENCE_QUARTERS):
        records.append(
            ev("T", "B", ts=T0 + quarter * PERSISTENCE_QUARTER_NS + 1, size=100)
        )
    records.append(
        ev("M", "A", ts=T0 + 10 * NANOS_PER_SECOND, price=PRICE * 101, size=trough,
           order_id=resting)
    )
    records.append(
        ev("M", "A", ts=T0 + 20 * NANOS_PER_SECOND, price=PRICE * 101, size=ask_end,
           order_id=resting)
    )
    records.append(ev("A", "B", ts=T_OBS_END + NANOS_PER_SECOND, price=PRICE * 98,
                      size=1))
    return records


def test_depletion_and_recovery_use_the_impacted_side_only():
    stats = scan(_depletion_records(), starts=[T0]).event_intervals[T0]
    baseline = build_baseline("AAPL", [stats] * 600)
    state = measure_event(
        symbol="AAPL", session_date="2025-06-02", story_id="s", t0_ns=T0,
        stats=stats, baseline=baseline,
    )
    assert state.direction == LONG
    assert state.depth_ref == 3000  # ask at S(t0)
    assert state.depth_min == 300
    assert state.depth_end == 400
    assert state.depletion_ratio == pytest.approx(0.1)
    # (400 - 300) / (3000 - 300)
    assert state.recovery_ratio == pytest.approx(100 / 2700)


def test_a_side_that_never_drew_down_has_recovery_one():
    """Liquidity that never fell cannot have failed to return."""
    stats = scan(
        _depletion_records(ask_start=1000, trough=1000, ask_end=1000), starts=[T0]
    ).event_intervals[T0]
    state = measure_event(
        symbol="AAPL", session_date="2025-06-02", story_id="s", t0_ns=T0,
        stats=stats, baseline=build_baseline("AAPL", [stats] * 600),
    )
    assert state.recovery_ratio == 1.0
    # M2 fires first here -- a side that never fell is also not depleted -- so
    # the replenishment gate is checked on a state that clears M2.
    assert qualifies(state, SPEC_PRIMARY)[1] == FAIL_NO_DEPLETION
    depleted_but_recovered = _state(depth_percentile=10.0, recovery_ratio=1.0)
    assert qualifies(depleted_but_recovered, SPEC_PRIMARY)[1] == FAIL_REPLENISHED


def test_a_short_event_measures_the_bid_ladder():
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records.append(ev("A", "A", ts=T0 - NANOS_PER_SECOND, price=PRICE * 101, size=5000))
    resting = 3131
    records.append(
        ev("A", "B", ts=T0 - NANOS_PER_SECOND, price=PRICE * 99, size=2000,
           order_id=resting)
    )
    for quarter in range(PERSISTENCE_QUARTERS):
        records.append(
            ev("T", "A", ts=T0 + quarter * PERSISTENCE_QUARTER_NS + 1, size=100)
        )
    records.append(
        ev("M", "B", ts=T0 + 10 * NANOS_PER_SECOND, price=PRICE * 99, size=200,
           order_id=resting)
    )
    records.append(ev("A", "A", ts=T_OBS_END + NANOS_PER_SECOND, price=PRICE * 102,
                      size=1))
    stats = scan(records, starts=[T0]).event_intervals[T0]
    state = measure_event(
        symbol="AAPL", session_date="2025-06-02", story_id="s", t0_ns=T0,
        stats=stats, baseline=build_baseline("AAPL", [stats] * 600),
    )
    assert state.direction == SHORT
    assert state.depth_ref == 2000  # bid at S(t0)
    assert state.depth_min == 200


# ---------------------------------------------------------------------------
# Lambda
# ---------------------------------------------------------------------------


def _stats_with(**overrides):
    from app.services.stage41_iag_v2_executor import IntervalStats

    base = {
        "start_ns": T0,
        "records": 100,
        "signable_trades": 10,
        "unsignable_trades": 0,
        "coherent_states": 50,
        "buy_shares": 1000,
        "sell_shares": 0,
        "quarter_signs": (1, 1, 1, 1),
        "anchor_bid_depth": 1000.0,
        "anchor_ask_depth": 1000.0,
        "anchor_midpoint": 100.0,
        "final_bid_depth": 900.0,
        "final_ask_depth": 200.0,
        "final_midpoint": 100.1,
        "final_spread_bps": 3.0,
        "min_bid_depth": 800.0,
        "min_ask_depth": 100.0,
        "withdrawn_bid": 0,
        "withdrawn_ask": 0,
        "added_bid": 0,
        "added_ask": 0,
        "added_since_min_bid": 0,
        "added_since_min_ask": 0,
        "execution_count": 10,
        "execution_volume": 1000,
        "absorbed_volume": 100,
        "timing_flagged": False,
    }
    base.update(overrides)
    return IntervalStats(**base)


def test_lambda_is_bps_per_thousand_aggressive_shares():
    assert local_lambda(_stats_with(), LONG) == pytest.approx(10.0)


def test_lambda_incorporates_direction_in_both_terms():
    long_value = local_lambda(_stats_with(), LONG)
    short_value = local_lambda(
        _stats_with(buy_shares=0, sell_shares=1000, final_midpoint=99.9), SHORT
    )
    assert long_value == pytest.approx(short_value)


@pytest.mark.parametrize(
    "override",
    [
        {"anchor_midpoint": None},
        {"final_midpoint": None},
        {"anchor_midpoint": 0.0},
        {"buy_shares": 99},
        {"buy_shares": 0},
    ],
)
def test_lambda_fails_closed(override):
    assert local_lambda(_stats_with(**override), LONG) is None


def test_lambda_reads_only_the_reduced_window():
    """Its inputs are one interval and a direction. No clock argument exists
    through which a later instant could enter."""
    assert list(inspect.signature(local_lambda).parameters) == ["stats", "direction"]


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


def test_the_baseline_is_prior_only_and_needs_five_hundred_tiles():
    assert MIN_BASELINE_TILES == 500
    assert not build_baseline("AAPL", [_stats_with()] * 499).is_sufficient
    assert build_baseline("AAPL", [_stats_with()] * 500).is_sufficient


def test_a_tile_with_no_coherent_state_is_dropped():
    """Borrowing a state from the previous tile would report one tile's
    liquidity as another's."""
    records = [opening_clear(T0 - 300 * NANOS_PER_SECOND)]
    records += two_sided_book(T0 - 300 * NANOS_PER_SECOND)
    # A record with no F_LAST produces no coherent state for its tile.
    records.append(ev("A", "A", ts=T0 + 1, price=PRICE * 104, size=10, flags=0))
    scanner = scan(records)
    for tile in scanner.tiles:
        assert tile.final_ask_depth is not None


def test_the_baseline_keeps_long_and_short_lambda_separate():
    baseline = build_baseline("AAPL", [_stats_with()] * 10)
    assert "lambda_long" in baseline.samples
    assert "lambda_short" in baseline.samples


def test_the_baseline_keeps_ask_and_bid_withdrawal_separate():
    baseline = build_baseline("AAPL", [_stats_with()] * 10)
    assert "withdrawal_pressure_ask" in baseline.samples
    assert "withdrawal_pressure_bid" in baseline.samples


def test_percentiles_locate_a_value_in_the_prior_distribution():
    tiles = [_stats_with(final_ask_depth=float(i)) for i in range(100)]
    baseline = build_baseline("AAPL", tiles)
    assert baseline.percentile_of("final_ask_depth", -1.0) == pytest.approx(0.0)
    assert baseline.percentile_of("final_ask_depth", 1000.0) == pytest.approx(100.0)
    assert baseline.percentile_of("final_ask_depth", None) is None


# ---------------------------------------------------------------------------
# Qualification and the ladder
# ---------------------------------------------------------------------------


def _state(**overrides):
    base = {
        "symbol": "AAPL",
        "session_date": "2025-06-02",
        "story_id": "abc",
        "t0_ns": T0,
        "t_obs_end_ns": T_OBS_END,
        "gate_failure": None,
        "records": 1500,
        "signable_trades": 20,
        "unsignable_trades": 0,
        "coherent_states": 400,
        "direction": LONG,
        "direction_reason": None,
        "agreeing_quarters": 4,
        "quarter_signs": (1, 1, 1, 1),
        "net_flow": 2000,
        "depth_ref": 3000.0,
        "depth_min": 200.0,
        "depth_end": 300.0,
        "depletion_ratio": 0.07,
        "recovery_ratio": 0.04,
        "replenishment_ratio": 0.03,
        "depth_percentile": 10.0,
        "absorption_percentile": 30.0,
        "lambda_value": 8.0,
        "lambda_percentile": 80.0,
        "spread_percentile": 80.0,
        "intensity_percentile": 40.0,
        "withdrawal_percentile": 40.0,
        "baseline_tiles": 1000,
    }
    base.update(overrides)
    return EventState(**base)


def test_a_fully_qualifying_event_passes():
    ok, reason, support = qualifies(_state(), SPEC_PRIMARY)
    assert (ok, reason, support) == (True, None, 2)


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"gate_failure": GATE_TIMING}, FAIL_GATE),
        ({"baseline_tiles": 499}, FAIL_THIN_BASELINE),
        ({"direction": None}, FAIL_AMBIGUOUS_DIRECTION),
        ({"depth_percentile": 26.0}, FAIL_NO_DEPLETION),
        ({"recovery_ratio": 0.26}, FAIL_REPLENISHED),
        ({"absorption_percentile": 75.0}, FAIL_ABSORBED),
        ({"lambda_percentile": 10.0, "spread_percentile": 10.0}, FAIL_SUPPORT),
    ],
)
def test_each_gate_refuses_with_its_own_reason(override, expected):
    ok, reason, _support = qualifies(_state(**override), SPEC_PRIMARY)
    assert ok is False
    assert reason == expected
    assert reason in FAILURE_REASONS


def test_lambda_alone_can_never_qualify_an_event():
    """It conditions on in-window displacement, so two conditions are always
    required and it can never be sufficient by itself."""
    state = _state(lambda_percentile=99.0, spread_percentile=10.0,
                   intensity_percentile=10.0, withdrawal_percentile=10.0)
    assert supporting_count(state) == 1
    assert qualifies(state, SPEC_PRIMARY)[0] is False


def test_an_unmeasurable_absorption_does_not_disqualify():
    assert qualifies(_state(absorption_percentile=None), SPEC_PRIMARY)[0] is True


def test_the_fallback_is_looser_on_exactly_two_declared_dimensions():
    assert SPEC_FALLBACK.depletion_percentile == 50.0
    assert SPEC_FALLBACK.recovery_threshold == 0.50
    assert SPEC_FALLBACK.min_supporting == SPEC_PRIMARY.min_supporting == 2
    marginal = _state(depth_percentile=40.0, recovery_ratio=0.40)
    assert qualifies(marginal, SPEC_PRIMARY)[0] is False
    assert qualifies(marginal, SPEC_FALLBACK)[0] is True


def _population(primary_count, fallback_only, sessions):
    states = []
    for i in range(primary_count):
        states.append(_state(story_id=f"p{i}",
                             session_date=f"2025-06-{(i % sessions) + 2:02d}"))
    for i in range(fallback_only):
        states.append(_state(story_id=f"f{i}",
                             session_date=f"2025-06-{(i % sessions) + 2:02d}",
                             depth_percentile=40.0, recovery_ratio=0.40))
    return states


def test_primary_is_selected_and_the_fallback_is_not_even_counted():
    selection = select_specification(_population(120, 50, 18))
    assert selection["selected_specification"] == SPEC_PRIMARY.name
    assert selection["fallback"] is None
    assert selection["fallback_evaluated"] is False
    assert selection["economic_run_authorized"] is True


def test_the_fallback_is_evaluated_only_when_primary_misses_a_floor():
    selection = select_specification(_population(40, 100, 18))
    assert selection["selected_specification"] == SPEC_FALLBACK.name
    assert selection["fallback_evaluated"] is True
    assert selection["primary"]["clears_floors"] is False


def test_a_session_shortfall_alone_sends_primary_to_the_fallback():
    selection = select_specification(_population(200, 0, 10))
    assert selection["primary"]["eligible_events"] == 200
    assert selection["primary"]["distinct_sessions"] == 10
    assert selection["primary"]["clears_floors"] is False


def test_neither_clearing_means_no_economic_run():
    selection = select_specification(_population(10, 10, 5))
    assert selection["selected_specification"] is None
    assert selection["economic_run_authorized"] is False
    assert selection["verdict_if_no_run"] == VERDICT_INSUFFICIENT


def test_the_ladder_reads_no_outcome():
    tree = ast.parse(inspect.getsource(select_specification))
    called = {getattr(n.func, "id", None) or getattr(n.func, "attr", None)
              for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert "gross_directional_displacement_bps" not in called
    assert "session_clustered_inference" not in called
    assert "decide_verdict" not in called


def test_the_floors_are_unchanged():
    assert MIN_EVENTS == 100
    assert MIN_SESSIONS == 15


def test_the_counts_report_the_failure_distribution():
    counts = count_specification(_population(5, 5, 3), SPEC_PRIMARY)
    assert counts.events == 5
    assert counts.failures[FAIL_NO_DEPLETION] == 5


# ---------------------------------------------------------------------------
# Selection persistence and the reveal gates
# ---------------------------------------------------------------------------


def test_the_selection_is_persisted_and_hashed(tmp_path):
    record = {"selected_specification": SPEC_PRIMARY.name}
    path = tmp_path / "selection.json"
    digest = write_selection(record, path)
    assert len(digest) == 64
    assert read_selection(path, expected_sha256=digest) == record


def test_an_edited_selection_is_refused(tmp_path):
    path = tmp_path / "selection.json"
    digest = write_selection({"selected_specification": SPEC_PRIMARY.name}, path)
    path.write_text(json.dumps({"selected_specification": SPEC_FALLBACK.name}),
                    encoding="utf-8")
    with pytest.raises(ValueError, match="selection has changed"):
        read_selection(path, expected_sha256=digest)


def test_a_missing_selection_is_refused(tmp_path):
    with pytest.raises(ValueError, match="Run diagnose first"):
        read_selection(tmp_path / "absent.json")


def test_only_declared_specifications_resolve_by_name():
    assert specification_by_name(SPEC_PRIMARY.name) is SPEC_PRIMARY
    with pytest.raises(ValueError, match="not a declared"):
        specification_by_name("IAG_v2_TUNED")


def test_the_run_is_gated_by_an_explicit_flag():
    import argparse as _argparse

    from app.cli.stage41_iag_v2 import run

    args = _argparse.Namespace(
        i_have_reviewed_the_design=False, output_dir=".", features_dir=".",
        raw_dir=".", selection_sha256=None, command="run",
    )
    with pytest.raises(ValueError, match="not authorized"):
        run(args)


def test_the_run_refuses_a_second_reveal(tmp_path):
    import argparse as _argparse

    from app.cli.stage41_iag_v2 import run
    from app.services.stage41_iag_v2_plan import RESULTS_FILENAME

    (tmp_path / RESULTS_FILENAME).write_text("{}", encoding="utf-8")
    args = _argparse.Namespace(
        i_have_reviewed_the_design=True, output_dir=str(tmp_path),
        features_dir=".", raw_dir=".", selection_sha256=None, command="run",
    )
    with pytest.raises(ValueError, match="already exists"):
        run(args)


def test_the_run_refuses_without_a_persisted_selection(tmp_path):
    import argparse as _argparse

    from app.cli.stage41_iag_v2 import run

    args = _argparse.Namespace(
        i_have_reviewed_the_design=True, output_dir=str(tmp_path),
        features_dir=".", raw_dir=".", selection_sha256=None, command="run",
    )
    with pytest.raises(ValueError, match="Run diagnose first"):
        run(args)


def test_the_run_has_no_limit_flag():
    from app.cli.stage41_iag_v2 import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["run", "--features-dir", "/x", "--raw-dir", "/y", "--limit", "10"]
        )


# ---------------------------------------------------------------------------
# The economic reveal, and the boundary around it
# ---------------------------------------------------------------------------


def test_gross_displacement_is_direction_times_midpoint_move():
    assert gross_directional_displacement_bps(
        direction=LONG, midpoint_at_decision=100.0, midpoint_at_horizon=100.12
    ) == pytest.approx(12.0)
    assert gross_directional_displacement_bps(
        direction=SHORT, midpoint_at_decision=100.0, midpoint_at_horizon=99.88
    ) == pytest.approx(12.0)


def test_the_mechanism_can_be_negative():
    assert gross_directional_displacement_bps(
        direction=LONG, midpoint_at_decision=100.0, midpoint_at_horizon=99.5
    ) == pytest.approx(-50.0)


def test_the_reveal_refuses_degenerate_inputs():
    with pytest.raises(ValueError, match="not positive"):
        gross_directional_displacement_bps(
            direction=LONG, midpoint_at_decision=0.0, midpoint_at_horizon=100.0
        )
    with pytest.raises(ValueError, match="neither"):
        gross_directional_displacement_bps(
            direction=0, midpoint_at_decision=100.0, midpoint_at_horizon=100.0
        )


def test_the_qualification_path_never_calls_the_reveal():
    """The central governance claim, checked structurally."""
    from app.services import stage41_iag_v2_executor as executor

    for function in (
        executor.measure_event, executor.qualifies, executor.supporting_count,
        executor.resolve_direction, executor.local_lambda,
        executor.select_specification, executor.count_specification,
        executor.build_baseline, executor.reduce_interval, executor.capture_state,
    ):
        tree = ast.parse(inspect.getsource(function))
        called = {getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                  for n in ast.walk(tree) if isinstance(n, ast.Call)}
        assert "gross_directional_displacement_bps" not in called, function.__name__


def test_the_scanner_never_calls_the_reveal():
    from app.services import stage41_iag_v2_executor as executor

    tree = ast.parse(inspect.getsource(executor.SessionScanner))
    called = {getattr(n.func, "id", None) or getattr(n.func, "attr", None)
              for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert "gross_directional_displacement_bps" not in called


def test_diagnose_never_imports_the_reveal():
    """The strongest available guarantee: the function is not in scope."""
    from app.cli import stage41_iag_v2 as cli

    tree = ast.parse(inspect.getsource(cli.diagnose))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(a.name for a in node.names)
    assert "gross_directional_displacement_bps" not in imported
    assert "session_clustered_inference" not in imported
    assert "decide_verdict" not in imported
    assert "select_specification" in imported


def test_the_probe_never_imports_the_reveal():
    from app.cli import stage41_iag_v2 as cli

    tree = ast.parse(inspect.getsource(cli.probe))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(a.name for a in node.names)
    assert "gross_directional_displacement_bps" not in imported


def test_only_run_imports_the_reveal():
    from app.cli import stage41_iag_v2 as cli

    tree = ast.parse(inspect.getsource(cli.run))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(a.name for a in node.names)
    assert "gross_directional_displacement_bps" in imported


def test_no_broker_client_is_reachable():
    from app.cli import stage41_iag_v2 as cli
    from app.services import stage41_iag_v2_executor as executor
    from app.services import stage41_iag_v2_plan as plan_module

    banned = ("alpaca", "broker", "tradeapi", "ib_insync", "ccxt")
    for module in (plan_module, executor, cli):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                for token in banned:
                    assert token not in name.lower(), f"{module.__name__}: {name}"


# ---------------------------------------------------------------------------
# Side-agnostic refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("feature", FORBIDDEN_AS_DIRECTIONAL_EVIDENCE)
def test_a_side_agnostic_stage1_counter_is_refused(feature):
    with pytest.raises(ValueError, match="both book sides"):
        assert_not_side_agnostic(feature)


def test_the_executor_never_reads_a_side_agnostic_counter():
    from app.services import stage41_iag_v2_executor as executor

    source = inspect.getsource(executor)
    tree = ast.parse(source)
    literals = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for name in FORBIDDEN_AS_DIRECTIONAL_EVIDENCE:
        assert name not in literals, name


# ---------------------------------------------------------------------------
# Inference and verdict
# ---------------------------------------------------------------------------


def test_clustered_inference_collapses_by_session_first():
    values = [10.0, 12.0, 14.0, 11.0, 13.0, 15.0]
    sessions = ["d1", "d1", "d2", "d2", "d3", "d3"]
    result = session_clustered_inference(values, sessions)
    assert result["events"] == 6
    assert result["distinct_sessions"] == 3
    assert result["clustering"] == "trading_session"
    assert result["ci95_describes"] == "session_mean_gross_bps"


def test_the_verdict_requires_the_hurdle_and_the_t_together():
    passing = {"events": 150, "distinct_sessions": 18,
               "mean_gross_bps": 14.0, "session_clustered_t": 4.0}
    assert decide_verdict(passing)["verdict"] == VERDICT_DETECTED
    assert decide_verdict({**passing, "mean_gross_bps": 11.9})["verdict"] == (
        VERDICT_NO_MECHANISM
    )
    assert decide_verdict({**passing, "session_clustered_t": 2.9})["verdict"] == (
        VERDICT_NO_MECHANISM
    )


def test_the_verdict_boundary_is_inclusive():
    boundary = {"events": 100, "distinct_sessions": 15,
                "mean_gross_bps": PRIMARY_GROSS_HURDLE_BPS,
                "session_clustered_t": T_HURDLE}
    assert decide_verdict(boundary)["verdict"] == VERDICT_DETECTED


def test_an_undersized_sample_outranks_the_hurdle():
    huge = {"events": 30, "distinct_sessions": 5,
            "mean_gross_bps": 90.0, "session_clustered_t": 12.0}
    assert decide_verdict(huge)["verdict"] == VERDICT_INSUFFICIENT


def test_an_undefined_t_fails_closed():
    degenerate = {"events": 150, "distinct_sessions": 18,
                  "mean_gross_bps": 90.0, "session_clustered_t": None}
    assert decide_verdict(degenerate)["verdict"] == VERDICT_NO_MECHANISM


def test_passing_authorizes_only_execution_simulation():
    passing = {"events": 150, "distinct_sessions": 18,
               "mean_gross_bps": 14.0, "session_clustered_t": 4.0}
    assert decide_verdict(passing)["authorizes"] == "stage_4_3_execution_simulation_only"


def test_the_secondary_horizons_cannot_rescue_the_primary():
    plan = statistical_plan()
    assert plan["economic_test"]["secondary_may_rescue_primary"] is False
    assert plan["economic_test"]["secondary_diagnostic_horizons"] == list(
        SECONDARY_HORIZON_MINUTES
    )
    assert plan["economic_test"]["primary_horizon_minutes"] == PRIMARY_HORIZON_MINUTES
    assert list(inspect.signature(decide_verdict).parameters) == ["inference"]


# ---------------------------------------------------------------------------
# Outcome filter and governance block
# ---------------------------------------------------------------------------


def test_the_outcome_filter_removes_displacement_keys():
    from app.cli.stage41_iag_v2 import _strip_outcomes

    payload = {
        "eligible_events": 150,
        "mean_gross_bps": 14.0,
        "nested": {"session_clustered_t": 4.0, "records": 10},
        "records": [{"displacement_bps": 3.0, "symbol": "AAPL"}],
    }
    assert _strip_outcomes(payload) == {
        "eligible_events": 150,
        "nested": {"records": 10},
        "records": [{"symbol": "AAPL"}],
    }


def test_the_filter_keeps_the_hurdle_and_the_governance_flags():
    from app.cli.stage41_iag_v2 import _strip_outcomes

    payload = {
        "primary_gross_hurdle_bps": 12.0,
        "contains_post_decision_return": False,
        "contains_pnl": False,
    }
    assert _strip_outcomes(payload) == payload


def test_the_diagnostic_declares_itself_blind():
    from app.cli.stage41_iag_v2 import _governance

    block = _governance(revealed=False)
    assert block["contains_strategy_outcome"] is False
    assert block["contains_post_decision_return"] is False
    assert block["contains_pnl"] is False
    assert block["effective_trials_after"] == 531


def test_the_reveal_declares_the_ledger_move():
    from app.cli.stage41_iag_v2 import _governance

    block = _governance(revealed=True)
    assert block["contains_post_decision_return"] is True
    assert block["effective_trials_before"] == 531
    assert block["effective_trials_after"] == 532
    assert block["authorizes_paper_or_live"] is False


def test_the_parser_exposes_exactly_the_declared_commands():
    import argparse as _argparse

    from app.cli.stage41_iag_v2 import build_parser

    parser = build_parser()
    choices: set[str] = set()
    for action in parser._actions:
        if isinstance(action, _argparse._SubParsersAction):
            choices |= set(action.choices)
    assert choices == {"plan", "semantics", "probe", "diagnose", "run"}


# ---------------------------------------------------------------------------
# End to end on a synthetic session
# ---------------------------------------------------------------------------


def test_a_constructed_iag_event_measures_and_qualifies():
    """Persistent buying, ask liquidity consumed and not restored, price
    drifting up, spread widening: the state the mechanism describes."""
    stats = scan(_depletion_records(ask_start=4000, trough=200, ask_end=250),
                 starts=[T0]).event_intervals[T0]
    # A baseline in which this window looks extreme.
    tiles = [
        _stats_with(final_ask_depth=4000.0 + i, final_spread_bps=0.5,
                    execution_count=1, withdrawn_ask=1, added_ask=99)
        for i in range(600)
    ]
    baseline = build_baseline("AAPL", tiles)
    state = measure_event(
        symbol="AAPL", session_date="2025-06-02", story_id="s", t0_ns=T0,
        stats=stats, baseline=baseline,
    )
    assert state.gate_failure is None
    assert state.direction == LONG
    assert state.agreeing_quarters == PERSISTENCE_QUARTERS
    assert state.depth_percentile == pytest.approx(0.0)
    assert state.recovery_ratio is not None and state.recovery_ratio < 0.25
    ok, reason, support = qualifies(state, SPEC_PRIMARY)
    assert (ok, reason) == (True, None), reason
    assert support >= 2


def test_one_pass_serves_every_window_in_the_session():
    """The quiet period keeps windows apart, so a single pointer suffices."""
    second = T0 + QUIET_PERIOD_MINUTES * 60 * NANOS_PER_SECOND
    records = [opening_clear(T0 - NANOS_PER_SECOND)]
    records += two_sided_book(T0 - NANOS_PER_SECOND)
    for start in (T0, second):
        for quarter in range(PERSISTENCE_QUARTERS):
            records.append(
                ev("T", "B", ts=start + quarter * PERSISTENCE_QUARTER_NS + 1, size=50)
            )
    records.append(ev("A", "B", ts=second + OBSERVATION_NS + NANOS_PER_SECOND, size=1))
    scanner = scan(records, starts=[T0, second])
    assert set(scanner.event_intervals) == {T0, second}
    for stats in scanner.event_intervals.values():
        assert stats.buy_shares == 200
        assert resolve_direction(stats).direction == LONG
