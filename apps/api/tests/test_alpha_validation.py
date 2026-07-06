from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.alpha_validation import (
    ValidationDataset,
    evaluate_evidence_rules,
    run_alpha_validation,
    run_bootstrap,
)


def make_dataset(symbol: str = "BTCUSDT", timeframe: str = "4h") -> ValidationDataset:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    candles = []
    features = []
    regimes = []
    for index in range(140):
        timestamp = start + timedelta(hours=4 * index)
        close = Decimal(100 + index)
        candles.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
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
                "symbol": symbol,
                "timeframe": timeframe,
                "timestamp": timestamp,
                "rsi_14": Decimal("55"),
                "macd": Decimal("2"),
                "macd_signal": Decimal("1"),
                "returns_5": Decimal("0.03"),
                "volume_change": Decimal("0.05"),
                "volatility_20": Decimal("0.012"),
                "ema_50": close - Decimal("10"),
                "distance_from_ema_50": Decimal("0.1"),
            }
        )
        regimes.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "timestamp": timestamp,
                "trend_regime": "bull_trend",
                "volatility_regime": "normal_volatility",
                "trend_strength": Decimal("0.1"),
            }
        )
    return ValidationDataset(symbol=symbol, timeframe=timeframe, candles=candles, features=features, regimes=regimes)


def test_evidence_rules_reject_low_trade_count() -> None:
    gates = evaluate_evidence_rules(
        {"number_of_trades": 14, "profit_factor": 1.4},
        stability=0.8,
        confidence_width=0.1,
        thresholds={"min_trades": 100, "min_profit_factor": 1.2, "min_stability_score": 0.6, "max_confidence_interval_width": 0.35},
    )

    assert gates["min_trades"] is False
    assert gates["profit_factor"] is True


def test_bootstrap_is_deterministic() -> None:
    trades = [
        {"pnl": Decimal("100"), "pnl_pct": Decimal("0.01")},
        {"pnl": Decimal("-50"), "pnl_pct": Decimal("-0.005")},
        {"pnl": Decimal("25"), "pnl_pct": Decimal("0.0025")},
    ]

    assert run_bootstrap(trades, 25) == run_bootstrap(trades, 25)


def test_alpha_validation_runs_cross_asset(monkeypatch) -> None:
    from app.services import alpha_validation

    datasets = [make_dataset("BTCUSDT"), make_dataset("ETHUSDT")]

    def fake_backtest(candles, features, params, strategy_decide):
        return {
            "metrics": {
                "initial_equity": 10000,
                "final_equity": 10300,
                "profit_factor": 1.35,
                "expectancy_per_trade": 10,
                "max_drawdown": 0.05,
                "sharpe_ratio": 0.5,
                "number_of_trades": 60,
                "win_rate": 0.55,
                "walk_forward": {"enabled": True},
            },
            "trades": [
                {
                    "symbol": candles[0]["symbol"],
                    "side": "long",
                    "entry_time": candles[90]["timestamp"],
                    "exit_time": candles[91]["timestamp"],
                    "pnl": Decimal("25"),
                    "pnl_pct": Decimal("0.0025"),
                }
                for _ in range(60)
            ],
        }

    monkeypatch.setattr(alpha_validation, "run_backtest", fake_backtest)
    report = run_alpha_validation(datasets, max_candidates=2, monte_carlo_runs=10, bootstrap_runs=10)

    assert report["candidate_count"] == 2
    assert report["summary"]["datasets"][0]["symbol"] == "BTCUSDT"
    assert report["leaderboard"][0]["stability"]["cross_asset_score"] == 1.0
    assert report["leaderboard"][0]["evidence_rules"]["min_trades"] is True
    assert "Alpha Validation Report" in report["markdown_report"]
