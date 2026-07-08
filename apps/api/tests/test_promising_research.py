from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.promising_research import ResearchDataset, evaluate_candidate, research_score, validation_status
from app.services.strategy import get_strategy_library
from app.services.strategy_experiments import get_experiment


def make_dataset(symbol: str, timeframe: str) -> ResearchDataset:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    candles = []
    features = []
    for index in range(240):
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
                "ema_50": close - Decimal("5"),
                "returns_5": Decimal("0.02"),
                "volatility_20": Decimal("0.012"),
                "distance_from_ema_50": Decimal("0.03"),
            }
        )
    return ResearchDataset(symbol=symbol, timeframe=timeframe, candles=candles, features=features, regimes=[])


def test_research_score_rewards_robust_profitable_candidates() -> None:
    strong = research_score({"profit_factor": 1.5, "expectancy_per_trade": 20, "max_drawdown": 0.05, "number_of_trades": 150}, 0.8, 0.8, 0.8)
    weak = research_score({"profit_factor": 0.7, "expectancy_per_trade": -10, "max_drawdown": 0.2, "number_of_trades": 20}, 0.1, 0.1, 0.0)

    assert strong > weak


def test_validation_status_does_not_bypass_thresholds() -> None:
    status = validation_status({"profit_factor": 1.1, "expectancy_per_trade": 5, "number_of_trades": 30}, 0.8, 0.8, 0.8)

    assert status == "Needs more evidence"


def test_evaluate_candidate_returns_cross_dataset_evidence(monkeypatch) -> None:
    from app.services import promising_research

    calls = {"count": 0}

    def fake_run_backtest(candles, features, params, decide):
        calls["count"] += 1
        profitable = calls["count"] % 2 == 0
        return {
            "metrics": {
                "profit_factor": 1.3 if profitable else 0.8,
                "expectancy_per_trade": 12 if profitable else -8,
                "max_drawdown": 0.04,
                "number_of_trades": 25,
                "win_rate": 0.55 if profitable else 0.42,
            }
        }

    monkeypatch.setattr(promising_research, "run_backtest", fake_run_backtest)
    experiment = get_experiment("breakout_lookback_volume_exit_sweep")
    strategy = get_strategy_library()["breakout_v1"]
    row = evaluate_candidate(
        {
            "candidate_id": "test_candidate",
            "experiment": experiment,
            "strategy": strategy,
            "parameters": strategy.parameters,
        },
        [make_dataset("BTCUSDT", "4h"), make_dataset("ETHUSDT", "4h")],
        train_ratio=0.7,
        fold_count=2,
    )

    assert row["candidate_id"] == "test_candidate"
    assert row["dataset_results"]
    assert row["train_test_results"]
    assert row["walk_forward"]["fold_count"] > 0
    assert row["research_report"]
