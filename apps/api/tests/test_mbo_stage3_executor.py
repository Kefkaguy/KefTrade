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
    NO_TRADE_RESOLVED_BEFORE_ENTRY,
    NO_TRADE_UNCERTIFIABLE_TIMING,
    NO_TRADE_UNRESOLVED_TARGET,
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
    CONSERVATIVE_FEE_SCHEDULE,
    FROZEN_SURVIVORS,
    LATENCY_RUNGS,
    MAX_BOOK_LEVELS_WALKED,
    PLAN_DESIGN_HASH,
    PRIMARY_FEE_SCHEDULE,
    PRIMARY_LATENCY,
    PRIMARY_RULE,
    RETAIL_CAT_STRESS_FEE_SCHEDULE,
    SECONDARY_RULE,
    SECTION_31_USD_PER_MILLION,
    SURVIVOR_HASH,
    TRADE_SIZE_SHARES,
    assert_session_dates_covered,
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


def test_the_plan_and_survivor_hashes_are_frozen():
    assert_frozen_plan()
    assert PLAN_DESIGN_HASH == (
        "055c3d83108ea6223c12bd541d824843ace071a110e3bd5e1292e1f0665186f4"
    )
    assert SURVIVOR_HASH == (
        "bea300ba23327075909e37e36864feee6087dc85a5d55108cb53a615c7046f00"
    )


def test_the_plan_states_governance_accurately():
    """The survivors ARE known. What must be true is that no economic outcome
    has been seen and that the rules were frozen before one could be."""
    plan = statistical_plan()
    assert plan["governance"] == {
        "stage2_survivors_known": True,
        "stage3_economic_outcome_viewed": False,
        "stage3_rules_frozen_before_economic_outcomes": True,
    }
    # The retracted claim must not survive anywhere in the artefact.
    assert "declared_before_survivors_were_known" not in plan
    assert plan["contains_economic_result"] is False
    assert plan["primary_latency"] == "250ms"


def test_the_four_survivors_are_the_confirmed_ones_and_are_all_event_clocked():
    assert list(FROZEN_SURVIVORS) == sorted([
        "50ev|next_change",
        "50ev|next_2_changes",
        "200ev|next_change",
        "200ev|next_2_changes",
    ])
    for cell in FROZEN_SURVIVORS:
        cadence, horizon = cell.split("|")
        assert cadence.endswith("ev"), cadence
        assert horizon in ("next_change", "next_2_changes"), horizon


def test_every_superseded_plan_is_recorded_with_its_reason():
    plan = statistical_plan()
    by_version = {e["version"]: e for e in plan["superseded_plan_versions"]}
    assert set(by_version) == {
        "tier1_stage3_economics_v1",
        "tier1_stage3_economics_v2",
        "tier1_stage3_economics_v3",
        "tier1_stage3_economics_v4",
    }
    for entry in by_version.values():
        assert entry["superseded_before_any_economic_outcome"] == "true"
        assert entry["plan_design_hash"]

    v1 = by_version["tier1_stage3_economics_v1"]
    assert "declared_before_survivors_were_known" in v1["reason"]
    assert "horizon_ns" in v1["reason"]

    v2 = by_version["tier1_stage3_economics_v2"]
    assert "June 2025" in v2["reason"]          # the wrong-year fee schedule
    assert "aggregated confirmation Gram" in v2["reason"]  # the wrong proof
    assert "CAT" in v2["reason"]

    v3 = by_version["tier1_stage3_economics_v3"]
    assert "unmeasured survivors" in v3["reason"]
    assert "CAT" in v3["reason"]

    v4 = by_version["tier1_stage3_economics_v4"]
    assert "in sample" in v4["reason"]
    assert "guessing filenames" in v4["reason"]
    assert "nullable" in v4["reason"]


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
        exit_resolution_ts=1_000_000_000 + 5 * SCALE,
        latency_ns=latency,
        book_at=book_at,
        price_scale=SCALE,
        schedule=PRIMARY_FEE_SCHEDULE,
    )
    assert reason is None and trade is not None
    assert trade.arrival_ts == decision + latency
    # The only instants consulted are the decision, the arrival and the exit.
    assert set(seen) == {decision, decision + latency, 1_000_000_000 + 5 * SCALE + latency}
    assert min(t for t in seen if t != decision) > decision


def test_a_later_rung_arrives_strictly_later():
    """Monotonic by construction: more latency can only mean a later book."""
    arrivals = []
    for name, latency in LATENCY_RUNGS:
        trade, reason = evaluate_candidate(
            predicted_bps=500.0,
            decision_ts=1_000_000_000,
            exit_resolution_ts=1_000_000_000 + 5 * SCALE,
            latency_ns=latency,
            book_at=static_book_at(100.00, 100.02),
            price_scale=SCALE,
            schedule=PRIMARY_FEE_SCHEDULE,
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
        exit_resolution_ts=1_000_000_000 + 5 * SCALE,
        latency_ns=LATENCY["250ms"],
        book_at=book_at,
        price_scale=SCALE,
        schedule=PRIMARY_FEE_SCHEDULE,
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
        exit_resolution_ts=1_000_000_000 + 5 * SCALE,
        latency_ns=LATENCY["250ms"],
        book_at=book_at,
        price_scale=SCALE,
        schedule=PRIMARY_FEE_SCHEDULE,
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
        exit_resolution_ts=1_000_000_000 + 5 * SCALE,
        latency_ns=LATENCY["250ms"],
        book_at=lambda ts: thin,
        price_scale=SCALE,
        schedule=PRIMARY_FEE_SCHEDULE,
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
        exit_resolution_ts=1_000_000_000 + 5 * SCALE,
        latency_ns=LATENCY["250ms"],
        book_at=lambda ts: one_sided,
        price_scale=SCALE,
        schedule=PRIMARY_FEE_SCHEDULE,
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
        exit_resolution_ts=1_000_000_000 + 5 * SCALE,
        latency_ns=LATENCY["250ms"],
        book_at=book_at,
        price_scale=SCALE,
        schedule=PRIMARY_FEE_SCHEDULE,
    )
    assert trade is None and reason == NO_TRADE_NO_EXIT


# ---------------------------------------------------------------------------
# The cost hurdle
# ---------------------------------------------------------------------------


def test_the_hurdle_is_at_least_the_quoted_spread():
    levels = book(0, 100.00, 100.02)
    hurdle = cost_hurdle_bps(levels, TRADE_SIZE_SHARES, SCALE, PRIMARY_FEE_SCHEDULE)
    spread_bps = 0.02 / 100.01 * 10_000
    assert hurdle is not None and hurdle > spread_bps


def test_a_prediction_below_the_hurdle_is_not_traded():
    """The whole primary rule in one case: a 0.5 bp edge cannot pay a 2 bp
    spread, so no trade happens."""
    trade, reason = evaluate_candidate(
        predicted_bps=0.5,
        decision_ts=1_000_000_000,
        exit_resolution_ts=1_000_000_000 + 5 * SCALE,
        latency_ns=LATENCY["250ms"],
        book_at=static_book_at(100.00, 100.02),
        price_scale=SCALE,
        schedule=PRIMARY_FEE_SCHEDULE,
    )
    assert trade is None and reason == NO_TRADE_BELOW_HURDLE


def test_a_wider_spread_raises_the_hurdle():
    tight = cost_hurdle_bps(book(0, 100.00, 100.02), TRADE_SIZE_SHARES, SCALE, PRIMARY_FEE_SCHEDULE)
    wide = cost_hurdle_bps(book(0, 100.00, 100.20), TRADE_SIZE_SHARES, SCALE, PRIMARY_FEE_SCHEDULE)
    assert tight is not None and wide is not None and wide > tight


def test_the_hurdle_uses_no_information_after_the_decision():
    """Two futures, one decision book: the hurdle must be identical."""
    decision_book = book(0, 100.00, 100.02)
    assert cost_hurdle_bps(
        decision_book, TRADE_SIZE_SHARES, SCALE, PRIMARY_FEE_SCHEDULE
    ) == cost_hurdle_bps(
        BookLevels(ts=999, bids=decision_book.bids, asks=decision_book.asks),
        TRADE_SIZE_SHARES,
        SCALE,
        PRIMARY_FEE_SCHEDULE,
    )


def test_the_secondary_rule_uses_a_discovery_threshold_not_the_hurdle():
    common = {
        "decision_ts": 1_000_000_000,
        "exit_resolution_ts": 1_000_000_000 + 5 * SCALE,
        "latency_ns": LATENCY["250ms"],
        "book_at": static_book_at(100.00, 100.02),
        "price_scale": SCALE,
        "schedule": PRIMARY_FEE_SCHEDULE,
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
        exit_resolution_ts=5 * SCALE,
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
    per_share = (
        PRIMARY_FEE_SCHEDULE["commission_usd_per_share"]
        + PRIMARY_FEE_SCHEDULE["exchange_take_fee_usd_per_share"]
        + PRIMARY_FEE_SCHEDULE["clearing_usd_per_share"]
        + PRIMARY_FEE_SCHEDULE["cat_usd_per_share"]
    ) * 100 * 2
    sec = PRIMARY_FEE_SCHEDULE["sec_section_31_usd_per_million_sold"] * 10_000 / 1_000_000
    taf = PRIMARY_FEE_SCHEDULE["finra_taf_usd_per_share_sold"] * 100
    expected = (per_share + sec + taf) / 10_000 * 10_000
    assert trade.fees_bps(SCALE, PRIMARY_FEE_SCHEDULE) == pytest.approx(expected)


def test_section_31_follows_the_sale_leg_when_the_rate_is_non_zero():
    """A long sells at exit, a short sells at entry. The rate is zero over the
    June-2025 window, so this is checked against the stress schedule's structure
    using a non-zero rate rather than asserted vacuously."""
    schedule = {**CONSERVATIVE_FEE_SCHEDULE, "sec_section_31_usd_per_million_sold": 27.80}
    long_trade = _round_trip(100.0, 110.0, direction=1)
    short_trade = _round_trip(110.0, 100.0, direction=-1)
    # Both sell $11,000 of notional, so the Section 31 charge in dollars is the
    # same even though their entry notionals differ.
    long_usd = long_trade.fees_bps(SCALE, schedule) / 10_000 * 10_000
    short_usd = short_trade.fees_bps(SCALE, schedule) / 10_000 * 11_000
    assert long_usd == pytest.approx(short_usd)


def test_a_round_trip_at_an_unchanged_price_loses_exactly_the_fees():
    trade = _round_trip(100.0, 100.0)
    assert trade.realized_return_bps == pytest.approx(0.0)
    assert trade.net_return_bps(SCALE, PRIMARY_FEE_SCHEDULE) == pytest.approx(-trade.fees_bps(SCALE, PRIMARY_FEE_SCHEDULE))
    assert trade.net_return_bps(SCALE, PRIMARY_FEE_SCHEDULE) < 0


def test_crossing_the_spread_shows_up_as_spread_paid():
    """Buy the offer, sell the bid, midpoint unmoved: the loss is the spread."""
    trade = Trade(
        symbol="A", session_date="d", decision_ts=0,
        arrival_ts=0, exit_ts=SCALE, exit_resolution_ts=SCALE,
        direction=1, predicted_bps=50.0,
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


# ---------------------------------------------------------------------------
# Event-time horizons: the exit comes from the frozen Stage-2 target, never a
# fabricated clock
# ---------------------------------------------------------------------------


def _label_row(prefix: str, decision: int, resolution: int) -> dict:
    """A wide Stage-2A label row, as the executor will read it."""
    return {
        "feature_available_ts_recv": decision,
        f"{prefix}_status": "ok",
        f"{prefix}_label_ts_event": resolution - 400,
        f"{prefix}_label_ts_recv": resolution - 200,
        f"{prefix}_available_ts_recv": resolution,
        f"{prefix}_realized_lag_ns": resolution - decision,
        f"{prefix}_return_bps": 12.0,
    }


@pytest.mark.parametrize("prefix", ["next_change", "next_2_changes"])
def test_the_exit_is_anchored_to_the_exact_stage2_target_event(prefix):
    """The two change horizons resolve at different instants for the same
    decision, and each trade must exit on its own target -- not on a shared,
    invented duration."""
    decision = 1_000_000_000
    resolution = decision + (3 * SCALE if prefix == "next_change" else 9 * SCALE)
    row = _label_row(prefix, decision, resolution)

    trade, reason = evaluate_candidate(
        predicted_bps=500.0,
        decision_ts=row["feature_available_ts_recv"],
        exit_resolution_ts=row[f"{prefix}_available_ts_recv"],
        latency_ns=LATENCY["250ms"],
        book_at=static_book_at(100.00, 100.02),
        price_scale=SCALE,
        schedule=PRIMARY_FEE_SCHEDULE,
    )
    assert reason is None and trade is not None
    # The exit order arrives at the target's availability instant plus latency.
    assert trade.exit_resolution_ts == resolution
    assert trade.exit_ts == resolution + LATENCY["250ms"]
    # The realized lag is the event clock's own answer, not an assumption.
    assert trade.realized_lag_ns == row[f"{prefix}_realized_lag_ns"]


def test_next_two_changes_holds_longer_than_next_change_for_one_decision():
    decision = 1_000_000_000
    trades = {}
    for prefix, gap in (("next_change", 3 * SCALE), ("next_2_changes", 9 * SCALE)):
        trade, reason = evaluate_candidate(
            predicted_bps=500.0,
            decision_ts=decision,
            exit_resolution_ts=decision + gap,
            latency_ns=LATENCY["250ms"],
            book_at=static_book_at(100.00, 100.02),
            price_scale=SCALE,
            schedule=PRIMARY_FEE_SCHEDULE,
        )
        assert reason is None and trade is not None
        trades[prefix] = trade
    assert trades["next_2_changes"].holding_ns > trades["next_change"].holding_ns
    assert trades["next_2_changes"].realized_lag_ns == 9 * SCALE


def test_the_executor_has_no_way_to_pass_a_clock_horizon():
    """The v1 mistake, pinned shut: there is no horizon_ns parameter through
    which a fabricated duration could be smuggled."""
    import inspect

    params = inspect.signature(evaluate_candidate).parameters
    assert "horizon_ns" not in params
    assert "exit_resolution_ts" in params


def test_a_target_that_resolved_before_entry_is_a_named_missed_opportunity():
    """The prediction may have been perfectly right and still unharvestable:
    the midpoint moved before the order could arrive."""
    decision = 1_000_000_000
    trade, reason = evaluate_candidate(
        predicted_bps=500.0,
        decision_ts=decision,
        exit_resolution_ts=decision + 10 * MS,
        latency_ns=LATENCY["250ms"],
        book_at=static_book_at(100.00, 100.02),
        price_scale=SCALE,
        schedule=PRIMARY_FEE_SCHEDULE,
    )
    assert trade is None
    assert reason == NO_TRADE_RESOLVED_BEFORE_ENTRY


def test_the_same_candidate_can_be_harvestable_at_50ms_and_not_at_250ms():
    """The exact reason the ladder exists."""
    decision = 1_000_000_000
    resolution = decision + 100 * MS
    outcomes = {}
    for name, latency in LATENCY_RUNGS:
        _, reason = evaluate_candidate(
            predicted_bps=500.0,
            decision_ts=decision,
            exit_resolution_ts=resolution,
            latency_ns=latency,
            book_at=static_book_at(100.00, 100.02),
            price_scale=SCALE,
            schedule=PRIMARY_FEE_SCHEDULE,
        )
        outcomes[name] = reason
    assert outcomes["50ms"] is None
    assert outcomes["250ms"] == NO_TRADE_RESOLVED_BEFORE_ENTRY
    assert outcomes["1s"] == NO_TRADE_RESOLVED_BEFORE_ENTRY


def test_an_unresolved_stage2_target_is_named_not_guessed():
    trade, reason = evaluate_candidate(
        predicted_bps=500.0,
        decision_ts=1_000_000_000,
        exit_resolution_ts=None,
        latency_ns=LATENCY["250ms"],
        book_at=static_book_at(100.00, 100.02),
        price_scale=SCALE,
        schedule=PRIMARY_FEE_SCHEDULE,
    )
    assert trade is None and reason == NO_TRADE_UNRESOLVED_TARGET


# ---------------------------------------------------------------------------
# The two fee schedules
# ---------------------------------------------------------------------------


def test_the_retail_schedule_does_not_charge_an_exchange_remove_fee():
    """The v1 mistake: billing a commission-free retail account the venue's
    per-share take fee."""
    assert PRIMARY_FEE_SCHEDULE["exchange_take_fee_usd_per_share"] == 0.0
    assert PRIMARY_FEE_SCHEDULE["commission_usd_per_share"] == 0.0
    assert PRIMARY_FEE_SCHEDULE["cat_usd_per_share"] == 0.0
    # Section 31 was $0.00 per million for the whole June-2025 window.
    assert PRIMARY_FEE_SCHEDULE["sec_section_31_usd_per_million_sold"] == 0.0
    # TAF still applies on the sale leg.
    assert PRIMARY_FEE_SCHEDULE["finra_taf_usd_per_share_sold"] == 0.000166


def test_the_stress_schedule_is_strictly_more_expensive():
    retail = _round_trip(100.0, 100.0).fees_bps(SCALE, PRIMARY_FEE_SCHEDULE)
    direct = _round_trip(100.0, 100.0).fees_bps(SCALE, CONSERVATIVE_FEE_SCHEDULE)
    assert direct > retail
    assert CONSERVATIVE_FEE_SCHEDULE["exchange_take_fee_usd_per_share"] == 0.0030


def test_both_schedules_are_versioned_and_dated_and_flag_verification():
    for schedule in (PRIMARY_FEE_SCHEDULE, CONSERVATIVE_FEE_SCHEDULE):
        assert schedule["schedule_version"]
        assert schedule["effective_from"]
        assert schedule["rates_require_verification"] is True
        assert "confirmed against the schedules" in schedule["verification_note"]


def test_the_hurdle_is_higher_under_the_stress_schedule():
    levels = book(0, 100.00, 100.02)
    retail = cost_hurdle_bps(levels, TRADE_SIZE_SHARES, SCALE, PRIMARY_FEE_SCHEDULE)
    direct = cost_hurdle_bps(levels, TRADE_SIZE_SHARES, SCALE, CONSERVATIVE_FEE_SCHEDULE)
    assert retail is not None and direct is not None
    assert direct > retail


def test_the_primary_family_is_the_retail_schedule_only():
    """A cell positive only under the stress schedule cannot answer the primary
    question, and a cell negative there cannot veto it."""
    results = [
        {"cell": "50ev|next_change", "latency": PRIMARY_LATENCY, "rule": PRIMARY_RULE,
         "fee_schedule": PRIMARY_FEE_SCHEDULE["name"], "reached_inference": True,
         "p_value": 0.0001, "net_return_bps": 2.0, "clustered_t": 5.0},
        {"cell": "50ev|next_change", "latency": PRIMARY_LATENCY, "rule": PRIMARY_RULE,
         "fee_schedule": CONSERVATIVE_FEE_SCHEDULE["name"], "reached_inference": True,
         "p_value": 0.0001, "net_return_bps": -3.0, "clustered_t": -6.0},
    ]
    report = assemble_report(results, {"survivors": list(FROZEN_SURVIVORS)})
    assert report["primary_family"]["fee_schedule"] == PRIMARY_FEE_SCHEDULE["name"]
    assert report["primary_family"]["size"] == 1
    assert report["economically_positive_at_primary"] == ["50ev|next_change"]


# ---------------------------------------------------------------------------
# F_BAD_TS_RECV
# ---------------------------------------------------------------------------


def test_a_flagged_receive_timestamp_makes_the_window_uncertifiable():
    import dataclasses

    from app.services.mbo_book_validator import MboBook
    from app.services.mbo_stage3_executor import BookReplay

    replay = BookReplay(MboBook)
    good = [
        _mbo_event(1000, "A", "B", 100 * SCALE, 500, 1, 1),
        _mbo_event(1000, "A", "A", 101 * SCALE, 500, 2, 2),
    ]
    # One record whose receipt instant the venue declines to vouch for.
    flagged = dataclasses.replace(
        _mbo_event(5000, "A", "B", 10050 * SCALE // 100, 300, 3, 3), flags=128 | 8
    )
    replay.run([*good, flagged], [500])

    assert replay.bad_recv_instants == [5000]
    assert replay.timing_certified(1, 4999) is True      # window ends before it
    assert replay.timing_certified(5001, 9000) is True   # window starts after it
    assert replay.timing_certified(1, 6000) is False     # window contains it
    assert replay.timing_certified(5000, 5000) is False  # exactly on it


def test_an_uncertifiable_candidate_is_excluded_and_counted_not_traded():
    decision = 1_000_000_000
    trade, reason = evaluate_candidate(
        predicted_bps=500.0,
        decision_ts=decision,
        exit_resolution_ts=decision + 5 * SCALE,
        latency_ns=LATENCY["250ms"],
        book_at=static_book_at(100.00, 100.02),
        price_scale=SCALE,
        schedule=PRIMARY_FEE_SCHEDULE,
        timing_certified=lambda lo, hi: False,
    )
    assert trade is None
    assert reason == NO_TRADE_UNCERTIFIABLE_TIMING


def test_a_certified_window_trades_normally():
    decision = 1_000_000_000
    trade, reason = evaluate_candidate(
        predicted_bps=500.0,
        decision_ts=decision,
        exit_resolution_ts=decision + 5 * SCALE,
        latency_ns=LATENCY["250ms"],
        book_at=static_book_at(100.00, 100.02),
        price_scale=SCALE,
        schedule=PRIMARY_FEE_SCHEDULE,
        timing_certified=lambda lo, hi: True,
    )
    assert reason is None and trade is not None


def test_the_bad_ts_rule_is_frozen_and_says_exclude():
    from app.services.mbo_stage3_plan import BAD_TS_RECV_RULE

    assert BAD_TS_RECV_RULE["frozen_before_any_economic_outcome"] == "true"
    assert "excluded" in BAD_TS_RECV_RULE["decision"]
    assert BAD_TS_RECV_RULE["excluded_candidates_are_counted"] is True
    assert BAD_TS_RECV_RULE["never_silently_dropped"] is True


# ---------------------------------------------------------------------------
# The June-2025 historical fee schedule
# ---------------------------------------------------------------------------


def test_section_31_is_zero_for_every_june_2025_session():
    """The rate was set to $0.00 per million effective 2025-05-14, so it is zero
    across the whole research window. A 2026-dated constant would have invented
    a cost that did not exist."""
    assert SECTION_31_USD_PER_MILLION == 0.0
    for schedule in (
        PRIMARY_FEE_SCHEDULE,
        RETAIL_CAT_STRESS_FEE_SCHEDULE,
        CONSERVATIVE_FEE_SCHEDULE,
    ):
        assert schedule["sec_section_31_usd_per_million_sold"] == 0.0
        assert schedule["sec_section_31_effective_from"] == "2025-05-14"


def test_the_schedule_window_covers_june_2025_and_refuses_anything_else():
    assert_session_dates_covered(["2025-06-02", "2025-06-16", "2025-06-30"])
    with pytest.raises(ValueError, match="outside the frozen fee window"):
        assert_session_dates_covered(["2025-06-02", "2026-06-02"])
    with pytest.raises(ValueError, match="outside the frozen fee window"):
        assert_session_dates_covered(["2025-05-30"])


def test_the_retail_schedule_charges_no_commission_and_no_exchange_take_fee():
    assert PRIMARY_FEE_SCHEDULE["commission_usd_per_share"] == 0.0
    assert PRIMARY_FEE_SCHEDULE["exchange_take_fee_usd_per_share"] == 0.0


def test_retail_cat_is_excluded_but_never_claimed_to_be_proven_zero():
    """The distinction the correction turns on: excluding an unverified charge is
    honest, asserting it was zero is not."""
    assert PRIMARY_FEE_SCHEDULE["cat_usd_per_share"] == 0.0
    assert PRIMARY_FEE_SCHEDULE["cat_treatment_verified"] is False
    assert "NOT PROVEN ZERO" in PRIMARY_FEE_SCHEDULE["cat_note"]


def test_a_named_cat_inclusive_retail_stress_case_exists():
    assert RETAIL_CAT_STRESS_FEE_SCHEDULE["name"] == (
        "retail_june_2025_with_cat_passthrough"
    )
    assert RETAIL_CAT_STRESS_FEE_SCHEDULE["cat_usd_per_share"] > 0
    # Same account, same rates, differing only in the unverified CAT treatment.
    assert (
        RETAIL_CAT_STRESS_FEE_SCHEDULE["exchange_take_fee_usd_per_share"]
        == PRIMARY_FEE_SCHEDULE["exchange_take_fee_usd_per_share"]
    )
    retail = _round_trip(100.0, 100.0).fees_bps(SCALE, PRIMARY_FEE_SCHEDULE)
    with_cat = _round_trip(100.0, 100.0).fees_bps(SCALE, RETAIL_CAT_STRESS_FEE_SCHEDULE)
    assert with_cat > retail


def test_the_direct_member_case_remains_a_secondary_stress_only():
    assert CONSERVATIVE_FEE_SCHEDULE["role"] == "conservative_stress"
    assert CONSERVATIVE_FEE_SCHEDULE["exchange_take_fee_usd_per_share"] == 0.0030
    assert CONSERVATIVE_FEE_SCHEDULE["clearing_usd_per_share"] > 0
    assert PRIMARY_FEE_SCHEDULE["role"] == "primary"


def test_the_effective_dates_and_rates_are_bound_into_the_plan_hash():
    """Changing a rate or an effective date must move the design hash, or the
    schedule is not really frozen."""
    import hashlib

    from app.services.mbo_stage3_plan import PLAN_DESIGN_ELEMENTS

    joined = "\n".join(PLAN_DESIGN_ELEMENTS)
    assert "sec_section_31=0.0@2025-05-14" in joined
    assert "finra_taf=0.000166/cap8.3@2025-01-01" in joined
    assert "session_window=2025-06-01..2025-06-30" in joined
    assert "retail_cat_treatment=unverified_excluded_with_named_stress" in joined
    assert hashlib.sha256(joined.encode()).hexdigest() == PLAN_DESIGN_HASH


# ---------------------------------------------------------------------------
# Coefficient reproduction, against Stage 2's real per-date semantics
# ---------------------------------------------------------------------------


def _stage2_world(seed: int = 5):
    """Grams plus the numbers Stage 2 would have recorded for them."""
    import numpy as np
    from app.services.mbo_stage2_executor import DESIGN_WIDTH, Gram, delta_r2, sum_grams

    def gram(i, rows):
        rng = np.random.default_rng(seed + i)
        x = rng.standard_normal((rows, DESIGN_WIDTH))
        x[:, 0] = 1.0
        y = 0.3 * x[:, 1] + 0.4 * x[:, 11] + rng.standard_normal(rows)
        g = Gram.zeros(DESIGN_WIDTH)
        g.add_rows(x, y)
        return g

    # Deliberately unequal row counts, so the mean of per-date values and the
    # aggregate-Gram value are genuinely different numbers.
    train_dates = [f"t{i}" for i in range(16)]
    conf_dates = ["c0", "c1", "c2", "c3"]
    grams = {d: gram(i, 400) for i, d in enumerate(train_dates)}
    for i, d in enumerate(conf_dates):
        grams[d] = gram(100 + i, 200 + i * 900)

    alpha = 1.0
    train = sum_grams((grams[d] for d in train_dates), DESIGN_WIDTH)
    per_date = [float(delta_r2(train, grams[d], alpha)) for d in conf_dates]
    return grams, train_dates, conf_dates, alpha, per_date


def test_reproduction_matches_the_mean_of_per_date_confirmation_values():
    import numpy as np
    from app.services.mbo_stage3_executor import reconstruct_confirmation_fit

    grams, train_dates, conf_dates, alpha, per_date = _stage2_world()
    result = reconstruct_confirmation_fit(
        grams, train_dates, conf_dates, alpha,
        recorded_confirmation_delta_r2=float(np.mean(per_date)),
        recorded_per_date_delta_r2=per_date,
    )
    assert result["reproduction_verified"] is True
    assert result["per_date_delta_r2"] == pytest.approx(per_date)
    assert result["mean_delta_r2"] == pytest.approx(float(np.mean(per_date)))
    assert result["confirmation_dates"] == conf_dates


def test_the_mean_and_the_aggregate_gram_are_genuinely_different_numbers():
    """If they were the same, the old check would have been harmless. They are
    not: the aggregate is notional-weighted across dates and the mean is not."""
    import numpy as np
    from app.services.mbo_stage2_executor import (
        DESIGN_WIDTH,
        delta_r2,
        sum_grams,
    )

    grams, train_dates, conf_dates, alpha, per_date = _stage2_world()
    train = sum_grams((grams[d] for d in train_dates), DESIGN_WIDTH)
    aggregate = sum_grams((grams[d] for d in conf_dates), DESIGN_WIDTH)
    aggregate_value = float(delta_r2(train, aggregate, alpha))
    assert abs(float(np.mean(per_date)) - aggregate_value) > 1e-6


def test_reproduction_refuses_a_mismatched_mean():
    from app.services.mbo_stage3_executor import reconstruct_confirmation_fit

    grams, train_dates, conf_dates, alpha, _ = _stage2_world()
    with pytest.raises(ValueError, match="does not reproduce the recorded value"):
        reconstruct_confirmation_fit(
            grams, train_dates, conf_dates, alpha,
            recorded_confirmation_delta_r2=0.42,
        )


def test_reproduction_refuses_a_mismatched_per_date_value():
    from app.services.mbo_stage3_executor import reconstruct_confirmation_fit

    grams, train_dates, conf_dates, alpha, per_date = _stage2_world()
    tampered = list(per_date)
    tampered[2] += 1e-6
    with pytest.raises(ValueError, match="per-date confirmation delta_R2 at position 2"):
        reconstruct_confirmation_fit(
            grams, train_dates, conf_dates, alpha,
            recorded_per_date_delta_r2=tampered,
        )


def test_reproduction_refuses_a_different_number_of_confirmation_dates():
    from app.services.mbo_stage3_executor import reconstruct_confirmation_fit

    grams, train_dates, conf_dates, alpha, per_date = _stage2_world()
    with pytest.raises(ValueError, match="recorded 3 confirmation dates"):
        reconstruct_confirmation_fit(
            grams, train_dates, conf_dates, alpha,
            recorded_per_date_delta_r2=per_date[:3],
        )


def test_the_fit_is_performed_once_from_discovery_plus_validation():
    from app.services.mbo_stage3_executor import reconstruct_confirmation_fit

    grams, train_dates, conf_dates, alpha, _ = _stage2_world()
    result = reconstruct_confirmation_fit(grams, train_dates, conf_dates, alpha)
    assert result["training_dates"] == train_dates
    assert result["alpha"] == alpha
    # No confirmation date may enter the training set.
    assert not set(result["training_dates"]) & set(conf_dates)


# ---------------------------------------------------------------------------
# Insufficient executable sample
# ---------------------------------------------------------------------------


def test_an_unmeasurable_cell_is_not_reported_as_a_negative_finding():
    """The most flattering possible error would be to collapse 'could not be
    executed enough to measure' into 'loses money'. They are separate verdicts."""
    from app.services.mbo_stage3_executor import VERDICT_INSUFFICIENT

    results = [
        {"cell": cell, "latency": PRIMARY_LATENCY, "rule": PRIMARY_RULE,
         "fee_schedule": PRIMARY_FEE_SCHEDULE["name"], "reached_inference": False,
         "trade_count": 12, "p_value": None, "net_return_bps": None}
        for cell in FROZEN_SURVIVORS
    ]
    report = assemble_report(results, {"survivors": list(FROZEN_SURVIVORS)})
    assert report["verdict"] == VERDICT_INSUFFICIENT
    assert report["insufficient_executable_sample"] == list(FROZEN_SURVIVORS)
    assert report["economically_positive_at_primary"] == []
    # It still fails to authorize anything.
    assert report["authorizes_stage4_or_paper"] is False
    assert "NOT a negative-return finding" in report["verdict_meaning"]


def test_a_measured_loss_is_still_reported_as_a_negative_finding():
    from app.services.mbo_stage3_executor import VERDICT_NEGATIVE

    results = [
        {"cell": cell, "latency": PRIMARY_LATENCY, "rule": PRIMARY_RULE,
         "fee_schedule": PRIMARY_FEE_SCHEDULE["name"], "reached_inference": True,
         "p_value": 1e-6, "net_return_bps": -2.0, "clustered_t": -7.0}
        for cell in FROZEN_SURVIVORS
    ]
    report = assemble_report(results, {"survivors": list(FROZEN_SURVIVORS)})
    assert report["verdict"] == VERDICT_NEGATIVE
    assert report["insufficient_executable_sample"] == []
    assert report["authorizes_stage4_or_paper"] is False


def test_a_mixed_family_is_insufficient_not_negative():
    """One cell measurable and losing, three unmeasurable.

    v3 called this negative, which labelled three unmeasured survivors as
    losers on the strength of one that was measured. The frozen precedence now
    says: nothing passed, something was unmeasurable, so the answer is that it
    could not be established.
    """
    from app.services.mbo_stage3_executor import VERDICT_INSUFFICIENT

    results = [
        {"cell": FROZEN_SURVIVORS[0], "latency": PRIMARY_LATENCY, "rule": PRIMARY_RULE,
         "fee_schedule": PRIMARY_FEE_SCHEDULE["name"], "reached_inference": True,
         "p_value": 0.9, "net_return_bps": -1.0, "clustered_t": -0.5},
        *[
            {"cell": cell, "latency": PRIMARY_LATENCY, "rule": PRIMARY_RULE,
             "fee_schedule": PRIMARY_FEE_SCHEDULE["name"], "reached_inference": False,
             "p_value": None, "net_return_bps": None}
            for cell in FROZEN_SURVIVORS[1:]
        ],
    ]
    report = assemble_report(results, {"survivors": list(FROZEN_SURVIVORS)})
    assert report["verdict"] == VERDICT_INSUFFICIENT
    assert len(report["insufficient_executable_sample"]) == 3
    assert report["authorizes_stage4_or_paper"] is False


def test_the_declared_minima_are_recorded_in_the_report():
    results = [
        {"cell": cell, "latency": PRIMARY_LATENCY, "rule": PRIMARY_RULE,
         "fee_schedule": PRIMARY_FEE_SCHEDULE["name"], "reached_inference": False,
         "p_value": None, "net_return_bps": None}
        for cell in FROZEN_SURVIVORS
    ]
    report = assemble_report(results, {"survivors": list(FROZEN_SURVIVORS)})
    assert report["minimum_trades_for_inference"] == 100
    assert report["minimum_session_dates"] == 4


def test_the_minima_are_declared_unlowerable():
    from app.services.mbo_stage3_plan import ECONOMIC_GATES

    assert ECONOMIC_GATES["minimum_trades_for_inference"] == 100
    assert ECONOMIC_GATES["minimum_session_dates"] == 4
    assert "may never be" in ECONOMIC_GATES["minima_are_frozen"]
    assert ECONOMIC_GATES["insufficient_sample_verdict"] == (
        "not_authorized_insufficient_executable_sample"
    )


# ---------------------------------------------------------------------------
# ts_recv: hard refusal, no reorder buffer
# ---------------------------------------------------------------------------


def test_there_is_no_reorder_buffer():
    """Databento guarantees per-symbol ts_recv monotonicity, so a violation is a
    corrupt file and must stay a refusal rather than being papered over."""
    import inspect

    from app.services.mbo_stage3_executor import BookReplay

    source = inspect.getsource(BookReplay)
    assert "reorder" not in source.lower()
    assert "ts_recv went backwards" in source


# ---------------------------------------------------------------------------
# Verdict precedence: an unmeasured survivor is never called negative
# ---------------------------------------------------------------------------


def _primary_row(cell, *, net=None, t=None, p=None, measured=True, schedule=None):
    return {
        "cell": cell,
        "latency": PRIMARY_LATENCY,
        "rule": PRIMARY_RULE,
        "fee_schedule": schedule or PRIMARY_FEE_SCHEDULE["name"],
        "reached_inference": measured,
        "p_value": p,
        "net_return_bps": net,
        "clustered_t": t,
    }


def test_one_measured_loser_and_three_unmeasured_is_insufficient():
    from app.services.mbo_stage3_executor import VERDICT_INSUFFICIENT

    results = [
        _primary_row(FROZEN_SURVIVORS[0], net=-1.0, t=-0.5, p=0.9),
        *[_primary_row(c, measured=False) for c in FROZEN_SURVIVORS[1:]],
    ]
    report = assemble_report(results, {"survivors": list(FROZEN_SURVIVORS)})
    assert report["verdict"] == VERDICT_INSUFFICIENT
    assert set(report["insufficient_executable_sample"]) == set(FROZEN_SURVIVORS[1:])


def test_all_four_measured_and_losing_is_negative():
    from app.services.mbo_stage3_executor import VERDICT_NEGATIVE

    results = [_primary_row(c, net=-2.0, t=-7.0, p=1e-6) for c in FROZEN_SURVIVORS]
    report = assemble_report(results, {"survivors": list(FROZEN_SURVIVORS)})
    assert report["verdict"] == VERDICT_NEGATIVE
    assert report["insufficient_executable_sample"] == []


def test_a_survivor_with_no_primary_row_at_all_counts_as_unmeasured():
    """Missing entirely is not the same as measured-and-flat."""
    from app.services.mbo_stage3_executor import VERDICT_INSUFFICIENT

    results = [_primary_row(FROZEN_SURVIVORS[0], net=-1.0, t=-1.0, p=0.5)]
    report = assemble_report(results, {"survivors": list(FROZEN_SURVIVORS)})
    assert report["verdict"] == VERDICT_INSUFFICIENT
    assert set(report["insufficient_executable_sample"]) == set(FROZEN_SURVIVORS[1:])


def test_a_pass_beats_an_unmeasured_sibling():
    """Precedence is positive first: one genuine pass is still a pass even if
    another survivor could not be measured."""
    from app.services.mbo_stage3_executor import VERDICT_POSITIVE

    results = [
        _primary_row(FROZEN_SURVIVORS[0], net=3.0, t=6.0, p=1e-5),
        *[_primary_row(c, measured=False) for c in FROZEN_SURVIVORS[1:]],
        # CAT-robust, so authorization is not blocked here.
        _primary_row(
            FROZEN_SURVIVORS[0], net=2.0, t=5.0, p=1e-5,
            schedule=RETAIL_CAT_STRESS_FEE_SCHEDULE["name"],
        ),
    ]
    report = assemble_report(results, {"survivors": list(FROZEN_SURVIVORS)})
    assert report["verdict"] == VERDICT_POSITIVE
    assert report["economically_positive_at_primary"] == [FROZEN_SURVIVORS[0]]


# ---------------------------------------------------------------------------
# Authorization under unverified historical CAT
# ---------------------------------------------------------------------------


def test_a_primary_positive_that_dies_under_cat_does_not_authorize():
    """The scientific verdict stands; deployment does not follow from it."""
    from app.services.mbo_stage3_executor import VERDICT_POSITIVE

    results = [
        _primary_row(c, net=1.0, t=5.0, p=1e-5) for c in FROZEN_SURVIVORS
    ] + [
        _primary_row(
            c, net=-0.5, t=-2.0, p=0.2,
            schedule=RETAIL_CAT_STRESS_FEE_SCHEDULE["name"],
        )
        for c in FROZEN_SURVIVORS
    ]
    report = assemble_report(results, {"survivors": list(FROZEN_SURVIVORS)})
    # The CAT stress does not redefine or veto the scientific result ...
    assert report["verdict"] == VERDICT_POSITIVE
    assert report["economically_positive_at_primary"] == list(FROZEN_SURVIVORS)
    # ... but it does block deployment.
    assert report["authorizes_stage4_or_paper"] is False
    assert report["deployment_blocker"] == "unverified_historical_cat_treatment"
    assert report["authorized_survivors"] == []


def test_a_cat_robust_survivor_authorizes_for_itself_only():
    from app.services.mbo_stage3_executor import VERDICT_POSITIVE

    robust, fragile = FROZEN_SURVIVORS[0], FROZEN_SURVIVORS[1]
    results = [
        _primary_row(robust, net=4.0, t=8.0, p=1e-6),
        _primary_row(fragile, net=1.0, t=4.0, p=1e-4),
        *[_primary_row(c, measured=False) for c in FROZEN_SURVIVORS[2:]],
        _primary_row(
            robust, net=2.5, t=6.0, p=1e-5,
            schedule=RETAIL_CAT_STRESS_FEE_SCHEDULE["name"],
        ),
        _primary_row(
            fragile, net=-0.2, t=-1.0, p=0.4,
            schedule=RETAIL_CAT_STRESS_FEE_SCHEDULE["name"],
        ),
    ]
    report = assemble_report(results, {"survivors": list(FROZEN_SURVIVORS)})
    assert report["verdict"] == VERDICT_POSITIVE
    assert set(report["economically_positive_at_primary"]) == {robust, fragile}
    assert report["cat_robust_survivors"] == [robust]
    assert report["authorizes_stage4_or_paper"] is True
    # Authorization is for the robust survivor only, not the whole positive set.
    assert report["authorized_survivors"] == [robust]
    assert report["deployment_blocker"] is None


def test_the_direct_member_stress_controls_no_authorization():
    """Descriptive only: a catastrophic direct-member result cannot block paper
    authorization for a CAT-robust retail positive."""
    cell = FROZEN_SURVIVORS[0]
    results = [
        _primary_row(cell, net=4.0, t=8.0, p=1e-6),
        _primary_row(
            cell, net=3.0, t=7.0, p=1e-6,
            schedule=RETAIL_CAT_STRESS_FEE_SCHEDULE["name"],
        ),
        _primary_row(
            cell, net=-50.0, t=-30.0, p=1e-12,
            schedule=CONSERVATIVE_FEE_SCHEDULE["name"],
        ),
        *[_primary_row(c, measured=False) for c in FROZEN_SURVIVORS[1:]],
    ]
    report = assemble_report(results, {"survivors": list(FROZEN_SURVIVORS)})
    assert report["authorizes_stage4_or_paper"] is True
    assert report["authorized_survivors"] == [cell]
    assert report["cat_stress"]["controls"] == "deployment authorization only"


def test_no_authorization_without_a_positive_verdict():
    """A CAT-viable cell that never passed the primary family authorizes
    nothing."""
    cell = FROZEN_SURVIVORS[0]
    results = [
        _primary_row(cell, net=-1.0, t=-3.0, p=0.01),
        *[_primary_row(c, measured=False) for c in FROZEN_SURVIVORS[1:]],
        _primary_row(
            cell, net=9.0, t=9.0, p=1e-9,
            schedule=RETAIL_CAT_STRESS_FEE_SCHEDULE["name"],
        ),
    ]
    report = assemble_report(results, {"survivors": list(FROZEN_SURVIVORS)})
    assert report["authorizes_stage4_or_paper"] is False
    assert report["cat_robust_survivors"] == []
    # Not a CAT blocker -- there was simply nothing positive to deploy.
    assert report["deployment_blocker"] is None


def test_the_authorization_rules_are_frozen_in_the_plan():
    from app.services.mbo_stage3_plan import AUTHORIZATION_RULES, ECONOMIC_GATES

    assert "may redefine or veto" in AUTHORIZATION_RULES["scientific_verdict_source"]
    assert AUTHORIZATION_RULES["if_no_primary_positive_survivor_is_cat_robust"][
        "deployment_blocker"
    ] == "unverified_historical_cat_treatment"
    assert AUTHORIZATION_RULES["if_at_least_one_is_cat_robust"][
        "authorizes_stage4_or_paper"
    ] is True
    assert "descriptive only" in AUTHORIZATION_RULES["direct_member_stress_role"]
    assert "never called" in ECONOMIC_GATES["verdict_precedence"]


# ---------------------------------------------------------------------------
# The wired run
# ---------------------------------------------------------------------------


def test_the_grid_covers_every_cell_latency_rule_and_schedule():
    from app.services.mbo_stage3_executor import make_sinks

    sinks = make_sinks(SCALE)
    assert len(sinks) == 4 * 3 * 2 * 3
    cells = {key[0] for key in sinks}
    assert cells == set(FROZEN_SURVIVORS)


def test_query_instants_cover_every_rung_and_nothing_else():
    import numpy as np
    from app.services.mbo_stage3_executor import query_instants

    decision = np.array([1_000_000_000], dtype=np.int64)
    resolution = np.array([1_000_000_000 + 5 * SCALE], dtype=np.int64)
    usable = np.array([True])
    instants = query_instants(decision, resolution, usable)
    expected = {int(decision[0])}
    for _, latency in LATENCY_RUNGS:
        expected.add(int(decision[0]) + latency)
        expected.add(int(resolution[0]) + latency)
    assert set(instants) == expected
    assert instants == sorted(instants)


def test_unusable_rows_contribute_no_query_instants():
    import numpy as np
    from app.services.mbo_stage3_executor import query_instants

    decision = np.array([1, 2], dtype=np.int64)
    resolution = np.array([100, 200], dtype=np.int64)
    instants = query_instants(decision, resolution, np.array([False, False]))
    assert instants == []


def test_predict_applies_the_frozen_beta_without_rescaling():
    import numpy as np
    from app.services.mbo_stage3_executor import predict

    design = np.array([[1.0, 2.0, 3.0], [1.0, 0.0, -1.0]])
    beta = np.array([0.5, 1.0, -2.0])
    np.testing.assert_allclose(predict(design, beta), design @ beta)


def test_the_run_command_is_still_gated():
    """Wired is not the same as authorized."""
    import argparse

    from app.cli.mbo_stage3 import run

    args = argparse.Namespace(
        i_have_reviewed_the_design=False,
        stage2_results="x.json", grams_dir="g", features_dir="f",
        labels_dir="l", raw_dir="r", output_dir="o",
    )
    with pytest.raises(ValueError, match="not authorized yet"):
        run(args)


# ---------------------------------------------------------------------------
# Out-of-sample only
# ---------------------------------------------------------------------------


def test_the_authorized_run_takes_no_subset_limit():
    """An option to run a subset is an option to peek at part of the answer."""
    from app.cli.mbo_stage3 import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([
            "run", "--stage2-results", "r.json", "--grams-dir", "g",
            "--features-dir", "f", "--labels-dir", "l", "--raw-dir", "raw",
            "--limit", "1",
        ])


def test_the_diagnostic_command_cannot_produce_an_economic_result():
    """It exists so a subset run has somewhere to live that is incapable of
    answering the primary question."""
    import inspect

    from app.cli.mbo_stage3 import diagnose

    source = inspect.getsource(diagnose)
    assert '"contains_economic_result": False' in source
    # Inspect the executable body, not the prose explaining why it is absent.
    body = source.split('"""')[2]
    for forbidden in ("assemble_report", "net_return_bps", "verdict", "win_rate"):
        assert forbidden not in body, forbidden


def test_only_confirmation_dates_are_evaluated():
    """The fit is trained on the first sixteen dates; scoring economics there
    would put 16/20 of the report in sample."""
    import inspect

    from app.cli.mbo_stage3 import run

    source = inspect.getsource(run)
    assert 'context["confirmation"]' in source
    assert 'context["training"]' not in source.split("_evaluate_block")[1].split(")")[0]


def test_no_discovery_or_validation_date_can_enter_an_accumulator(monkeypatch, tmp_path):
    """The decisive test: whatever dates exist, the economic block is the four
    confirmation dates and nothing else."""
    from app.services.mbo_stage2_executor import split_dates

    dates = [f"2025-06-{d:02d}" for d in range(2, 22)]
    blocks = split_dates(dates)
    evaluated: list[str] = []

    import app.cli.mbo_stage3 as cli

    context = {
        "frozen": {"survivors": list(FROZEN_SURVIVORS)},
        "fits": {},
        "deciles": {},
        "blocks": blocks,
        "confirmation": blocks["confirmation"],
        "training": blocks["discovery"] + blocks["validation"],
        "by_date": {d: [] for d in dates},
        "features_dir": tmp_path,
        "labels_dir": tmp_path,
        "raw_dir": tmp_path,
        "economic": True,
    }

    def fake_prepare(args, *, economic):
        return context

    def fake_evaluate(ctx, session_dates, *, verify_hash=True):
        evaluated.extend(session_dates)
        return {}, [], {}

    monkeypatch.setattr(cli, "_prepare", fake_prepare)
    monkeypatch.setattr(cli, "_evaluate_block", fake_evaluate)
    monkeypatch.setattr(
        cli, "assemble_report", lambda *a, **k: {}, raising=False
    )

    import argparse

    args = argparse.Namespace(
        i_have_reviewed_the_design=True, output_dir=str(tmp_path),
        stage2_results="x", grams_dir="g", features_dir=str(tmp_path),
        labels_dir=str(tmp_path), raw_dir=str(tmp_path),
    )
    cli.run(args)

    assert evaluated == blocks["confirmation"]
    assert len(evaluated) == 4
    for date in blocks["discovery"] + blocks["validation"]:
        assert date not in evaluated


# ---------------------------------------------------------------------------
# Raw input resolved and hashed through Stage-1 provenance
# ---------------------------------------------------------------------------


def _write_raw(directory, name, payload=b"certified-bytes"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(payload)
    return path


def _manifest_for(path):
    from app.services.mbo_stage3_executor import sha256_file

    return {
        "source": {
            "filename": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    }


def test_the_raw_file_comes_from_the_manifest_not_from_the_stem(tmp_path):
    """Stage-1 recorded what it opened; Stage 3 must not guess a convention."""
    from app.services.mbo_stage3_executor import resolve_raw_source

    raw = tmp_path / "raw"
    # A realistic Databento name, which no stem-based rule would reproduce.
    actual = _write_raw(raw, "xnas-itch-20250602.mbo.dbn.zst")
    _write_raw(raw, "AAPL_2025-06-02.mbo.dbn.zst", b"the-wrong-file")

    resolved = resolve_raw_source(
        _manifest_for(actual), raw, stem="AAPL_2025-06-02"
    )
    assert resolved == actual


def test_a_missing_raw_source_is_refused(tmp_path):
    from app.services.mbo_stage3_executor import resolve_raw_source

    raw = tmp_path / "raw"
    raw.mkdir()
    manifest = {"source": {"filename": "absent.dbn.zst", "bytes": 1, "sha256": "x"}}
    with pytest.raises(ValueError, match="not found under"):
        resolve_raw_source(manifest, raw, stem="AAPL_2025-06-02")


def test_an_ambiguous_raw_source_is_refused(tmp_path):
    from app.services.mbo_stage3_executor import resolve_raw_source

    raw = tmp_path / "raw"
    first = _write_raw(raw / "a", "xnas-itch-20250602.mbo.dbn.zst")
    _write_raw(raw / "b", "xnas-itch-20250602.mbo.dbn.zst")
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_raw_source(_manifest_for(first), raw, stem="AAPL_2025-06-02")


def test_a_size_mismatch_is_refused(tmp_path):
    from app.services.mbo_stage3_executor import resolve_raw_source

    raw = tmp_path / "raw"
    path = _write_raw(raw, "xnas-itch-20250602.mbo.dbn.zst")
    manifest = _manifest_for(path)
    manifest["source"]["bytes"] += 1
    with pytest.raises(ValueError, match="bytes, Stage 1 recorded"):
        resolve_raw_source(manifest, raw, stem="AAPL_2025-06-02")


def test_a_sha256_mismatch_is_refused(tmp_path):
    """Same name, same size, different bytes -- the case a size check misses."""
    from app.services.mbo_stage3_executor import resolve_raw_source

    raw = tmp_path / "raw"
    path = _write_raw(raw, "xnas-itch-20250602.mbo.dbn.zst", b"AAAAAAAA")
    manifest = _manifest_for(path)
    path.write_bytes(b"BBBBBBBB")  # identical length
    with pytest.raises(ValueError, match="does not match the Stage-1 SHA-256"):
        resolve_raw_source(manifest, raw, stem="AAPL_2025-06-02")


def test_a_manifest_with_no_source_is_refused(tmp_path):
    from app.services.mbo_stage3_executor import resolve_raw_source

    with pytest.raises(ValueError, match="will not guess"):
        resolve_raw_source({}, tmp_path, stem="AAPL_2025-06-02")


# ---------------------------------------------------------------------------
# Feature / label re-certification
# ---------------------------------------------------------------------------


def _frozen_batch_manifest():
    from app.services.mbo_feature_engine import (
        FEATURE_ENGINE_VERSION,
        FEATURE_SEMANTICS_HASH,
        FEATURE_VOCABULARY_HASH,
    )

    return {
        "definitions": {
            "feature_engine_version": FEATURE_ENGINE_VERSION,
            "feature_semantics_hash": FEATURE_SEMANTICS_HASH,
            "feature_vocabulary_hash": FEATURE_VOCABULARY_HASH,
        },
        "feature_semantics_consistent": True,
    }


def test_a_frozen_feature_batch_is_accepted():
    from app.services.mbo_stage3_executor import assert_feature_batch_is_frozen

    assert_feature_batch_is_frozen(_frozen_batch_manifest())


@pytest.mark.parametrize(
    "key", ["feature_engine_version", "feature_semantics_hash", "feature_vocabulary_hash"]
)
def test_a_stale_feature_batch_is_refused(key):
    from app.services.mbo_stage3_executor import assert_feature_batch_is_frozen

    manifest = _frozen_batch_manifest()
    manifest["definitions"][key] = "stale"
    with pytest.raises(ValueError, match=key):
        assert_feature_batch_is_frozen(manifest)


def test_an_inconsistent_feature_batch_is_refused():
    from app.services.mbo_stage3_executor import assert_feature_batch_is_frozen

    manifest = _frozen_batch_manifest()
    manifest["feature_semantics_consistent"] = False
    with pytest.raises(ValueError, match="not semantics-consistent"):
        assert_feature_batch_is_frozen(manifest)


def test_misaligned_labels_are_refused():
    import numpy as np
    from app.services.mbo_stage3_executor import assert_labels_align

    features = np.arange(10, dtype=np.int64)
    assert_labels_align("AAPL_2025-06-02", "50ev", features, features.copy())
    with pytest.raises(ValueError, match="do not align one-for-one"):
        assert_labels_align("AAPL_2025-06-02", "50ev", features, features[:-1])
    shuffled = features.copy()
    shuffled[3], shuffled[4] = shuffled[4], shuffled[3]
    with pytest.raises(ValueError, match="do not align one-for-one"):
        assert_labels_align("AAPL_2025-06-02", "50ev", features, shuffled)


# ---------------------------------------------------------------------------
# Nullable event-horizon availability
# ---------------------------------------------------------------------------


def test_null_availability_on_non_ok_labels_stays_none():
    """The bug a wholesale int64 cast would introduce: a null becomes a real
    timestamp, arithmetically valid and silently wrong."""
    import numpy as np
    import pyarrow as pa
    from app.services.mbo_stage3_executor import event_horizon_availability

    status = np.array(["ok", "session_end_before_horizon", "ok", "no_further_midpoint_change"])
    column = pa.array([1_000, None, 2_000, None], pa.int64())
    assert event_horizon_availability(status, column) == [1_000, None, 2_000, None]


def test_a_non_null_value_on_a_non_ok_row_is_still_discarded():
    """Status governs. A stale value under a non-OK status is not a resolution."""
    import numpy as np
    import pyarrow as pa
    from app.services.mbo_stage3_executor import event_horizon_availability

    status = np.array(["ok", "session_end_before_horizon"])
    column = pa.array([1_000, 9_999], pa.int64())
    assert event_horizon_availability(status, column) == [1_000, None]


def test_a_null_availability_yields_no_trade_rather_than_a_fabricated_exit():
    trade, reason = evaluate_candidate(
        predicted_bps=500.0,
        decision_ts=1_000_000_000,
        exit_resolution_ts=None,
        latency_ns=LATENCY["250ms"],
        book_at=static_book_at(100.00, 100.02),
        price_scale=SCALE,
        schedule=PRIMARY_FEE_SCHEDULE,
    )
    assert trade is None and reason == NO_TRADE_UNRESOLVED_TARGET


# ---------------------------------------------------------------------------
# The discovery decile, wired
# ---------------------------------------------------------------------------


def test_the_decile_threshold_is_the_frozen_quantile_of_absolute_predictions():
    import numpy as np
    from app.services.mbo_stage3_executor import discovery_decile_threshold
    from app.services.mbo_stage3_plan import DISCOVERY_DECILE_QUANTILE

    values = list(np.arange(1.0, 101.0))
    threshold = discovery_decile_threshold(values)
    assert threshold == pytest.approx(np.quantile(values, DISCOVERY_DECILE_QUANTILE))


def test_the_decile_ignores_non_finite_predictions():
    import numpy as np
    from app.services.mbo_stage3_executor import discovery_decile_threshold

    assert discovery_decile_threshold([np.nan, np.inf, 1.0, 2.0]) is not None
    assert discovery_decile_threshold([np.nan, np.inf]) is None
    assert discovery_decile_threshold([]) is None


def test_a_wired_decile_threshold_actually_admits_trades():
    """With the threshold unwired the secondary rule took zero trades, which
    would have reported it as producing nothing rather than as never running."""
    common = {
        "decision_ts": 1_000_000_000,
        "exit_resolution_ts": 1_000_000_000 + 5 * SCALE,
        "latency_ns": LATENCY["250ms"],
        "book_at": static_book_at(100.00, 100.02),
        "price_scale": SCALE,
        "schedule": PRIMARY_FEE_SCHEDULE,
        "rule": SECONDARY_RULE,
    }
    unwired, reason = evaluate_candidate(
        predicted_bps=9.0, decile_threshold_bps=None, **common
    )
    assert unwired is None and reason == NO_TRADE_BELOW_HURDLE
    wired, reason = evaluate_candidate(
        predicted_bps=9.0, decile_threshold_bps=5.0, **common
    )
    assert reason is None and wired is not None


def test_the_decile_is_calibrated_on_discovery_only():
    from app.services.mbo_stage3_plan import DECILE_CALIBRATION_RULES

    assert DECILE_CALIBRATION_RULES["block"] == "discovery"
    assert DECILE_CALIBRATION_RULES["uses_outcomes"] is False


# ---------------------------------------------------------------------------
# The common factor, wired
# ---------------------------------------------------------------------------


def test_the_common_factor_is_equal_weighted_across_symbols():
    from app.services.mbo_stage3_executor import common_factor_by_date

    factor = common_factor_by_date([
        ("2025-06-18", "AAAA", 10.0),
        ("2025-06-18", "BBBB", 20.0),
        ("2025-06-18", "CCCC", 30.0),
        ("2025-06-19", "AAAA", -4.0),
    ])
    assert factor["2025-06-18"] == pytest.approx(20.0)
    assert factor["2025-06-19"] == pytest.approx(-4.0)


def test_a_symbol_day_with_no_usable_midpoints_is_omitted_not_zeroed():
    """A missing observation is not a flat one; averaging it in as zero would
    drag the factor toward the mean."""
    import numpy as np
    from app.services.mbo_stage3_executor import (
        common_factor_by_date,
        session_return_bps,
    )

    assert session_return_bps(np.array([np.nan, np.nan])) is None
    factor = common_factor_by_date([
        ("2025-06-18", "AAAA", 10.0),
        ("2025-06-18", "BBBB", None),
    ])
    assert factor["2025-06-18"] == pytest.approx(10.0)


def test_session_return_is_first_to_last_midpoint_in_bps():
    import numpy as np
    from app.services.mbo_stage3_executor import session_return_bps

    assert session_return_bps(np.array([100.0, 999.0, 101.0])) == pytest.approx(100.0)
    assert session_return_bps(np.array([100.0])) is None


def test_the_common_factor_definition_is_frozen():
    from app.services.mbo_stage3_plan import COMMON_FACTOR

    assert COMMON_FACTOR["name"] == "equal_weighted_cross_symbol_session_return_bps"
    assert COMMON_FACTOR["cadence"] == "50ev"
    assert COMMON_FACTOR["declared_before_any_economic_outcome"] == "true"


def test_the_run_passes_the_factor_into_the_summaries():
    """factor_beta existed but was never fed; the promise was unkept."""
    import inspect

    from app.cli.mbo_stage3 import run

    assert "summarize(sinks, market)" in inspect.getsource(run)
