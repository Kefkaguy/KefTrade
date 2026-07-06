from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.alpha_discovery import (
    calculate_confidence_score,
    generate_alpha_candidates,
    make_strategy_definition,
    run_alpha_discovery,
    run_monte_carlo,
)


def test_generator_builds_valid_deterministic_candidates() -> None:
    candidates = generate_alpha_candidates(
        {
            "trend_filter": ["ema"],
            "trend_fast": [20, 100],
            "trend_slow": [50],
            "momentum_block": ["rsi"],
            "rsi_min": [35],
            "roc_min": [0.01],
            "volatility_block": ["none"],
            "volatility_min": [0.01],
            "volume_block": ["none"],
            "volume_change_min": [0],
            "price_action": ["pullback"],
            "breakout_lookback": [12],
            "swing_lookback": [5],
            "risk_reward": [2],
            "atr_multiplier": [1.5],
        },
        max_candidates=10,
    )

    assert len(candidates) == 1
    assert candidates[0].parameters["trend_fast"] == 20
    assert candidates[0].blocks["momentum"] == "rsi"


def test_generated_strategy_definition_uses_common_interface() -> None:
    candidate = generate_alpha_candidates(max_candidates=1)[0]
    strategy = make_strategy_definition(candidate)

    assert strategy.name == "generated_alpha"
    assert strategy.parameters
    assert strategy.entry_rules
    assert callable(strategy.decide)


def test_monte_carlo_is_deterministic() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    trades = [
        {"exit_time": start, "pnl": Decimal("100"), "pnl_pct": Decimal("0.01")},
        {"exit_time": start + timedelta(days=1), "pnl": Decimal("-50"), "pnl_pct": Decimal("-0.005")},
        {"exit_time": start + timedelta(days=2), "pnl": Decimal("25"), "pnl_pct": Decimal("0.0025")},
    ]

    first = run_monte_carlo(trades, runs=25)
    second = run_monte_carlo(trades, runs=25)

    assert first == second
    assert first["p50_final_equity"] == 10075.0


def test_alpha_discovery_returns_leaderboard(monkeypatch) -> None:
    from app.services import alpha_discovery

    start = datetime(2024, 1, 1, tzinfo=UTC)
    candles = []
    features = []
    for index in range(120):
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

    def fake_backtest(candles, features, params, strategy_decide):
        return {
            "metrics": {
                "profit_factor": 1.4,
                "expectancy_per_trade": 20,
                "max_drawdown": 0.05,
                "sharpe_ratio": 0.5,
                "number_of_trades": 35,
                "win_rate": 0.6,
            },
            "trades": [
                {
                    "symbol": "BTCUSDT",
                    "side": "long",
                    "entry_time": candles[80]["timestamp"],
                    "exit_time": candles[81]["timestamp"],
                    "pnl": Decimal("50"),
                    "pnl_pct": Decimal("0.005"),
                }
            ],
        }

    monkeypatch.setattr(alpha_discovery, "run_backtest", fake_backtest)
    report = run_alpha_discovery(candles, features, max_candidates=3, monte_carlo_runs=10)

    assert report["candidate_count"] == 3
    assert report["leaderboard"][0]["rank"] == 1
    assert "alpha_score" in report["leaderboard"][0]
    assert "Alpha Report" in report["leaderboard"][0]["alpha_report"]


def test_confidence_score_rewards_sample_and_stability() -> None:
    score = calculate_confidence_score(
        {"number_of_trades": 50},
        stability=1.0,
        consistency=0.6,
        monte_carlo={"p05_final_equity": 10100},
    )

    assert score > 80
