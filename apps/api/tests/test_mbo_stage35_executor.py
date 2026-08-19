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
    NOT_COMPARABLE_OUTSIDE_COVERAGE,
    NOT_COMPARABLE_TIMED_LIQUIDITY,
    NOT_COMPARABLE_UNCERTIFIABLE,
    SELL,
    TRIGGER_DEADLINE,
    TRIGGER_NONE,
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
        price_scale=SCALE,
        **kw,
    )


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------


def test_the_plan_and_cell_hashes_are_frozen():
    assert_frozen_plan()
    assert PLAN_DESIGN_HASH == (
        "097b5d65dfd49d9c648865df3b31c716b51b0c685c6e8b347c772a3b6992ba94"
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
    assert pair.predicted_bps > 0
    assert pair.delayed_side == SELL
    assert pair.savings_bps(BUY) == 0.0


def test_predicted_down_delays_the_buy_and_executes_the_sell_now():
    pair, _ = make_pair(-50.0, static_book(100.00, 100.02))
    assert pair.predicted_bps < 0
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


# ---------------------------------------------------------------------------
# 1. The send instant is clamped to the decision
# ---------------------------------------------------------------------------


def test_a_target_before_the_decision_never_sends_early():
    """The correction that matters most: a target timestamp preceding the
    prediction would otherwise send a 'delayed' order before the prediction
    existed -- a policy nobody could run, scoring as though they had."""
    decision = 1_000_000_000
    send, trigger = timed_send_instant(decision, decision - 500 * MS)
    assert send == decision
    assert send >= decision
    assert trigger == TRIGGER_TARGET


@pytest.mark.parametrize(
    "target_offset_ms", [-5_000, -750, -1, 0, 1, 300, 749, 750, 751, 5_000]
)
def test_the_send_invariant_holds_for_every_target(target_offset_ms):
    decision = 1_000_000_000
    send, _ = timed_send_instant(decision, decision + target_offset_ms * MS)
    assert decision <= send <= decision + DELAY_DEADLINE_NS
    arrival = send + LATENCY_NS
    assert decision + LATENCY_NS <= arrival <= decision + MAX_ARRIVAL_NS


def test_an_early_target_still_produces_a_valid_pair():
    decision = 1_000_000_000
    pair, reason = make_pair(
        -50.0, static_book(100.00, 100.02), target=decision - 5 * SCALE, decision=decision
    )
    assert reason is None and pair is not None
    assert pair.timed_send_ts == decision
    assert pair.timed_arrival_ts == decision + LATENCY_NS
    assert pair.timed_arrival_ts >= pair.decision_ts


# ---------------------------------------------------------------------------
# 2. Only the delayed side needs a future fill
# ---------------------------------------------------------------------------


def _thin_for(side_action, decision, switch_ts):
    """A book that becomes unfillable on one side only, after ``switch_ts``."""
    def book_at(ts):
        if ts < switch_ts:
            return book(ts, 100.00, 100.02)
        if side_action == "buy":
            return BookLevels(
                ts=ts,
                bids=((int(99.99 * SCALE), 5_000),),
                asks=((int(100.02 * SCALE), 1),),
            )
        return BookLevels(
            ts=ts,
            bids=((int(100.00 * SCALE), 1),),
            asks=((int(100.02 * SCALE), 5_000),),
        )

    return book_at


def test_the_non_delayed_sides_future_illiquidity_does_not_break_the_pair():
    """Decisive. Predicted down delays the BUY. The SELL's future liquidity
    vanishes -- but the SELL executes at the baseline instant under both
    policies, so that market state is one the policy never touches."""
    decision = 1_000_000_000
    switch = decision + 400 * MS

    def book_at(ts):
        if ts < switch:
            return book(ts, 100.00, 100.02)
        # Asks deep (the delayed BUY can fill), bids gone (a SELL could not).
        return BookLevels(
            ts=ts,
            bids=((int(99.90 * SCALE), 1),),
            asks=((int(99.92 * SCALE), 5_000),),
        )

    pair, reason = make_pair(
        -50.0, book_at, target=decision + 500 * MS, decision=decision
    )
    assert reason is None, reason
    assert pair is not None
    assert pair.delayed_side == BUY


def test_the_delayed_sides_future_illiquidity_does_break_the_pair():
    """The converse. If the side that actually delays cannot fill, the
    observation is genuinely unavailable."""
    decision = 1_000_000_000
    pair, reason = make_pair(
        -50.0,
        _thin_for("buy", decision, decision + 400 * MS),
        target=decision + 500 * MS,
        decision=decision,
    )
    assert pair is None
    assert reason == NOT_COMPARABLE_TIMED_LIQUIDITY


def test_the_non_delayed_side_reuses_its_baseline_fill_verbatim():
    decision = 1_000_000_000
    pair, _ = make_pair(
        50.0,
        moving_book(decision, (100.00, 100.02), (101.00, 101.02), decision + 400 * MS),
        target=decision + 500 * MS,
        decision=decision,
    )
    assert pair.delayed_side == SELL
    assert pair.buy["timed_fill"] == pair.buy["baseline_fill"]
    assert pair.buy["timed_midpoint"] == pair.buy["baseline_midpoint"]
    # Which makes its savings exactly zero as arithmetic, not as a special case.
    assert pair.savings_bps(BUY) == 0.0


def test_a_baseline_failure_on_either_side_still_refuses():
    """Both parent orders exist, so both baseline fills are genuinely required."""
    thin = BookLevels(
        ts=0, bids=((int(99.99 * SCALE), 5),), asks=((int(100.00 * SCALE), 5_000),)
    )
    pair, reason = make_pair(-50.0, lambda ts: thin, target=1_000_000_000 + 300 * MS)
    assert pair is None and reason == NOT_COMPARABLE_BASELINE_LIQUIDITY


# ---------------------------------------------------------------------------
# 3. Fixed-point conversion
# ---------------------------------------------------------------------------


def test_dollar_savings_use_the_real_fixed_point_scale():
    """A ten-cent improvement on 100 shares is $10, not 1e10."""
    from app.services.mbo_feature_engine import FIXED_PRICE_SCALE

    decision = 1_000_000_000
    pair, _ = evaluate_pair(
        cell="50ev|next_change", symbol="AAAA", session_date="2025-06-18",
        block="confirmation", predicted_bps=-50.0, decision_ts=decision,
        target_available_ts_recv=decision + 500 * MS,
        book_at=moving_book(
            decision, (100.00, 100.10), (99.90, 100.00), decision + 400 * MS
        ),
        price_scale=float(FIXED_PRICE_SCALE),
    )
    assert pair.delayed_side == BUY
    # Baseline buys the 100.10 offer, timed buys the 100.00 offer: 10c x 100.
    assert pair.delayed_dollar_savings == pytest.approx(10.0)
    assert abs(pair.delayed_dollar_savings) < 1_000  # not fixed-point units


def test_dollar_savings_are_negative_when_the_delay_hurts():
    from app.services.mbo_feature_engine import FIXED_PRICE_SCALE

    decision = 1_000_000_000
    pair, _ = evaluate_pair(
        cell="50ev|next_change", symbol="AAAA", session_date="2025-06-18",
        block="confirmation", predicted_bps=-50.0, decision_ts=decision,
        target_available_ts_recv=decision + 500 * MS,
        book_at=moving_book(
            decision, (100.00, 100.02), (100.20, 100.22), decision + 400 * MS
        ),
        price_scale=float(FIXED_PRICE_SCALE),
    )
    assert pair.delayed_dollar_savings == pytest.approx(-20.0)


def test_the_fee_difference_is_real_usd_at_a_non_zero_rate():
    """The June-2025 rate is zero, so this restores a real historical rate to
    prove the notional conversion is dimensionally correct."""
    from app.services.mbo_feature_engine import FIXED_PRICE_SCALE
    from app.services.mbo_stage3_plan import PRIMARY_FEE_SCHEDULE

    decision = 1_000_000_000
    pair, _ = evaluate_pair(
        cell="50ev|next_change", symbol="AAAA", session_date="2025-06-18",
        block="confirmation", predicted_bps=50.0, decision_ts=decision,
        target_available_ts_recv=decision + 500 * MS,
        book_at=moving_book(
            decision, (100.00, 100.02), (110.00, 110.02), decision + 400 * MS
        ),
        price_scale=float(FIXED_PRICE_SCALE),
    )
    schedule = {**PRIMARY_FEE_SCHEDULE, "sec_section_31_usd_per_million_sold": 27.80}
    difference = price_dependent_fee_difference_usd(pair, schedule)
    # Selling 100 shares ~$1,000 lower notional at $27.80/M is fractions of a cent.
    assert difference != 0.0
    assert abs(difference) < 1.0


def test_the_fee_difference_is_zero_under_the_june_2025_schedule():
    from app.services.mbo_feature_engine import FIXED_PRICE_SCALE
    from app.services.mbo_stage3_plan import PRIMARY_FEE_SCHEDULE

    decision = 1_000_000_000
    pair, _ = evaluate_pair(
        cell="50ev|next_change", symbol="AAAA", session_date="2025-06-18",
        block="confirmation", predicted_bps=50.0, decision_ts=decision,
        target_available_ts_recv=decision + 500 * MS,
        book_at=moving_book(
            decision, (100.00, 100.02), (110.00, 110.02), decision + 400 * MS
        ),
        price_scale=float(FIXED_PRICE_SCALE),
    )
    assert price_dependent_fee_difference_usd(pair, PRIMARY_FEE_SCHEDULE) == 0.0


# ---------------------------------------------------------------------------
# 4. Delay reporting
# ---------------------------------------------------------------------------


def _filled_cell(n=1_200, dates=12):
    from app.services.mbo_feature_engine import FIXED_PRICE_SCALE
    from app.services.mbo_stage3_plan import PRIMARY_FEE_SCHEDULE

    cell = CellTiming(cell="50ev|next_change", price_scale=float(FIXED_PRICE_SCALE))
    decision = 1_000_000_000
    for i in range(n):
        pair, reason = evaluate_pair(
            cell="50ev|next_change", symbol=f"S{i % 4}",
            session_date=f"2025-06-{2 + i % dates:02d}", block="confirmation",
            predicted_bps=(-50.0 if i % 2 else 50.0), decision_ts=decision,
            target_available_ts_recv=decision + 500 * MS,
            book_at=moving_book(
                decision, (100.00, 100.02), (99.99, 100.01), decision + 400 * MS
            ),
            price_scale=float(FIXED_PRICE_SCALE),
        )
        assert reason is None
        cell.record_pair(pair, PRIMARY_FEE_SCHEDULE)
    return cell


def test_delay_reporting_distinguishes_pairs_from_parent_orders():
    """Every pair contains a delay, but each pair contains TWO parent orders and
    only one delays. Calling that 1.0 overstates the reach of the mechanism."""
    summary = _filled_cell().summary()
    assert summary["pairs_with_a_delay_fraction"] == 1.0
    assert summary["parent_orders_delayed_fraction"] == 0.5
    assert "delayed_fraction" not in summary


def test_zero_predictions_lower_both_delay_fractions():
    from app.services.mbo_feature_engine import FIXED_PRICE_SCALE
    from app.services.mbo_stage3_plan import PRIMARY_FEE_SCHEDULE

    cell = _filled_cell(n=100)
    decision = 1_000_000_000
    for i in range(100):
        pair, reason = evaluate_pair(
            cell="50ev|next_change", symbol="Z", session_date="2025-06-02",
            block="confirmation", predicted_bps=0.0, decision_ts=decision,
            target_available_ts_recv=decision + 500 * MS,
            book_at=static_book(100.00, 100.02),
            price_scale=float(FIXED_PRICE_SCALE),
        )
        assert reason is None
        cell.record_pair(pair, PRIMARY_FEE_SCHEDULE)
    summary = cell.summary()
    assert summary["pairs_with_a_delay_fraction"] == pytest.approx(0.5)
    assert summary["parent_orders_delayed_fraction"] == pytest.approx(0.25)
    assert summary["zero_prediction_pairs"] == 100


# ---------------------------------------------------------------------------
# 7. The exact-zero prediction
# ---------------------------------------------------------------------------


def test_an_exact_zero_prediction_delays_neither_side():
    """An exact tie expresses no direction, so the policy expresses no timing
    preference. Classifying zero as predicted-down would invent a direction --
    and always in favour of delaying the buy."""
    pair, reason = make_pair(0.0, static_book(100.00, 100.02))
    assert reason is None
    assert pair.delayed_side is None
    assert pair.trigger == TRIGGER_NONE
    assert pair.savings_bps(BUY) == 0.0
    assert pair.savings_bps(SELL) == 0.0
    assert pair.balanced_savings_bps == 0.0
    assert pair.delayed_savings_bps == 0.0


def test_a_zero_prediction_queries_no_future_instant():
    """It never delays, so it never needs a future book -- and a moving market
    cannot change its (zero) savings."""
    decision = 1_000_000_000
    seen: list[int] = []

    def book_at(ts):
        seen.append(ts)
        return book(ts, 100.00, 100.02)

    pair, _ = make_pair(0.0, book_at, target=decision + 500 * MS, decision=decision)
    assert max(seen) == decision + LATENCY_NS
    assert pair.timed_arrival_ts == pair.baseline_arrival_ts


def test_the_zero_rule_is_frozen_and_is_not_a_magnitude_threshold():
    rule = statistical_plan()["zero_prediction_rule"]
    assert rule["condition"] == "predicted_bps == 0.0 exactly"
    assert rule["classification"] == "no_direction_zero_prediction"
    assert rule["is_a_magnitude_threshold"] is False
    assert rule["counted"] is True


def test_the_smallest_non_zero_prediction_still_takes_a_side():
    """The zero rule is an exact tie rule, not a threshold: 1e-300 still acts."""
    up, _ = make_pair(1e-300, static_book(100.00, 100.02))
    down, _ = make_pair(-1e-300, static_book(100.00, 100.02))
    assert up.delayed_side == SELL
    assert down.delayed_side == BUY


# ---------------------------------------------------------------------------
# 6. Certified coverage
# ---------------------------------------------------------------------------


def _mbo_event(ts_recv, action, side, price, size, order_id, seq):
    from app.services.mbo_book_validator import MboEvent

    return MboEvent(
        ts_event=ts_recv - 500, action=action, side=side, price=price, size=size,
        order_id=order_id, flags=128, sequence=seq, ts_recv=ts_recv,
    )


def test_coverage_tracks_the_receive_span_of_the_certified_file():
    from app.services.mbo_stage35_executor import CoverageTracker

    tracker = CoverageTracker()
    events = [
        _mbo_event(1_000, "A", "B", 100 * SCALE, 500, 1, 1),
        _mbo_event(9_000, "A", "A", 101 * SCALE, 500, 2, 2),
    ]
    assert list(tracker.wrap(events)) == events
    assert tracker.first_ts_recv == 1_000
    assert tracker.last_ts_recv == 9_000
    assert tracker.records == 2
    assert tracker.covers(2_000, 8_000) is True
    assert tracker.covers(500, 8_000) is False     # before the first record
    assert tracker.covers(2_000, 9_001) is False   # past the last record


def test_an_arrival_past_the_end_of_the_stream_is_refused():
    """BookReplay would serve it from the final snapshot as though tradable.
    That fill would look completely ordinary and be entirely fictitious."""
    decision = 1_000_000_000
    pair, reason = make_pair(
        -50.0,
        static_book(100.00, 100.02),
        target=decision + 500 * MS,
        decision=decision,
        within_coverage=lambda lo, hi: hi <= decision + 600 * MS,
    )
    assert pair is None
    assert reason == NOT_COMPARABLE_OUTSIDE_COVERAGE


def test_an_arrival_inside_coverage_is_accepted():
    decision = 1_000_000_000
    pair, reason = make_pair(
        -50.0,
        static_book(100.00, 100.02),
        target=decision + 500 * MS,
        decision=decision,
        within_coverage=lambda lo, hi: True,
    )
    assert reason is None and pair is not None


def test_coverage_is_checked_before_any_book_is_queried():
    """A refusal must not depend on the stale book answering plausibly."""
    queried: list[int] = []

    def book_at(ts):
        queried.append(ts)
        return book(ts, 100.00, 100.02)

    pair, reason = make_pair(
        -50.0, book_at, target=1_000_000_000 + 500 * MS,
        within_coverage=lambda lo, hi: False,
    )
    assert pair is None and reason == NOT_COMPARABLE_OUTSIDE_COVERAGE
    assert queried == []


# ---------------------------------------------------------------------------
# 5. The wired run and its Stage-2 reproduction gate
# ---------------------------------------------------------------------------


def _stage2_world(seed: int = 11):
    """Per-date Grams plus the per-date delta_R2 Stage 2 would have recorded."""
    import numpy as np
    from app.services.mbo_stage2_executor import DESIGN_WIDTH, Gram, delta_r2, sum_grams

    def gram(i, rows=400):
        rng = np.random.default_rng(seed + i)
        x = rng.standard_normal((rows, DESIGN_WIDTH))
        x[:, 0] = 1.0
        y = 0.3 * x[:, 1] + 0.4 * x[:, 11] + rng.standard_normal(rows)
        g = Gram.zeros(DESIGN_WIDTH)
        g.add_rows(x, y)
        return g

    grams = {d: gram(i) for i, d in enumerate(DATES)}
    alpha = 1.0
    recorded = {}
    for block, dates in BLOCKS.items():
        values = []
        for date in dates:
            _, training = training_dates_for(date, BLOCKS)
            train = sum_grams((grams[d] for d in training), DESIGN_WIDTH)
            values.append(float(delta_r2(train, grams[date], alpha)))
        recorded[block] = values
    return grams, alpha, recorded


def test_per_date_betas_use_stage2s_own_fit_and_never_train_on_the_date():
    from app.services.mbo_stage2_executor import DESIGN_WIDTH
    from app.services.mbo_stage35_executor import per_date_betas

    grams, alpha, _ = _stage2_world()
    betas = per_date_betas(grams, BLOCKS, alpha)
    assert len(betas) == 20
    for date, entry in betas.items():
        assert date not in entry["training_dates"]
        assert entry["beta"].shape == (DESIGN_WIDTH,)
    assert len(betas[DATES[0]]["training_dates"]) == 9
    assert len(betas[DATES[12]]["training_dates"]) == 10
    assert len(betas[DATES[18]]["training_dates"]) == 16


def test_the_chronology_reproduces_stage2s_recorded_per_date_delta_r2():
    """The strong gate: if this passes, the models feeding the experiment are
    demonstrably the same out-of-sample models Stage 2 used."""
    from app.services.mbo_stage35_executor import (
        per_date_betas,
        recorded_stage2_per_date,
        reproduce_stage2_delta_r2,
    )

    grams, alpha, recorded = _stage2_world()
    record = {
        block: {"per_date_delta_r2": values} for block, values in recorded.items()
    }
    betas = per_date_betas(grams, BLOCKS, alpha)
    result = reproduce_stage2_delta_r2(
        "50ev|next_change", grams, betas, alpha,
        recorded_stage2_per_date(record, BLOCKS),
    )
    assert result["reproduction_verified"] is True
    assert result["dates_checked"] == 20


def test_a_chronology_that_does_not_reproduce_stage2_is_refused():
    from app.services.mbo_stage35_executor import (
        per_date_betas,
        recorded_stage2_per_date,
        reproduce_stage2_delta_r2,
    )

    grams, alpha, recorded = _stage2_world()
    tampered = {k: list(v) for k, v in recorded.items()}
    tampered["confirmation"][1] += 1e-6
    record = {b: {"per_date_delta_r2": v} for b, v in tampered.items()}
    betas = per_date_betas(grams, BLOCKS, alpha)
    with pytest.raises(ValueError, match="do not reproduce Stage 2's recorded"):
        reproduce_stage2_delta_r2(
            "50ev|next_change", grams, betas, alpha,
            recorded_stage2_per_date(record, BLOCKS),
        )


def test_a_wrong_training_window_fails_the_reproduction_gate():
    """The gate is not decorative: change the chronology and it fires."""
    from app.services.mbo_stage2_executor import DESIGN_WIDTH, fit, sum_grams
    from app.services.mbo_stage35_executor import (
        recorded_stage2_per_date,
        reproduce_stage2_delta_r2,
    )

    grams, alpha, recorded = _stage2_world()
    record = {b: {"per_date_delta_r2": v} for b, v in recorded.items()}
    # Every date trained on all sixteen -- wrong for discovery and validation.
    wrong = {}
    training = BLOCKS["discovery"] + BLOCKS["validation"]
    train = sum_grams((grams[d] for d in training), DESIGN_WIDTH)
    for date in DATES:
        block, _ = training_dates_for(date, BLOCKS)
        wrong[date] = {
            "beta": fit(train, alpha), "block": block,
            "training_dates": training, "train_gram": train,
        }
    with pytest.raises(ValueError, match="do not reproduce Stage 2's recorded"):
        reproduce_stage2_delta_r2(
            "50ev|next_change", grams, wrong, alpha,
            recorded_stage2_per_date(record, BLOCKS),
        )


def test_query_instants_omit_the_non_delayed_future_arrival():
    import numpy as np
    from app.services.mbo_stage35_executor import query_instants

    decision = np.array([1_000_000_000], dtype=np.int64)
    availability = [1_000_000_000 + 500 * MS]
    instants = query_instants(decision, availability, np.array([True]), np.array([50.0]))
    assert set(instants) == {
        1_000_000_000,
        1_000_000_000 + LATENCY_NS,
        1_000_000_000 + 500 * MS + LATENCY_NS,
    }


def test_a_zero_prediction_adds_no_future_query_instant():
    import numpy as np
    from app.services.mbo_stage35_executor import query_instants

    decision = np.array([1_000_000_000], dtype=np.int64)
    instants = query_instants(
        decision, [1_000_000_000 + 500 * MS], np.array([True]), np.array([0.0])
    )
    assert set(instants) == {1_000_000_000, 1_000_000_000 + LATENCY_NS}


def test_the_run_is_wired_but_still_gated():
    """Wired is not the same as authorized."""
    import argparse
    import inspect

    from app.cli.mbo_stage35 import run

    source = inspect.getsource(run)
    assert "evaluate_pair" in source
    assert "assemble_report" in source
    assert "NotImplementedError" not in source

    args = argparse.Namespace(
        i_have_reviewed_the_design=False, stage2_results="x", grams_dir="g",
        features_dir="f", labels_dir="l", raw_dir="r", output_dir="o",
    )
    with pytest.raises(ValueError, match="not authorized yet"):
        run(args)


def test_the_run_uses_stage2s_fit_rather_than_reimplementing_ridge():
    import inspect

    from app.services.mbo_stage35_executor import per_date_betas

    source = inspect.getsource(per_date_betas)
    assert "from app.services.mbo_stage2_executor import" in source
    assert "fit" in source
    # No local ridge algebra.
    for token in ("np.linalg.solve", "np.eye", "penalty"):
        assert token not in source, token


def test_the_run_verifies_reproduction_before_replaying_anything():
    import inspect

    from app.cli.mbo_stage35 import run

    source = inspect.getsource(run)
    # Compare call sites, not the import block: the fits (and therefore the
    # reproduction gate) must be built before any book is replayed.
    assert source.index("_build_fits(context)") < source.index("BookReplay(MboBook)")


def test_the_superseded_v1_plan_is_recorded_with_its_reason():
    plan = statistical_plan()
    versions = {e["version"] for e in plan["superseded_plan_versions"]}
    assert versions == {
        "tier1_stage35_execution_timing_v1",
        "tier1_stage35_execution_timing_v2",
    }
    v1 = next(
        e for e in plan["superseded_plan_versions"]
        if e["version"] == "tier1_stage35_execution_timing_v1"
    )
    assert v1["version"] == "tier1_stage35_execution_timing_v1"
    assert v1["superseded_before_any_execution_outcome"] == "true"
    for token in ("clamped", "price scale", "delayed_fraction", "zero", "final snapshot"):
        assert token in v1["reason"], token


# ---------------------------------------------------------------------------
# A. Unresolved event targets are eligible for the deadline policy
# ---------------------------------------------------------------------------


def _eligibility(statuses, finite=None):
    import numpy as np
    from app.services.mbo_stage35_executor import execution_eligibility

    status = np.array(statuses)
    if finite is None:
        finite = np.ones(len(statuses), dtype=bool)
    return execution_eligibility(status, np.asarray(finite))


def test_no_further_midpoint_change_rows_are_eligible():
    """The bug: these were discarded before the deadline policy could fire,
    making the plan's own unresolved-target rule unreachable -- and removing the
    quiet periods specifically."""
    mask, counts = _eligibility(["ok", "no_further_midpoint_change"])
    assert mask.tolist() == [True, True]
    assert counts["no_further_midpoint_change"] == 1


def test_source_midpoint_unavailable_is_never_eligible():
    mask, _ = _eligibility(["source_midpoint_unavailable", "ok"])
    assert mask.tolist() == [False, True]


def test_session_end_before_horizon_is_excluded_too():
    mask, _ = _eligibility(["session_end_before_horizon", "ok"])
    assert mask.tolist() == [False, True]


def test_a_non_finite_design_row_is_still_excluded():
    mask, _ = _eligibility(
        ["ok", "no_further_midpoint_change"], finite=[False, False]
    )
    assert mask.tolist() == [False, False]


def test_an_unrecognised_status_is_refused_not_guessed_at():
    """A status this plan has not considered is a reason to stop."""
    with pytest.raises(ValueError, match="unrecognised label statuses"):
        _eligibility(["ok", "some_new_status"])


def test_status_counts_are_reported_for_provenance():
    _, counts = _eligibility(
        ["ok", "ok", "no_further_midpoint_change", "source_midpoint_unavailable"]
    )
    assert counts == {
        "ok": 2,
        "no_further_midpoint_change": 1,
        "source_midpoint_unavailable": 1,
    }


def test_an_unresolved_target_with_coverage_is_evaluated_at_the_deadline():
    """(1) The whole point of the correction: a row whose midpoint never moves
    again is a valid observation of the deadline policy."""
    decision = 1_000_000_000
    pair, reason = make_pair(
        -50.0,
        static_book(100.00, 100.02),
        target=None,  # no_further_midpoint_change -> availability is None
        decision=decision,
        within_coverage=lambda lo, hi: True,
    )
    assert reason is None
    assert pair.trigger == TRIGGER_DEADLINE
    assert pair.timed_send_ts == decision + DELAY_DEADLINE_NS
    assert pair.timed_arrival_ts == decision + MAX_ARRIVAL_NS


def test_an_unresolved_target_beyond_coverage_is_refused():
    """(2) Same row, but the deadline arrival runs past the certified stream."""
    decision = 1_000_000_000
    pair, reason = make_pair(
        -50.0,
        static_book(100.00, 100.02),
        target=None,
        decision=decision,
        within_coverage=lambda lo, hi: hi <= decision + 500 * MS,
    )
    assert pair is None
    assert reason == NOT_COMPARABLE_OUTSIDE_COVERAGE


def test_a_resolved_target_after_the_deadline_also_uses_the_deadline():
    """(4) Resolution exists but arrives too late to act on."""
    decision = 1_000_000_000
    pair, reason = make_pair(
        -50.0,
        static_book(100.00, 100.02),
        target=decision + 5 * SCALE,
        decision=decision,
    )
    assert reason is None
    assert pair.trigger == TRIGGER_DEADLINE
    assert pair.timed_send_ts == decision + DELAY_DEADLINE_NS


def test_the_loader_admits_unresolved_targets():
    """The eligibility rule the CLI actually applies, not a parallel one."""
    import inspect

    from app.cli.mbo_stage35 import _read_cell_inputs

    source = inspect.getsource(_read_cell_inputs)
    assert "execution_eligibility" in source
    assert "status == LABEL_OK" not in source


def test_the_eligible_statuses_are_frozen_in_the_plan():
    rule = statistical_plan()["label_status_rule"]
    assert rule["eligible"] == ["ok", "no_further_midpoint_change"]
    assert "source_midpoint_unavailable" in rule["excluded"]
    assert "refused" in rule["unknown_statuses"]
    assert rule["status_counts_reported"] is True


# ---------------------------------------------------------------------------
# B. The reproduction gate fails closed
# ---------------------------------------------------------------------------


def _recorded(recorded):
    return {b: {"per_date_delta_r2": v} for b, v in recorded.items()}


def test_a_missing_training_gram_is_refused_not_silently_shortened():
    """The certified Gram batch is complete, so an absent training Gram means the
    inputs are wrong -- not that the training set should quietly shrink."""
    from app.services.mbo_stage35_executor import per_date_betas

    grams, alpha, _ = _stage2_world()
    del grams[DATES[3]]
    with pytest.raises(ValueError, match="training Grams absent"):
        per_date_betas(grams, BLOCKS, alpha)


def test_a_mismatched_recorded_count_is_refused_not_skipped():
    from app.services.mbo_stage35_executor import recorded_stage2_per_date

    _grams, _alpha, recorded = _stage2_world()
    truncated = {k: list(v) for k, v in recorded.items()}
    truncated["discovery"] = truncated["discovery"][:9]
    with pytest.raises(ValueError, match="cannot be associated unambiguously"):
        recorded_stage2_per_date(_recorded(truncated), BLOCKS)


def test_an_empty_recorded_block_is_refused():
    from app.services.mbo_stage35_executor import recorded_stage2_per_date

    _grams, _alpha, recorded = _stage2_world()
    emptied = {k: list(v) for k, v in recorded.items()}
    emptied["validation"] = []
    with pytest.raises(ValueError, match="recorded no per-date values"):
        recorded_stage2_per_date(_recorded(emptied), BLOCKS)


def test_a_missing_block_entirely_is_refused():
    from app.services.mbo_stage35_executor import recorded_stage2_per_date

    _grams, _alpha, recorded = _stage2_world()
    partial = {b: {"per_date_delta_r2": v} for b, v in recorded.items()}
    del partial["confirmation"]
    with pytest.raises(ValueError, match="confirmation: Stage 2 recorded no"):
        recorded_stage2_per_date(partial, BLOCKS)


def test_reproduction_requires_all_twenty_dates():
    """9/6/4 must refuse even though every checked date matched."""
    from app.services.mbo_stage35_executor import (
        per_date_betas,
        recorded_stage2_per_date,
        reproduce_stage2_delta_r2,
    )

    grams, alpha, recorded = _stage2_world()
    mapped = recorded_stage2_per_date(_recorded(recorded), BLOCKS)
    betas = per_date_betas(grams, BLOCKS, alpha)
    # Drop one discovery date from the recorded map: 9/6/4.
    del mapped["discovery"][DATES[0]]
    with pytest.raises(ValueError, match="discovery: 9 dates reproduced, expected 10"):
        reproduce_stage2_delta_r2("50ev|next_change", grams, betas, alpha, mapped)


def test_reproduction_requires_the_full_validation_block():
    """10/5/4 must refuse too."""
    from app.services.mbo_stage35_executor import (
        per_date_betas,
        recorded_stage2_per_date,
        reproduce_stage2_delta_r2,
    )

    grams, alpha, recorded = _stage2_world()
    mapped = recorded_stage2_per_date(_recorded(recorded), BLOCKS)
    betas = per_date_betas(grams, BLOCKS, alpha)
    del mapped["validation"][DATES[10]]
    with pytest.raises(ValueError, match="validation: 5 dates reproduced, expected 6"):
        reproduce_stage2_delta_r2("50ev|next_change", grams, betas, alpha, mapped)


def test_exact_ten_six_four_passes_and_is_reported():
    from app.services.mbo_stage35_executor import (
        EXPECTED_REPRODUCTION_TOTAL,
        per_date_betas,
        recorded_stage2_per_date,
        reproduce_stage2_delta_r2,
    )

    grams, alpha, recorded = _stage2_world()
    betas = per_date_betas(grams, BLOCKS, alpha)
    result = reproduce_stage2_delta_r2(
        "50ev|next_change", grams, betas, alpha,
        recorded_stage2_per_date(_recorded(recorded), BLOCKS),
    )
    assert result["reproduction_verified"] is True
    assert result["dates_checked"] == EXPECTED_REPRODUCTION_TOTAL == 20
    assert result["dates_checked_by_block"] == {
        "discovery": 10, "validation": 6, "confirmation": 4
    }


def test_a_gram_missing_at_scoring_time_is_refused():
    from app.services.mbo_stage35_executor import (
        per_date_betas,
        recorded_stage2_per_date,
        reproduce_stage2_delta_r2,
    )

    grams, alpha, recorded = _stage2_world()
    betas = per_date_betas(grams, BLOCKS, alpha)
    mapped = recorded_stage2_per_date(_recorded(recorded), BLOCKS)
    scoring = {k: v for k, v in grams.items() if k != DATES[19]}
    with pytest.raises(ValueError, match="no Gram to score against"):
        reproduce_stage2_delta_r2("50ev|next_change", scoring, betas, alpha, mapped)


def test_the_reproduction_requirement_is_frozen_in_the_plan():
    requirement = statistical_plan()["reproduction_requirement"]
    assert requirement["fails_closed"] is True
    assert requirement["expected_checked"] == {
        "discovery": 10, "validation": 6, "confirmation": 4, "total": 20
    }
    assert "refusal" in requirement["unmappable_records_refuse"]


def test_the_superseded_v2_plan_is_recorded_with_its_reason():
    plan = statistical_plan()
    v2 = next(
        e for e in plan["superseded_plan_versions"]
        if e["version"] == "tier1_stage35_execution_timing_v2"
    )
    assert v2["superseded_before_any_execution_outcome"] == "true"
    assert "unresolved-target rule unreachable" in v2["reason"]
    assert "reproduction_verified" in v2["reason"]


# ---------------------------------------------------------------------------
# The diagnostic must exercise the gates that matter
# ---------------------------------------------------------------------------


def _diagnose_body() -> str:
    """The executable body of `diagnose`, without its docstring.

    Inspecting the body rather than the whole source matters: the docstring
    names the very things it must not do, and matching on those would pass or
    fail for the wrong reason.
    """
    import inspect

    from app.cli.mbo_stage35 import diagnose

    body = inspect.getsource(diagnose).split('"""')[2]
    # Comments explain what the function deliberately avoids, so matching on
    # them would pass or fail for the wrong reason.
    return "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )


def test_the_diagnostic_exercises_the_reproduction_gate():
    """The bug this closes: a diagnostic that only calls _prepare reports clean,
    and then the real run fails on the 20-date gate it never touched."""
    body = _diagnose_body()
    assert "_prepare(args)" in body
    assert "_build_fits(context)" in body


def test_the_diagnostic_exercises_the_eligibility_path():
    """It must read the actual feature/label files, so spine certification and
    the label-status eligibility rule genuinely run."""
    body = _diagnose_body()
    assert "_read_cell_inputs(" in body
    assert "label_status_counts" in body


def test_the_diagnostic_binds_and_hashes_every_raw_source():
    body = _diagnose_body()
    assert "resolve_raw_source(" in body
    assert "manifest.json" in body


def test_the_diagnostic_never_evaluates_an_execution_pair():
    """The whole point: it walks the same inputs and stops short of a fill."""
    body = _diagnose_body()
    for forbidden in (
        "evaluate_pair",
        "assemble_report",
        "CellTiming",
        ".summary(",
        "BookReplay",
        "CoverageTracker",
        "query_instants",
        "savings",
        "net_return",
        "walk_book",
    ):
        assert forbidden not in body, forbidden


def test_the_diagnostic_never_replays_a_book():
    body = _diagnose_body()
    for forbidden in ("iter_dbn_events", "replay.run", "book_at"):
        assert forbidden not in body, forbidden


def test_the_diagnostic_does_not_record_predictions():
    """A prediction is not an outcome, but it is not a count either."""
    body = _diagnose_body()
    assert '"predictions"' not in body
    assert "inputs[\'predictions\']" not in body


def test_the_diagnostic_keeps_the_recursive_strip():
    body = _diagnose_body()
    assert "_strip_outcomes(payload)" in body


def test_the_diagnostic_declares_itself_outcome_free():
    body = _diagnose_body()
    assert '"diagnostic_only": True' in body
    assert '"contains_execution_outcome": False' in body


def test_the_diagnostic_reports_the_expected_universe_counts():
    body = _diagnose_body()
    for field in (
        "symbol_days_inspected",
        "session_date_count",
        "frozen_cell_count",
        "raw_sources_verified",
        "spine_certified_cell_files",
        "source_label_status_counts",
        "eligible_rows_by_cell",
        "excluded_rows_by_cell_and_status",
        "stage2_reproduction",
    ):
        assert field in body, field


def test_the_diagnostic_fails_when_the_reproduction_gate_fails(monkeypatch, tmp_path):
    """A failing 20-date gate must stop the diagnostic, not be reported as a
    finding alongside a clean bill of health."""
    import argparse

    import app.cli.mbo_stage35 as cli

    monkeypatch.setattr(cli, "_prepare", lambda args: {"frozen": {"survivors": []}})

    def exploding_build_fits(context):
        raise ValueError(
            "the Stage-3.5 per-date fits for 50ev|next_change do not reproduce "
            "Stage 2's recorded delta_R2 across the complete frozen date block"
        )

    monkeypatch.setattr(cli, "_build_fits", exploding_build_fits)
    args = argparse.Namespace(
        output_dir=str(tmp_path), stage2_results="x", grams_dir="g",
        features_dir="f", labels_dir="l", raw_dir="r",
    )
    with pytest.raises(ValueError, match="do not reproduce Stage 2"):
        cli.diagnose(args)
    assert not (tmp_path / "stage35_diagnostic.json").exists()


def test_the_diagnostic_fails_when_a_raw_source_cannot_be_bound(monkeypatch, tmp_path):
    """Binding is not advisory: an unverifiable raw file stops the diagnostic."""
    import argparse

    import app.cli.mbo_stage35 as cli

    features = tmp_path / "features"
    (features / "manifests").mkdir(parents=True)
    (features / "50ev").mkdir(parents=True)
    (features / "50ev" / "AAAA_2025-06-18.50ev.parquet").write_bytes(b"x")

    monkeypatch.setattr(
        cli,
        "_prepare",
        lambda args: {
            "frozen": {"survivors": list(FROZEN_CELLS)},
            "batch_completeness": {},
            "blocks": BLOCKS,
            "chronology": {},
            "session_dates": DATES,
            "features_dir": features,
            "labels_dir": tmp_path / "labels",
            "grams_dir": tmp_path / "grams",
            "raw_dir": tmp_path / "raw",
            "stage2": {},
        },
    )
    monkeypatch.setattr(
        cli, "_build_fits", lambda context: {"fits": {}, "reproduction": {}}
    )
    args = argparse.Namespace(
        output_dir=str(tmp_path), stage2_results="x", grams_dir="g",
        features_dir=str(features), labels_dir="l", raw_dir="r",
    )
    with pytest.raises(ValueError, match="no Stage-1 manifest"):
        cli.diagnose(args)


def test_the_diagnostic_and_the_run_share_the_same_loader():
    """If they diverged, a clean diagnostic would stop meaning anything about
    the run."""
    import inspect

    from app.cli.mbo_stage35 import diagnose, run

    assert "_read_cell_inputs(" in inspect.getsource(diagnose)
    assert "_read_cell_inputs(" in inspect.getsource(run)


def test_status_counts_survive_the_outcome_filter_while_outcomes_do_not():
    """The bug this closes: two label statuses contain the word "midpoint", and
    the generic filter strips any key containing it -- so serializing statuses
    as keys silently deleted exactly the provenance the diagnostic exists to
    show. The filter is right to be blunt; the fix belongs in the shape.
    """
    from app.cli.mbo_stage35 import _status_records, _strip_outcomes

    payload = {
        "source_label_status_counts": _status_records(
            {
                "50ev|next_change": {
                    "ok": 123,
                    "no_further_midpoint_change": 45,
                    "source_midpoint_unavailable": 2,
                }
            }
        ),
        "excluded_rows_by_cell_and_status": _status_records(
            {"200ev|next_change": {"source_midpoint_unavailable": 7}}
        ),
        # Genuine outcomes, which must still be removed.
        "balanced_parent_flow_savings_bps": 0.9,
        "midpoint_timing_benefit_bps": 0.4,
        "p_value": 1e-9,
        "verdict": "supported",
    }
    clean = _strip_outcomes(payload)

    assert set(clean) == {
        "source_label_status_counts",
        "excluded_rows_by_cell_and_status",
    }
    statuses = clean["source_label_status_counts"][0]["statuses"]
    assert {entry["status"]: entry["count"] for entry in statuses} == {
        "ok": 123,
        "no_further_midpoint_change": 45,
        "source_midpoint_unavailable": 2,
    }
    excluded = clean["excluded_rows_by_cell_and_status"][0]
    assert excluded["cell"] == "200ev|next_change"
    assert excluded["statuses"] == [
        {"status": "source_midpoint_unavailable", "count": 7}
    ]


def test_status_counts_as_keys_would_have_been_stripped():
    """Demonstrates the defect rather than merely asserting the fix, so the
    reason for the record shape cannot be optimised away later."""
    from app.cli.mbo_stage35 import _strip_outcomes

    as_keys = {
        "counts": {
            "ok": 123,
            "no_further_midpoint_change": 45,
            "source_midpoint_unavailable": 2,
        }
    }
    assert _strip_outcomes(as_keys) == {"counts": {"ok": 123}}


def test_the_outcome_filter_was_not_weakened():
    """No token was removed to make the statuses fit."""
    from app.cli.mbo_stage35 import OUTCOME_BEARING

    for token in (
        "saving", "savings", "benefit", "fill", "midpoint", "dollar", "bps",
        "clustered_t", "p_value", "verdict", "passing",
    ):
        assert token in OUTCOME_BEARING, token


def test_the_diagnostic_serializes_statuses_as_records():
    body = _diagnose_body()
    assert "_status_records(label_statuses)" in body
    assert "_status_records(excluded_rows)" in body


def test_the_plan_and_design_hash_did_not_move_for_the_diagnostic_patch():
    """Wiring the diagnostic is not a mechanism change."""
    from app.services.mbo_stage35_plan import PLAN_DESIGN_HASH, STAGE35_PLAN_VERSION

    assert STAGE35_PLAN_VERSION == "tier1_stage35_execution_timing_v3"
    assert PLAN_DESIGN_HASH == (
        "097b5d65dfd49d9c648865df3b31c716b51b0c685c6e8b347c772a3b6992ba94"
    )


# ---------------------------------------------------------------------------
# Streaming accumulation must reproduce the batch estimand exactly
# ---------------------------------------------------------------------------


def _batch_summary(cell_name, pairs, schedule):
    """The batch computation the streaming version replaces.

    Kept here, in the tests, as the reference the new implementation is measured
    against. Deleting it and trusting the replacement would have removed the
    only thing that can catch a changed estimand.
    """
    import numpy as np
    from app.services.mbo_stage35_executor import (
        BUY,
        MIN_COMPARABLE_PAIRS,
        MIN_SESSION_DATES,
        TRIGGER_DEADLINE,
        TRIGGER_TARGET,
        clustered_t,
        price_dependent_fee_difference_usd,
    )

    directional = [p for p in pairs if p.delayed_side is not None]
    balanced = np.array([p.balanced_savings_bps for p in pairs])
    delayed = np.array([p.delayed_savings_bps for p in directional])
    midpoint = np.array([p.midpoint_benefit_bps(p.delayed_side) for p in directional])
    book = np.array([p.book_walk_benefit_bps(p.delayed_side) for p in directional])
    dollars = np.array([p.delayed_dollar_savings for p in directional])

    by_date, by_symbol = {}, {}
    buy_savings, sell_savings = [], []
    for pair, value in zip(pairs, balanced, strict=True):
        by_date.setdefault(pair.session_date, []).append(float(value))
        by_symbol.setdefault(pair.symbol, []).append(float(value))
        if pair.delayed_side == BUY:
            buy_savings.append(pair.savings_bps(BUY))
        elif pair.delayed_side is not None:
            sell_savings.append(pair.savings_bps(pair.delayed_side))

    date_means = {d: float(np.mean(v)) for d, v in sorted(by_date.items())}
    statistic, p_value = clustered_t(list(date_means.values()))

    def mean_or_none(values):
        return float(np.mean(values)) if len(values) else None

    return {
        "cell": cell_name,
        "comparable_pairs": len(pairs),
        "asymmetric_fill_failures": 0,
        "reached_screen": (
            len(pairs) >= MIN_COMPARABLE_PAIRS and len(date_means) >= MIN_SESSION_DATES
        ),
        "session_dates": len(date_means),
        "balanced_parent_flow_savings_bps": float(balanced.mean()),
        "delayed_side_savings_bps": mean_or_none(delayed),
        "midpoint_timing_benefit_bps": mean_or_none(midpoint),
        "book_walk_benefit_bps": mean_or_none(book),
        "dollar_savings_per_100_shares": mean_or_none(dollars),
        "buy_savings_bps": mean_or_none(buy_savings),
        "sell_savings_bps": mean_or_none(sell_savings),
        "delayed_buy_pairs": len(buy_savings),
        "delayed_sell_pairs": len(sell_savings),
        "zero_prediction_pairs": len(pairs) - len(directional),
        "pairs_with_a_delay_fraction": len(directional) / len(pairs),
        "parent_orders_delayed_fraction": len(directional) / (2 * len(pairs)),
        "target_triggered_delays": sum(
            1 for p in directional if p.trigger == TRIGGER_TARGET
        ),
        "deadline_triggered_delays": sum(
            1 for p in directional if p.trigger == TRIGGER_DEADLINE
        ),
        "mean_displayed_liquidity_shares": mean_or_none(
            [p.leg(p.delayed_side)["timed_displayed"] for p in directional]
        ),
        "mean_levels_walked": mean_or_none(
            [p.leg(p.delayed_side)["timed_levels"] for p in directional]
        ),
        "price_dependent_fee_difference_usd": mean_or_none(
            [price_dependent_fee_difference_usd(p, schedule) for p in pairs]
        ),
        "clustered_t": statistic,
        "p_value": p_value,
        "per_session_date_balanced_bps": date_means,
        "by_symbol_balanced_bps": {
            s: float(np.mean(v)) for s, v in sorted(by_symbol.items())
        },
    }


def _deterministic_pairs(n=240):
    """A varied but reproducible stream: both directions, zero predictions,
    both triggers, moving and static books, several dates and symbols."""
    from app.services.mbo_feature_engine import FIXED_PRICE_SCALE

    scale = float(FIXED_PRICE_SCALE)
    decision = 1_000_000_000
    pairs = []
    for i in range(n):
        if i % 7 == 0:
            predicted = 0.0
        elif i % 3 == 0:
            predicted = -50.0 - i
        else:
            predicted = 50.0 + i
        drift = ((i % 11) - 5) * 0.01
        target = None if i % 13 == 0 else decision + (50 + (i % 900)) * MS
        pair, reason = evaluate_pair(
            cell="50ev|next_change",
            symbol=f"SYM{i % 5}",
            session_date=f"2025-06-{2 + (i % 12):02d}",
            block="confirmation",
            predicted_bps=predicted,
            decision_ts=decision,
            target_available_ts_recv=target,
            book_at=moving_book(
                decision,
                (100.00, 100.02),
                (100.00 + drift, 100.02 + drift + (i % 3) * 0.01),
                decision + 300 * MS,
            ),
            price_scale=scale,
        )
        assert reason is None, reason
        pairs.append(pair)
    return pairs


def test_streaming_reproduces_the_batch_summary_field_for_field():
    """The estimand must be identical; only the order of summation changes."""
    from app.services.mbo_feature_engine import FIXED_PRICE_SCALE
    from app.services.mbo_stage3_plan import PRIMARY_FEE_SCHEDULE

    pairs = _deterministic_pairs()
    expected = _batch_summary("50ev|next_change", pairs, PRIMARY_FEE_SCHEDULE)

    cell = CellTiming(cell="50ev|next_change", price_scale=float(FIXED_PRICE_SCALE))
    for pair in pairs:
        cell.record_pair(pair, PRIMARY_FEE_SCHEDULE)
    actual = cell.summary()

    exact_fields = (
        "cell",
        "comparable_pairs",
        "session_dates",
        "reached_screen",
        "delayed_buy_pairs",
        "delayed_sell_pairs",
        "zero_prediction_pairs",
        "target_triggered_delays",
        "deadline_triggered_delays",
        "asymmetric_fill_failures",
    )
    for field_name in exact_fields:
        assert actual[field_name] == expected[field_name], field_name

    float_fields = (
        "balanced_parent_flow_savings_bps",
        "delayed_side_savings_bps",
        "midpoint_timing_benefit_bps",
        "book_walk_benefit_bps",
        "dollar_savings_per_100_shares",
        "buy_savings_bps",
        "sell_savings_bps",
        "pairs_with_a_delay_fraction",
        "parent_orders_delayed_fraction",
        "mean_displayed_liquidity_shares",
        "mean_levels_walked",
        "price_dependent_fee_difference_usd",
        "clustered_t",
        "p_value",
    )
    for field_name in float_fields:
        if expected[field_name] is None:
            assert actual[field_name] is None, field_name
        else:
            assert actual[field_name] == pytest.approx(
                expected[field_name], rel=1e-12, abs=1e-12
            ), field_name

    assert set(actual["per_session_date_balanced_bps"]) == set(
        expected["per_session_date_balanced_bps"]
    )
    for date, value in expected["per_session_date_balanced_bps"].items():
        assert actual["per_session_date_balanced_bps"][date] == pytest.approx(
            value, rel=1e-12, abs=1e-12
        ), date

    assert set(actual["by_symbol_balanced_bps"]) == set(expected["by_symbol_balanced_bps"])
    for symbol, value in expected["by_symbol_balanced_bps"].items():
        assert actual["by_symbol_balanced_bps"][symbol] == pytest.approx(
            value, rel=1e-12, abs=1e-12
        ), symbol


def test_streaming_parity_holds_for_an_all_zero_prediction_stream():
    """The degenerate case: no pair delays, so every directional mean is None."""
    from app.services.mbo_feature_engine import FIXED_PRICE_SCALE
    from app.services.mbo_stage3_plan import PRIMARY_FEE_SCHEDULE

    scale = float(FIXED_PRICE_SCALE)
    decision = 1_000_000_000
    pairs = []
    for i in range(12):
        pair, reason = evaluate_pair(
            cell="50ev|next_change", symbol="AAAA",
            session_date=f"2025-06-{2 + i:02d}", block="confirmation",
            predicted_bps=0.0, decision_ts=decision,
            target_available_ts_recv=decision + 400 * MS,
            book_at=static_book(100.00, 100.02), price_scale=scale,
        )
        assert reason is None
        pairs.append(pair)

    expected = _batch_summary("50ev|next_change", pairs, PRIMARY_FEE_SCHEDULE)
    cell = CellTiming(cell="50ev|next_change", price_scale=scale)
    for pair in pairs:
        cell.record_pair(pair, PRIMARY_FEE_SCHEDULE)
    actual = cell.summary()

    assert actual["zero_prediction_pairs"] == expected["zero_prediction_pairs"] == 12
    assert actual["pairs_with_a_delay_fraction"] == 0.0
    assert actual["parent_orders_delayed_fraction"] == 0.0
    for field_name in (
        "delayed_side_savings_bps",
        "midpoint_timing_benefit_bps",
        "book_walk_benefit_bps",
        "buy_savings_bps",
        "sell_savings_bps",
        "mean_displayed_liquidity_shares",
        "mean_levels_walked",
    ):
        assert actual[field_name] is None, field_name
        assert expected[field_name] is None, field_name


def test_an_empty_cell_still_reports_why():
    cell = CellTiming(cell="50ev|next_change")
    cell.record_not_comparable(NOT_COMPARABLE_TIMED_LIQUIDITY)
    summary = cell.summary()
    assert summary["comparable_pairs"] == 0
    assert summary["reached_screen"] is False
    assert summary["not_comparable"][NOT_COMPARABLE_TIMED_LIQUIDITY] == 1


# ---------------------------------------------------------------------------
# Retained state must not grow with the number of observations
# ---------------------------------------------------------------------------


def test_cell_timing_holds_no_collection_proportional_to_observations():
    import dataclasses

    names = {f.name for f in dataclasses.fields(CellTiming)}
    assert "pairs" not in names
    cell = CellTiming(cell="50ev|next_change")
    for name in names:
        value = getattr(cell, name)
        assert not isinstance(value, list), name


def test_retained_state_is_bounded_by_dates_and_symbols_not_by_row_count():
    """Ten times the observations, over the same dates and symbols, must leave
    the same amount of state behind."""
    from app.services.mbo_feature_engine import FIXED_PRICE_SCALE
    from app.services.mbo_stage3_plan import PRIMARY_FEE_SCHEDULE

    scale = float(FIXED_PRICE_SCALE)
    decision = 1_000_000_000

    def stream(n):
        cell = CellTiming(cell="50ev|next_change", price_scale=scale)
        for i in range(n):
            pair, reason = evaluate_pair(
                cell="50ev|next_change", symbol=f"SYM{i % 5}",
                session_date=f"2025-06-{2 + (i % 12):02d}", block="confirmation",
                predicted_bps=(50.0 if i % 2 else -50.0), decision_ts=decision,
                target_available_ts_recv=decision + 400 * MS,
                book_at=static_book(100.00, 100.02), price_scale=scale,
            )
            assert reason is None
            cell.record_pair(pair, PRIMARY_FEE_SCHEDULE)
        return cell

    small, large = stream(200), stream(2_000)
    assert large.comparable_pairs == 10 * small.comparable_pairs
    # The only growing structures are the bounded date and symbol maps.
    assert len(large.by_date) == len(small.by_date) == 12
    assert len(large.by_symbol) == len(small.by_symbol) == 5
    assert len(large.not_comparable) == len(small.not_comparable) == 0


def test_the_run_folds_pairs_in_rather_than_retaining_them():
    import inspect

    from app.cli.mbo_stage35 import run

    source = inspect.getsource(run)
    assert "record_pair(" in source
    assert "pairs.append(" not in source
    assert "sink.summary()" in source
