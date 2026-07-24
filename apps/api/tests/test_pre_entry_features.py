from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.labs.intraday.pre_entry_features import (
    PRE_ENTRY_FEATURE_LOOKBACK_BARS,
    compute_pre_entry_features_for_trades,
    compute_pre_entry_scalar_features,
)


def make_candle(close, *, high=None, low=None, volume=100):
    close = Decimal(str(close))
    return {
        "close": close,
        "high": Decimal(str(high)) if high is not None else close + Decimal("1"),
        "low": Decimal(str(low)) if low is not None else close - Decimal("1"),
        "volume": Decimal(str(volume)),
    }


def test_returns_none_for_features_needing_more_bars_than_available():
    candles = [make_candle(100) for _ in range(3)]  # enough for return_1 (needs 2), not the rest

    result = compute_pre_entry_scalar_features(candles, vwap_distance=None, minutes_from_open=10, minutes_to_close=380)

    assert result["pre_entry_return_1"] == 0.0
    assert result["pre_entry_return_5"] is None
    assert result["pre_entry_atr_relative_move"] is None
    assert result["pre_entry_trend_slope"] is None
    assert result["pre_entry_volume_acceleration"] is None
    # session_progress needs no candles at all
    assert result["pre_entry_session_progress"] == 10 / 390


def test_returns_none_for_every_feature_with_zero_bars():
    result = compute_pre_entry_scalar_features([], vwap_distance=None, minutes_from_open=None, minutes_to_close=None)

    assert result["pre_entry_return_1"] is None
    assert result["pre_entry_return_5"] is None
    assert result["pre_entry_atr_relative_move"] is None
    assert result["pre_entry_trend_slope"] is None
    assert result["pre_entry_volume_acceleration"] is None
    assert result["pre_entry_session_progress"] is None


def test_return_1_and_return_5_computed_from_closes_only():
    closes = [100, 100, 100, 100, 100, 105, 110]  # 7 bars; last is "the bar before entry"
    candles = [make_candle(value) for value in closes]

    result = compute_pre_entry_scalar_features(candles, vwap_distance=None, minutes_from_open=0, minutes_to_close=0)

    assert result["pre_entry_return_1"] == (110 - 105) / 105
    assert result["pre_entry_return_5"] == (110 - 100) / 100


def test_trend_slope_is_positive_for_a_rising_series_and_negative_for_falling():
    rising = [make_candle(100 + index) for index in range(PRE_ENTRY_FEATURE_LOOKBACK_BARS)]
    falling = [make_candle(100 - index) for index in range(PRE_ENTRY_FEATURE_LOOKBACK_BARS)]

    rising_result = compute_pre_entry_scalar_features(rising, vwap_distance=None, minutes_from_open=None, minutes_to_close=None)
    falling_result = compute_pre_entry_scalar_features(falling, vwap_distance=None, minutes_from_open=None, minutes_to_close=None)

    assert rising_result["pre_entry_trend_slope"] > 0
    assert falling_result["pre_entry_trend_slope"] < 0


def test_volume_acceleration_reflects_a_volume_spike_on_the_most_recent_bar():
    candles = [make_candle(100, volume=100) for _ in range(PRE_ENTRY_FEATURE_LOOKBACK_BARS)]
    candles.append(make_candle(100, volume=500))

    result = compute_pre_entry_scalar_features(candles, vwap_distance=None, minutes_from_open=None, minutes_to_close=None)

    assert result["pre_entry_volume_acceleration"] == 5.0


def test_vwap_distance_passthrough_and_session_progress_computation():
    result = compute_pre_entry_scalar_features([], vwap_distance=Decimal("0.0123"), minutes_from_open=60, minutes_to_close=180)

    assert result["pre_entry_vwap_distance"] == 0.0123
    assert result["pre_entry_session_progress"] == 60 / 240


def test_atr_relative_move_is_none_when_range_is_flat_zero():
    candles = [make_candle(100, high=100, low=100) for _ in range(PRE_ENTRY_FEATURE_LOOKBACK_BARS + 1)]

    result = compute_pre_entry_scalar_features(candles, vwap_distance=None, minutes_from_open=None, minutes_to_close=None)

    assert result["pre_entry_atr_relative_move"] is None


class FakeCandleConn:
    """Minimal fake returning a fixed candle/feature series for the DB-touching wrapper."""

    def __init__(self, candles, features):
        self.candles = candles
        self.features = features

    def execute(self, query, params=None):
        if "FROM candles" in query or "FROM research_dataset_candles" in query:
            return FakeResult(self.candles)
        return FakeResult(self.features)


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


def test_compute_pre_entry_features_for_trades_excludes_the_entry_bar_itself():
    base = datetime(2026, 3, 2, 9, 30, tzinfo=UTC)
    candles = [
        {"timestamp": base + timedelta(minutes=30 * i), "open": 100, "high": 101, "low": 99, "close": 100 + i, "volume": 100}
        for i in range(15)
    ]
    entry_time = base + timedelta(minutes=30 * 12)  # bar index 12
    trades = [{"entry_time": entry_time, "entry_minutes_from_open": 360, "entry_minutes_to_close": 30}]

    results = compute_pre_entry_features_for_trades(FakeCandleConn(candles, []), symbol="AMD", timeframe="30m", trades=trades)

    assert len(results) == 1
    # close at bar index 11 (the bar immediately before entry) is 100+11=111,
    # not bar index 12's own close (100+12=112) -- proves the entry bar itself
    # was excluded from the lookback window.
    assert results[0]["pre_entry_return_1"] == (111 - 110) / 110


def test_compute_pre_entry_features_for_trades_returns_empty_list_for_no_trades():
    results = compute_pre_entry_features_for_trades(FakeCandleConn([], []), symbol="AMD", timeframe="30m", trades=[])
    assert results == []
