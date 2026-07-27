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
        "promotion_state": "elite",
        "validation_state": "validated",
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


def test_variant_exposes_promotion_and_validation_state_for_paper_lab_eligibility() -> None:
    # The paper lab has to be able to verify "promotion_state='elite' and
    # validation_state='validated'" from the evidence itself, not merely trust
    # that load_elite_candidate_variants's own WHERE clause already filtered
    # for it -- otherwise a legacy elite promoted through the older
    # pooled-consistency gate would be silently indistinguishable from one that
    # actually passed the champion validation battery.
    row = elite_job_row()
    row["validation_state"] = "pending_validation"

    variant = candidate_variant(row)

    assert variant["promotion_state"] == "elite"
    assert variant["validation_state"] == "pending_validation"


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
            assert "jsonb_object_length" not in query
            assert "COALESCE(j.result->'strategy_returns', '{}'::jsonb) = '{}'::jsonb" in query
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


# --- Paper lab wiring: mode dispatch and pool loading ------------------------


def test_paper_lab_preview_from_database_evaluates_the_full_elite_pool(monkeypatch) -> None:
    from app.services import elite_portfolio_repository as repo

    pool = [{
        "candidate_key": "c1|AMD|30m", "candidate_id": "c1", "campaign_id": 1, "research_job_id": 100,
        "promotion_state": "elite", "validation_state": "validated", "symbol": "AMD", "timeframe": "30m",
        "family_id": "session_momentum", "strategy_direction": "long", "execution_capability": "external_observe",
        "parameters": {}, "profit_factor": 1.8, "expectancy": 6.0, "max_drawdown": 0.05, "trade_count": 90,
        "quality_score": 0.8, "research_score": 0.8, "strategy_returns": {}, "signal_returns": {},
    }]
    monkeypatch.setattr(repo, "load_elite_candidate_variants", lambda _conn: pool)

    result = repo.paper_lab_preview_from_database(object())

    assert result["eligible_count"] == 1
    assert result["mode"] == "all_validated_elites_paper_lab"


def test_recompute_for_run_dispatches_to_the_paper_lab_preview_by_mode(monkeypatch) -> None:
    from app.services import elite_portfolio_repository as repo

    monkeypatch.setattr(repo, "paper_lab_preview_from_database", lambda _conn: {"snapshot": {"decision_hash": "paper-lab-hash"}})
    monkeypatch.setattr(
        repo,
        "preview_from_database",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not call the diversified preview for a paper lab run")),
    )

    run = {"source_configuration": {"mode": "all_validated_elites_paper_lab"}}
    result = repo._recompute_for_run(object(), run)

    assert result["snapshot"]["decision_hash"] == "paper-lab-hash"


def test_recompute_for_run_uses_the_diversified_preview_for_a_normal_run(monkeypatch) -> None:
    from app.services import elite_portfolio_repository as repo

    monkeypatch.setattr(repo, "preview_from_database", lambda _conn, config, use_cache=False: {"snapshot": {"decision_hash": "diversified-hash"}, "seen_config": config})
    monkeypatch.setattr(
        repo,
        "paper_lab_preview_from_database",
        lambda _conn: (_ for _ in ()).throw(AssertionError("must not call the paper lab preview for a diversified run")),
    )

    run = {"source_configuration": {"profile": "strict_diversified"}}
    result = repo._recompute_for_run(object(), run)

    assert result["snapshot"]["decision_hash"] == "diversified-hash"
    assert result["seen_config"] == {"profile": "strict_diversified"}


def test_recompute_for_run_treats_a_run_with_no_mode_key_as_diversified(monkeypatch) -> None:
    # A run created before the paper lab existed has no "mode" key at all --
    # this must still route to the diversified preview, not raise or silently
    # take the paper lab path.
    from app.services import elite_portfolio_repository as repo

    monkeypatch.setattr(repo, "preview_from_database", lambda _conn, config, use_cache=False: {"snapshot": {"decision_hash": "diversified-hash"}})
    monkeypatch.setattr(
        repo,
        "paper_lab_preview_from_database",
        lambda _conn: (_ for _ in ()).throw(AssertionError("must not call the paper lab preview")),
    )

    result = repo._recompute_for_run(object(), {"source_configuration": {}})

    assert result["snapshot"]["decision_hash"] == "diversified-hash"
