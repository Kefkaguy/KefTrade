from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.strategy_research import build_parameter_sweep, run_strategy_research


def test_parameter_sweep_generates_valid_variants() -> None:
    base = {
        "ema_fast": 20,
        "ema_slow": 50,
        "rsi_min": 40,
        "rsi_max": 60,
        "volume_change_min": -0.25,
        "entry_distance_to_ema20_max": 0.015,
        "swing_lookback": 5,
        "risk_reward": 2,
        "fee_rate": 0.001,
        "slippage_rate": 0.0005,
        "risk_per_trade": 0.01,
        "initial_equity": 10000,
        "walk_forward_train_ratio": 0.7,
    }
    sweep = {
        "ema_fast": [10, 60],
        "ema_slow": [50],
        "rsi_min": [35],
        "rsi_max": [65],
        "risk_reward": [2],
        "swing_lookback": [5],
    }

    variants = build_parameter_sweep(base, sweep)

    assert len(variants) == 1
    assert variants[0]["ema_fast"] == 10
    assert variants[0]["ema_slow"] == 50


def test_strategy_research_ranks_runs_from_identical_data(monkeypatch) -> None:
    from app.services import strategy_research

    base = {
        "ema_fast": 20,
        "ema_slow": 50,
        "rsi_min": 40,
        "rsi_max": 60,
        "volume_change_min": -0.25,
        "entry_distance_to_ema20_max": 0.015,
        "swing_lookback": 5,
        "risk_reward": 2,
        "fee_rate": 0.001,
        "slippage_rate": 0.0005,
        "risk_per_trade": 0.01,
        "initial_equity": 10000,
        "walk_forward_train_ratio": 0.7,
    }
    sweep = {
        "ema_fast": [10],
        "ema_slow": [50],
        "rsi_min": [35],
        "rsi_max": [65],
        "risk_reward": [1.5, 2.5],
        "swing_lookback": [5],
    }

    def fake_backtest(candles, features, params, strategy_decide):
        assert candles == ["same candles"]
        assert features == ["same features"]
        profit_factor = 2 if params["risk_reward"] == 2.5 else 1
        return {
            "metrics": {
                "profit_factor": profit_factor,
                "expectancy_per_trade": profit_factor * 10,
                "max_drawdown": 0.05,
                "sharpe_ratio": profit_factor,
                "win_rate": 0.5,
                "number_of_trades": 12,
                "average_win": 120,
                "average_loss": 60,
                "longest_losing_streak": 2,
                "average_holding_time_hours": 8,
                "walk_forward": {"enabled": True},
            },
            "trades": [],
            "equity_curve_summary": {"points": 3, "start": 10000, "end": 10100, "high": 10100, "low": 10000},
        }

    monkeypatch.setattr(strategy_research, "run_backtest", fake_backtest)

    report = run_strategy_research(
        candles=["same candles"],
        features=["same features"],
        strategy_name="trend_pullback",
        strategy_version="v1",
        base_params=base,
        sweep=sweep,
    )

    assert report["run_count"] == 2
    assert report["ranking_table"][0]["parameters"]["risk_reward"] == 2.5
    assert "profit_factor" in report["charts"]
    assert "## Recommendation" in report["ranking_table"][0]["markdown_report"]


def test_strategy_research_compares_full_strategy_library(monkeypatch) -> None:
    from app.services import strategy_research

    start = datetime(2024, 1, 1, tzinfo=UTC)
    candles = [
        {
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "timestamp": start + timedelta(hours=4 * index),
            "open": Decimal("100"),
            "high": Decimal("105"),
            "low": Decimal("95"),
            "close": Decimal("102"),
            "volume": Decimal("1000"),
        }
        for index in range(3)
    ]
    features = [
        {
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "timestamp": candle["timestamp"],
            "ema_50": Decimal("100"),
            "returns_5": Decimal("0.02"),
            "volatility_20": Decimal("0.012"),
        }
        for candle in candles
    ]

    def fake_backtest(candles, features, params, strategy_decide):
        trade_count = 35 if params["risk_reward"] >= 2 else 10
        return {
            "metrics": {
                "profit_factor": 1.3 if trade_count == 35 else 0.8,
                "expectancy_per_trade": 15 if trade_count == 35 else -5,
                "max_drawdown": 0.08,
                "sharpe_ratio": 0.4,
                "win_rate": 0.55,
                "number_of_trades": trade_count,
                "average_win": 100,
                "average_loss": 70,
                "longest_losing_streak": 3,
                "average_holding_time_hours": 12,
                "walk_forward": {"enabled": True},
            },
            "trades": [
                {
                    "symbol": "BTCUSDT",
                    "side": "long",
                    "entry_time": candles[1]["timestamp"],
                    "exit_time": candles[2]["timestamp"],
                    "entry_price": Decimal("100"),
                    "exit_price": Decimal("101"),
                    "pnl": Decimal("25"),
                    "pnl_pct": Decimal("0.0025"),
                    "exit_reason": "take_profit",
                    "holding_period_hours": 4,
                    "entry_reason": ["test setup"],
                    "entry_candle": candles[1],
                    "exit_candle": candles[2],
                    "indicators": {
                        "rsi_14": Decimal("55"),
                        "distance_from_ema_20": Decimal("0.01"),
                        "distance_from_ema_50": Decimal("0.02"),
                        "macd": Decimal("1"),
                        "volume_change": Decimal("0.1"),
                        "volatility_20": Decimal("0.012"),
                    },
                }
            ],
            "equity_curve_summary": {"points": 2, "start": 10000, "end": 10025, "high": 10025, "low": 10000},
        }

    monkeypatch.setattr(strategy_research, "run_backtest", fake_backtest)

    report = run_strategy_research(candles=candles, features=features)

    assert report["run_count"] == 6
    assert len(report["strategy_library"]) == 6
    assert report["ranking_table"][0]["recommendation"] == "Candidate for Paper Trading"
    assert report["ranking_table"][0]["by_year"][0]["year"] == 2024
    assert report["ranking_table"][0]["by_market_regime"][0]["regime"] == "bull_trend"
    assert report["ranking_table"][0]["by_volatility_regime"][0]["regime"] == "normal_volatility"
    assert report["ranking_table"][0]["by_trend_strength"][0]["regime"] == "weak"
    assert report["ranking_table"][0]["feature_correlations"]
    assert report["ranking_table"][0]["trade_explorer"][0]["trend_regime"] == "bull_trend"
    assert "monthly_returns" in report["ranking_table"][0]["dashboard"]
    assert "strategy_heatmap" in report["dashboard"]
    assert "Executive Summary" in report["markdown_report"]
