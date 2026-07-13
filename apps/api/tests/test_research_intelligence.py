from datetime import UTC, datetime, timedelta

from app.services.research_intelligence import build_research_intelligence, filter_archive, persist_research_ranking_snapshots


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


def test_research_score_is_deterministic_weighted_and_classified() -> None:
    row = candidate("tsla_momentum_bull_v2_007", 10, "Research More", "TSLA", "1h", "bull_trend", 2026)
    row["metrics"] = {
        "profit_factor": 1.5220,
        "expectancy_per_trade": 17.2001,
        "number_of_trades": 56,
        "max_drawdown": 0.0367,
        "out_of_sample_score": 0.75,
        "walk_forward_pass_rate": 0.7,
        "stability_score": 0.8,
        "cross_asset_consistency": 0.6,
        "timeframe_consistency": 0.7,
    }
    now = datetime(2026, 7, 6, tzinfo=UTC)
    context = {
        "symbols": [{"symbol": "TSLA", "asset_class": "us_equity"}],
        "latest_candles": [{"symbol": "TSLA", "timeframe": "1h", "timestamp": now}],
        "snapshot_timestamp": now,
    }
    evidence = build_research_intelligence([], [{"id": 1, "result": {"leaderboard": [row]}, "created_at": now, "recommendation": "Research More"}], [], [], **context)
    ranked = evidence["rankings"][0]

    assert ranked["score"]["calculation_version"] == "research_score_v1"
    assert ranked["score"]["component_weights"]["performance_quality"] == 20
    assert ranked["research_score"] == build_research_intelligence([], [{"id": 1, "result": {"leaderboard": [row]}, "created_at": now, "recommendation": "Research More"}], [], [], **context)["rankings"][0]["research_score"]
    assert ranked["classification"] in {"Strong research candidate", "Promising but incomplete", "High-quality research evidence"}


def test_missing_and_insufficient_metrics_are_explicit_not_perfect_or_zero() -> None:
    row = candidate("incomplete", 1, "Research More", "AAPL", "1h", "sideways", 2026)
    row["metrics"] = {"profit_factor": None, "expectancy_per_trade": None, "number_of_trades": 8, "max_drawdown": None}

    report = build_research_intelligence([], [{"id": 1, "result": {"leaderboard": [row]}, "created_at": datetime(2026, 7, 6, tzinfo=UTC), "recommendation": "Research More"}], [], [])
    score = report["rankings"][0]["score"]

    states = {item["state"] for item in score["missing_inputs"]}
    assert "Missing" in states
    assert "Insufficient sample" in states
    assert 0 < score["total_score"] < 100


def test_stale_data_and_unhealthy_deployment_lower_review_priority() -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    strong = candidate("strong_stale", 10, "Research More", "TSLA", "1h", "bull_trend", 2026)
    strong["metrics"].update({"profit_factor": 2.2, "expectancy_per_trade": 20, "number_of_trades": 200, "max_drawdown": 0.02, "out_of_sample_score": 0.9})
    healthy = candidate("healthy_setup", 7, "Research More", "NVDA", "1h", "bull_trend", 2026)
    healthy["metrics"].update({"profit_factor": 1.3, "expectancy_per_trade": 8, "number_of_trades": 80, "max_drawdown": 0.05, "out_of_sample_score": 0.6})

    report = build_research_intelligence(
        [],
        [{"id": 1, "result": {"leaderboard": [strong, healthy]}, "created_at": now, "recommendation": "Research More"}],
        [],
        [],
        symbols=[{"symbol": "TSLA", "asset_class": "us_equity"}, {"symbol": "NVDA", "asset_class": "us_equity"}],
        latest_candles=[{"symbol": "TSLA", "timeframe": "1h", "timestamp": now - timedelta(days=8)}, {"symbol": "NVDA", "timeframe": "1h", "timestamp": now}],
        reviews=[{"symbol": "NVDA", "timeframe": "1h", "strategy_id": "generated_alpha_v1", "status": "Setup Worth Reviewing", "verdict": "Setup Worth Reviewing", "created_at": now, "simulation_only": True}],
        deployments=[{"symbol": "TSLA", "timeframe": "1h", "strategy_name": "generated_alpha", "strategy_version": "v1", "status": "active", "simulation_only": True, "last_signal": "stale_data_warning"}],
        snapshot_timestamp=now,
    )

    priorities = {row["candidate_id"]: row for row in report["review_priorities"]}
    assert priorities["healthy_setup"]["priority_rank"] < priorities["strong_stale"]["priority_rank"]
    assert "Stale stored market data" in " ".join(priorities["strong_stale"]["blocking_issues"])


def test_leaderboards_portfolio_and_snapshot_persistence() -> None:
    report = build_research_intelligence(*research_history())

    assert report["strategy_leaderboard"]
    assert report["asset_leaderboard"]
    assert "research_diversification_score" in report["portfolio_intelligence"]
    conn = SnapshotConn()
    persist_research_ranking_snapshots(conn, report["rankings"], datetime(2026, 7, 12, tzinfo=UTC))

    assert len(conn.inserted) == len(report["rankings"])
    assert conn.inserted[0]["calculation_version"] == "research_score_v1"


class SnapshotResult:
    def fetchall(self):
        return []


class SnapshotConn:
    def __init__(self):
        self.inserted = []

    def execute(self, query, params=None):
        if "INSERT INTO research_ranking_snapshots" in query:
            self.inserted.append(
                {
                    "candidate_id": params[0],
                    "research_score": params[1],
                    "rank": params[2],
                    "classification": params[3],
                    "review_priority": params[4],
                    "component_scores": params[5],
                    "calculation_version": params[6],
                }
            )
        return SnapshotResult()
