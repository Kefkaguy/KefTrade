from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.backtester import calculate_metrics, run_backtest, walk_forward_split


PARAMS = {
    "ema_fast": 20,
    "ema_slow": 50,
    "rsi_min": 40,
    "rsi_max": 60,
    "volume_change_min": -0.25,
    "entry_distance_to_ema20_max": 0.015,
    "swing_lookback": 5,
    "risk_reward": 2,
    "fee_rate": 0,
    "slippage_rate": 0,
    "risk_per_trade": 0.01,
    "initial_equity": 10000,
    "walk_forward_train_ratio": 0.7,
}


def make_rows(count: int = 100) -> tuple[list[dict], list[dict]]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    candles = []
    features = []
    for index in range(count):
        timestamp = start + timedelta(hours=4 * index)
        close = Decimal("100")
        high = Decimal("101")
        low = Decimal("99")
        if index == 71:
            high = Decimal("105")
        candles.append(
            {
                "symbol": "BTCUSDT",
                "timeframe": "4h",
                "timestamp": timestamp,
                "open": close,
                "high": high,
                "low": low,
                "close": close,
                "volume": Decimal("1000"),
            }
        )
        features.append(
            {
                "symbol": "BTCUSDT",
                "timeframe": "4h",
                "timestamp": timestamp,
                "ema_20": Decimal("100"),
                "ema_50": Decimal("95"),
                "rsi_14": Decimal("50"),
                "volume_change": Decimal("0"),
                "distance_from_ema_20": Decimal("0"),
            }
        )
    return candles, features


def test_walk_forward_split_separates_validation_window() -> None:
    rows = [{"idx": index} for index in range(100)]
    train, validation = walk_forward_split(rows, 0.7)

    assert len(train) == 70
    assert validation[0]["idx"] == 70


def test_backtest_enters_after_signal_candle_to_avoid_lookahead() -> None:
    candles, features = make_rows()
    result = run_backtest(candles, features, PARAMS)

    assert result["metrics"]["walk_forward"]["enabled"] is True
    assert result["trades"]
    assert result["trades"][0]["entry_time"] == candles[71]["timestamp"]


def test_expectancy_per_trade_uses_win_loss_asymmetry() -> None:
    metrics = calculate_metrics(
        Decimal("10000"),
        Decimal("10090"),
        [
            {"pnl": Decimal("120"), "pnl_pct": Decimal("0.012")},
            {"pnl": Decimal("-30"), "pnl_pct": Decimal("-0.003")},
        ],
        [Decimal("10000"), Decimal("10120"), Decimal("10090")],
    )

    assert metrics["expectancy_per_trade"] == 45.0
    assert metrics["win_rate"] == 0.5

