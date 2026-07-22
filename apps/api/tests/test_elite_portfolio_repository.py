from __future__ import annotations

import pytest

from app.services.elite_portfolio_repository import aligned_daily_evidence, backfill_correlation_evidence, candidate_variant, preview_from_database, trade_return_series


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
        "result": {"metrics": {"number_of_trades": 60, "walk_forward": {"validation_start": "2026-01-01T00:00:00Z", "validation_end": "2026-04-30T00:00:00Z"}}, "trades": trades},
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
    assert len(variant["strategy_returns"]) >= 30
    assert set(variant["strategy_returns"]) == set(variant["signal_returns"])


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


def test_historical_sparse_trades_are_aligned_to_frozen_daily_window() -> None:
    result = {
        "metrics": {"walk_forward": {"validation_start": "2026-01-01T00:00:00Z", "validation_end": "2026-03-31T00:00:00Z"}},
        "trades": [
            {"entry_time": "2026-01-05T15:00:00Z", "exit_time": "2026-01-08T16:00:00Z", "side": "long", "pnl_pct": 0.02},
            {"entry_time": "2026-02-02T15:00:00Z", "exit_time": "2026-02-03T16:00:00Z", "side": "short", "pnl_pct": -0.01},
        ],
    }

    returns, exposure = aligned_daily_evidence(result)

    assert len(returns) >= 60
    assert set(returns) == set(exposure)
    assert returns["2026-01-08T00:00:00+00:00"] == 0.02
    assert exposure["2026-01-06T00:00:00+00:00"] == 1.0
    assert exposure["2026-02-03T00:00:00+00:00"] == -1.0


def test_new_marked_returns_are_compounded_and_aligned_daily() -> None:
    result = {
        "strategy_returns": {
            "2026-01-05T10:00:00Z": 0.01,
            "2026-01-05T11:00:00Z": 0.02,
            "2026-01-06T10:00:00Z": -0.01,
        },
        "signal_exposure": {
            "2026-01-05T10:00:00Z": 1,
            "2026-01-06T10:00:00Z": -1,
        },
        "metrics": {"walk_forward": {"validation_start": "2026-01-01T00:00:00Z", "validation_end": "2026-03-31T00:00:00Z"}},
    }

    returns, exposure = aligned_daily_evidence(result)

    assert returns["2026-01-05T00:00:00+00:00"] == pytest.approx(0.0302)
    assert exposure["2026-01-06T00:00:00+00:00"] == -1


def test_stored_frozen_evidence_precedes_replay_fallback() -> None:
    row = elite_job_row()
    row["result"] = {"metrics": row["result"]["metrics"]}
    row["stored_strategy_returns"] = {
        f"2026-01-{day:02d}T10:00:00Z": 0.001 for day in range(1, 29)
    } | {
        f"2026-02-{day:02d}T10:00:00Z": -0.001 for day in range(1, 15)
    }
    row["stored_signal_exposure"] = {key: 1 for key in row["stored_strategy_returns"]}
    row["replay_trades"] = [{"entry_time": "2026-03-01T00:00:00Z", "exit_time": "2026-03-02T00:00:00Z", "pnl_pct": 0.9}]

    variant = candidate_variant(row)

    assert len(variant["strategy_returns"]) >= 30
    assert max(variant["strategy_returns"].values()) < 0.9


class BackfillResult:
    def __init__(self, rows=None):
        self.rows = rows or []

    def fetchall(self):
        return self.rows


class BackfillConnection:
    def __init__(self):
        self.inserted = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, query, params=()):
        if "SELECT DISTINCT ON (j.id)" in query:
            return BackfillResult([{
                "id": 41,
                "symbol": "AAPL",
                "timeframe": "1h",
                "candidate": {"candidate_id": "sd_test", "parameters": {}},
                "dataset_id": 12,
                "elite_candidate_id": 7,
            }])
        if "INSERT INTO elite_candidate_correlation_evidence" in query:
            self.inserted.append(params)
            return BackfillResult()
        raise AssertionError(query)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_backfill_appends_frozen_evidence_without_rewriting_results(monkeypatch) -> None:
    from app.services import research_campaigns

    timestamps = {f"2026-01-{day:02d}T10:00:00Z": 0.001 for day in range(1, 29)}
    timestamps.update({f"2026-02-{day:02d}T10:00:00Z": 0.001 for day in range(1, 4)})
    monkeypatch.setattr(research_campaigns, "run_campaign_job", lambda _conn, _job: {
        "strategy_returns": timestamps,
        "signal_exposure": {key: 1 for key in timestamps},
    })
    conn = BackfillConnection()

    result = backfill_correlation_evidence(conn, limit=20)

    assert result["generated"] == 1
    assert result["historical_results_rewritten"] is False
    assert result["constraints_relaxed"] == 0
    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert len(conn.inserted) == 1
