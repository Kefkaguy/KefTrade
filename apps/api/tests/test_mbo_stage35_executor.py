"""Stage 3.5: the execution-timing mechanism must be honest before it is believed.

The failure modes here are different from Stage 3's. The dangerous ones are not
about crossing spreads; they are about (a) letting the model choose which side
of the parent flow gets measured, (b) letting a fit predict a date it trained
on, and (c) reporting the delayed side as though it were the whole flow. Those
are what most of what follows tests.
"""

from __future__ import annotations

import pytest
from app.services.mbo_stage3_executor import BookLevels
from app.services.mbo_stage35_executor import (
    BUY,
    NOT_COMPARABLE_BASELINE_LIQUIDITY,
    NOT_COMPARABLE_NO_TIMED_BOOK,
    NOT_COMPARABLE_TIMED_LIQUIDITY,
    NOT_COMPARABLE_UNCERTIFIABLE,
    SELL,
    TRIGGER_DEADLINE,
    TRIGGER_TARGET,
    CellTiming,
    assemble_report,
    assert_chronology_is_clean,
    assert_frozen_plan,
    chronology_map,
    evaluate_pair,
    price_dependent_fee_difference_usd,
    timed_send_instant,
    training_dates_for,
)
from app.services.mbo_stage35_plan import (
    DELAY_DEADLINE_NS,
    FROZEN_CELLS,
    LATENCY_NS,
    MAX_ARRIVAL_NS,
    MIN_COMPARABLE_PAIRS,
    MIN_SESSION_DATES,
    PLAN_DESIGN_HASH,
    T_HURDLE,
    statistical_plan,
)

SCALE = 1_000_000_000
MS = 1_000_000
DATES = [f"2025-06-{d:02d}" for d in range(2, 22)]
BLOCKS = {
    "discovery": DATES[:10],
    "validation": DATES[10:16],
    "confirmation": DATES[16:],
}


def book(ts: int, bid: float, ask: float, size: int = 1_000, levels: int = 3):
    cent = SCALE // 100
    return BookLevels(
        ts=ts,
        bids=tuple((int(bid * SCALE) - i * cent, size) for i in range(levels)),
        asks=tuple((int(ask * SCALE) + i * cent, size) for i in range(levels)),
    )


def static_book(bid: float, ask: float, **kw):
    return lambda ts: book(ts, bid, ask, **kw)


def moving_book(decision, before, after, switch_ts, **kw):
    def book_at(ts):
        prices = after if ts >= switch_ts else before
        return book(ts, prices[0], prices[1], **kw)

    return book_at


def make_pair(predicted_bps, book_at, *, target=None, decision=1_000_000_000, **kw):
    return evaluate_pair(
        cell="50ev|next_change",
        symbol="AAAA",
        session_date="2025-06-18",
        block="confirmation",
        predicted_bps=predicted_bps,
        decision_ts=decision,
        target_available_ts_recv=target,
        book_at=book_at,
        **kw,
    )


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------


def test_the_plan_and_cell_hashes_are_frozen():
    assert_frozen_plan()
    assert PLAN_DESIGN_HASH == (
        "ab0d42679cbedf6ac6b23706766ad16896e7d86413162b8f66e42cd3153c9fa7"
    )


def test_the_cells_are_exactly_the_four_stage2_survivors():
    assert list(FROZEN_CELLS) == sorted([
        "50ev|next_change",
        "50ev|next_2_changes",
        "200ev|next_change",
        "200ev|next_2_changes",
    ])


def test_the_plan_declares_itself_exploratory_and_non_confirmatory():
    plan = statistical_plan()
    assert plan["contains_execution_outcome"] is False
    assert plan["is_a_directional_strategy"] is False
    assert plan["governance"]["confirmatory"] is False
    assert plan["governance"]["evidence_class"] == "exploratory mechanism development"
    assert plan["governance"]["stage3_verdict"].startswith("closed and unaltered")
    assert plan["model_reuse"]["refitting_against_execution_outcomes"] is False


def test_the_screen_cannot_authorize_paper_or_live():
    screen = statistical_plan()["mechanism_screen"]
    assert "external" in screen["what_a_pass_authorizes"]
    assert "paper trading" in screen["what_a_pass_does_not_authorize"]
    assert "live trading" in screen["what_a_pass_does_not_authorize"]
    forbidden = statistical_plan()["forbidden"]
    assert "reinterpreting or reopening the Stage-3 verdict" in forbidden
    assert "authorizing paper or live trading from this study" in forbidden
    assert "rescue" in screen["if_no_cell_passes"] or "close" in screen["if_no_cell_passes"]


def test_the_ledger_carries_forward_and_adds_four():
    governance = statistical_plan()["governance"]
    assert governance["prior_effective_trials"] == 526  # 522 + Stage 3's four
    assert governance["adds_to_ledger_when_outcomes_are_viewed"] == 4


# ---------------------------------------------------------------------------
# Row-level chronology
# ---------------------------------------------------------------------------


def test_a_discovery_date_is_left_out_of_its_own_fit():
    block, training = training_dates_for(DATES[0], BLOCKS)
    assert block == "discovery"
    assert DATES[0] not in training
    assert len(training) == 9
    assert set(training) == set(BLOCKS["discovery"]) - {DATES[0]}


def test_a_validation_date_trains_on_all_ten_discovery_dates():
    block, training = training_dates_for(DATES[12], BLOCKS)
    assert block == "validation"
    assert training == BLOCKS["discovery"]
    assert len(training) == 10


def test_a_confirmation_date_trains_on_all_sixteen_earlier_dates():
    block, training = training_dates_for(DATES[18], BLOCKS)
    assert block == "confirmation"
    assert training == BLOCKS["discovery"] + BLOCKS["validation"]
    assert len(training) == 16


def test_no_date_anywhere_trains_on_itself():
    mapping = chronology_map(BLOCKS)
    assert len(mapping) == 20
    for session_date, entry in mapping.items():
        assert session_date not in entry["training_dates"]
        assert entry["trains_on_itself"] is False
    assert_chronology_is_clean(mapping)


def test_validation_and_confirmation_never_train_on_later_dates():
    mapping = chronology_map(BLOCKS)
    for session_date, entry in mapping.items():
        if entry["block"] == "discovery":
            continue
        assert all(d < session_date for d in entry["training_dates"]), session_date


def test_a_contaminated_chronology_is_refused():
    mapping = chronology_map(BLOCKS)
    mapping[DATES[0]]["trains_on_itself"] = True
    with pytest.raises(ValueError, match="trained on themselves"):
        assert_chronology_is_clean(mapping)


def test_a_confirmation_fit_reaching_forward_is_refused():
    mapping = chronology_map(BLOCKS)
    mapping[DATES[16]]["training_dates"] = [*BLOCKS["discovery"], DATES[19]]
    with pytest.raises(ValueError, match="would train on later dates"):
        assert_chronology_is_clean(mapping)


def test_an_unknown_date_is_refused():
    with pytest.raises(ValueError, match="is in none of the frozen blocks"):
        training_dates_for("2025-07-01", BLOCKS)


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


def test_the_target_releases_the_order_when_it_arrives_before_the_deadline():
    send, trigger = timed_send_instant(1_000_000_000, 1_000_000_000 + 300 * MS)
    assert send == 1_000_000_000 + 300 * MS
    assert trigger == TRIGGER_TARGET


def test_the_deadline_releases_the_order_when_the_target_is_late():
    send, trigger = timed_send_instant(1_000_000_000, 1_000_000_000 + 900 * MS)
    assert send == 1_000_000_000 + DELAY_DEADLINE_NS
    assert trigger == TRIGGER_DEADLINE


def test_an_unresolved_target_still_sends_at_the_deadline():
    """The wait must terminate whether or not the event ever comes; a policy
    that could hang is not one a desk could run."""
    send, trigger = timed_send_instant(1_000_000_000, None)
    assert send == 1_000_000_000 + DELAY_DEADLINE_NS
    assert trigger == TRIGGER_DEADLINE


def test_arrival_can_never_exceed_one_second_after_the_decision():
    decision = 1_000_000_000
    for target in (None, decision + 10 * MS, decision + 5 * SCALE):
        send, _ = timed_send_instant(decision, target)
        assert send + LATENCY_NS <= decision + MAX_ARRIVAL_NS
    assert MAX_ARRIVAL_NS == SCALE


def test_the_two_arrival_instants_are_what_the_plan_declares():
    pair, reason = make_pair(50.0, static_book(100.00, 100.02), target=1_000_000_000 + 300 * MS)
    assert reason is None and pair is not None
    assert pair.baseline_arrival_ts == 1_000_000_000 + LATENCY_NS
    assert pair.timed_send_ts == 1_000_000_000 + 300 * MS
    assert pair.timed_arrival_ts == pair.timed_send_ts + LATENCY_NS


# ---------------------------------------------------------------------------
# The policy: sign only, and the side is not the model's to choose
# ---------------------------------------------------------------------------


def test_predicted_up_delays_the_sell_and_executes_the_buy_now():
    pair, _ = make_pair(50.0, static_book(100.00, 100.02))
    assert pair.predicted_up is True
    assert pair.delayed_side == SELL
    assert pair.savings_bps(BUY) == 0.0


def test_predicted_down_delays_the_buy_and_executes_the_sell_now():
    pair, _ = make_pair(-50.0, static_book(100.00, 100.02))
    assert pair.predicted_up is False
    assert pair.delayed_side == BUY
    assert pair.savings_bps(SELL) == 0.0


def test_exactly_one_side_delays_at_every_prediction():
    """The structural fact that makes balanced savings half the delayed side."""
    for predicted in (50.0, -50.0, 0.001, -0.001):
        pair, _ = make_pair(predicted, static_book(100.00, 100.02))
        delayed = [s for s in (BUY, SELL) if s == pair.delayed_side]
        assert len(delayed) == 1


def test_the_magnitude_of_the_prediction_changes_nothing():
    """Sign only. A tiny prediction and an enormous one produce the same
    decision, because a magnitude threshold would be a searchable parameter."""
    small, _ = make_pair(0.0001, static_book(100.00, 100.02))
    large, _ = make_pair(9_999.0, static_book(100.00, 100.02))
    assert small.delayed_side == large.delayed_side
    assert small.timed_send_ts == large.timed_send_ts


def test_both_sides_are_always_evaluated():
    pair, _ = make_pair(50.0, static_book(100.00, 100.02))
    for leg in (pair.buy, pair.sell):
        assert leg["baseline_fill"] > 0
        assert leg["timed_fill"] > 0


# ---------------------------------------------------------------------------
# Savings
# ---------------------------------------------------------------------------


def test_a_falling_price_helps_the_delayed_buy():
    """Predicted down, so the BUY waits. The price falls; the buy fills cheaper."""
    decision = 1_000_000_000
    switch = decision + 400 * MS
    pair, reason = make_pair(
        -50.0,
        moving_book(decision, (100.00, 100.02), (99.90, 99.92), switch),
        target=decision + 500 * MS,
        decision=decision,
    )
    assert reason is None
    assert pair.delayed_side == BUY
    assert pair.savings_bps(BUY) > 0
    assert pair.savings_bps(SELL) == 0.0
    assert pair.delayed_savings_bps == pair.savings_bps(BUY)


def test_a_rising_price_hurts_the_delayed_buy():
    """The mechanism must be able to lose. A wrong call costs real money."""
    decision = 1_000_000_000
    pair, _ = make_pair(
        -50.0,
        moving_book(decision, (100.00, 100.02), (100.20, 100.22), decision + 400 * MS),
        target=decision + 500 * MS,
        decision=decision,
    )
    assert pair.savings_bps(BUY) < 0


def test_a_rising_price_helps_the_delayed_sell():
    decision = 1_000_000_000
    pair, _ = make_pair(
        50.0,
        moving_book(decision, (100.00, 100.02), (100.20, 100.22), decision + 400 * MS),
        target=decision + 500 * MS,
        decision=decision,
    )
    assert pair.delayed_side == SELL
    assert pair.savings_bps(SELL) > 0


def test_balanced_savings_are_exactly_half_the_delayed_side():
    """Reporting only the delayed side would double the apparent benefit of a
    mechanism that in practice sees both sides of the parent flow."""
    decision = 1_000_000_000
    pair, _ = make_pair(
        -50.0,
        moving_book(decision, (100.00, 100.02), (99.90, 99.92), decision + 400 * MS),
        target=decision + 500 * MS,
        decision=decision,
    )
    assert pair.balanced_savings_bps == pytest.approx(pair.delayed_savings_bps / 2.0)


def test_an_unchanged_book_yields_exactly_zero_savings():
    pair, _ = make_pair(50.0, static_book(100.00, 100.02), target=1_000_000_000 + 300 * MS)
    assert pair.delayed_savings_bps == pytest.approx(0.0)
    assert pair.balanced_savings_bps == pytest.approx(0.0)


def test_the_non_delayed_side_is_exactly_zero_not_approximately():
    decision = 1_000_000_000
    pair, _ = make_pair(
        50.0,
        moving_book(decision, (100.00, 100.02), (101.00, 101.02), decision + 400 * MS),
        target=decision + 500 * MS,
        decision=decision,
    )
    assert pair.savings_bps(BUY) == 0.0
    assert pair.midpoint_benefit_bps(BUY) == 0.0
    assert pair.book_walk_benefit_bps(BUY) == 0.0
    assert pair.dollar_savings_per_100(BUY) == 0.0


# ---------------------------------------------------------------------------
# The decomposition identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("predicted", [50.0, -50.0])
@pytest.mark.parametrize(
    "after", [(99.90, 99.92), (100.20, 100.22), (100.00, 100.02), (100.00, 100.50)]
)
def test_total_savings_equal_midpoint_plus_book_walk(predicted, after):
    """The identity that separates 'the price moved our way' from 'the book was
    cheaper to cross'. Asserted, not assumed."""
    decision = 1_000_000_000
    pair, reason = make_pair(
        predicted,
        moving_book(decision, (100.00, 100.02), after, decision + 400 * MS),
        target=decision + 500 * MS,
        decision=decision,
    )
    assert reason is None
    side = pair.delayed_side
    assert pair.savings_bps(side) == pytest.approx(
        pair.midpoint_benefit_bps(side) + pair.book_walk_benefit_bps(side), abs=1e-9
    )


def test_a_pure_spread_widening_shows_up_as_book_walk_not_midpoint():
    """Midpoint unchanged, spread doubled: the whole cost is the book walk."""
    decision = 1_000_000_000
    pair, _ = make_pair(
        -50.0,
        moving_book(decision, (100.00, 100.02), (99.98, 100.04), decision + 400 * MS),
        target=decision + 500 * MS,
        decision=decision,
    )
    assert pair.midpoint_benefit_bps(BUY) == pytest.approx(0.0, abs=1e-9)
    assert pair.book_walk_benefit_bps(BUY) < 0


def test_a_pure_midpoint_move_shows_up_as_midpoint_not_book_walk():
    """Spread identical, level shifted: the whole benefit is timing."""
    decision = 1_000_000_000
    pair, _ = make_pair(
        -50.0,
        moving_book(decision, (100.00, 100.02), (99.90, 99.92), decision + 400 * MS),
        target=decision + 500 * MS,
        decision=decision,
    )
    assert pair.book_walk_benefit_bps(BUY) == pytest.approx(0.0, abs=1e-9)
    assert pair.midpoint_benefit_bps(BUY) > 0


# ---------------------------------------------------------------------------
# Comparability
# ---------------------------------------------------------------------------


def test_a_pair_needs_both_legs_to_be_comparable():
    """Dropping half-evaluated pairs would select on execution difficulty, which
    is correlated with the book states this mechanism claims to exploit."""
    decision = 1_000_000_000
    thin = BookLevels(
        ts=0,
        bids=((int(99.99 * SCALE), 10),),
        asks=((int(100.00 * SCALE), 10),),
    )

    def book_at(ts):
        return thin if ts >= decision + 400 * MS else book(ts, 100.00, 100.02)

    pair, reason = make_pair(-50.0, book_at, target=decision + 500 * MS, decision=decision)
    assert pair is None
    assert reason == NOT_COMPARABLE_TIMED_LIQUIDITY


def test_a_thin_baseline_leg_is_also_not_comparable():
    thin = BookLevels(
        ts=0, bids=((int(99.99 * SCALE), 5),), asks=((int(100.00 * SCALE), 5),)
    )
    pair, reason = make_pair(-50.0, lambda ts: thin, target=1_000_000_000 + 300 * MS)
    assert pair is None
    assert reason == NOT_COMPARABLE_BASELINE_LIQUIDITY


def test_a_missing_timed_book_is_not_comparable():
    decision = 1_000_000_000

    def book_at(ts):
        if ts >= decision + 400 * MS:
            return BookLevels(ts=ts, bids=(), asks=())
        return book(ts, 100.00, 100.02)

    pair, reason = make_pair(-50.0, book_at, target=decision + 500 * MS, decision=decision)
    assert pair is None and reason == NOT_COMPARABLE_NO_TIMED_BOOK


def test_a_flagged_timing_window_is_not_comparable():
    pair, reason = make_pair(
        50.0, static_book(100.00, 100.02), timing_certified=lambda lo, hi: False
    )
    assert pair is None and reason == NOT_COMPARABLE_UNCERTIFIABLE


def test_asymmetric_failures_are_counted_not_dropped():
    cell = CellTiming(cell="50ev|next_change")
    cell.record_not_comparable(NOT_COMPARABLE_TIMED_LIQUIDITY)
    cell.record_not_comparable(NOT_COMPARABLE_TIMED_LIQUIDITY)
    cell.record_not_comparable(NOT_COMPARABLE_BASELINE_LIQUIDITY)
    cell.record_not_comparable(NOT_COMPARABLE_UNCERTIFIABLE)
    assert cell.asymmetric_failures == 3
    summary = cell.summary()
    assert summary["comparable_pairs"] == 0
    assert summary["not_comparable"][NOT_COMPARABLE_TIMED_LIQUIDITY] == 2
    assert summary["reached_screen"] is False


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------


def test_the_price_dependent_fee_difference_is_zero_under_june_2025():
    """Section 31 was $0.00 per million all window, and every per-share charge
    is identical under both policies because the order executes either way."""
    from app.services.mbo_stage3_plan import PRIMARY_FEE_SCHEDULE

    decision = 1_000_000_000
    pair, _ = make_pair(
        50.0,
        moving_book(decision, (100.00, 100.02), (101.00, 101.02), decision + 400 * MS),
        target=decision + 500 * MS,
        decision=decision,
    )
    assert price_dependent_fee_difference_usd(pair, PRIMARY_FEE_SCHEDULE) == 0.0


def test_a_non_zero_section_31_rate_would_produce_a_difference():
    """The zero above is a measurement of the June-2025 rate, not a hard-coded
    convenience: restore the rate and a difference appears."""
    from app.services.mbo_stage3_plan import PRIMARY_FEE_SCHEDULE

    decision = 1_000_000_000
    pair, _ = make_pair(
        50.0,
        moving_book(decision, (100.00, 100.02), (101.00, 101.02), decision + 400 * MS),
        target=decision + 500 * MS,
        decision=decision,
    )
    schedule = {**PRIMARY_FEE_SCHEDULE, "sec_section_31_usd_per_million_sold": 27.80}
    assert price_dependent_fee_difference_usd(pair, schedule) != 0.0


def test_no_round_trip_cost_is_charged():
    fees = statistical_plan()["fees"]
    assert fees["round_trip_costs_apply"] is False
    assert "no exit leg" in fees["why"]


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------


def _summary(cell, *, savings, t, p, pairs=2_000, dates=15, reached=True):
    return {
        "cell": cell,
        "reached_screen": reached,
        "comparable_pairs": pairs,
        "session_dates": dates,
        "balanced_parent_flow_savings_bps": savings,
        "clustered_t": t,
        "p_value": p,
    }


def test_a_passing_cell_authorizes_only_an_external_experiment():
    report = assemble_report(
        [_summary(FROZEN_CELLS[0], savings=0.4, t=6.0, p=1e-6)]
        + [_summary(c, savings=-0.1, t=-1.0, p=0.4) for c in FROZEN_CELLS[1:]],
        chronology={"ok": True},
    )
    assert report["cells_passing_mechanism_screen"] == [FROZEN_CELLS[0]]
    assert report["verdict"] == "execution_timing_mechanism_supported_exploratory"
    assert "external" in report["authorizes"]
    assert report["authorizes_paper_or_live"] is False
    assert report["confirmatory"] is False
    assert report["reinterprets_stage3_verdict"] is False


def test_a_negative_mean_cannot_pass_however_significant():
    report = assemble_report(
        [_summary(c, savings=-0.5, t=-9.0, p=1e-12) for c in FROZEN_CELLS],
        chronology={},
    )
    assert report["cells_passing_mechanism_screen"] == []
    assert report["verdict"] == "no_execution_timing_mechanism"
    assert "close this mechanism" in report["authorizes"]


def test_a_cell_below_the_t_hurdle_cannot_pass():
    report = assemble_report(
        [_summary(c, savings=0.4, t=T_HURDLE - 0.01, p=1e-9) for c in FROZEN_CELLS],
        chronology={},
    )
    assert report["cells_passing_mechanism_screen"] == []


def test_too_few_pairs_or_dates_cannot_pass():
    thin_pairs = _summary(
        FROZEN_CELLS[0], savings=0.9, t=9.0, p=1e-9,
        pairs=MIN_COMPARABLE_PAIRS - 1, reached=False,
    )
    thin_dates = _summary(
        FROZEN_CELLS[1], savings=0.9, t=9.0, p=1e-9,
        dates=MIN_SESSION_DATES - 1, reached=False,
    )
    report = assemble_report([thin_pairs, thin_dates], chronology={})
    assert report["cells_passing_mechanism_screen"] == []


def test_the_family_is_four_cells_and_bh_uses_that_denominator():
    report = assemble_report(
        [_summary(c, savings=0.3, t=5.0, p=0.02) for c in FROZEN_CELLS],
        chronology={},
    )
    assert report["family"]["size"] == 4
    assert len(report["family"]["benjamini_hochberg"]) == 4


def test_the_report_never_claims_to_authorize_deployment():
    for savings, t in ((0.9, 9.0), (-0.9, -9.0)):
        report = assemble_report(
            [_summary(c, savings=savings, t=t, p=1e-9) for c in FROZEN_CELLS],
            chronology={},
        )
        assert report["authorizes_paper_or_live"] is False
        assert report["reinterprets_stage3_verdict"] is False
        assert report["evidence_class"] == "exploratory mechanism development"


# ---------------------------------------------------------------------------
# The diagnostic mode
# ---------------------------------------------------------------------------


def test_the_diagnostic_strips_anything_that_could_be_a_saving():
    from app.cli.mbo_stage35 import _strip_outcomes

    payload = {
        "counts": {"pairs": 10},
        "balanced_parent_flow_savings_bps": 0.9,
        "midpoint_timing_benefit_bps": 0.4,
        "nested": {"book_walk_benefit_bps": 0.1, "symbol_days": 160},
        "cells": [{"cell": "50ev|next_change", "clustered_t": 9.0, "comparable_pairs": 5}],
        "verdict": "supported",
    }
    clean = _strip_outcomes(payload)
    assert clean == {
        "counts": {"pairs": 10},
        "nested": {"symbol_days": 160},
        "cells": [{"cell": "50ev|next_change", "comparable_pairs": 5}],
    }


def test_the_diagnostic_command_cannot_assemble_a_report():
    import inspect

    from app.cli.mbo_stage35 import diagnose

    body = inspect.getsource(diagnose).split('"""')[2]
    for forbidden in ("assemble_report", "evaluate_pair", "savings"):
        assert forbidden not in body, forbidden
    assert "_strip_outcomes" in body


def test_the_run_command_is_gated():
    import argparse

    from app.cli.mbo_stage35 import run

    args = argparse.Namespace(
        i_have_reviewed_the_design=False, stage2_results="x", grams_dir="g",
        features_dir="f", labels_dir="l", raw_dir="r", output_dir="o",
    )
    with pytest.raises(ValueError, match="not authorized yet"):
        run(args)


def test_the_cli_offers_no_symbol_filter_or_threshold_option():
    """No symbol filtering, threshold search, delay search or latency search --
    and no flag through which one could be attempted."""
    from app.cli.mbo_stage35 import build_parser

    actions = {
        option
        for action in build_parser()._actions
        for option in action.option_strings
    }
    for banned in ("--symbol", "--symbols", "--threshold", "--delay", "--latency", "--limit"):
        assert banned not in actions
