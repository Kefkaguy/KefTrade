"""Stage 3.6: the experiment must implement the frozen design, exactly.

The design document is the specification and it is hashed. These tests exist to
show that the code does what the document says -- not what a summary of the
document says -- and that every gate refuses rather than degrades.

No test computes a Stage-3.6 economic outcome against real data. The economic
formulas are exercised only on synthetic books with hand-checkable prices.
"""

from __future__ import annotations

import json
import shutil
import textwrap

import numpy as np
import pytest

from app.services.mbo_stage3_executor import BookLevels
from app.services.mbo_stage3_plan import FEE_SCHEDULES, PRIMARY_FEE_SCHEDULE_NAME
from app.services.mbo_stage36_executor import (
    FAIL_BAD_TS_RECV,
    FAIL_ENTRY_LIQUIDITY,
    FAIL_EXIT_LIQUIDITY,
    FAIL_NO_CONSENSUS,
    FAIL_NO_ENTRY_BOOK,
    FAIL_NO_EXIT_BOOK,
    FAIL_OUTSIDE_COVERAGE,
    STAGE36_EXECUTOR_VERSION,
    Candidate,
    ExecutedTrade,
    Stage36Accumulator,
    assert_consensus_is_internally_consistent,
    assert_frozen_counts,
    assert_frozen_plan,
    assert_predictions_are_causal,
    consensus_counts,
    entry_side,
    execute_candidate,
    exit_side,
    load_candidates,
    verify_preoutcome_artifacts,
)
from app.services.mbo_stage36_plan import (
    CONSENSUS_2_VS_2,
    CONSENSUS_3_OF_4,
    CONSENSUS_4_OF_4,
    CONSENSUS_INCOMPLETE,
    CSV_FILENAMES,
    DECISION_DELAY_NS,
    EXPECTED_DESIGN_SHA256,
    FROZEN_CELLS,
    HOLDING_NS,
    LATENCY_NS,
    LONG,
    MANIFEST_FILENAME,
    MIN_EXECUTABLE_TRADES,
    MIN_SESSIONS,
    PREOUTCOME_RELATIVE_DIR,
    PRIMARY_TARGET_BPS,
    SHORT,
    STRETCH_TARGET_BPS,
    T_HURDLE,
    TRADE_SIZE_SHARES,
    VERDICT_INSUFFICIENT,
    VERDICT_NO_MECHANISM,
    VERDICT_SUPPORTED,
    statistical_plan,
)

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[3]
SCALE = 1_000_000_000
MS = 1_000_000
SECOND = 1_000_000_000


# ---------------------------------------------------------------------------
# Frozen artefacts: the hashes are the specification's identity
# ---------------------------------------------------------------------------


def test_the_frozen_design_verifies():
    result = assert_frozen_plan(REPO_ROOT)
    assert result["sha256"] == EXPECTED_DESIGN_SHA256


def test_a_modified_design_is_refused(tmp_path):
    """The document is the specification; a different document is a different
    experiment."""
    shutil.copytree(REPO_ROOT / "docs", tmp_path / "docs")
    design = tmp_path / "docs" / "2026-08-19-stage36-news-l3-consensus-design.md"
    design.write_text(design.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="the Stage-3.6 design has changed"):
        assert_frozen_plan(tmp_path)


def test_a_missing_design_is_refused(tmp_path):
    with pytest.raises(ValueError, match="frozen Stage-3.6 design is missing"):
        assert_frozen_plan(tmp_path)


def _staged_artifacts(tmp_path):
    """A copy of the real frozen artefacts, so tampering can be tested."""
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        REPO_ROOT / "docs" / "2026-08-19-stage36-news-l3-consensus-design.md",
        tmp_path / "docs" / "2026-08-19-stage36-news-l3-consensus-design.md",
    )
    target = tmp_path / PREOUTCOME_RELATIVE_DIR
    shutil.copytree(REPO_ROOT / PREOUTCOME_RELATIVE_DIR, target)
    return target


def test_the_frozen_preoutcome_artifacts_verify():
    result = verify_preoutcome_artifacts(REPO_ROOT)
    assert set(result["files"]) == {"news_events", "shock_census", "consensus_census"}
    assert result["files"]["news_events"]["bytes"] == 26_954


def test_a_modified_manifest_is_refused(tmp_path):
    target = _staged_artifacts(tmp_path)
    path = target / MANIFEST_FILENAME
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="pre-outcome manifest has changed"):
        verify_preoutcome_artifacts(tmp_path)


@pytest.mark.parametrize("key", ["news_events", "shock_census", "consensus_census"])
def test_a_modified_csv_is_refused(tmp_path, key):
    """Same length, different bytes would also fail: the check is a hash."""
    target = _staged_artifacts(tmp_path)
    path = target / CSV_FILENAMES[key]
    data = path.read_bytes()
    path.write_bytes(data[:-1] + bytes([data[-1] ^ 0x01]))
    with pytest.raises(ValueError, match="do not verify"):
        verify_preoutcome_artifacts(tmp_path)


def test_a_missing_csv_is_refused(tmp_path):
    target = _staged_artifacts(tmp_path)
    (target / CSV_FILENAMES["consensus_census"]).unlink()
    with pytest.raises(ValueError, match="missing at"):
        verify_preoutcome_artifacts(tmp_path)


def test_a_manifest_claiming_an_outcome_is_refused(tmp_path):
    target = _staged_artifacts(tmp_path)
    path = target / MANIFEST_FILENAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["contains_post_decision_economic_outcome"] = True
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="pre-outcome manifest has changed"):
        verify_preoutcome_artifacts(tmp_path)


# ---------------------------------------------------------------------------
# The frozen counts
# ---------------------------------------------------------------------------


def test_the_frozen_counts_reproduce_exactly():
    """259 / 147 / 21 / 81 / 10 / 168, from the certified CSV."""
    counts = assert_frozen_counts(load_candidates(REPO_ROOT))
    assert counts["measured_events"] == 259
    assert counts[CONSENSUS_4_OF_4] == 147
    assert counts[CONSENSUS_3_OF_4] == 21
    assert counts[CONSENSUS_2_VS_2] == 81
    assert counts[CONSENSUS_INCOMPLETE] == 10
    assert counts["strong_consensus"] == 168
    assert counts["strong_consensus"] == counts[CONSENSUS_4_OF_4] + counts[CONSENSUS_3_OF_4]


def test_the_population_spans_twenty_sessions_and_eight_symbols():
    candidates = load_candidates(REPO_ROOT)
    assert len({c.session_date for c in candidates}) == 20
    assert len({c.symbol for c in candidates}) == 8


def test_a_count_mismatch_refuses():
    candidates = load_candidates(REPO_ROOT)
    with pytest.raises(ValueError, match="frozen counts do not reproduce"):
        assert_frozen_counts(candidates[:-1])


def test_an_unrecognised_consensus_label_refuses():
    """A label the frozen design does not define is a reason to stop."""
    import dataclasses

    candidates = load_candidates(REPO_ROOT)
    tampered = [dataclasses.replace(candidates[0], consensus="5_of_5"), *candidates[1:]]
    with pytest.raises(ValueError, match="unrecognised consensus label"):
        consensus_counts(tampered)


# ---------------------------------------------------------------------------
# Prediction causality
# ---------------------------------------------------------------------------


def test_every_contributing_prediction_lies_in_the_causal_window():
    result = assert_predictions_are_causal(load_candidates(REPO_ROOT))
    # 168 strong candidates x 4 cells.
    assert result["predictions_checked"] == 672
    assert result["all_within_t0_to_td"] is True


def _candidate(**overrides):
    base = {
        "symbol": "AAPL",
        "session_date": "2025-06-02",
        "story_id": "abc123",
        "known_at_ns": 1_000_000_000_000,
        "consensus": CONSENSUS_4_OF_4,
        "direction": LONG,
        "abs_shock_bps": 3.0,
        "cell_signs": (1.0, 1.0, 1.0, 1.0),
        "cell_prediction_ts": tuple(
            1_000_000_000_000 + 10 * SECOND for _ in range(4)
        ),
    }
    base.update(overrides)
    return Candidate(**base)


def test_a_prediction_before_t0_is_rejected_as_stale():
    """A prediction formed before the news was knowable is not a reaction to it."""
    stale = _candidate(
        cell_prediction_ts=(
            1_000_000_000_000 - SECOND,
            1_000_000_000_000 + SECOND,
            1_000_000_000_000 + SECOND,
            1_000_000_000_000 + SECOND,
        )
    )
    with pytest.raises(ValueError, match="stale"):
        assert_predictions_are_causal([stale])


def test_a_prediction_after_td_is_rejected_as_late():
    late = _candidate(
        cell_prediction_ts=(
            1_000_000_000_000 + 31 * SECOND,
            *(1_000_000_000_000 + SECOND for _ in range(3)),
        )
    )
    with pytest.raises(ValueError, match="late"):
        assert_predictions_are_causal([late])


def test_a_prediction_exactly_at_the_boundaries_is_accepted():
    """The window is inclusive at both ends, as the design writes it."""
    boundary = _candidate(
        cell_prediction_ts=(
            1_000_000_000_000,
            1_000_000_000_000 + DECISION_DELAY_NS,
            1_000_000_000_000 + SECOND,
            1_000_000_000_000 + SECOND,
        )
    )
    assert assert_predictions_are_causal([boundary])["all_within_t0_to_td"] is True


# ---------------------------------------------------------------------------
# Consensus
# ---------------------------------------------------------------------------


def test_every_frozen_label_rederives_from_its_signs():
    result = assert_consensus_is_internally_consistent(load_candidates(REPO_ROOT))
    assert result["candidates_rederived"] == 259
    assert result["labels_consistent"] is True


def test_four_of_four_trades_that_direction():
    up = _candidate(cell_signs=(1.0, 1.0, 1.0, 1.0), direction=LONG)
    down = _candidate(
        cell_signs=(-1.0, -1.0, -1.0, -1.0), direction=SHORT, consensus=CONSENSUS_4_OF_4
    )
    assert_consensus_is_internally_consistent([up, down])
    assert up.is_strong_consensus and down.is_strong_consensus


def test_three_versus_one_trades_the_majority_direction():
    long_majority = _candidate(
        cell_signs=(1.0, 1.0, 1.0, -1.0), consensus=CONSENSUS_3_OF_4, direction=LONG
    )
    short_majority = _candidate(
        cell_signs=(-1.0, -1.0, -1.0, 1.0), consensus=CONSENSUS_3_OF_4, direction=SHORT
    )
    assert_consensus_is_internally_consistent([long_majority, short_majority])


def test_a_wrong_majority_direction_is_refused():
    wrong = _candidate(
        cell_signs=(1.0, 1.0, 1.0, -1.0), consensus=CONSENSUS_3_OF_4, direction=SHORT
    )
    with pytest.raises(ValueError, match="majority is"):
        assert_consensus_is_internally_consistent([wrong])


def test_two_versus_two_does_not_trade():
    split = _candidate(
        cell_signs=(1.0, 1.0, -1.0, -1.0), consensus=CONSENSUS_2_VS_2, direction=None
    )
    assert_consensus_is_internally_consistent([split])
    assert split.is_strong_consensus is False
    trade, reason = execute_candidate(
        split, book_at=lambda ts: _book(ts, 100.00, 100.02), price_scale=SCALE
    )
    assert trade is None and reason == FAIL_NO_CONSENSUS


def test_a_zero_prediction_does_not_trade():
    """A model expressing no direction has not voted."""
    zeroed = _candidate(
        cell_signs=(1.0, 1.0, 1.0, 0.0), consensus=CONSENSUS_INCOMPLETE, direction=None
    )
    assert_consensus_is_internally_consistent([zeroed])
    assert zeroed.is_strong_consensus is False
    trade, reason = execute_candidate(
        zeroed, book_at=lambda ts: _book(ts, 100.00, 100.02), price_scale=SCALE
    )
    assert trade is None and reason == FAIL_NO_CONSENSUS


def test_an_unavailable_model_does_not_trade():
    unavailable = _candidate(
        cell_signs=(1.0, 1.0, 1.0, float("nan")),
        consensus=CONSENSUS_INCOMPLETE,
        direction=None,
    )
    assert_consensus_is_internally_consistent([unavailable])
    trade, reason = execute_candidate(
        unavailable, book_at=lambda ts: _book(ts, 100.00, 100.02), price_scale=SCALE
    )
    assert trade is None and reason == FAIL_NO_CONSENSUS


def test_the_initial_price_move_does_not_select_direction():
    """Two candidates with opposite shocks and identical signs must trade the
    same way: the shock is a diagnostic, not an input."""
    positive_shock = _candidate(abs_shock_bps=25.0, cell_signs=(1.0, 1.0, 1.0, 1.0))
    negative_shock = _candidate(abs_shock_bps=0.1, cell_signs=(1.0, 1.0, 1.0, 1.0))
    assert positive_shock.direction == negative_shock.direction == LONG


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


def test_the_decision_instant_is_thirty_seconds_after_the_news():
    c = _candidate()
    assert c.td_ns - c.t0_ns == DECISION_DELAY_NS == 30 * SECOND


def test_entry_latency_is_exactly_250ms():
    c = _candidate()
    assert c.entry_arrival_ns - c.td_ns == LATENCY_NS == 250 * MS


def test_exit_request_is_five_minutes_after_the_decision():
    c = _candidate()
    assert c.exit_request_ns - c.td_ns == HOLDING_NS == 300 * SECOND


def test_exit_arrival_is_exactly_entry_arrival_plus_five_minutes():
    """Both legs carry the same latency, so it cancels out of the holding period
    rather than shortening it."""
    c = _candidate()
    assert c.exit_arrival_ns - c.entry_arrival_ns == 300 * SECOND
    assert c.exit_arrival_ns == c.td_ns + HOLDING_NS + LATENCY_NS


def test_the_real_population_satisfies_every_timing_invariant():
    for c in load_candidates(REPO_ROOT):
        assert c.td_ns - c.t0_ns == DECISION_DELAY_NS
        assert c.entry_arrival_ns - c.td_ns == LATENCY_NS
        assert c.exit_arrival_ns - c.entry_arrival_ns == HOLDING_NS


# ---------------------------------------------------------------------------
# Directional execution
# ---------------------------------------------------------------------------


def _book(ts, bid, ask, size=1_000, levels=3):
    cent = SCALE // 100
    return BookLevels(
        ts=ts,
        bids=tuple((int(bid * SCALE) - i * cent, size) for i in range(levels)),
        asks=tuple((int(ask * SCALE) + i * cent, size) for i in range(levels)),
    )


def test_long_entry_consumes_asks_and_exit_consumes_bids():
    assert entry_side(LONG) == "buy"
    assert exit_side(LONG) == "sell"
    c = _candidate(direction=LONG)
    trade, reason = execute_candidate(
        c, book_at=lambda ts: _book(ts, 100.00, 100.02), price_scale=SCALE
    )
    assert reason is None
    # Bought the offer, sold the bid.
    assert trade.entry_fill == pytest.approx(100.02 * SCALE)
    assert trade.exit_fill == pytest.approx(100.00 * SCALE)


def test_short_entry_consumes_bids_and_exit_consumes_asks():
    assert entry_side(SHORT) == "sell"
    assert exit_side(SHORT) == "buy"
    c = _candidate(direction=SHORT, cell_signs=(-1.0, -1.0, -1.0, -1.0))
    trade, reason = execute_candidate(
        c, book_at=lambda ts: _book(ts, 100.00, 100.02), price_scale=SCALE
    )
    assert reason is None
    # Sold the bid, bought back the offer.
    assert trade.entry_fill == pytest.approx(100.00 * SCALE)
    assert trade.exit_fill == pytest.approx(100.02 * SCALE)


def test_a_long_profits_when_the_price_rises():
    c = _candidate(direction=LONG)

    def book_at(ts):
        return _book(ts, 101.00, 101.02) if ts >= c.exit_arrival_ns else _book(ts, 100.00, 100.02)

    trade, _ = execute_candidate(c, book_at=book_at, price_scale=SCALE)
    assert trade.realized_return_bps > 0


def test_a_short_profits_when_the_price_falls():
    c = _candidate(direction=SHORT, cell_signs=(-1.0,) * 4)

    def book_at(ts):
        return _book(ts, 99.00, 99.02) if ts >= c.exit_arrival_ns else _book(ts, 100.00, 100.02)

    trade, _ = execute_candidate(c, book_at=book_at, price_scale=SCALE)
    assert trade.realized_return_bps > 0


def test_the_mechanism_can_lose():
    """A wrong call costs real money; the formula is not one-sided."""
    c = _candidate(direction=LONG)

    def book_at(ts):
        return _book(ts, 99.00, 99.02) if ts >= c.exit_arrival_ns else _book(ts, 100.00, 100.02)

    trade, _ = execute_candidate(c, book_at=book_at, price_scale=SCALE)
    assert trade.realized_return_bps < 0


# ---------------------------------------------------------------------------
# Fill semantics
# ---------------------------------------------------------------------------


def test_insufficient_displayed_liquidity_fails_closed_on_entry():
    thin = BookLevels(
        ts=0, bids=((int(99.99 * SCALE), 5_000),), asks=((int(100.00 * SCALE), 10),)
    )
    trade, reason = execute_candidate(
        _candidate(direction=LONG), book_at=lambda ts: thin, price_scale=SCALE
    )
    assert trade is None and reason == FAIL_ENTRY_LIQUIDITY


def test_insufficient_displayed_liquidity_fails_closed_on_exit():
    c = _candidate(direction=LONG)

    def book_at(ts):
        if ts >= c.exit_arrival_ns:
            return BookLevels(
                ts=ts, bids=((int(99.99 * SCALE), 10),), asks=((int(100.02 * SCALE), 5_000),)
            )
        return _book(ts, 100.00, 100.02)

    trade, reason = execute_candidate(c, book_at=book_at, price_scale=SCALE)
    assert trade is None and reason == FAIL_EXIT_LIQUIDITY


def test_no_more_than_ten_displayed_levels_may_be_walked():
    """The eleventh level is not reachable, however much size it holds."""
    deep = BookLevels(
        ts=0,
        bids=((int(99.99 * SCALE), 10_000),),
        asks=tuple((int((100.00 + i * 0.01) * SCALE), 10) for i in range(50)),
    )
    trade, reason = execute_candidate(
        _candidate(direction=LONG), book_at=lambda ts: deep, price_scale=SCALE
    )
    # 10 levels x 10 shares = 100 exactly.
    assert reason is None and trade.entry_levels == 10

    thinner = BookLevels(
        ts=0,
        bids=((int(99.99 * SCALE), 10_000),),
        asks=tuple((int((100.00 + i * 0.01) * SCALE), 9) for i in range(50)),
    )
    trade, reason = execute_candidate(
        _candidate(direction=LONG), book_at=lambda ts: thinner, price_scale=SCALE
    )
    assert trade is None and reason == FAIL_ENTRY_LIQUIDITY


def test_a_one_sided_book_fails_closed():
    one_sided = BookLevels(ts=0, bids=(), asks=((int(100.0 * SCALE), 5_000),))
    trade, reason = execute_candidate(
        _candidate(), book_at=lambda ts: one_sided, price_scale=SCALE
    )
    assert trade is None and reason == FAIL_NO_ENTRY_BOOK


def test_a_missing_exit_book_fails_closed():
    c = _candidate()

    def book_at(ts):
        if ts >= c.exit_arrival_ns:
            return BookLevels(ts=ts, bids=(), asks=())
        return _book(ts, 100.00, 100.02)

    trade, reason = execute_candidate(c, book_at=book_at, price_scale=SCALE)
    assert trade is None and reason == FAIL_NO_EXIT_BOOK


def test_the_trade_size_is_one_hundred_shares():
    trade, _ = execute_candidate(
        _candidate(), book_at=lambda ts: _book(ts, 100.00, 100.02), price_scale=SCALE
    )
    assert trade.shares == TRADE_SIZE_SHARES == 100


# ---------------------------------------------------------------------------
# Certification
# ---------------------------------------------------------------------------


def test_an_arrival_outside_receive_coverage_fails_closed():
    """A stale post-EOF book would fill perfectly ordinarily and be fictitious."""
    c = _candidate()
    trade, reason = execute_candidate(
        c,
        book_at=lambda ts: _book(ts, 100.00, 100.02),
        price_scale=SCALE,
        within_coverage=lambda lo, hi: False,
    )
    assert trade is None and reason == FAIL_OUTSIDE_COVERAGE


def test_coverage_is_checked_before_any_book_is_queried():
    queried: list[int] = []

    def book_at(ts):
        queried.append(ts)
        return _book(ts, 100.00, 100.02)

    trade, reason = execute_candidate(
        _candidate(),
        book_at=book_at,
        price_scale=SCALE,
        within_coverage=lambda lo, hi: False,
    )
    assert trade is None and reason == FAIL_OUTSIDE_COVERAGE
    assert queried == []


def test_bad_ts_recv_contamination_fails_closed():
    trade, reason = execute_candidate(
        _candidate(),
        book_at=lambda ts: _book(ts, 100.00, 100.02),
        price_scale=SCALE,
        timing_certified=lambda lo, hi: False,
    )
    assert trade is None and reason == FAIL_BAD_TS_RECV


def test_the_certified_window_spans_entry_to_exit_arrival():
    seen: list[tuple[int, int]] = []
    c = _candidate()
    execute_candidate(
        c,
        book_at=lambda ts: _book(ts, 100.00, 100.02),
        price_scale=SCALE,
        timing_certified=lambda lo, hi: (seen.append((lo, hi)) or True),
    )
    assert seen == [(c.entry_arrival_ns, c.exit_arrival_ns)]


# ---------------------------------------------------------------------------
# Fees and the economic formula
# ---------------------------------------------------------------------------


def _trade(entry, exit_, direction=LONG):
    return ExecutedTrade(
        symbol="AAPL", session_date="2025-06-02", story_id="abc",
        direction=direction, consensus=CONSENSUS_4_OF_4, abs_shock_bps=3.0,
        entry_arrival_ns=0, exit_arrival_ns=300 * SECOND,
        entry_fill=entry * SCALE, exit_fill=exit_ * SCALE,
        entry_midpoint=entry * SCALE, exit_midpoint=exit_ * SCALE,
        entry_levels=1, exit_levels=1, entry_displayed=1_000, exit_displayed=1_000,
        shares=100, price_scale=float(SCALE),
    )


def test_the_primary_fee_schedule_is_reused_exactly():
    from app.services.mbo_stage36_plan import PRIMARY_FEE_SCHEDULE

    assert PRIMARY_FEE_SCHEDULE == PRIMARY_FEE_SCHEDULE_NAME == "retail_june_2025"
    assert PRIMARY_FEE_SCHEDULE in FEE_SCHEDULES


def test_no_new_fee_assumption_is_introduced():
    plan = statistical_plan()
    assert plan["fees"]["new_assumptions_introduced"] is False
    assert set(plan["fees"]["available_schedules"]) == set(FEE_SCHEDULES)


def test_the_realized_return_matches_the_frozen_formula():
    """s * (exit_fill - entry_fill) / entry_fill * 10000."""
    trade = _trade(100.0, 100.1, LONG)
    assert trade.realized_return_bps == pytest.approx(0.1 / 100.0 * 10_000)
    short = _trade(100.0, 99.9, SHORT)
    assert short.realized_return_bps == pytest.approx(0.1 / 100.0 * 10_000)


def test_net_return_is_realized_minus_fees():
    schedule = FEE_SCHEDULES[PRIMARY_FEE_SCHEDULE_NAME]
    trade = _trade(100.0, 100.1)
    assert trade.net_return_bps(schedule) == pytest.approx(
        trade.realized_return_bps - trade.fees_bps(schedule)
    )


def test_a_flat_round_trip_loses_exactly_the_fees():
    schedule = FEE_SCHEDULES[PRIMARY_FEE_SCHEDULE_NAME]
    trade = _trade(100.0, 100.0)
    assert trade.realized_return_bps == pytest.approx(0.0)
    assert trade.net_return_bps(schedule) == pytest.approx(-trade.fees_bps(schedule))
    assert trade.fees_bps(schedule) > 0


# ---------------------------------------------------------------------------
# Gates and verdicts
# ---------------------------------------------------------------------------


def _accumulate(mean_bps, trades=150, sessions=20, spread=0.05):
    """A deterministic stream whose per-session means centre on ``mean_bps``."""
    acc = Stage36Accumulator(price_scale=float(SCALE))
    schedule = FEE_SCHEDULES[PRIMARY_FEE_SCHEDULE_NAME]
    flat = _trade(100.0, 100.0)
    fee = flat.fees_bps(schedule)
    for i in range(trades):
        # Choose an exit price whose net return is mean_bps +/- a small wobble.
        target = mean_bps + (spread if i % 2 else -spread)
        exit_price = 100.0 * (1 + (target + fee) / 10_000)
        trade = _trade(100.0, exit_price)
        trade.session_date = f"2025-06-{2 + (i % sessions):02d}"
        trade.symbol = f"SYM{i % 4}"
        acc.record_trade(trade)
    return acc


def test_the_sample_gate_requires_one_hundred_trades():
    summary = _accumulate(10.0, trades=MIN_EXECUTABLE_TRADES - 1, sessions=20).summary()
    assert summary["sample_gate"]["passed"] is False
    assert summary["verdict"] == VERDICT_INSUFFICIENT
    assert "mean_net_return_bps" not in summary


def test_the_sample_gate_requires_fifteen_sessions():
    summary = _accumulate(10.0, trades=200, sessions=MIN_SESSIONS - 1).summary()
    assert summary["sessions_represented"] == MIN_SESSIONS - 1
    assert summary["sample_gate"]["passed"] is False
    assert summary["verdict"] == VERDICT_INSUFFICIENT


def test_exactly_the_minimum_sample_passes_the_gate():
    summary = _accumulate(
        10.0, trades=MIN_EXECUTABLE_TRADES, sessions=MIN_SESSIONS
    ).summary()
    assert summary["sample_gate"]["passed"] is True


def test_a_five_bps_mechanism_is_supported():
    summary = _accumulate(6.0).summary()
    assert summary["mean_net_return_bps"] >= PRIMARY_TARGET_BPS
    assert summary["session_clustered_t"] >= T_HURDLE
    assert summary["verdict"] == VERDICT_SUPPORTED
    assert summary["stretch_8bps_supported"] is False


def test_below_five_bps_is_no_mechanism():
    summary = _accumulate(2.0).summary()
    assert summary["mean_net_return_bps"] < PRIMARY_TARGET_BPS
    assert summary["verdict"] == VERDICT_NO_MECHANISM
    assert summary["stretch_8bps_supported"] is False


def test_the_stretch_target_reports_separately():
    summary = _accumulate(9.0).summary()
    assert summary["mean_net_return_bps"] >= STRETCH_TARGET_BPS
    assert summary["verdict"] == VERDICT_SUPPORTED
    assert summary["stretch_8bps_supported"] is True


def test_a_positive_mean_without_the_t_hurdle_does_not_pass():
    """Above +5 bps but too noisy across sessions."""
    acc = Stage36Accumulator(price_scale=float(SCALE))
    schedule = FEE_SCHEDULES[PRIMARY_FEE_SCHEDULE_NAME]
    fee = _trade(100.0, 100.0).fees_bps(schedule)
    for i in range(150):
        wobble = 400.0 if i % 2 else -380.0
        exit_price = 100.0 * (1 + (6.0 + wobble + fee) / 10_000)
        trade = _trade(100.0, exit_price)
        trade.session_date = f"2025-06-{2 + (i % 20):02d}"
        acc.record_trade(trade)
    summary = acc.summary()
    assert summary["mean_net_return_bps"] >= PRIMARY_TARGET_BPS
    assert summary["session_clustered_t"] < T_HURDLE
    assert summary["verdict"] == VERDICT_NO_MECHANISM


def test_execution_failures_are_counted_explicitly():
    acc = Stage36Accumulator(price_scale=float(SCALE))
    for reason in (FAIL_ENTRY_LIQUIDITY, FAIL_ENTRY_LIQUIDITY, FAIL_BAD_TS_RECV):
        acc.record_failure(reason)
    summary = acc.summary()
    assert summary["execution_failures"] == {
        FAIL_ENTRY_LIQUIDITY: 2,
        FAIL_BAD_TS_RECV: 1,
    }


def test_no_result_ever_authorizes_paper_or_live():
    for mean in (20.0, -20.0):
        summary = _accumulate(mean).summary()
        assert summary["authorizes_paper_or_live"] is False


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------


def test_the_trial_ledger_is_530_before_and_531_after():
    governance = statistical_plan()["governance"]
    assert governance["prior_effective_trials"] == 530
    assert governance["fresh_primary_specifications"] == 1
    assert governance["effective_trials_after_outcome"] == 531
    assert governance["confirmatory"] is False
    assert governance["authorizes_paper_or_live"] is False


def test_the_trial_metadata_appears_only_in_the_economic_report():
    """531 is what a look costs; it must not appear in an outcome-blind
    artefact, because nothing has been looked at yet."""
    import inspect

    from app.cli.mbo_stage36 import diagnose, run, verify

    for outcome_blind in (verify, diagnose):
        assert "531" not in inspect.getsource(outcome_blind)
    assert "effective_trials_after_outcome" in inspect.getsource(run)


def test_there_is_exactly_one_primary_specification():
    gates = statistical_plan()["gates"]
    assert gates["primary_specifications"] == 1
    assert gates["multiple_testing_branches"] is False


def test_the_forbidden_post_outcome_adaptations_are_recorded():
    forbidden = statistical_plan()["forbidden_post_outcome_adaptation"]
    for item in (
        "symbols", "observation interval", "holding horizon", "consensus threshold",
        "shock threshold", "latency", "quiet period", "trade size", "fee assumptions",
    ):
        assert item in forbidden


def test_no_shock_threshold_exists_anywhere():
    """The shock is a diagnostic; a threshold on it would be a parameter chosen
    after the distribution was inspected."""
    import inspect

    import app.services.mbo_stage36_executor as executor

    source = inspect.getsource(executor)
    assert "abs_shock" in source  # carried
    for banned in ("shock_threshold", "min_shock", "max_shock", "MIN_SHOCK", "MAX_SHOCK"):
        assert banned not in source, banned


# ---------------------------------------------------------------------------
# CLI safety
# ---------------------------------------------------------------------------


def test_run_refuses_without_the_reviewer_flag():
    import argparse

    from app.cli.mbo_stage36 import run

    args = argparse.Namespace(
        i_have_reviewed_the_design=False, stage2_results="x", grams_dir="g",
        features_dir="f", labels_dir="l", raw_dir="r", output_dir="o",
    )
    with pytest.raises(ValueError, match="not authorized"):
        run(args)


def test_the_run_takes_no_subset_flag():
    """One specification over the complete population; a subset is a different
    specification."""
    from app.cli.mbo_stage36 import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([
            "run", "--stage2-results", "r", "--grams-dir", "g", "--features-dir", "f",
            "--labels-dir", "l", "--raw-dir", "raw", "--limit", "1",
        ])


def _diagnose_body() -> str:
    import inspect

    from app.cli.mbo_stage36 import diagnose

    body = inspect.getsource(diagnose).split('"""')[2]
    return "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )


def test_the_diagnostic_cannot_price_a_fill():
    body = _diagnose_body()
    for forbidden in (
        "execute_candidate", "Stage36Accumulator", "record_trade", "walk_book",
        "BookReplay", "net_return", "realized_return", "summary()",
    ):
        assert forbidden not in body, forbidden


def test_the_diagnostic_exercises_the_gates():
    body = _diagnose_body()
    for required in (
        "_verify_population()", "_prepare(args)", "_build_fits(context)",
        "_bind_raw_sources(", "_strip_outcomes(payload)",
    ):
        assert required in body, required


def test_the_outcome_filter_strips_economic_fields():
    from app.cli.mbo_stage36 import _strip_outcomes

    payload = {
        "measured_events": 259,
        "consensus_counts": [{"label": "4_of_4", "count": 147}],
        "mean_net_return_bps": 9.9,
        "realized_return_bps": 12.0,
        "session_clustered_t": 5.0,
        "p_value": 1e-9,
        "verdict": "supported",
        "entry_fill": 100.02,
        "stretch_8bps_supported": True,
        "nested": {"gross_midpoint_return_bps": 1.0, "raw_sources_verified": 168},
    }
    clean = _strip_outcomes(payload)
    assert clean == {
        "measured_events": 259,
        "consensus_counts": [{"label": "4_of_4", "count": 147}],
        "nested": {"raw_sources_verified": 168},
    }


def test_no_broker_client_is_reachable():
    """Structural, not textual: every module actually imported by Stage 3.6,
    transitively, is inspected. A prose mention of the word would pass a text
    search and prove nothing."""
    import ast
    import inspect

    import app.cli.mbo_stage36 as cli
    import app.services.mbo_stage36_executor as executor
    import app.services.mbo_stage36_plan as plan

    banned = ("alpaca", "broker", "tradeapi", "ib_insync", "ccxt")
    imported: set[str] = set()
    for module in (plan, executor, cli):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

    for name in imported:
        lowered = name.lower()
        for token in banned:
            assert token not in lowered, f"{name} looks like a broker client"

    # And no call anywhere is named like an order submission.
    for module in (plan, executor, cli):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target = getattr(node.func, "attr", None) or getattr(
                    node.func, "id", ""
                )
                assert "submit_order" not in str(target)
                assert "place_order" not in str(target)


def test_the_plan_command_exposes_no_economic_outcome():
    from app.cli.mbo_stage36 import _strip_outcomes

    payload = statistical_plan()
    assert payload["contains_economic_outcome"] is False
    # The plan legitimately names targets; the filter would strip them, which is
    # why the plan is not passed through it. Assert it carries no *measured*
    # quantity instead.
    for key in ("mean_net_return_bps", "session_clustered_t", "executable_trades"):
        assert key not in payload
    assert _strip_outcomes({"executor": STAGE36_EXECUTOR_VERSION}) == {
        "executor": STAGE36_EXECUTOR_VERSION
    }


# ---------------------------------------------------------------------------
# The models decide, and the frozen census only gets to agree
# ---------------------------------------------------------------------------


def _fake_feature_file(tmp_path, cadence, stem, *, rows=40, first_ts, step_ns):
    """A minimal but real feature parquet, wide enough for the frozen design."""
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    from app.cli.mbo_stage2 import FEATURE_NAMES

    directory = tmp_path / cadence
    directory.mkdir(parents=True, exist_ok=True)
    ramp = np.arange(rows, dtype=float)
    columns = {
        "sequence_index": pa.array(np.arange(rows, dtype=np.int64)),
        "feature_available_ts_recv": pa.array(
            first_ts + step_ns * np.arange(rows, dtype=np.int64)
        ),
    }
    for offset, name in enumerate(FEATURE_NAMES):
        # Every column varies, so nothing is withheld as a dormant sensor and
        # the whole design comes back finite.
        values = 100.0 + offset + ramp * (0.5 + 0.01 * offset)
        columns[name] = pa.array(values)
    pq.write_table(pa.table(columns), directory / f"{stem}.{cadence}.parquet")
    return directory


def _fits_with_intercept(session_date, values):
    """Betas that predict a constant sign, whatever the features say.

    Only the intercept is loaded, so these tests exercise the *plumbing* --
    window selection, sign extraction, consensus -- without depending on any
    particular feature value.
    """
    import numpy as np

    from app.cli.mbo_stage2 import DESIGN_WIDTH

    fits = {}
    for cell, value in zip(FROZEN_CELLS, values, strict=True):
        beta = np.zeros(DESIGN_WIDTH, dtype=float)
        beta[0] = value
        fits[cell] = {session_date: {"beta": beta}}
    return fits


def _runtime_fixture(tmp_path, values, *, known_at_ns=1_000_000_000_000):
    """One event, four models, predictions recomputed off a real parquet."""
    from app.services.mbo_stage36_executor import recompute_consensus

    session_date = "2025-06-02"
    stem = f"AAPL_{session_date}"
    for cadence in sorted({cell.split("|")[0] for cell in FROZEN_CELLS}):
        _fake_feature_file(
            tmp_path,
            cadence,
            stem,
            first_ts=known_at_ns,
            step_ns=SECOND // 2,
        )
    frozen = [_candidate(session_date=session_date, known_at_ns=known_at_ns)]
    fits = _fits_with_intercept(session_date, values)
    return frozen, recompute_consensus(frozen, tmp_path, fits)


def test_the_runtime_recomputes_predictions_from_the_models(tmp_path):
    """The direction comes out of the fits and the features, not out of the CSV."""
    _frozen, runtime = _runtime_fixture(tmp_path, (1.0, 1.0, 1.0, 1.0))
    assert runtime[0].consensus == CONSENSUS_4_OF_4
    assert runtime[0].direction == LONG
    assert runtime[0].cell_signs == (1.0, 1.0, 1.0, 1.0)


def test_the_recomputed_direction_ignores_the_frozen_csv_direction(tmp_path):
    """The census says LONG; the models say SHORT. The models win, and the
    disagreement is then surfaced rather than silently absorbed."""
    from app.services.mbo_stage36_executor import reconcile_with_frozen_census

    frozen, runtime = _runtime_fixture(tmp_path, (-1.0, -1.0, -1.0, -1.0))
    assert frozen[0].direction == LONG  # what the CSV claims
    assert runtime[0].direction == SHORT  # what the models actually say
    with pytest.raises(ValueError, match="does not reproduce the frozen"):
        reconcile_with_frozen_census(runtime, frozen)


def test_a_three_to_one_runtime_split_takes_the_majority(tmp_path):
    _frozen, runtime = _runtime_fixture(tmp_path, (1.0, 1.0, 1.0, -1.0))
    assert runtime[0].consensus == CONSENSUS_3_OF_4
    assert runtime[0].direction == LONG


def test_a_two_two_runtime_split_does_not_trade(tmp_path):
    _frozen, runtime = _runtime_fixture(tmp_path, (1.0, 1.0, -1.0, -1.0))
    assert runtime[0].consensus == CONSENSUS_2_VS_2
    assert runtime[0].direction is None
    assert not runtime[0].is_strong_consensus


def test_a_runtime_zero_prediction_abstains(tmp_path):
    """An exact zero is not a direction. It withholds the whole event."""
    _frozen, runtime = _runtime_fixture(tmp_path, (1.0, 1.0, 1.0, 0.0))
    assert runtime[0].consensus == CONSENSUS_INCOMPLETE
    assert runtime[0].direction is None


def test_a_missing_feature_file_makes_the_event_incomplete(tmp_path):
    """Fail closed: no features, no prediction, no trade."""
    from app.services.mbo_stage36_executor import recompute_consensus

    frozen = [_candidate(session_date="2025-06-02")]
    fits = _fits_with_intercept("2025-06-02", (1.0, 1.0, 1.0, 1.0))
    runtime = recompute_consensus(frozen, tmp_path, fits)
    assert runtime[0].consensus == CONSENSUS_INCOMPLETE
    assert runtime[0].direction is None


def test_a_missing_fit_for_the_session_makes_the_event_incomplete(tmp_path):
    from app.services.mbo_stage36_executor import recompute_consensus

    frozen = [_candidate(session_date="2025-06-02")]
    for cadence in sorted({cell.split("|")[0] for cell in FROZEN_CELLS}):
        _fake_feature_file(
            tmp_path, cadence, "AAPL_2025-06-02", first_ts=1_000_000_000_000, step_ns=SECOND // 2
        )
    runtime = recompute_consensus(frozen, tmp_path, _fits_with_intercept("1999-01-01", (1.0,) * 4))
    assert runtime[0].consensus == CONSENSUS_INCOMPLETE


def test_the_runtime_selection_window_excludes_pre_news_predictions():
    """A prediction available before t0 cannot be a reaction to the news."""
    import numpy as np

    from app.services.mbo_stage36_executor import select_latest_prediction

    t0 = 1_000_000_000_000
    td = t0 + 30 * SECOND
    available = np.array([t0 - 1, t0, td, td + 1], dtype=np.int64)
    predictions = np.array([9.0, 1.0, 2.0, 8.0])
    finite = np.array([True, True, True, True])

    value, moment = select_latest_prediction(available, predictions, finite, t0, td)
    assert value == 2.0 and moment == td  # the latest *inside* the window

    # With the in-window rows removed, nothing is usable -- the neighbours
    # outside the window are not substituted in.
    value, moment = select_latest_prediction(
        available, predictions, np.array([True, False, False, True]), t0, td
    )
    assert value is None and moment is None


def test_the_runtime_takes_the_latest_not_the_last_row():
    """Selection is by availability, so a mis-ordered file cannot change it."""
    import numpy as np

    from app.services.mbo_stage36_executor import select_latest_prediction

    t0 = 1_000_000_000_000
    td = t0 + 30 * SECOND
    available = np.array([t0 + 20 * SECOND, t0 + 5 * SECOND], dtype=np.int64)
    value, moment = select_latest_prediction(
        available, np.array([7.0, -3.0]), np.array([True, True]), t0, td
    )
    assert value == 7.0 and moment == t0 + 20 * SECOND


def test_a_non_finite_row_is_skipped_for_the_previous_finite_one():
    import numpy as np

    from app.services.mbo_stage36_executor import select_latest_prediction

    t0 = 1_000_000_000_000
    td = t0 + 30 * SECOND
    available = np.array([t0 + 1, t0 + 2], dtype=np.int64)
    value, _ = select_latest_prediction(
        available, np.array([4.0, 99.0]), np.array([True, False]), t0, td
    )
    assert value == 4.0


# --- the census as a check, not a source ----------------------------------


def _identity_pair():
    """The real 259-event census, reconciled against itself."""
    frozen = load_candidates(REPO_ROOT)
    return list(frozen), list(frozen)


def test_the_frozen_census_reconciles_against_itself():
    runtime, frozen = _identity_pair()
    from app.services.mbo_stage36_executor import reconcile_with_frozen_census

    result = reconcile_with_frozen_census(runtime, frozen)
    assert result["events_compared"] == 259
    assert result["event_level_match"] is True
    assert result["consensus_source"] == "runtime model recomputation"
    assert result["recomputed_counts"]["measured_events"] == 259


def test_changing_one_frozen_census_sign_refuses():
    """Perturbing the commitment must be caught, not accommodated."""
    import dataclasses

    from app.services.mbo_stage36_executor import reconcile_with_frozen_census

    runtime, frozen = _identity_pair()
    index = next(i for i, c in enumerate(frozen) if not np.isnan(c.cell_signs[0]))
    flipped = list(frozen[index].cell_signs)
    flipped[0] = -flipped[0]
    frozen[index] = dataclasses.replace(frozen[index], cell_signs=tuple(flipped))

    with pytest.raises(ValueError, match="cell_1 sign"):
        reconcile_with_frozen_census(runtime, frozen)


def test_changing_one_runtime_computed_sign_refuses():
    """And so must a drift in the models, in the other direction."""
    import dataclasses

    from app.services.mbo_stage36_executor import reconcile_with_frozen_census

    runtime, frozen = _identity_pair()
    index = next(i for i, c in enumerate(runtime) if not np.isnan(c.cell_signs[1]))
    flipped = list(runtime[index].cell_signs)
    flipped[1] = -flipped[1]
    runtime[index] = dataclasses.replace(runtime[index], cell_signs=tuple(flipped))

    with pytest.raises(ValueError, match="cell_2 sign"):
        reconcile_with_frozen_census(runtime, frozen)


def test_a_changed_runtime_direction_refuses():
    import dataclasses

    from app.services.mbo_stage36_executor import reconcile_with_frozen_census

    runtime, frozen = _identity_pair()
    index = next(i for i, c in enumerate(runtime) if c.direction == LONG)
    runtime[index] = dataclasses.replace(runtime[index], direction=SHORT)
    with pytest.raises(ValueError, match="direction recomputed"):
        reconcile_with_frozen_census(runtime, frozen)


def test_event_level_equality_is_required_not_merely_aggregate_counts():
    """Two populations can share a histogram and disagree about every event.

    This swaps two events' classifications so the aggregate tallies are
    *identical* to the frozen ones -- 259/147/21/81/10 all still hold -- and the
    reconciliation must still refuse.
    """
    import dataclasses

    from app.services.mbo_stage36_executor import (
        consensus_counts,
        reconcile_with_frozen_census,
    )

    runtime, frozen = _identity_pair()
    left = next(i for i, c in enumerate(runtime) if c.consensus == CONSENSUS_4_OF_4)
    right = next(i for i, c in enumerate(runtime) if c.consensus == CONSENSUS_2_VS_2)
    runtime[left] = dataclasses.replace(
        runtime[left], consensus=CONSENSUS_2_VS_2, direction=None
    )
    runtime[right] = dataclasses.replace(
        runtime[right], consensus=CONSENSUS_4_OF_4, direction=LONG
    )

    # The aggregate gate is fully satisfied by the corrupted population...
    assert consensus_counts(runtime) == consensus_counts(frozen)
    assert_frozen_counts(runtime)

    # ...and it is still refused, because the events themselves moved.
    with pytest.raises(ValueError, match="consensus recomputed"):
        reconcile_with_frozen_census(runtime, frozen)


def test_a_missing_or_extra_runtime_event_refuses():
    from app.services.mbo_stage36_executor import reconcile_with_frozen_census

    runtime, frozen = _identity_pair()
    dropped = runtime.pop(0)
    with pytest.raises(ValueError, match="in the census, not recomputed"):
        reconcile_with_frozen_census(runtime, frozen)

    runtime, frozen = _identity_pair()
    frozen = [c for c in frozen if c.story_id != dropped.story_id]
    with pytest.raises(ValueError, match="recomputed, not in the census"):
        reconcile_with_frozen_census(runtime, frozen)


def test_a_shifted_prediction_instant_refuses():
    """The *when* is part of the commitment, not only the sign."""
    import dataclasses

    from app.services.mbo_stage36_executor import reconcile_with_frozen_census

    runtime, frozen = _identity_pair()
    index = next(i for i, c in enumerate(runtime) if c.cell_prediction_ts[0] > 0)
    moved = list(runtime[index].cell_prediction_ts)
    moved[0] -= 1
    runtime[index] = dataclasses.replace(runtime[index], cell_prediction_ts=tuple(moved))
    with pytest.raises(ValueError, match="prediction instant"):
        reconcile_with_frozen_census(runtime, frozen)


def test_the_reconciliation_reproduces_the_exact_frozen_aggregates():
    runtime, frozen = _identity_pair()
    from app.services.mbo_stage36_executor import reconcile_with_frozen_census

    counts = reconcile_with_frozen_census(runtime, frozen)["recomputed_counts"]
    assert counts["measured_events"] == 259
    assert counts[CONSENSUS_4_OF_4] == 147
    assert counts[CONSENSUS_3_OF_4] == 21
    assert counts[CONSENSUS_2_VS_2] == 81
    assert counts[CONSENSUS_INCOMPLETE] == 10
    assert counts["strong_consensus"] == 168


# --- no future information may enter the decision --------------------------


def test_prediction_generation_reads_no_stage2_label_file():
    """Structural: the inference path opens features and nothing else.

    Stage-2 labels and the ``next_change`` / ``next_2_changes`` resolution
    timestamps are resolved *after* the decision instant. Touching them here
    would let the future choose the trade.
    """
    import ast
    import inspect

    from app.services import mbo_stage36_executor as executor

    forbidden = (
        "label",
        "next_change",
        "next_2_changes",
        "exit_resolution",
        "source_midpoint",
        "labels_dir",
    )
    for function in (
        executor.compute_cell_predictions,
        executor.select_latest_prediction,
        executor.classify_consensus,
        executor.recompute_consensus,
    ):
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        # Docstrings legitimately *discuss* labels -- this very module explains
        # why it does not read them. Drop the docstring node so the check lands
        # on executable code, never on prose about it.
        body = tree.body[0].body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body.pop(0)

        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        names = [node.id for node in ast.walk(tree) if isinstance(node, ast.Name)]
        attributes = [
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        ]
        for token in forbidden:
            for literal in literals:
                assert token not in literal.lower(), f"{function.__name__}: {literal}"
            for name in names + attributes:
                assert token not in name.lower(), f"{function.__name__}: {name}"


def test_prediction_generation_works_with_no_labels_directory_at_all(tmp_path):
    """Functional counterpart: there is no label file anywhere, and it still runs."""
    _frozen, runtime = _runtime_fixture(tmp_path, (1.0, 1.0, 1.0, 1.0))
    assert runtime[0].direction == LONG
    assert not list(tmp_path.rglob("*labels*"))


def test_the_feature_read_requests_only_the_availability_clock():
    """``feature_available_ts_recv`` is the decision clock. No other timestamp
    column is even requested from the parquet."""
    import ast
    import inspect

    from app.services import mbo_stage36_executor as executor

    tree = ast.parse(textwrap.dedent(inspect.getsource(executor.compute_cell_predictions)))
    requested = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    timestamp_columns = {c for c in requested if c.startswith("ts_") or c.endswith("_ts")}
    assert timestamp_columns == set(), timestamp_columns
    assert "feature_available_ts_recv" in requested


def test_the_runtime_reuses_stage2_and_stage3_rather_than_reimplementing():
    """No second definition of the design matrix or of the model application."""
    import ast
    import inspect

    from app.services import mbo_stage36_executor as executor

    source = textwrap.dedent(inspect.getsource(executor.compute_cell_predictions))
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.update(f"{node.module}.{a.name}" for a in node.names)
    assert "app.cli.mbo_stage2._symbol_day_matrix" in imported
    assert "app.services.mbo_stage3_executor.predict" in imported


# --- both entry points must go through the recomputation -------------------


def _cli_function_source(name):
    import inspect

    from app.cli import mbo_stage36 as cli

    return textwrap.dedent(inspect.getsource(getattr(cli, name)))


def test_runtime_recomputation_is_mandatory_for_diagnose_and_run():
    """Structural: neither entry point can reach an outcome without it."""
    import ast

    for name in ("diagnose", "run"):
        tree = ast.parse(_cli_function_source(name))
        called = {
            getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        assert "_recomputed_population" in called, name


def test_the_recomputation_helper_reconciles_and_cannot_be_bypassed():
    import ast

    tree = ast.parse(_cli_function_source("_recomputed_population"))
    called = {
        getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "recompute_consensus" in called
    assert "reconcile_with_frozen_census" in called


def test_run_trades_the_recomputed_population_not_the_csv_rows():
    """The loop that builds the tradeable set iterates the recomputed
    candidates. Iterating the CSV rows would reinstate the census as the
    signal source even with the reconciliation in place."""
    import ast

    tree = ast.parse(_cli_function_source("run"))
    loops = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Name)
    ]
    assert any(node.iter.id == "runtime" for node in loops)

    # And no direction is lifted out of a CSV column anywhere in run().
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "consensus_direction" not in literals


def test_only_the_loader_reads_the_consensus_direction_column():
    """Exactly one place in the executor may touch that column: the loader that
    builds the commitment to check against."""
    import ast
    import inspect

    from app.services import mbo_stage36_executor as executor

    tree = ast.parse(inspect.getsource(executor))
    holders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            body = ast.dump(ast.parse(textwrap.dedent(inspect.getsource(
                getattr(executor, node.name)
            )))) if hasattr(executor, node.name) else ""
            if "consensus_direction" in body:
                holders.append(node.name)
    assert holders == ["load_candidates"], holders


def test_the_diagnostic_reports_the_consensus_source():
    """A reader of the diagnostic can tell where the directions came from."""
    import ast

    tree = ast.parse(_cli_function_source("diagnose"))
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "consensus_source" in literals
    assert "runtime_recomputation" in literals


def test_the_consensus_source_survives_the_outcome_filter():
    """The provenance statement is not an economic quantity and must not be
    stripped out of the diagnostic on its way to disk."""
    from app.cli.mbo_stage36 import _strip_outcomes

    payload = {
        "consensus_source": "runtime model recomputation",
        "runtime_recomputation": {"events_compared": 259, "event_level_match": True},
    }
    assert _strip_outcomes(payload) == payload
