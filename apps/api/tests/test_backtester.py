from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.backtester import calculate_metrics, run_backtest, walk_forward_split


PARAMS = {
    "ema_fast": 20,
    "ema_slow": 50,
    "rsi_min": 40,
    "rsi_max": 60,
    "volume_change_min": -0.25,
    "entry_distance_to_ema20_max": 0.25,
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
        open_price = Decimal("100")
        high = Decimal("101")
        low = Decimal("99")
        if index == 70:
            close = Decimal("120")
        if index == 71:
            open_price = Decimal("103")
            high = Decimal("112")
            low = Decimal("99")
        candles.append(
            {
                "symbol": "BTCUSDT",
                "timeframe": "4h",
                "timestamp": timestamp,
                "open": open_price,
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
    assert result["trades"][0]["entry_price"] == candles[71]["open"]


def test_same_candle_stop_target_policy_is_stop_first() -> None:
    candles, features = make_rows()
    result = run_backtest(candles, features, PARAMS)

    assert result["trades"][0]["exit_reason"] == "stop_loss_stop_first"
    assert result["trades"][0]["exit_price"] == result["trades"][0]["stop_loss"]
    assert result["metrics"]["max_drawdown"] > 0


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


def test_backtest_route_persists_strategy_parameter_snapshot(monkeypatch) -> None:
    from app.routers import backtests as route

    class FakeResult:
        rowcount = 1

        def __init__(self, row=None) -> None:
            self.row = row

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.row

    class FakeConn:
        def __init__(self) -> None:
            self.backtest_params = None

        def execute(self, sql, params=None):
            if "INSERT INTO backtests" in sql:
                self.backtest_params = params
                return FakeResult({"id": 7})
            if "SELECT *" in sql and "FROM features" in sql:
                return FakeResult([])
            return FakeResult()

        def commit(self) -> None:
            pass

    strategy_params = {**PARAMS, "risk_reward": 3}
    fake_conn = FakeConn()
    monkeypatch.setattr(route, "sync_features", lambda *args, **kwargs: None)
    monkeypatch.setattr(route, "get_strategy_version", lambda conn: {"name": "trend_pullback", "version": "v1", "parameters": strategy_params})
    monkeypatch.setattr(route, "load_candles", lambda *args, **kwargs: [])
    monkeypatch.setattr(route, "run_backtest", lambda *args, **kwargs: {"metrics": {"walk_forward": {"enabled": False}}, "trades": []})

    result = route.create_backtest(symbol="BTCUSDT", timeframe="4h", conn=fake_conn)

    assert result["id"] == 7
    assert fake_conn.backtest_params[4].obj == strategy_params
