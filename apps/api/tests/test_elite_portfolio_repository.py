from __future__ import annotations

from app.services.elite_portfolio_repository import candidate_variant, preview_from_database, trade_return_series


def elite_job_row() -> dict:
    trades = [
        {"entry_timestamp": f"2026-01-{(index % 28) + 1:02d}T10:00:00Z", "exit_timestamp": f"2026-01-{(index % 28) + 1:02d}T11:00:00Z", "pnl_pct": 0.01 if index % 2 else -0.005}
        for index in range(60)
    ]
    return {
        "id": 7,
        "candidate_id": "sd_test",
        "campaign_id": 9,
        "strategy_name": "autonomous_strategy_discovery",
        "strategy_version": "sd_test",
        "family_id": "Bearish Breakdown",
        "strategy_direction": "short",
        "execution_capability": "internal_only",
        "symbol": "aapl",
        "timeframe": "4h",
        "candidate": {"parameters": {"lookback": 20}, "dataset_snapshot_id": "dataset-1"},
        "result": {"metrics": {"number_of_trades": 60}, "trades": trades},
        "research_score": 8.1,
        "profit_factor": 1.4,
        "expectancy": 0.02,
        "max_drawdown": 0.08,
        "trade_count": 60,
        "stability": 0.75,
        "assets_passed": 2,
        "timeframes_passed": 1,
        "regimes_passed": 2,
        "forward_validation_state": "collecting_forward_evidence",
    }


def test_database_row_becomes_immutable_strategy_market_variant() -> None:
    variant = candidate_variant(elite_job_row())

    assert variant["candidate_key"] == "sd_test|AAPL|4h"
    assert variant["strategy_direction"] == "short"
    assert variant["execution_capability"] == "internal_only"
    assert variant["health"] == "healthy"
    assert variant["dataset_ids"] == ["dataset-1"]
    assert len(variant["strategy_returns"]) == 60


def test_duplicate_trade_timestamps_remain_distinct_correlation_observations() -> None:
    result = {"trades": [{"exit_timestamp": "2026-01-01T10:00:00Z", "pnl_pct": 0.1}, {"exit_timestamp": "2026-01-01T10:00:00Z", "pnl_pct": -0.1}]}

    series = trade_return_series(result)

    assert len(series) == 2
    assert sorted(series.values()) == [-0.1, 0.1]


def test_cached_preview_uses_evidence_digest_without_loading_candidate_json(monkeypatch) -> None:
    from app.services import elite_portfolio_repository

    cached = {"status": "infeasible", "snapshot": {"decision_hash": "a" * 64}}
    monkeypatch.setattr(elite_portfolio_repository, "candidate_evidence_version", lambda _conn: {"variant_count": 355, "evidence_digest": "digest"})
    monkeypatch.setattr(elite_portfolio_repository, "get_json", lambda _key: dict(cached))
    monkeypatch.setattr(elite_portfolio_repository, "load_elite_candidate_variants", lambda _conn: (_ for _ in ()).throw(AssertionError("cache hit loaded evidence")))

    result = preview_from_database(object(), {})

    assert result["cache"]["hit"] is True
    assert result["snapshot"]["decision_hash"] == "a" * 64
