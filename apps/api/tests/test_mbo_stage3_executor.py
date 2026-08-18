"""Stage 3: the economics must be right before they are ever believed.

The dangerous failures here are not statistical, they are temporal. A fill that
peeks one microsecond past its arrival instant turns a losing strategy into a
winning one, and nothing downstream would notice. So most of what follows tests
*when* information is allowed to be used, not what the numbers come out to.
"""

from __future__ import annotations

import json

import pytest
from app.services.mbo_stage3_executor import (
    NO_TRADE_BELOW_HURDLE,
    NO_TRADE_NO_BOOK,
    NO_TRADE_NO_EXIT,
    NO_TRADE_NO_LIQUIDITY,
    BookLevels,
    CellEconomics,
    Trade,
    assemble_report,
    assert_frozen_plan,
    clustered_t,
    cost_hurdle_bps,
    evaluate_candidate,
    factor_beta,
    freeze_survivors,
    load_frozen_survivors,
    walk_book,
)
from app.services.mbo_stage3_plan import (
    FEE_SCHEDULE,
    LATENCY_RUNGS,
    MAX_BOOK_LEVELS_WALKED,
    PLAN_DESIGN_HASH,
    PRIMARY_LATENCY,
    PRIMARY_RULE,
    SECONDARY_RULE,
    TRADE_SIZE_SHARES,
    statistical_plan,
)

SCALE = 1_000_000_000  # fixed-point price scale, as in the certified stream
MS = 1_000_000
LATENCY = dict(LATENCY_RUNGS)


def book(ts: int, bid: float, ask: float, size: int = 1_000, levels: int = 3) -> BookLevels:
    """A tidy symmetric book at one instant, one cent between levels."""
    cent = SCALE // 100
    return BookLevels(
        ts=ts,
        bids=tuple((int(bid * SCALE) - i * cent, size) for i in range(levels)),
        asks=tuple((int(ask * SCALE) + i * cent, size) for i in range(levels)),
    )


def static_book_at(bid: float, ask: float, **kwargs):
    def book_at(ts: int) -> BookLevels:
        return book(ts, bid, ask, **kwargs)

    return book_at


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------


def test_the_plan_design_hash_is_frozen():
    assert_frozen_plan()
    assert PLAN_DESIGN_HASH == (
        "f6878f6608002f1363982a4b38e7de719b460e34aa0c371db65aac4a93a83221"
    )


def test_the_plan_declares_itself_pre_outcome_and_pre_survivor():
    plan = statistical_plan()
    assert plan["declared_before_any_economic_outcome"] is True
    assert plan["declared_before_survivors_were_known"] is True
    assert plan["contains_economic_result"] is False
    assert plan["primary_latency"] == "250ms"


def test_survivors_are_taken_from_confirmation_not_re_judged():
    results = {
        "plan_hash": "abc",
        "verdict": "confirmed",
        "cells": [
            {"cadence": "1s", "horizon": "5s", "confirmation": {"passed": True}},
            {"cadence": "5s", "horizon": "1s", "confirmation": {"passed": False}},
            # A near miss is not a survivor. Stage 3 is not a court of appeal.
            {"cadence": "5s", "horizon": "10s", "confirmation": {"passed": False},
             "discovery": {"delta_r2": 0.9, "clustered_t": 99.0}},
            {"cadence": "200ev", "horizon": "next_change", "confirmation": {"run": False}},
        ],
    }
    frozen = freeze_survivors(results)
    assert frozen["survivors"] == ["1s|5s"]
    assert frozen["survivor_count"] == 1
    assert frozen["survivor_hash"]


def test_stage3_refuses_when_stage2_never_ran(tmp_path):
    with pytest.raises(ValueError, match="may not invent survivors"):
        load_frozen_survivors(tmp_path / "stage2_results.json")


def test_stage3_refuses_when_nothing_survived(tmp_path):
    path = tmp_path / "stage2_results.json"
    path.write_text(json.dumps({"cells": [
        {"cadence": "1s", "horizon": "1s", "confirmation": {"passed": False}}
    ]}))
    with pytest.raises(ValueError, match="no surviving cell"):
        load_frozen_survivors(path)


def test_stage3_refuses_a_survivor_count_that_does_not_match_the_declaration(tmp_path):
    path = tmp_path / "stage2_results.json"
    path.write_text(json.dumps({"cells": [
        {"cadence": "1s", "horizon": "1s", "confirmation": {"passed": True}}
    ]}))
    with pytest.raises(ValueError, match="expected 4 frozen survivors"):
        load_frozen_survivors(path, expected_count=4)


# ---------------------------------------------------------------------------
# Causality -- the failures that would silently manufacture profit
# ---------------------------------------------------------------------------


def test_the_fill_cannot_use_information_before_arrival():
    """The order arrives at decision + latency. A book queried at any earlier
    instant must never be the one that fills it."""
    seen: list[int] = []

    def book_at(ts: int) -> BookLevels:
        seen.append(ts)
        return book(ts, 100.00, 100.02)

    decision = 1_000_000_000
    latency = LATENCY["250ms"]
    trade, reason = evaluate_candidate(
        predicted_bps=500.0,
        decision_ts=decision,
        horizon_ns=5 * SCALE,
        latency_ns=latency,
        book_at=book_at,
        price_scale=SCALE,
    )
    assert reason is None and trade is not None
    assert trade.arrival_ts == decision + latency
    # The only instants consulted are the decision, the arrival and the exit.
    assert set(seen) == {decision, decision + latency, decision + 5 * SCALE + latency}
    assert min(t for t in seen if t != decision) > decision


def test_a_later_rung_arrives_strictly_later():
    """Monotonic by construction: more latency can only mean a later book."""
    arrivals = []
    for name, latency in LATENCY_RUNGS:
        trade, reason = evaluate_candidate(
            predicted_bps=500.0,
            decision_ts=1_000_000_000,
            horizon_ns=5 * SCALE,
            latency_ns=latency,
            book_at=static_book_at(100.00, 100.02),
            price_scale=SCALE,
        )
        assert reason is None and trade is not None, name
        arrivals.append(trade.arrival_ts)
    assert arrivals == sorted(arrivals)
    assert len(set(arrivals)) == 3


def test_a_future_price_move_before_arrival_is_charged_as_adverse_selection():
    """The market runs away between deciding and arriving. That cost must land
    on the adverse-selection line, and must not be invisible."""

    def book_at(ts: int) -> BookLevels:
        # Price jumps up 10 cents once the 250 ms have elapsed.
        if ts >= 1_000_000_000 + LATENCY["250ms"]:
            return book(ts, 100.10, 100.12)
        return book(ts, 100.00, 100.02)

    trade, reason = evaluate_candidate(
        predicted_bps=500.0,
        decision_ts=1_000_000_000,
        horizon_ns=5 * SCALE,
        latency_ns=LATENCY["250ms"],
        book_at=book_at,
        price_scale=SCALE,
    )
    assert reason is None and trade is not None
    # We decided to buy at 100.01 mid and arrived to a 100.11 mid: the move
    # happened without us, so adverse selection is a ~10 bp charge against.
    assert trade.adverse_selection_bps == pytest.approx(9.999, abs=0.01)
    assert trade.decision_midpoint < trade.arrival_midpoint


def test_a_short_is_charged_adverse_selection_with_the_opposite_sign():
    def book_at(ts: int) -> BookLevels:
        if ts >= 1_000_000_000 + LATENCY["250ms"]:
            return book(ts, 100.10, 100.12)
        return book(ts, 100.00, 100.02)

    trade, _ = evaluate_candidate(
        predicted_bps=-500.0,
        decision_ts=1_000_000_000,
        horizon_ns=5 * SCALE,
        latency_ns=LATENCY["250ms"],
        book_at=book_at,
        price_scale=SCALE,
    )
    assert trade is not None and trade.direction == -1
    # The same drift now helps a short, so the sign flips.
    assert trade.adverse_selection_bps == pytest.approx(-9.999, abs=0.01)


# ---------------------------------------------------------------------------
# The fill model
# ---------------------------------------------------------------------------


def test_a_marketable_buy_walks_the_ask_upward():
    levels = BookLevels(
        ts=0,
        bids=((int(99.99 * SCALE), 500),),
        asks=((int(100.00 * SCALE), 60), (int(100.01 * SCALE), 40)),
    )
    vwap, consumed = walk_book(levels, "buy", 100)
    assert consumed == 2
    # 60 at 100.00 and 40 at 100.01.
    assert vwap == pytest.approx((60 * 100.00 + 40 * 100.01) / 100 * SCALE)


def test_a_marketable_sell_walks_the_bid_downward():
    levels = BookLevels(
        ts=0,
        bids=((int(100.00 * SCALE), 60), (int(99.99 * SCALE), 40)),
        asks=((int(100.02 * SCALE), 500),),
    )
    vwap, consumed = walk_book(levels, "sell", 100)
    assert consumed == 2
    assert vwap == pytest.approx((60 * 100.00 + 40 * 99.99) / 100 * SCALE)


def test_insufficient_displayed_liquidity_is_not_a_fill_at_a_worse_price():
    """The tempting bug: assume the rest fills somewhere. It must not."""
    thin = BookLevels(
        ts=0,
        bids=((int(99.99 * SCALE), 500),),
        asks=((int(100.00 * SCALE), 10),),
    )
    assert walk_book(thin, "buy", 100) is None

    trade, reason = evaluate_candidate(
        predicted_bps=500.0,
        decision_ts=1_000_000_000,
        horizon_ns=5 * SCALE,
        latency_ns=LATENCY["250ms"],
        book_at=lambda ts: thin,
        price_scale=SCALE,
    )
    assert trade is None
    assert reason == NO_TRADE_NO_LIQUIDITY


def test_the_level_budget_is_respected():
    deep = BookLevels(
        ts=0,
        bids=((int(99.99 * SCALE), 10_000),),
        asks=tuple((int((100.00 + i * 0.01) * SCALE), 1) for i in range(50)),
    )
    # Only MAX_BOOK_LEVELS_WALKED levels of 1 share each are reachable.
    assert walk_book(deep, "buy", MAX_BOOK_LEVELS_WALKED) is not None
    assert walk_book(deep, "buy", MAX_BOOK_LEVELS_WALKED + 1) is None


def test_a_one_sided_book_produces_no_trade():
    one_sided = BookLevels(ts=0, bids=(), asks=((int(100.0 * SCALE), 1_000),))
    trade, reason = evaluate_candidate(
        predicted_bps=500.0,
        decision_ts=1_000_000_000,
        horizon_ns=5 * SCALE,
        latency_ns=LATENCY["250ms"],
        book_at=lambda ts: one_sided,
        price_scale=SCALE,
    )
    assert trade is None and reason == NO_TRADE_NO_BOOK


def test_a_missing_exit_book_produces_no_trade():
    def book_at(ts: int) -> BookLevels:
        if ts >= 1_000_000_000 + 5 * SCALE:
            return BookLevels(ts=ts, bids=(), asks=())
        return book(ts, 100.00, 100.02)

    trade, reason = evaluate_candidate(
        predicted_bps=500.0,
        decision_ts=1_000_000_000,
        horizon_ns=5 * SCALE,
        latency_ns=LATENCY["250ms"],
        book_at=book_at,
        price_scale=SCALE,
    )
    assert trade is None and reason == NO_TRADE_NO_EXIT


# ---------------------------------------------------------------------------
# The cost hurdle
# ---------------------------------------------------------------------------


def test_the_hurdle_is_at_least_the_quoted_spread():
    levels = book(0, 100.00, 100.02)
    hurdle = cost_hurdle_bps(levels, TRADE_SIZE_SHARES, SCALE)
    spread_bps = 0.02 / 100.01 * 10_000
    assert hurdle is not None and hurdle > spread_bps


def test_a_prediction_below_the_hurdle_is_not_traded():
    """The whole primary rule in one case: a 0.5 bp edge cannot pay a 2 bp
    spread, so no trade happens."""
    trade, reason = evaluate_candidate(
        predicted_bps=0.5,
        decision_ts=1_000_000_000,
        horizon_ns=5 * SCALE,
        latency_ns=LATENCY["250ms"],
        book_at=static_book_at(100.00, 100.02),
        price_scale=SCALE,
    )
    assert trade is None and reason == NO_TRADE_BELOW_HURDLE


def test_a_wider_spread_raises_the_hurdle():
    tight = cost_hurdle_bps(book(0, 100.00, 100.02), TRADE_SIZE_SHARES, SCALE)
    wide = cost_hurdle_bps(book(0, 100.00, 100.20), TRADE_SIZE_SHARES, SCALE)
    assert tight is not None and wide is not None and wide > tight


def test_the_hurdle_uses_no_information_after_the_decision():
    """Two futures, one decision book: the hurdle must be identical."""
    decision_book = book(0, 100.00, 100.02)
    assert cost_hurdle_bps(decision_book, TRADE_SIZE_SHARES, SCALE) == cost_hurdle_bps(
        BookLevels(ts=999, bids=decision_book.bids, asks=decision_book.asks),
        TRADE_SIZE_SHARES,
        SCALE,
    )


def test_the_secondary_rule_uses_a_discovery_threshold_not_the_hurdle():
    common = {
        "decision_ts": 1_000_000_000,
        "horizon_ns": 5 * SCALE,
        "latency_ns": LATENCY["250ms"],
        "book_at": static_book_at(100.00, 100.02),
        "price_scale": SCALE,
        "rule": SECONDARY_RULE,
    }
    below, reason = evaluate_candidate(predicted_bps=1.0, decile_threshold_bps=5.0, **common)
    assert below is None and reason == NO_TRADE_BELOW_HURDLE
    above, reason = evaluate_candidate(predicted_bps=9.0, decile_threshold_bps=5.0, **common)
    assert reason is None and above is not None


# ---------------------------------------------------------------------------
# Costs, hand-computed
# ---------------------------------------------------------------------------


def _round_trip(entry: float, exit_: float, direction: int = 1) -> Trade:
    trade = Trade(
        symbol="AAAA",
        session_date="2025-06-02",
        decision_ts=0,
        arrival_ts=LATENCY["250ms"],
        exit_ts=LATENCY["250ms"] + 5 * SCALE,
        direction=direction,
        predicted_bps=10.0,
        decision_midpoint=100.0 * SCALE,
        arrival_midpoint=100.0 * SCALE,
        entry_price=entry * SCALE,
        exit_price=exit_ * SCALE,
        shares=TRADE_SIZE_SHARES,
        levels_consumed_entry=1,
        levels_consumed_exit=1,
        displayed_entry=1_000,
        displayed_exit=1_000,
    )
    trade.exit_midpoint = 100.0 * SCALE
    return trade


def test_fees_match_the_frozen_schedule_by_hand():
    """100 shares in and out at $100. Taker and clearing on both legs, Section
    31 and TAF on the sale leg only."""
    trade = _round_trip(100.0, 100.0)
    taker = FEE_SCHEDULE["nasdaq_taker_fee_usd_per_share"] * 100 * 2  # 0.60
    clearing = FEE_SCHEDULE["clearing_usd_per_share"] * 100 * 2  # 0.04
    sec = FEE_SCHEDULE["sec_section_31_usd_per_million_sold"] * 10_000 / 1_000_000
    taf = FEE_SCHEDULE["finra_taf_usd_per_share_sold"] * 100
    expected = (taker + clearing + sec + taf) / 10_000 * 10_000
    assert trade.fees_bps(SCALE) == pytest.approx(expected)


def test_section_31_and_taf_are_charged_on_the_sale_leg_only():
    """A long sells at exit, a short sells at entry. With different prices on
    the two legs the fee must follow the sale, not the entry."""
    long_trade = _round_trip(100.0, 110.0, direction=1)
    short_trade = _round_trip(110.0, 100.0, direction=-1)
    # Both sell $11,000 of notional, so their Section 31 charge in dollars is
    # equal even though their entry notionals differ.
    long_fees_usd = long_trade.fees_bps(SCALE) / 10_000 * 10_000
    short_fees_usd = short_trade.fees_bps(SCALE) / 10_000 * 11_000
    assert long_fees_usd == pytest.approx(short_fees_usd)


def test_a_round_trip_at_an_unchanged_price_loses_exactly_the_fees():
    trade = _round_trip(100.0, 100.0)
    assert trade.realized_return_bps == pytest.approx(0.0)
    assert trade.net_return_bps(SCALE) == pytest.approx(-trade.fees_bps(SCALE))
    assert trade.net_return_bps(SCALE) < 0


def test_crossing_the_spread_shows_up_as_spread_paid():
    """Buy the offer, sell the bid, midpoint unmoved: the loss is the spread."""
    trade = Trade(
        symbol="A", session_date="d", decision_ts=0,
        arrival_ts=0, exit_ts=SCALE, direction=1, predicted_bps=50.0,
        decision_midpoint=100.01 * SCALE, arrival_midpoint=100.01 * SCALE,
        entry_price=100.02 * SCALE, exit_price=100.00 * SCALE,
        shares=100, levels_consumed_entry=1, levels_consumed_exit=1,
        displayed_entry=500, displayed_exit=500,
    )
    trade.exit_midpoint = 100.01 * SCALE
    assert trade.gross_return_bps == pytest.approx(0.0)
    assert trade.spread_paid_bps == pytest.approx(0.02 / 100.01 * 10_000, rel=1e-6)
    assert trade.realized_return_bps < 0


# ---------------------------------------------------------------------------
# Aggregation, inference and the primary family
# ---------------------------------------------------------------------------


def test_a_cell_with_no_trades_reports_why_rather_than_nothing():
    cell = CellEconomics(cell="1s|5s", latency="250ms", rule=PRIMARY_RULE, price_scale=SCALE)
    cell.record_no_trade(NO_TRADE_BELOW_HURDLE)
    cell.record_no_trade(NO_TRADE_BELOW_HURDLE)
    cell.record_no_trade(NO_TRADE_NO_LIQUIDITY)
    summary = cell.summary()
    assert summary["trade_count"] == 0
    assert summary["reached_inference"] is False
    assert summary["no_trade_reasons"] == {
        NO_TRADE_BELOW_HURDLE: 2,
        NO_TRADE_NO_LIQUIDITY: 1,
    }


def test_inference_is_withheld_below_the_declared_trade_minimum():
    cell = CellEconomics(cell="1s|5s", latency="250ms", rule=PRIMARY_RULE, price_scale=SCALE)
    for i in range(20):  # fewer than the 100 declared
        trade = _round_trip(100.0, 100.5)
        trade.session_date = f"2025-06-{2 + i % 5:02d}"
        cell.trades.append(trade)
    assert cell.summary()["reached_inference"] is False


def test_clustered_t_is_one_observation_per_session_date():
    statistic, p = clustered_t([1.0] * 8)
    assert statistic is None  # no dispersion, no statistic
    statistic, p = clustered_t([1.0, 1.2, 0.9, 1.1, 1.3, 0.8])
    assert statistic is not None and statistic > 3
    assert p is not None and 0 < p < 0.01


def test_factor_beta_separates_a_market_bet_from_an_edge():
    """A 'strategy' that is exactly the tape must show beta 1 and no alpha."""
    market = {f"d{i}": v for i, v in enumerate([1.0, -2.0, 3.0, -1.0, 0.5, 2.0])}
    same = dict(market)
    result = factor_beta(same, market)
    assert result["beta"] == pytest.approx(1.0)
    assert result["alpha_bps"] == pytest.approx(0.0, abs=1e-9)


def test_factor_beta_finds_alpha_that_is_independent_of_the_tape():
    market = {f"d{i}": v for i, v in enumerate([1.0, -2.0, 3.0, -1.0, 0.5, 2.0])}
    net = {k: 4.0 for k in market}  # flat 4 bps regardless of the market
    result = factor_beta(net, market)
    assert result["beta"] == pytest.approx(0.0, abs=1e-9)
    assert result["alpha_bps"] == pytest.approx(4.0)


def test_the_primary_family_is_the_250ms_rung_under_the_primary_rule():
    results = [
        {"cell": "1s|5s", "latency": name, "rule": rule, "reached_inference": True,
         "p_value": 0.0001, "net_return_bps": 3.0, "clustered_t": 5.0}
        for name, _ in LATENCY_RUNGS
        for rule in (PRIMARY_RULE, SECONDARY_RULE)
    ]
    report = assemble_report(results, {"survivors": ["1s|5s"], "survivor_count": 1})
    assert report["primary_family"]["latency"] == PRIMARY_LATENCY
    assert report["primary_family"]["rule"] == PRIMARY_RULE
    assert report["primary_family"]["size"] == 1
    assert report["economically_positive_at_primary"] == ["1s|5s"]


def test_a_negative_mean_cannot_pass_however_significant():
    """Significantly losing money is a failure, not a discovery."""
    results = [{
        "cell": "1s|5s", "latency": PRIMARY_LATENCY, "rule": PRIMARY_RULE,
        "reached_inference": True, "p_value": 1e-12,
        "net_return_bps": -4.0, "clustered_t": -9.0,
    }]
    report = assemble_report(results, {"survivors": ["1s|5s"]})
    assert report["economically_positive_at_primary"] == []
    assert report["verdict"] == "no_economically_viable_survivor"


def test_a_secondary_rung_cannot_answer_the_primary_question():
    """Positive at 50 ms, negative at 250 ms: the verdict follows 250 ms."""
    results = [
        {"cell": "1s|5s", "latency": "50ms", "rule": PRIMARY_RULE,
         "reached_inference": True, "p_value": 1e-9, "net_return_bps": 8.0,
         "clustered_t": 12.0},
        {"cell": "1s|5s", "latency": PRIMARY_LATENCY, "rule": PRIMARY_RULE,
         "reached_inference": True, "p_value": 0.4, "net_return_bps": -1.0,
         "clustered_t": -0.8},
    ]
    report = assemble_report(results, {"survivors": ["1s|5s"]})
    assert report["economically_positive_at_primary"] == []
    assert report["verdict"] == "no_economically_viable_survivor"


def test_bh_denominator_is_the_frozen_survivor_count():
    results = [
        {"cell": f"c{i}", "latency": PRIMARY_LATENCY, "rule": PRIMARY_RULE,
         "reached_inference": True, "p_value": 0.02, "net_return_bps": 1.0,
         "clustered_t": 4.0}
        for i in range(4)
    ]
    report = assemble_report(results, {"survivors": [f"c{i}" for i in range(4)]})
    bh = report["primary_family"]["benjamini_hochberg"]
    assert len(bh) == 4
    assert all(entry["bh_critical"] > 0 for entry in bh.values())


# ---------------------------------------------------------------------------
# The replay, against a real reconstructed book
# ---------------------------------------------------------------------------


def _mbo_event(ts_recv, action, side, price, size, order_id, seq):
    from app.services.mbo_book_validator import MboEvent

    return MboEvent(
        ts_event=ts_recv - 500,
        action=action,
        side=side,
        price=price,
        size=size,
        order_id=order_id,
        flags=128,
        sequence=seq,
        ts_recv=ts_recv,
    )


def _replay():
    from app.services.mbo_book_validator import MboBook
    from app.services.mbo_stage3_executor import BookReplay

    return BookReplay(MboBook)


def test_the_replay_answers_each_instant_from_records_that_had_arrived():
    """The bid improves at ts_recv 3000. A query at 2999 must not see it; a
    query at 3001 must."""
    events = [
        _mbo_event(1000, "A", "B", 100 * SCALE, 500, 1, 1),
        _mbo_event(1000, "A", "A", 101 * SCALE, 500, 2, 2),
        _mbo_event(3000, "A", "B", 10050 * SCALE // 100, 300, 3, 3),
    ]
    answers = _replay().run(events, [2999, 3001])
    assert answers[2999].best_bid == 100 * SCALE
    assert answers[3001].best_bid == 10050 * SCALE // 100


def test_the_replay_refuses_a_stream_whose_receipt_clock_goes_backwards():
    """Out of order by receipt time means a single pass would fill from
    information that had not arrived. That must be a refusal, not a number."""
    events = [
        _mbo_event(5000, "A", "B", 100 * SCALE, 500, 1, 1),
        _mbo_event(4000, "A", "A", 101 * SCALE, 500, 2, 2),
    ]
    with pytest.raises(ValueError, match="ts_recv went backwards"):
        _replay().run(events, [6000])


def test_an_instant_after_the_final_record_sees_the_final_book():
    events = [
        _mbo_event(1000, "A", "B", 100 * SCALE, 500, 1, 1),
        _mbo_event(1000, "A", "A", 101 * SCALE, 500, 2, 2),
    ]
    answers = _replay().run(events, [10**12])
    assert answers[10**12].two_sided


def test_reconstructing_the_frozen_fit_refuses_when_the_artefacts_disagree():
    """The integrity check that stops Stage 3 trading a model it cannot account
    for."""
    import numpy as np
    from app.services.mbo_stage2_executor import DESIGN_WIDTH, Gram
    from app.services.mbo_stage3_executor import reconstruct_confirmation_beta

    def gram(seed):
        rng = np.random.default_rng(seed)
        x = rng.standard_normal((400, DESIGN_WIDTH))
        x[:, 0] = 1.0
        y = 0.3 * x[:, 1] + rng.standard_normal(400)
        g = Gram.zeros(DESIGN_WIDTH)
        g.add_rows(x, y)
        return g

    grams = {f"d{i}": gram(i) for i in range(8)}
    train = [f"d{i}" for i in range(6)]
    confirm = [f"d{i}" for i in range(6, 8)]

    # No recorded value to check against: reproduction proceeds.
    beta = reconstruct_confirmation_beta(grams, train, 1.0)
    assert beta.shape == (DESIGN_WIDTH,)

    # A recorded value that does not match: refuse.
    with pytest.raises(ValueError, match="does not reproduce the recorded"):
        reconstruct_confirmation_beta(
            grams, train, 1.0,
            recorded_delta_r2=0.42,
            confirmation_dates=confirm,
        )
