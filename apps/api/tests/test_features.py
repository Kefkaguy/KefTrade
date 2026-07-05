from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.features import calculate_features


def make_candles(count: int = 70) -> list[dict]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        {
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "timestamp": start + timedelta(hours=4 * index),
            "open": Decimal(100 + index),
            "high": Decimal(101 + index),
            "low": Decimal(99 + index),
            "close": Decimal(100 + index),
            "volume": Decimal(1000 + index),
        }
        for index in range(count)
    ]


def test_features_are_available_only_after_required_history() -> None:
    rows = calculate_features(make_candles())

    assert rows[10]["ema_20"] is None
    assert rows[18]["rsi_14"] is not None
    assert rows[48]["ema_50"] is None
    assert rows[49]["ema_50"] is not None


def test_feature_timestamp_matches_source_candle_timestamp() -> None:
    candles = make_candles()
    rows = calculate_features(candles)

    assert rows[-1]["timestamp"] == candles[-1]["timestamp"]
    assert rows[-1]["symbol"] == "BTCUSDT"

