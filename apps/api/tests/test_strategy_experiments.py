from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.strategy_experiments import list_strategy_experiments, run_strategy_experiment


def make_rows() -> tuple[list[dict], list[dict], list[dict]]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    candles = []
    features = []
    regimes = []
    for index in range(80):
        timestamp = start + timedelta(hours=4 * index)
        close = Decimal(100 + index)
        candles.append(
            {
                "symbol": "BTCUSDT",
                "timeframe": "4h",
                "timestamp": timestamp,
                "open": close,
                "high": close + Decimal("2"),
                "low": close - Decimal("2"),
                "close": close,
                "volume": Decimal("1000"),
            }
        )
        features.append(
            {
                "symbol": "BTCUSDT",
                "timeframe": "4h",
                "timestamp": timestamp,
                "ema_50": close - Decimal("5"),
                "returns_5": Decimal("0.02"),
                "volatility_20": Decimal("0.012"),
                "distance_from_ema_50": Decimal("0.03"),
            }
        )
        regimes.append(
            {
                "symbol": "BTCUSDT",
                "timeframe": "4h",
                "timestamp": timestamp,
                "trend_regime": "bull_trend",
                "volatility_regime": "normal_volatility",
                "trend_strength": Decimal("0.03"),
            }
        )
    return candles, features, regimes


def test_strategy_experiment_catalog_includes_all_core_strategies() -> None:
    experiments = list_strategy_experiments()
    strategies = {experiment["strategy"] for experiment in experiments}

    assert {
        "trend_pullback",
        "breakout",
        "mean_reversion",
        "momentum",
        "volatility_breakout",
        "trend_following_200ema",
    }.issubset(strategies)
    assert all(experiment["hypothesis"] for experiment in experiments)
    assert all(experiment["sweep"] for experiment in experiments)


def test_strategy_experiment_runs_research_sweep(monkeypatch) -> None:
    from app.services import strategy_research

    candles, features, regimes = make_rows()

    def fake_backtest(candles, features, params, strategy_decide):
        trades = [
            {
                "symbol": "BTCUSDT",
                "side": "long",
                "entry_time": candles[60]["timestamp"],
                "exit_time": candles[61]["timestamp"],
                "entry_price": Decimal("100"),
                "exit_price": Decimal("101"),
                "pnl": Decimal("20"),
                "pnl_pct": Decimal("0.002"),
                "exit_reason": "take_profit",
                "holding_period_hours": 4,
                "entry_reason": ["experiment"],
                "entry_candle": candles[60],
                "exit_candle": candles[61],
                "indicators": {},
            }
        ]
        return {
            "metrics": {
                "profit_factor": 1.1,
                "expectancy_per_trade": 20,
                "max_drawdown": 0.03,
                "sharpe_ratio": 0.4,
                "win_rate": 0.55,
                "number_of_trades": 12,
                "average_win": 80,
                "average_loss": 50,
                "longest_losing_streak": 2,
                "average_holding_time_hours": 4,
                "walk_forward": {"enabled": True},
            },
            "trades": trades,
            "equity_curve_summary": {"points": 2, "start": 10000, "end": 10020, "high": 10020, "low": 10000},
        }

    monkeypatch.setattr(strategy_research, "run_backtest", fake_backtest)

    report = run_strategy_experiment(
        candles=candles,
        features=features,
        regimes=regimes,
        experiment_id="momentum_trend_return_sweep",
        max_runs=8,
    )

    assert report["experiment"]["strategy"] == "momentum"
    assert report["run_count"] <= 8
    assert report["ranking_table"]
    assert report["research_takeaways"]
