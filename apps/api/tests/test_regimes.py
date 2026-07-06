from datetime import UTC, datetime
from decimal import Decimal

from app.services.regimes import calculate_regimes, classify_market_regime, summarize_regimes


def candle(close: str = "110") -> dict:
    return {
        "symbol": "BTCUSDT",
        "timeframe": "4h",
        "timestamp": datetime(2024, 1, 1, tzinfo=UTC),
        "open": Decimal(close),
        "high": Decimal(close),
        "low": Decimal(close),
        "close": Decimal(close),
        "volume": Decimal("1000"),
    }


def test_classifies_bull_trend_high_volatility() -> None:
    regime = classify_market_regime(
        candle("110"),
        {
            "ema_50": Decimal("100"),
            "returns_5": Decimal("0.03"),
            "distance_from_ema_50": Decimal("0.10"),
            "volatility_20": Decimal("0.025"),
        },
    )

    assert regime["trend_regime"] == "bull_trend"
    assert regime["volatility_regime"] == "high_volatility"
    assert regime["trend_strength"] == Decimal("0.10")


def test_classifies_bear_trend_low_volatility() -> None:
    regime = classify_market_regime(
        candle("90"),
        {
            "ema_50": Decimal("100"),
            "returns_5": Decimal("-0.03"),
            "distance_from_ema_50": Decimal("-0.10"),
            "volatility_20": Decimal("0.005"),
        },
    )

    assert regime["trend_regime"] == "bear_trend"
    assert regime["volatility_regime"] == "low_volatility"


def test_calculate_regimes_matches_candles_to_features() -> None:
    candles = [candle("100")]
    features = [
        {
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "timestamp": candles[0]["timestamp"],
            "ema_50": Decimal("100"),
            "returns_5": Decimal("0"),
            "distance_from_ema_50": Decimal("0"),
            "volatility_20": Decimal("0.012"),
        }
    ]

    regimes = calculate_regimes(candles, features)

    assert regimes[0]["trend_regime"] == "sideways"
    assert summarize_regimes(regimes)["volatility"]["normal_volatility"] == 1
