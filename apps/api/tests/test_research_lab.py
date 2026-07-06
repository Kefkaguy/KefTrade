from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.alpha_discovery import AlphaCandidate
from app.services.alpha_validation import ValidationDataset
from app.services.research_lab import ResearchHypothesis, explain_failure, run_research_experiment


def make_dataset(symbol: str = "BTCUSDT", timeframe: str = "4h") -> ValidationDataset:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    candles = []
    features = []
    regimes = []
    for index in range(120):
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


def make_candidate() -> AlphaCandidate:
    return AlphaCandidate(
        name="generated_alpha",
        version="v1",
        description="Test candidate",
        parameters={
            "trend_filter": "ema",
            "trend_fast": 20,
            "trend_slow": 50,
            "momentum_block": "rsi",
            "rsi_min": 35,
            "volatility_block": "none",
            "volatility_min": 0.01,
            "volume_block": "none",
            "price_action": "pullback",
            "risk_reward": 2.0,
            "atr_multiplier": 1.5,
        },
        blocks={
            "trend_filter": "ema",
            "momentum": "rsi",
            "volatility": "none",
            "volume": "none",
            "price_action": "pullback",
        },
    )


def validation_row(candidate: AlphaCandidate) -> dict:
    return {
        "rank": 0,
        "candidate_id": "validation_test",
        "strategy_name": candidate.name,
        "strategy_version": candidate.version,
        "blocks": candidate.blocks,
        "parameters": candidate.parameters,
        "metrics": {
            "profit_factor": 0.95,
            "expectancy_per_trade": -3.0,
            "max_drawdown": 0.12,
            "sharpe_ratio": -0.2,
            "number_of_trades": 80,
            "average_win": 30,
            "average_loss": 45,
            "win_rate": 0.42,
        },
        "market_results": [
            {
                "symbol": "BTCUSDT",
                "timeframe": "4h",
                "by_year": [
                    {"year": 2024, "metrics": {"profit_factor": 1.35, "expectancy_per_trade": 12, "number_of_trades": 20}},
                    {"year": 2025, "metrics": {"profit_factor": 0.7, "expectancy_per_trade": -8, "number_of_trades": 25}},
                ],
                "by_regime": [
                    {"regime": "bull_trend", "metrics": {"profit_factor": 1.4, "expectancy_per_trade": 10, "number_of_trades": 18}},
                    {"regime": "sideways", "metrics": {"profit_factor": 0.5, "expectancy_per_trade": -11, "number_of_trades": 15}},
                ],
                "by_volatility": [
                    {"regime": "high_volatility", "metrics": {"profit_factor": 1.3, "expectancy_per_trade": 7, "number_of_trades": 12}},
                    {"regime": "low_volatility", "metrics": {"profit_factor": 0.6, "expectancy_per_trade": -6, "number_of_trades": 16}},
                ],
            }
        ],
        "robustness": {},
        "stability": {
            "stability_score": 0.25,
            "cross_asset_score": 0.0,
            "confidence_interval_width": 0.8,
            "confidence_score": 40,
        },
        "evidence_rules": {
            "min_trades": False,
            "profit_factor": False,
            "stability": False,
            "confidence_interval": False,
        },
        "validation_score": -5,
        "recommendation": "Reject",
        "markdown_report": "",
    }


def test_research_experiment_tracks_hypothesis_failure_edges_and_journal(monkeypatch) -> None:
    from app.services import research_lab

    candidate = make_candidate()
    monkeypatch.setattr(research_lab, "generate_alpha_candidates", lambda max_candidates=25: [candidate])
    monkeypatch.setattr(research_lab, "validate_candidate", lambda candidate, datasets, monte_carlo_runs, bootstrap_runs, thresholds: validation_row(candidate))

    report = run_research_experiment(
        hypothesis=ResearchHypothesis(
            title="Volatility compression momentum",
            hypothesis="Momentum performs better after volatility compression.",
            tags=["momentum", "volatility"],
        ),
        datasets=[make_dataset()],
        max_candidates=1,
        monte_carlo_runs=10,
        bootstrap_runs=10,
    )

    top = report["leaderboard"][0]
    assert report["summary"]["best_recommendation"] == "Reject"
    assert report["journal_entry"]["conclusion"] == "No statistically valid edge found under current evidence rules."
    assert top["failure_analysis"]["trade_frequency"] == "Insufficient"
    assert top["failure_analysis"]["loss_regimes"][0]["condition"] == "sideways"
    assert top["edge_conditions"][0]["condition_type"] == "market_regime"
    assert top["strategy_evolution"][0]["to_strategy"] == "generated_alpha_v2"
    assert "Alpha Research Experiment" in report["markdown_report"]


def test_failure_analysis_explains_evidence_gate_failures() -> None:
    candidate = make_candidate()
    analysis = explain_failure(validation_row(candidate))

    assert "Trade frequency was insufficient for statistical review." in analysis["why_failed"]
    assert "Profit factor did not exceed the required evidence threshold." in analysis["why_failed"]
    assert analysis["exit_quality"] == "Average loss exceeded average win."
