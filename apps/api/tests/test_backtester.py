from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path

from app.services.backtester import calculate_metrics, count_setup_opportunities, find_exit_index, mark_to_market_equity, run_backtest, walk_forward_split
from app.services.strategy import StrategyDecision


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


def test_backtest_honors_strategy_decision_risk_reward() -> None:
    candles, features = make_rows(120)

    def decide(candle, feature, recent_candles, params):
        close = Decimal(candle["close"])
        return StrategyDecision("setup", (close, close), close - Decimal("1"), close + Decimal("3"), Decimal("3"), ["test"])

    result = run_backtest(
        candles,
        features,
        {**PARAMS, "risk_reward": 1, "walk_forward_train_ratio": .7},
        decide,
    )

    assert result["trades"]
    trade = result["trades"][0]
    assert trade["take_profit"] - trade["entry_price"] > Decimal("2")


def test_backtest_enters_after_signal_candle_to_avoid_lookahead() -> None:
    candles, features = make_rows()
    result = run_backtest(candles, features, PARAMS)

    assert result["metrics"]["walk_forward"]["enabled"] is True
    assert result["trades"]
    assert result["trades"][0]["entry_time"] == candles[71]["timestamp"]
    assert result["trades"][0]["entry_price"] == candles[71]["open"]


def test_frozen_long_backtest_baseline_v1() -> None:
    candles, features = make_rows()
    result = run_backtest(candles, features, PARAMS)
    fixture = json.loads((Path(__file__).parent / "fixtures" / "long_backtest_baseline_v1.json").read_text(encoding="utf-8"))
    trades = [
        {
            key: value.isoformat() if hasattr(value, "isoformat") else str(value) if isinstance(value, Decimal) else value
            for key, value in trade.items()
            if key in fixture["trades"][0]
        }
        for trade in result["trades"]
    ]

    assert result["metrics"] == fixture["metrics"]
    assert trades == fixture["trades"]


def test_backtest_emits_regular_marked_returns_and_signal_exposure() -> None:
    candles, features = make_rows()

    result = run_backtest(candles, features, PARAMS)

    assert len(result["strategy_returns"]) == 30
    assert set(result["strategy_returns"]) == set(result["signal_exposure"])
    assert any(value != 0 for value in result["strategy_returns"].values())
    assert set(result["signal_exposure"].values()) <= {-1, 0, 1}


def test_backtest_can_delay_entry_by_additional_bars() -> None:
    candles, features = make_rows()
    candles[72]["open"] = Decimal("104")

    result = run_backtest(candles, features, {**PARAMS, "entry_delay_bars": 1})

    assert result["trades"]
    assert result["trades"][0]["entry_time"] == candles[72]["timestamp"]
    assert result["trades"][0]["entry_price"] == candles[72]["open"]


def test_same_candle_stop_target_policy_is_stop_first() -> None:
    candles, features = make_rows()
    result = run_backtest(candles, features, PARAMS)

    assert result["trades"][0]["exit_reason"] == "stop_loss_stop_first"
    assert result["trades"][0]["exit_price"] == result["trades"][0]["stop_loss"]
    assert result["metrics"]["max_drawdown"] > 0


def test_short_backtest_uses_inverse_geometry_and_pnl() -> None:
    candles, features = make_rows()
    candles[71].update({"open": Decimal("100"), "high": Decimal("101"), "low": Decimal("93"), "close": Decimal("95")})

    def short_setup(candle, feature, recent_candles, params):
        close = Decimal(candle["close"])
        if candle["timestamp"] != candles[70]["timestamp"]:
            return StrategyDecision("avoid", None, None, None, None, ["wait"], direction="short")
        return StrategyDecision("setup", (close, close), Decimal("102"), Decimal("96"), Decimal("2"), ["short"], direction="short")

    result = run_backtest(candles, features, PARAMS, short_setup)
    trade = result["trades"][0]

    assert trade["side"] == "short"
    assert trade["stop_loss"] > trade["entry_price"]
    assert trade["take_profit"] < trade["entry_price"]
    assert trade["pnl"] > 0


def test_short_same_candle_stop_and_target_is_stop_first() -> None:
    rows = [{"candle": {"low": Decimal("90"), "high": Decimal("110")}}]
    arrays = {"low": __import__("numpy").array([90.0]), "high": __import__("numpy").array([110.0])}

    index, reason = find_exit_index(rows, arrays, start_index=0, stop_loss=Decimal("105"), take_profit=Decimal("95"), max_holding_bars=0, direction="short")

    assert index == 0
    assert reason == "stop_loss_stop_first"


def test_short_mark_to_market_inverts_price_movement() -> None:
    assert mark_to_market_equity(Decimal("10000"), Decimal("100"), Decimal("90"), Decimal("10"), direction="short") == Decimal("10100")


def test_backtest_can_exit_after_max_holding_bars() -> None:
    candles, features = make_rows()

    def always_setup(candle, feature, recent_candles, params):
        close = Decimal(candle["close"])
        return StrategyDecision("setup", (close, close), close - Decimal("50"), close + Decimal("50"), Decimal("2"), ["test setup"])

    result = run_backtest(candles, features, {**PARAMS, "max_holding_bars": 2}, always_setup)

    assert result["trades"]
    assert result["trades"][0]["exit_reason"] == "time_exit"


def test_frequency_screen_counts_raw_validation_opportunities_without_simulating_positions() -> None:
    candles, features = make_rows()

    def always_setup(candle, feature, recent_candles, params):
        close = Decimal(candle["close"])
        return StrategyDecision("setup", (close, close), close - Decimal("1"), close + Decimal("2"), Decimal("2"), ["screen"])

    result = count_setup_opportunities(candles, features, PARAMS, always_setup)

    assert result["walk_forward_enabled"] is True
    assert result["execution_rows"] == 30
    assert result["opportunities"] == 29


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
