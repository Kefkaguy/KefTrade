from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services import research_campaigns
from app.services.rug import generate_rug_candidates, rug_channel_counts
from app.services.strategy_discovery import (
    candidate_execution_key,
    entry_window_passes,
    rsi_value,
)


def test_rug_is_reproducible_unique_and_changes_stream_by_batch() -> None:
    first, first_metrics = generate_rug_candidates(max_candidates=120, seed=42, batch_index=0)
    repeated, _ = generate_rug_candidates(max_candidates=120, seed=42, batch_index=0)
    next_batch, _ = generate_rug_candidates(max_candidates=120, seed=42, batch_index=1)

    assert [row.candidate_id for row in first] == [row.candidate_id for row in repeated]
    assert {row.candidate_id for row in first}.isdisjoint({row.candidate_id for row in next_batch})
    assert len({candidate_execution_key(row) for row in first}) == 120
    assert first_metrics["judge"] == "existing_backtester_and_validation_pipeline"
    assert first_metrics["promotion_authority"] is False


def test_rug_protects_random_exploration_and_uses_evidence_channels() -> None:
    no_evidence = rug_channel_counts(100, guidance_available=False)
    assert no_evidence == {"evidence_exploitation": 0, "random_exploration": 90, "challenge": 10}

    guidance = {
        "available": True,
        "search_prioritization": {"strategy_families": ["breakout"], "avoid_parameter_regions": []},
    }
    candidates, metrics = generate_rug_candidates(max_candidates=100, seed=9, guidance=guidance)
    assert metrics["channels"] == {"evidence_exploitation": 60, "random_exploration": 30, "challenge": 10}
    assert {row.parameters["rug_channel"] for row in candidates} == {
        "evidence_exploitation",
        "random_exploration",
        "challenge",
    }


def test_rug_changes_executable_rsi_periods_ema_risk_and_entry_windows() -> None:
    candidates, _ = generate_rug_candidates(max_candidates=250, seed=17)
    assert len({row.parameters["rsi_period"] for row in candidates}) > 1
    assert len({(row.parameters["trend_fast"], row.parameters["trend_slow"]) for row in candidates}) > 10
    assert len({row.parameters["atr_multiplier"] for row in candidates}) > 1
    assert len({row.parameters["risk_reward"] for row in candidates}) > 1
    assert len({(row.parameters["entry_start_minute_utc"], row.parameters["entry_end_minute_utc"]) for row in candidates}) > 1
    assert all(row.parameters["trend_fast"] < row.parameters["trend_slow"] for row in candidates)


def test_dynamic_rsi_and_entry_window_are_real_decision_inputs() -> None:
    start = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
    rising = [
        {"timestamp": start + timedelta(minutes=index), "close": Decimal(100 + index)}
        for index in range(30)
    ]
    assert rsi_value(rising, 7) == Decimal(100)
    assert rsi_value(rising[:8], 14) is None

    candle = {"timestamp": datetime(2026, 1, 1, 10, 0, tzinfo=UTC)}
    assert entry_window_passes(candle, {"entry_start_minute_utc": 570, "entry_end_minute_utc": 660}) is True
    assert entry_window_passes(candle, {"entry_start_minute_utc": 780, "entry_end_minute_utc": 960}) is False


def test_completed_rug_batch_queues_next_batch_after_learning(monkeypatch) -> None:
    observed = {}
    dispatched = {}
    scheduling = {}

    def fake_create(_conn, **kwargs):
        observed.update(kwargs)
        return {"campaign": {"id": 222}}

    def fake_dispatch(_conn, **kwargs):
        dispatched.update(kwargs)
        return {"started": True, "workers": kwargs["workers"]}

    def fake_scheduling(_conn, campaign_id, updates):
        scheduling.update({"campaign_id": campaign_id, "updates": updates})
        return {"campaign_id": campaign_id}

    monkeypatch.setattr(research_campaigns, "create_research_campaign", fake_create)
    monkeypatch.setattr(research_campaigns, "run_parallel_campaign_batch", fake_dispatch)
    monkeypatch.setattr(research_campaigns, "update_campaign_scheduling_config", fake_scheduling)
    campaign = {
        "universe_key": "research_core_ten",
        "dataset_id": 7,
        "dataset_mode": "reproducibility",
        "target_workers": 6,
        "scheduling_config": {"batch_size": 40, "daily_experiment_budget": 100000, "target_workers": 6},
        "controls": {
            "asset_limit": 10,
            "timeframes": ["15m", "30m"],
            "rug": {
                "enabled": True,
                "auto_continue": True,
                "seed": 99,
                "batch_index": 0,
                "batch_candidates": 1000,
                "batch_size": 1000,
                "target_candidates": 2500,
            },
        },
    }

    result = research_campaigns.continue_rug_run_after_learning(object(), campaign)

    assert result == {
        "queued": True,
        "complete": False,
        "completed_candidates": 1000,
        "target_candidates": 2500,
        "next_campaign_id": 222,
        "next_batch_index": 1,
        "next_batch_candidates": 1000,
        "inherited_workers": 6,
        "inherited_scheduling": {"batch_size": 40, "daily_experiment_budget": 100000},
        "worker_dispatch": {"started": True, "workers": 6},
    }
    assert observed["generator_mode"] == "rug"
    assert observed["rug_batch_index"] == 1
    assert observed["rug_auto_continue"] is True
    assert observed["dataset_id"] == 7
    assert dispatched == {"campaign_id": 222, "workers": 6, "jobs_per_worker": 40}
    assert scheduling == {
        "campaign_id": 222,
        "updates": {"batch_size": 40, "daily_experiment_budget": 100000},
    }
