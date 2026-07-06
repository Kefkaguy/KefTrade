from datetime import UTC, datetime, timedelta

from app.services.research_intelligence import build_research_intelligence, filter_archive


def candidate(candidate_id: str, score: float, recommendation: str, symbol: str, timeframe: str, regime: str, year: int) -> dict:
    return {
        "candidate_id": candidate_id,
        "strategy_name": "generated_alpha",
        "strategy_version": "v1",
        "blocks": {
            "trend_filter": "ema",
            "momentum": "rsi",
            "volatility": "volatility",
            "volume": "relative_volume",
            "price_action": "pullback",
        },
        "parameters": {
            "trend_fast": 20,
            "trend_slow": 50,
            "rsi_min": 35,
            "risk_reward": 2.0,
            "atr_multiplier": 1.5,
        },
        "metrics": {
            "profit_factor": 0.8 if recommendation == "Reject" else 1.4,
            "expectancy_per_trade": -8 if recommendation == "Reject" else 12,
            "number_of_trades": 80,
        },
        "market_results": [
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "metrics": {
                    "profit_factor": 0.8 if recommendation == "Reject" else 1.4,
                    "expectancy_per_trade": -8 if recommendation == "Reject" else 12,
                    "number_of_trades": 80,
                },
                "by_year": [
                    {"year": year, "metrics": {"profit_factor": 0.8, "expectancy_per_trade": -8, "number_of_trades": 40}},
                ],
                "by_regime": [
                    {"regime": regime, "metrics": {"profit_factor": 0.7, "expectancy_per_trade": -10, "number_of_trades": 30}},
                ],
                "by_volatility": [
                    {"regime": "low_volatility", "metrics": {"profit_factor": 0.6, "expectancy_per_trade": -12, "number_of_trades": 30}},
                ],
            }
        ],
        "failure_analysis": {
            "why_failed": ["Performance was not stable across years, assets, regimes, or volatility buckets."],
            "loss_regimes": [{"condition": regime, "symbol": symbol, "timeframe": timeframe}],
            "loss_volatility_regimes": [{"condition": "low_volatility", "symbol": symbol, "timeframe": timeframe}],
        },
        "edge_conditions": [],
        "evidence_rules": {
            "min_trades": True,
            "profit_factor": recommendation != "Reject",
            "stability": False,
            "confidence_interval": False,
        },
        "validation_score": score,
        "recommendation": recommendation,
    }


def research_history():
    now = datetime(2026, 7, 6, tzinfo=UTC)
    hypotheses = [
        {
            "id": 1,
            "title": "Volatility compression momentum",
            "hypothesis": "Momentum works better after volatility compression.",
            "status": "rejected",
            "tags": ["momentum"],
            "created_at": now,
            "updated_at": now,
        }
    ]
    experiments = [
        {
            "id": 10,
            "hypothesis_id": 1,
            "name": "Volatility compression momentum",
            "recommendation": "Reject",
            "result": {
                "leaderboard": [
                    candidate("exp_a", -4, "Reject", "BTCUSDT", "4h", "low_volatility", 2024),
                    candidate("exp_b", -3, "Reject", "ETHUSDT", "1d", "low_volatility", 2025),
                ]
            },
            "created_at": now + timedelta(minutes=1),
        }
    ]
    journal_entries = [
        {
            "id": 100,
            "hypothesis_id": 1,
            "experiment_id": 10,
            "entry_type": "experiment_run",
            "conclusion": "No statistically valid edge found under current evidence rules.",
            "created_at": now + timedelta(minutes=2),
        }
    ]
    validation_runs = [
        {
            "id": 20,
            "candidate_count": 1,
            "report": {"leaderboard": [candidate("val_a", -2, "Reject", "BTCUSDT", "1d", "sideways", 2025)]},
            "created_at": now + timedelta(minutes=3),
        }
    ]
    return hypotheses, experiments, journal_entries, validation_runs


def test_research_intelligence_builds_traceable_recommendations_graph_and_timeline() -> None:
    report = build_research_intelligence(*research_history())

    assert report["summary"]["evidence_item_count"] == 3
    assert report["knowledge_engine"]["repeatedly_failed_hypotheses"][0]["hypothesis_id"] == 1
    rejection_rules = {row["value"] for row in report["meta_analysis"]["most_common_rejection_rules"]}
    assert "stability" in rejection_rules
    assert report["recommendations"]
    assert all(row["evidence_refs"] for row in report["recommendations"])
    assert any(node["type"] == "indicator" and node["label"] == "rsi" for node in report["knowledge_graph"]["nodes"])
    assert any(edge["relationship"] == "supports" for edge in report["knowledge_graph"]["edges"])
    assert any(event["event_type"] == "experiment" for event in report["timeline"])
    assert any(row["confidence"] in {"medium", "high"} for row in report["confidence"])
    assert "Research Intelligence Report" in report["markdown_report"]


def test_research_archive_filters_by_indicator_asset_regime_and_status() -> None:
    report = build_research_intelligence(*research_history())
    rows = filter_archive(
        report["archive"],
        {
            "strategy": "generated_alpha",
            "indicator": "rsi",
            "asset": "ETHUSDT",
            "timeframe": "1d",
            "market_regime": "low_volatility",
            "recommendation": "Reject",
            "failure_reason": "stable",
            "validation_status": "Reject",
        },
    )

    assert len(rows) == 1
    assert rows[0]["candidate_id"] == "exp_b"
