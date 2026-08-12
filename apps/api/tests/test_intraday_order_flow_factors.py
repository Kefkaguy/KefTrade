from datetime import UTC, date, datetime, timedelta

import pytest

from app.services.intraday_factor_diagnostics import FACTOR_SPECS
from app.services.intraday_order_flow_factors import (
    MINIMUM_TRADE_COUNT,
    premarket_undiscovered_gap_observations,
    sector_relative_forced_flow_observations,
    signed_trade_imbalance_observations,
)

SESSION = date(2025, 3, 3)
OPEN = datetime(2025, 3, 3, 14, 30, tzinfo=UTC)  # 09:30 ET
SLOTS = 13


def bar(index, *, close=100.0, open_price=100.0, volume=1000.0, day=SESSION):
    start = datetime.combine(day, OPEN.timetz()).replace(tzinfo=UTC)
    return {
        "timestamp": start + timedelta(minutes=30 * index),
        "open": open_price,
        "high": max(open_price, close),
        "low": min(open_price, close),
        "close": close,
        "volume": volume,
    }


def session(day=SESSION, **overrides):
    return [bar(index, day=day, **overrides) for index in range(SLOTS)]


# ---------------------------------------------------------------------------
# premarket-undiscovered gaps
# ---------------------------------------------------------------------------


def premarket(**overrides):
    row = {
        "opening_gap": 0.02,
        "gap_discovered_premarket": 0.05,
        "premarket_relative_volume": 0.4,
    }
    row.update(overrides)
    return {"AAPL": {SESSION: row}}


MISSING = object()


def gaps(candles=None, premarket_by_symbol=MISSING, **kwargs):
    return premarket_undiscovered_gap_observations(
        candles or {"AAPL": session()},
        timeframe="30m",
        premarket_by_symbol=(
            premarket() if premarket_by_symbol is MISSING else premarket_by_symbol
        ),
        **kwargs,
    )


def test_an_unpriced_gap_qualifies():
    rows = gaps()

    assert len(rows) == 1
    assert rows[0]["factor_key"] == "premarket_undiscovered_gap_reversal"
    assert rows[0]["signal_polarity"] == "reversal"


def test_the_reversal_score_opposes_the_gap():
    up = gaps(premarket_by_symbol=premarket(opening_gap=0.02))
    down = gaps(premarket_by_symbol=premarket(opening_gap=-0.02))

    assert up[0]["score"] < 0
    assert down[0]["score"] > 0


def test_a_gap_the_premarket_already_priced_is_excluded():
    assert gaps(premarket_by_symbol=premarket(gap_discovered_premarket=0.9)) == []


def test_a_gap_discovered_on_heavy_premarket_volume_is_excluded():
    assert gaps(premarket_by_symbol=premarket(premarket_relative_volume=3.0)) == []


def test_a_gap_too_small_to_matter_is_excluded():
    assert gaps(premarket_by_symbol=premarket(opening_gap=0.0005)) == []


def test_an_unmeasurable_gap_is_dropped_not_treated_as_zero():
    assert gaps(premarket_by_symbol=premarket(opening_gap=None)) == []
    assert gaps(premarket_by_symbol=premarket(gap_discovered_premarket=None)) == []
    assert gaps(premarket_by_symbol=premarket(premarket_relative_volume=None)) == []


def test_without_the_premarket_channel_nothing_is_produced():
    assert gaps(premarket_by_symbol={}) == []


def test_entry_is_the_bar_after_the_decision_and_never_reads_it():
    rows = gaps()
    row = rows[0]

    assert row["signal_bar_timestamp"] < row["entry_bar_timestamp"]
    assert row["decision_timestamp"] <= row["entry_bar_timestamp"]
    assert row["entry_bar_timestamp"] <= row["exit_bar_timestamp"]


def test_a_horizon_that_would_run_past_the_close_is_dropped_not_shortened():
    short = {"AAPL": session()[:3]}

    assert gaps(candles=short, horizon_bars=1) != []
    assert gaps(candles=short, horizon_bars=4) == []


# ---------------------------------------------------------------------------
# signed trade imbalance
# ---------------------------------------------------------------------------


def flow(**overrides):
    row = {
        "signed_trade_imbalance": 0.6,
        "trade_count": 5_000,
        "unclassified_share": 0.05,
        "effective_trade_count": 1_000.0,
        "total_volume": 100_000.0,
    }
    row.update(overrides)
    rows = session()
    return {"AAPL": {candle["timestamp"]: dict(row) for candle in rows}}


def imbalance(candles=None, trade_flow_by_symbol=MISSING, **kwargs):
    return signed_trade_imbalance_observations(
        candles or {"AAPL": session()},
        timeframe="30m",
        trade_flow_by_symbol=(
            flow() if trade_flow_by_symbol is MISSING else trade_flow_by_symbol
        ),
        **kwargs,
    )


def test_a_strongly_one_sided_bar_qualifies():
    rows = imbalance()

    assert rows
    assert rows[0]["signal_polarity"] == "continuation"
    assert rows[0]["score"] == 0.6


def test_the_score_carries_the_side_that_was_aggressing():
    buying = imbalance(trade_flow_by_symbol=flow(signed_trade_imbalance=0.6))
    selling = imbalance(trade_flow_by_symbol=flow(signed_trade_imbalance=-0.6))

    assert buying[0]["score"] > 0
    assert selling[0]["score"] < 0


def test_a_balanced_bar_is_not_an_event():
    assert imbalance(trade_flow_by_symbol=flow(signed_trade_imbalance=0.05)) == []


def test_a_bar_with_too_few_prints_is_excluded():
    assert (
        imbalance(trade_flow_by_symbol=flow(trade_count=MINIMUM_TRADE_COUNT - 1)) == []
    )


def test_a_bar_where_most_volume_could_not_be_signed_is_excluded():
    assert imbalance(trade_flow_by_symbol=flow(unclassified_share=0.8)) == []


def test_without_the_trade_flow_channel_nothing_is_produced():
    assert imbalance(trade_flow_by_symbol={}) == []


def test_v2_refuses_to_run_without_a_frozen_return_blind_calibration():
    spec = FACTOR_SPECS["signed_trade_imbalance_continuation_v2_1bar"]

    with pytest.raises(ValueError, match="requires a frozen return-blind calibration"):
        spec.builder(
            {"AAPL": session()},
            timeframe="30m",
            trade_flow_by_symbol=flow(signed_trade_imbalance=0.6),
        )


def test_v2_uses_the_calibrated_threshold_instead_of_the_legacy_point_30():
    spec = FACTOR_SPECS["signed_trade_imbalance_continuation_v2_1bar"]

    def calibration(threshold):
        return {
            "id": 9,
            "report": {
                "threshold": {"mode": "global", "global_rounded_up": threshold}
            },
        }

    rejected = spec.builder(
        {"AAPL": session()},
        timeframe="30m",
        trade_flow_by_symbol=flow(signed_trade_imbalance=0.6),
        trade_imbalance_calibration=calibration(0.7),
    )
    accepted = spec.builder(
        {"AAPL": session()},
        timeframe="30m",
        trade_flow_by_symbol=flow(signed_trade_imbalance=0.6),
        trade_imbalance_calibration=calibration(0.5),
    )

    assert rejected == []
    assert accepted
    assert all(row["trade_imbalance_calibration_id"] == 9 for row in accepted)
    assert all(row["minimum_signed_imbalance"] == 0.5 for row in accepted)


def test_v2_exhaustion_reversal_uses_same_threshold_but_flips_direction():
    continuation = FACTOR_SPECS["signed_trade_imbalance_continuation_v2_1bar"]
    reversal = FACTOR_SPECS["signed_trade_imbalance_exhaustion_reversal_v3_1bar"]
    calibration = {
        "id": 9,
        "report": {
            "threshold": {"mode": "global", "global_rounded_up": 0.5}
        },
    }

    continuation_rows = continuation.builder(
        {"AAPL": session()},
        timeframe="30m",
        trade_flow_by_symbol=flow(signed_trade_imbalance=0.6),
        trade_imbalance_calibration=calibration,
    )
    reversal_rows = reversal.builder(
        {"AAPL": session()},
        timeframe="30m",
        trade_flow_by_symbol=flow(signed_trade_imbalance=0.6),
        trade_imbalance_calibration=calibration,
    )

    assert continuation_rows
    assert reversal_rows
    assert continuation_rows[0]["score"] == 0.6
    assert reversal_rows[0]["score"] == -0.6
    assert reversal_rows[0]["signal_polarity"] == "reversal"
    assert reversal_rows[0]["minimum_signed_imbalance"] == 0.5


def test_no_position_is_opened_that_cannot_be_closed_in_session():
    rows = imbalance(horizon_bars=2)
    last = max(row["exit_bar_timestamp"] for row in rows)
    close = session()[-1]["timestamp"]

    assert last <= close


def test_a_longer_horizon_produces_fewer_events_not_shortened_ones():
    one = imbalance(horizon_bars=1)
    two = imbalance(horizon_bars=2)

    assert len(two) < len(one)
    for row in two:
        assert row["horizon_bars"] == 2


# ---------------------------------------------------------------------------
# sector-relative forced flow
# ---------------------------------------------------------------------------


TECH = {f"T{index}": "Technology" for index in range(6)}


def peer_universe(target_close, *, peer_closes=None):
    """T0 moves to ``target_close``; peers jitter slightly around unchanged.

    The peers must disperse a little: a sector with literally zero dispersion
    has no scale to standardize a residual against, and the builder correctly
    withholds the measure rather than dividing by zero.
    """
    peer_closes = peer_closes or [99.9, 100.0, 100.1, 99.95, 100.05]
    candles = {}
    for position, symbol in enumerate(sorted(TECH)):
        if symbol == "T0":
            close, volume = target_close, 9000.0
        else:
            close, volume = peer_closes[(position - 1) % len(peer_closes)], 1000.0
        candles[symbol] = [
            bar(index, close=close, volume=volume) for index in range(SLOTS)
        ]
    return candles


def forced(candles, sector_by_symbol=None, **kwargs):
    return sector_relative_forced_flow_observations(
        candles,
        timeframe="30m",
        sector_by_symbol=TECH if sector_by_symbol is None else sector_by_symbol,
        **kwargs,
    )


def _with_relative_volume(candles, *, target=6.0, peer=1.0):
    for symbol, rows in candles.items():
        for row in rows:
            row["session_relative_volume"] = target if symbol == "T0" else peer
    return candles


def test_a_dumped_name_against_a_flat_sector_qualifies():
    candles = _with_relative_volume(peer_universe(96.0))

    rows = forced(candles)

    assert any(row["symbol"] == "T0" for row in rows)
    assert all(row["signal_polarity"] == "reversal" for row in rows)


def test_the_reversal_score_opposes_the_idiosyncratic_move():
    down = forced(_with_relative_volume(peer_universe(96.0)))
    up = forced(_with_relative_volume(peer_universe(104.0)))

    assert next(row for row in down if row["symbol"] == "T0")["score"] > 0
    assert next(row for row in up if row["symbol"] == "T0")["score"] < 0


def test_a_move_the_whole_sector_shares_is_not_forced_flow():
    # Every name falls about four percent, so T0 has no residual to fade even
    # though its move is large and its participation is heavy.
    candles = _with_relative_volume(
        peer_universe(96.0, peer_closes=[95.9, 96.0, 96.1, 95.95, 96.05])
    )

    assert [row for row in forced(candles) if row["symbol"] == "T0"] == []


def test_a_large_move_on_ordinary_participation_is_excluded():
    candles = _with_relative_volume(peer_universe(96.0), target=1.0)

    assert forced(candles) == []


def test_without_the_sector_map_nothing_is_produced():
    candles = _with_relative_volume(peer_universe(96.0))

    assert forced(candles, sector_by_symbol={}) == []


# ---------------------------------------------------------------------------
# registration and gating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,flag",
    [
        ("premarket_undiscovered_gap_reversal_1bar", "requires_premarket"),
        ("premarket_undiscovered_gap_reversal_2bar", "requires_premarket"),
        ("signed_trade_imbalance_continuation_1bar", "requires_trade_flow"),
        ("signed_trade_imbalance_continuation_2bar", "requires_trade_flow"),
        ("sector_relative_forced_flow_reversal_1bar", "requires_sector_context"),
        ("sector_relative_forced_flow_reversal_2bar", "requires_sector_context"),
    ],
)
def test_each_order_flow_spec_declares_the_channel_it_depends_on(key, flag):
    spec = FACTOR_SPECS[key]

    assert getattr(spec, flag) is True
    assert spec.factor_type == "directional_event"
    assert spec.supported_timeframes == ("30m",)


def test_the_bound_horizon_rewrites_the_factor_key_so_each_is_its_own_trial():
    spec = FACTOR_SPECS["signed_trade_imbalance_continuation_2bar"]

    rows = spec.builder({"AAPL": session()}, timeframe="30m", trade_flow_by_symbol=flow())

    assert rows
    assert {row["factor_key"] for row in rows} == {
        "signed_trade_imbalance_continuation_2bar"
    }
    assert {row["horizon_bars"] for row in rows} == {2}


# ---------------------------------------------------------------------------
# batching axis
# ---------------------------------------------------------------------------


def test_a_symbol_batch_would_redefine_the_peer_group():
    """Why the sector factor must never be batched by symbol.

    Splitting the universe into batches leaves each name measured against
    whichever peers shared its batch, which is a different peer group and so a
    different score -- silently, with no error.
    """
    candles = _with_relative_volume(peer_universe(96.0))
    half = dict(list(candles.items())[:3])

    whole_universe = forced(candles)
    one_batch = forced(half)

    assert whole_universe != one_batch


def test_batching_by_session_leaves_the_peer_group_intact():
    other_day = date(2025, 3, 4)
    candles = _with_relative_volume(peer_universe(96.0))
    for symbol, rows in candles.items():
        rows.extend(
            bar(index, close=rows[0]["close"], day=other_day, volume=rows[0]["volume"])
            for index in range(SLOTS)
        )
        for row in rows:
            row["session_relative_volume"] = 6.0 if symbol == "T0" else 1.0

    both_sessions = forced(candles)

    # Split by time, every symbol present in each slice, then concatenated.
    def only(day):
        return {
            symbol: [row for row in rows if row["timestamp"].date() == day]
            for symbol, rows in candles.items()
        }

    batched = forced(only(SESSION)) + forced(only(other_day))

    key = lambda row: (row["symbol"], row["entry_bar_timestamp"])  # noqa: E731
    assert sorted(batched, key=key) == sorted(both_sessions, key=key)
    assert batched


# ---------------------------------------------------------------------------
# failure interpretation
# ---------------------------------------------------------------------------


def _failed_result(gross_bps):
    return {
        "status": "measured",
        "evidence_gate": {"passed": False, "failed": ["clears_stressed_costs"]},
        "power_and_stability": {"power": {"null_result_is_interpretable": True}},
        "factor_research_readiness": {"ready": True},
        "validation": {"gross_directional_edge_bps": gross_bps},
    }


def test_a_factor_with_no_gross_edge_did_not_fail_on_cost():
    from app.services.intraday_factor_diagnostics import interpret_factor_failure

    # An edge of zero would still be zero if trading were free, so no holding
    # horizon or cheaper venue can rescue it.
    verdict = interpret_factor_failure(_failed_result(-0.05))

    assert verdict["verdict"] == "no_gross_edge"
    assert verdict["action"] == "retire_hypothesis"
    assert verdict["retire_hypothesis"] is True


def test_a_real_edge_eaten_by_costs_is_still_reported_as_such():
    from app.services.intraday_factor_diagnostics import interpret_factor_failure

    verdict = interpret_factor_failure(_failed_result(12.0))

    assert verdict["verdict"] == "fails_on_cost"
    assert verdict["action"] == "retire_or_redesign_holding_horizon"


def test_an_unmeasured_gross_edge_is_not_assumed_absent():
    from app.services.intraday_factor_diagnostics import interpret_factor_failure

    verdict = interpret_factor_failure(_failed_result(None))

    assert verdict["verdict"] == "fails_on_cost"
