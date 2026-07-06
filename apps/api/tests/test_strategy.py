from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.strategy import trend_pullback_decision


PARAMS = {
    "ema_fast": 20,
    "ema_slow": 50,
    "rsi_min": 40,
    "rsi_max": 60,
    "volume_change_min": -0.25,
    "entry_distance_to_ema20_max": 0.015,
    "swing_lookback": 5,
    "risk_reward": 2,
}


def recent_candles() -> list[dict]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        {
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "timestamp": start + timedelta(hours=4 * index),
            "open": Decimal(90 + index),
            "high": Decimal(95 + index),
            "low": Decimal("98"),
            "close": Decimal(90 + index),
            "volume": Decimal("1000"),
        }
        for index in range(60)
    ]


def test_trend_pullback_setup_when_all_filters_pass() -> None:
    candle = {"symbol": "BTCUSDT", "close": Decimal("150"), "low": Decimal("99")}
    feature = {
        "ema_20": Decimal("100"),
        "ema_50": Decimal("95"),
        "rsi_14": Decimal("50"),
        "volume_change": Decimal("0.10"),
        "distance_from_ema_20": Decimal("0.01"),
    }

    params = {**PARAMS, "entry_distance_to_ema20_max": 0.10}
    decision = trend_pullback_decision(candle, feature, recent_candles(), params)

    assert decision.signal == "setup"
    assert decision.stop_loss == Decimal("98")
    assert decision.risk_reward == Decimal("2")


def test_trend_pullback_avoids_when_trend_filter_fails() -> None:
    candle = {"symbol": "BTCUSDT", "close": Decimal("91"), "low": Decimal("90")}
    feature = {
        "ema_20": Decimal("94"),
        "ema_50": Decimal("95"),
        "rsi_14": Decimal("50"),
        "volume_change": Decimal("0.10"),
        "distance_from_ema_20": Decimal("0.01"),
    }

    decision = trend_pullback_decision(candle, feature, recent_candles(), PARAMS)

    assert decision.signal == "avoid"
