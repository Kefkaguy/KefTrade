"""Cross-sectional relative-strength ranking: the core new computation
behind CrossSectionalMomentumV2. See app/services/labs/intraday/cross_sectional.py.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.labs.intraday.cross_sectional import (
    MINIMUM_PEERS_FOR_RANKING,
    compute_cross_sectional_percentiles,
    merge_percentiles_into_features,
)

BASE_TIME = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)


def candles(closes: list[float], *, start=BASE_TIME, step_minutes=30):
    return [
        {"timestamp": start + timedelta(minutes=step_minutes * i), "close": Decimal(str(close))}
        for i, close in enumerate(closes)
    ]


def test_strongest_trailing_return_gets_the_highest_percentile():
    candles_by_symbol = {
        "WEAK": candles([100, 100, 100, 100, 100, 100, 100, 100, 100]),  # flat
        "MID": candles([100, 100, 100, 100, 100, 100, 100, 100, 103]),  # +3%
        "STRONG": candles([100, 100, 100, 100, 100, 100, 100, 100, 110]),  # +10%
    }

    percentiles = compute_cross_sectional_percentiles(candles_by_symbol, lookback_bars=8)

    last_timestamp = candles_by_symbol["STRONG"][-1]["timestamp"]
    assert percentiles["STRONG"][last_timestamp] == 1.0
    assert percentiles["MID"][last_timestamp] == 0.5
    assert percentiles["WEAK"][last_timestamp] == 0.0


def test_ranking_never_compares_returns_from_different_timestamps():
    """STRONG's big move happens at a bar where WEAK has no data at all --
    that timestamp must be entirely absent from the ranking, not padded
    with a stale or default value for WEAK."""
    candles_by_symbol = {
        "STRONG": candles([100, 100, 100, 100, 100, 100, 100, 100, 150]),
        "WEAK": candles([100, 100, 100, 100, 100, 100, 100, 100]),  # one bar shorter
    }

    percentiles = compute_cross_sectional_percentiles(candles_by_symbol, lookback_bars=8)

    last_timestamp = candles_by_symbol["STRONG"][-1]["timestamp"]
    # Only STRONG has a trailing return at this timestamp -- below the
    # minimum peer count, so it must be omitted for everyone, not ranked
    # against nothing.
    assert last_timestamp not in percentiles["STRONG"]
    assert MINIMUM_PEERS_FOR_RANKING == 3


def test_no_lookahead_a_symbols_own_future_bars_cannot_change_a_past_percentile():
    original = [100, 100, 100, 100, 100, 100, 100, 100, 105, 100, 100]
    shocked = list(original)
    shocked[-1] = 500  # a violent move on the LAST bar only

    def build(closes):
        return {
            "A": candles(closes),
            "B": candles([100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100]),
            "C": candles([100, 100, 100, 100, 100, 100, 100, 100, 95, 100, 100]),
        }

    percentiles_before = compute_cross_sectional_percentiles(build(original), lookback_bars=8)
    percentiles_after = compute_cross_sectional_percentiles(build(shocked), lookback_bars=8)

    earlier_timestamp = candles(original)[8]["timestamp"]
    assert percentiles_before["A"][earlier_timestamp] == percentiles_after["A"][earlier_timestamp]


def test_tied_returns_share_the_average_rank():
    candles_by_symbol = {
        "A": candles([100, 100, 100, 100, 100, 100, 100, 100, 105]),
        "B": candles([100, 100, 100, 100, 100, 100, 100, 100, 105]),
        "C": candles([100, 100, 100, 100, 100, 100, 100, 100, 100]),
    }

    percentiles = compute_cross_sectional_percentiles(candles_by_symbol, lookback_bars=8)

    last_timestamp = candles_by_symbol["A"][-1]["timestamp"]
    # A and B are tied for 1st/2nd (ranks 1,2 -> average rank 1.5 -> percentile 0.75).
    assert percentiles["A"][last_timestamp] == pytest.approx(0.75)
    assert percentiles["B"][last_timestamp] == pytest.approx(0.75)
    assert percentiles["C"][last_timestamp] == 0.0


def test_a_symbol_with_no_computable_history_yet_is_simply_absent():
    candles_by_symbol = {
        "A": candles([100, 100, 100, 100, 100, 100, 100, 100, 105]),
        "B": candles([100, 100, 100, 100, 100, 100, 100, 100, 95]),
        "C": candles([100, 100, 100, 100, 100, 100, 100, 100, 100]),
    }

    percentiles = compute_cross_sectional_percentiles(candles_by_symbol, lookback_bars=8)

    first_timestamp = candles_by_symbol["A"][0]["timestamp"]
    assert first_timestamp not in percentiles["A"]


def test_merge_attaches_percentile_by_timestamp_and_none_when_unmeasured():
    features = [
        {"timestamp": BASE_TIME, "some_other_field": 1},
        {"timestamp": BASE_TIME + timedelta(minutes=30), "some_other_field": 2},
    ]
    percentiles_for_symbol = {BASE_TIME: 0.9}

    merged = merge_percentiles_into_features(features, percentiles_for_symbol)

    assert merged[0]["cross_sectional_momentum_percentile"] == 0.9
    assert merged[1]["cross_sectional_momentum_percentile"] is None
    # Original rows must not be mutated in place.
    assert "cross_sectional_momentum_percentile" not in features[0]
