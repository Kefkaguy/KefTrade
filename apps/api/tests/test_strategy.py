from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.strategy import (
    breakout_decision,
    get_strategy_library,
    mean_reversion_decision,
    momentum_decision,
    trend_following_200ema_decision,
    trend_pullback_decision,
    volatility_breakout_decision,
)


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


def rising_candles(count: int = 220) -> list[dict]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    candles = []
    for index in range(count):
        close = Decimal(100 + index)
        candles.append(
            {
                "symbol": "BTCUSDT",
                "timeframe": "4h",
                "timestamp": start + timedelta(hours=4 * index),
                "open": close - Decimal("1"),
                "high": close + Decimal("1"),
                "low": close - Decimal("2"),
                "close": close,
                "volume": Decimal("1000"),
            }
        )
    return candles


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


def test_strategy_library_contains_required_metadata() -> None:
    library = get_strategy_library()

    for key in [
        "breakout_v1",
        "mean_reversion_v1",
        "momentum_v1",
        "volatility_breakout_v1",
        "trend_following_200ema_v1",
    ]:
        strategy = library[key]
        assert strategy.description
        assert strategy.parameters
        assert strategy.entry_rules
        assert strategy.exit_rules
        assert strategy.supported_market_regimes


def test_breakout_v1_setup_on_prior_high_break() -> None:
    candles = rising_candles(30)
    candle = {**candles[-1], "close": Decimal("140"), "high": Decimal("141"), "low": Decimal("132")}
    feature = {"volume_change": Decimal("0.20")}
    params = {**PARAMS, "breakout_lookback": 20, "volume_change_min": 0.05}

    decision = breakout_decision(candle, feature, candles, params)

    assert decision.signal == "setup"
    assert decision.stop_loss is not None
    assert decision.take_profit is not None


def test_mean_reversion_v1_setup_on_oversold_stretch_above_ema50() -> None:
    candles = [
        {
            **row,
            "open": Decimal("104"),
            "high": Decimal("106"),
            "low": Decimal("99"),
            "close": Decimal("104"),
        }
        for row in rising_candles(60)
    ]
    candle = {**candles[-1], "close": Decimal("105"), "low": Decimal("100")}
    feature = {
        "ema_20": Decimal("110"),
        "ema_50": Decimal("100"),
        "rsi_14": Decimal("30"),
        "distance_from_ema_20": Decimal("-0.045"),
    }
    params = {**PARAMS, "rsi_oversold": 35, "distance_from_ema_20_min": -0.025, "swing_lookback": 10, "risk_reward": 1.5}

    decision = mean_reversion_decision(candle, feature, candles, params)

    assert decision.signal == "setup"
    assert decision.take_profit == Decimal("110")


def test_momentum_v1_setup_on_return_macd_and_trend_confirmation() -> None:
    candles = rising_candles(80)
    candle = {**candles[-1], "close": Decimal("190")}
    feature = {
        "ema_50": Decimal("150"),
        "returns_5": Decimal("0.04"),
        "macd": Decimal("3"),
        "macd_signal": Decimal("2"),
    }
    params = {**PARAMS, "returns_5_min": 0.025, "swing_lookback": 8}

    decision = momentum_decision(candle, feature, candles, params)

    assert decision.signal == "setup"


def test_volatility_breakout_v1_setup_on_high_volatility_range_break() -> None:
    candles = rising_candles(40)
    candle = {**candles[-1], "close": Decimal("150"), "high": Decimal("151"), "low": Decimal("142")}
    feature = {"volatility_20": Decimal("0.03"), "volume_change": Decimal("0.25")}
    params = {**PARAMS, "breakout_lookback": 12, "volatility_20_min": 0.015, "volume_change_min": 0.1}

    decision = volatility_breakout_decision(candle, feature, candles, params)

    assert decision.signal == "setup"


def test_trend_following_200ema_v1_setup_above_long_trend() -> None:
    candles = rising_candles(220)
    candle = {**candles[-1], "close": Decimal("340")}
    feature = {"ema_50": Decimal("300"), "returns_5": Decimal("0.03")}
    params = {**PARAMS, "ema_long": 200, "returns_5_min": 0.01, "swing_lookback": 20}

    decision = trend_following_200ema_decision(candle, feature, candles, params)

    assert decision.signal == "setup"
