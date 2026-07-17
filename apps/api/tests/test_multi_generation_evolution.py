from __future__ import annotations

from app.services.multi_generation_evolution import (
    PHASE_4_VERSION,
    build_evolution_blueprint,
    parent_eligibility,
    select_diverse_parents,
)
from app.services.strategy_discovery import candidate_execution_key


def promoted_parent(
    candidate_id: str,
    *,
    symbol: str = "GOOGL",
    family: str = "Trend Following",
    score: float = 10.0,
) -> dict:
    return {
        "id": int(candidate_id[-1]) if candidate_id[-1].isdigit() else 1,
        "campaign_id": 51,
        "candidate_id": candidate_id,
        "symbol": symbol,
        "timeframe": "1h",
        "strategy_family": family,
        "status": "promoted",
        "dataset_id": 1,
        "hypothesis_version_id": 28,
        "validation_score": score,
        "latest_error": None,
        "failure_classification": None,
        "candidate": {
            "candidate_id": candidate_id,
            "family_id": "family_test",
            "parent_candidate_id": None,
            "generation": 1,
            "blocks": {
                "trend": "ema_20_50",
                "momentum": "rsi_55",
                "volatility": "atr_stop_ready",
                "volume": "relative_volume",
                "entry": "pullback",
                "exit": "fixed_rr_15",
            },
            "parameters": {
                "trend_fast": 20,
                "trend_slow": 50,
                "momentum": "rsi",
                "rsi_min": 55,
                "entry": "pullback",
                "entry_distance_to_ema20_max": 0.035,
                "risk_reward": 1.5,
                "atr_multiplier": 1.5,
                "volume_change_min": 0.1,
                "max_holding_bars": 18,
            },
            "complexity": 6,
            "canonical_key": f"parent-{candidate_id}",
        },
        "result": {
            "metrics": {
                "profit_factor": 1.45,
                "expectancy_per_trade": 12.0,
                "max_drawdown": 0.05,
                "number_of_trades": 40,
                "walk_forward": {"enabled": True},
            },
            "paper_readiness": {"paper_ready": True},
        },
    }


def test_parent_eligibility_requires_promoted_complete_gate_passing_specialist() -> None:
    parent = promoted_parent("sd_parent1")
    eligible = parent_eligibility(parent)
    rejected = parent_eligibility({**parent, "hypothesis_version_id": None})

    assert eligible["eligible"] is True
    assert all(eligible["checks"].values())
    assert rejected["eligible"] is False
    assert rejected["checks"]["complete_hypothesis_lineage"] is False


def test_phase4_blueprint_creates_lineaged_executable_children_without_confirmation_claims() -> None:
    parents = [
        promoted_parent("sd_parent1", symbol="GOOGL", family="Trend Following", score=10),
        promoted_parent("sd_parent2", symbol="AMD", family="Trend Following", score=9),
        promoted_parent("sd_parent3", symbol="TSLA", family="Momentum", score=8),
    ]
    blueprint = build_evolution_blueprint(
        parents,
        dataset_id=1,
        validation_dataset_id=None,
        independent_validation_available=False,
        children_per_parent=4,
    )

    assert blueprint["controls"]["phase4_version"] == PHASE_4_VERSION
    assert blueprint["controls"]["validation_thresholds_changed"] is False
    assert blueprint["controls"]["independent_validation_required_for_improvement"] is True
    assert blueprint["children"]
    assert blueprint["diversity"]["duplicate_execution_keys"] == 0
    assert blueprint["diversity"]["diversity_collapsed"] is False
    assert len({candidate_execution_key(child) for child in blueprint["children"]}) == len(blueprint["children"])
    for row in blueprint["lineage"]:
        assert row["parent_candidate_id"]
        assert row["root_ancestor_id"]
        assert row["generation"] == 2
        assert row["hypothesis_id"] == 28
        assert row["dataset_id"] == 1
        assert row["classification"] == "Promising descendant - unconfirmed"
        assert row["mutated_parameter"] not in {"phase4_version", "expected_behavior", "hypothesis_key"}


def test_parent_selection_preserves_family_and_asset_diversity() -> None:
    parents = [
        promoted_parent("sd_parent1", symbol="GOOGL", family="Trend Following", score=10),
        promoted_parent("sd_parent2", symbol="GOOGL", family="Trend Following", score=9),
        promoted_parent("sd_parent3", symbol="AMD", family="Trend Following", score=8),
        promoted_parent("sd_parent4", symbol="TSLA", family="Momentum", score=7),
    ]

    selected = select_diverse_parents(parents, max_parents=3)

    assert len(selected) == 3
    assert len({row["symbol"] for row in selected}) >= 2
    assert len({row["strategy_family"] for row in selected}) >= 2


def test_parent_selection_fills_budget_when_available_parents_share_family() -> None:
    parents = [
        promoted_parent("sd_parent1", symbol="GOOGL", family="Trend Following", score=10),
        promoted_parent("sd_parent2", symbol="AMD", family="Trend Following", score=9),
        promoted_parent("sd_parent3", symbol="TSLA", family="Trend Following", score=8),
    ]

    selected = select_diverse_parents(parents, max_parents=3)

    assert len(selected) == 3
