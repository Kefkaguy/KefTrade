from __future__ import annotations

from app.services.edge_discovery import (
    EDGE_DISCOVERY_VERSION,
    build_edge_discovery_hypotheses,
    derive_lifecycle_interpretation,
)
from app.services.research_architecture import generate_targeted_candidates


def job(
    job_id: int,
    *,
    campaign_id: int = 64,
    family: str = "Momentum",
    candidate_id: str = "sd_test",
    symbol: str = "QQQ",
    status: str = "rejected",
    trades: int = 32,
    pf: float = 1.05,
    expectancy: float = 2.0,
    paper_ready: bool = False,
    generation_channel: str = "exploitation",
) -> dict:
    return {
        "id": job_id,
        "campaign_id": campaign_id,
        "dataset_id": 1,
        "hypothesis_version_id": 76,
        "candidate_id": candidate_id,
        "symbol": symbol,
        "timeframe": "1h",
        "strategy_family": family,
        "status": status,
        "candidate": {
            "candidate_id": candidate_id,
            "family_id": "phase2_family_momentum",
            "generation": 1,
            "blocks": {
                "trend": "momentum_trend_context",
                "momentum": "momentum_confirmation",
                "volatility": "momentum_volatility_context",
                "volume": "momentum_participation",
                "entry": "momentum",
                "exit": "momentum_atr_risk_reward",
            },
            "parameters": {
                "strategy_architecture": "research_strategy_families_v1",
                "phase2_strategy_family": family,
                "momentum_short_bars": 3 + job_id,
                "momentum_long_bars": 12 + job_id,
                "momentum_short_min": 0.003,
                "momentum_long_min": 0.01,
                "momentum_acceleration_min": 0.0,
                "risk_reward": 2.0,
                "atr_multiplier": 2.0,
                "max_holding_bars": 12,
                "generation_channel": generation_channel,
            },
            "complexity": 6,
            "canonical_key": f"candidate-{job_id}",
        },
        "result": {
            "metrics": {
                "number_of_trades": trades,
                "profit_factor": pf,
                "expectancy_per_trade": expectancy,
                "max_drawdown": 0.06,
                "walk_forward": {"enabled": True},
            },
            "paper_readiness": {"paper_ready": paper_ready},
        },
        "rejection_diagnostics": [
            {"name": "trade_count", "passed": trades >= 30},
            {"name": "profit_factor", "passed": pf >= 1.2},
            {"name": "positive_expectancy", "passed": expectancy > 0},
            {"name": "paper_readiness", "passed": paper_ready},
        ],
    }


def test_edge_discovery_creates_post_hoc_standard_hypotheses_from_winners_and_losers() -> None:
    jobs = [
        job(1, candidate_id="sd_a", symbol="QQQ", pf=1.07, expectancy=2.6, trades=32),
        job(2, candidate_id="sd_a", symbol="SPY", pf=0.31, expectancy=-30.8, trades=16),
        job(3, candidate_id="sd_b", symbol="QQQ", pf=0.7, expectancy=-10, trades=45),
        job(4, candidate_id="sd_b", symbol="SPY", pf=0.8, expectancy=-8, trades=38),
        job(5, candidate_id="sd_c", symbol="QQQ", pf=0.6, expectancy=-5, trades=42),
        job(6, candidate_id="sd_c", symbol="SPY", pf=0.5, expectancy=-11, trades=40),
        job(7, candidate_id="sd_d", symbol="QQQ", pf=0.9, expectancy=-4, trades=35),
        job(8, candidate_id="sd_d", symbol="SPY", pf=0.75, expectancy=-9, trades=37),
        job(9, candidate_id="sd_e", symbol="QQQ", pf=1.3, expectancy=4, status="promoted", paper_ready=True),
        job(10, candidate_id="sd_e", symbol="SPY", pf=0.4, expectancy=-12),
    ]
    history = {
        "dataset_id": 1,
        "campaigns": [
            {
                "id": 64,
                "dataset_id": 1,
                "immutable_config": {"scope": {"type": "cluster", "ref": "cluster_qqq_spy", "assets": ["QQQ", "SPY"]}},
            }
        ],
        "jobs": jobs,
        "hypotheses": [],
    }

    discovery = build_edge_discovery_hypotheses(history)

    assert discovery["edge_discovery_version"] == EDGE_DISCOVERY_VERSION
    assert discovery["controls"]["validation_thresholds_changed"] is False
    assert discovery["controls"]["candidate_volume_increased"] is False
    assert discovery["unique_execution_keys"] >= 5
    assert discovery["hypotheses"]
    assert any(row["test_summary"]["discovery_type"] == "near_pass_subcondition" for row in discovery["hypotheses"])
    assert any(row["test_summary"]["discovery_type"] == "winner_loser_transfer_condition" for row in discovery["hypotheses"])
    for hypothesis in discovery["hypotheses"]:
        assert hypothesis["status"] == "proposed"
        assert hypothesis["test_summary"]["post_hoc"] is True
        assert hypothesis["test_summary"]["confirmation_status"] == "unconfirmed"
        assert hypothesis["test_summary"]["candidate_generation_contract"].startswith("standard generate_targeted_candidates")
        assert hypothesis["supporting_evidence"]
        assert "research_campaign_job:" in hypothesis["supporting_evidence"][0]


def test_edge_hypotheses_are_directly_consumable_by_targeted_generator() -> None:
    history = {
        "dataset_id": 1,
        "campaigns": [{"id": 65, "immutable_config": {"scope": {"type": "cluster", "ref": "cluster_qqq_spy"}}}],
        "jobs": [job(index, candidate_id=f"sd_{index}", pf=0.8, expectancy=-5, trades=35) for index in range(1, 11)],
        "hypotheses": [],
    }
    hypothesis = build_edge_discovery_hypotheses(history)["hypotheses"][0]
    hypothesis["id"] = 101

    generated = generate_targeted_candidates(hypothesis, max_candidates=10)

    assert len(generated["candidates"]) == 10
    assert generated["hypothesis_id"] == 101
    assert all(candidate.parameters["hypothesis_version_id"] == 101 for candidate in generated["candidates"])
    assert all(candidate.parameters["hypothesis_strategy_family"] == hypothesis["strategy_family"] for candidate in generated["candidates"])


def test_lifecycle_interpretation_preserves_history_but_marks_confirmed_wording_unconfirmed() -> None:
    interpretation = derive_lifecycle_interpretation(
        {
            "id": 28,
            "hypothesis_key": "hyp_old",
            "status": "testing",
            "title": "Confirmed directional persistence",
            "observation": "Confirmed profile aggregate",
            "hypothesis": "Confirmed behavior continues.",
            "test_summary": {"source_dataset_id": 1},
            "evidence_window": {"dataset_id": 1},
        }
    )

    assert interpretation["stored_status"] == "testing"
    assert interpretation["authoritative_confirmation_status"] == "unconfirmed"
    assert interpretation["wording_status_inconsistent"] is True
    assert interpretation["immutable_history_rewritten"] is False

    unconfirmed = derive_lifecycle_interpretation(
        {
            "id": 96,
            "hypothesis_key": "edge_hyp",
            "status": "proposed",
            "title": "Post-hoc unconfirmed edge hypothesis",
            "observation": "Post-hoc and unconfirmed.",
            "hypothesis": "Future test is required.",
            "test_summary": {"confirmation_status": "unconfirmed"},
            "evidence_window": {"dataset_id": 1},
        }
    )

    assert unconfirmed["claims_confirmed_in_text"] is False
    assert unconfirmed["wording_status_inconsistent"] is False


def test_lifecycle_hypothesis_is_not_dropped_by_market_edge_limit() -> None:
    history = {
        "dataset_id": 1,
        "campaigns": [{"id": 65, "immutable_config": {"scope": {"type": "cluster", "ref": "cluster_qqq_spy"}}}],
        "jobs": [job(index, candidate_id=f"sd_{index}", pf=0.8, expectancy=-5, trades=35) for index in range(1, 11)],
        "hypotheses": [
            {
                "id": 28,
                "hypothesis_key": "hyp_old",
                "status": "testing",
                "title": "Confirmed directional persistence",
                "observation": "Confirmed profile aggregate",
                "hypothesis": "Confirmed behavior continues.",
                "test_summary": {"source_dataset_id": 1},
                "evidence_window": {"dataset_id": 1},
            }
        ],
    }

    discovery = build_edge_discovery_hypotheses(history, max_hypotheses=1)

    assert len(discovery["hypotheses"]) == 2
    assert any((row["test_summary"] or {}).get("discovery_type") == "hypothesis_lifecycle_interpretation" for row in discovery["hypotheses"])
