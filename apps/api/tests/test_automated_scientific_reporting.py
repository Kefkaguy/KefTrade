from app.services.automated_scientific_reporting import (
    INSUFFICIENT,
    PHASE_6_REPORT_VERSION,
    build_scientific_report,
    scientific_report_markdown,
)


def bundle() -> dict:
    campaign = {
        "id": 73,
        "name": "Phase 4 corrected evolution",
        "status": "completed",
        "dataset_id": 1,
        "hypothesis_version_id": 34,
        "analytics": {
            "research_architecture": {
                "scope": {"type": "cluster", "ref": "cluster_trend"},
                "candidate_levels": {"asset_specialist": 2},
            }
        },
    }
    jobs = [
        {
            "id": 1,
            "campaign_id": 73,
            "candidate_id": "child_a",
            "symbol": "AMD",
            "timeframe": "1h",
            "status": "promoted",
            "parent_candidate_id": "parent_a",
            "strategy_family": "Pullback",
            "execution_runtime_ms": 1000,
            "result": {"metrics": {"profit_factor": 1.7, "expectancy_per_trade": 5, "number_of_trades": 40}},
            "candidate": {"parameters": {"trend_maturity_score": 0.8, "momentum_persistence_score": 0.7}},
        },
        {
            "id": 2,
            "campaign_id": 73,
            "candidate_id": "child_a",
            "symbol": "GOOGL",
            "timeframe": "1h",
            "status": "rejected",
            "parent_candidate_id": "parent_a",
            "strategy_family": "Pullback",
            "execution_runtime_ms": 1200,
            "failure_reasons": ["Profit factor 0.8 must be >= 1.2."],
            "rejection_diagnostics": [{"name": "profit_factor", "passed": False, "actual": 0.8, "threshold": 1.2}],
            "result": {"metrics": {"profit_factor": 0.8, "expectancy_per_trade": -2, "number_of_trades": 35}},
            "candidate": {"parameters": {"trend_maturity_score": 0.3, "momentum_persistence_score": 0.2}},
        },
    ]
    return {
        "campaign": campaign,
        "jobs": jobs,
        "stage_rows": [
            {
                "id": 9,
                "evidence_key": "stage_child_a",
                "candidate_id": "child_a",
                "candidate_level": "asset_specialist",
                "promoted": True,
            }
        ],
        "hypothesis": {
            "id": 34,
            "hypothesis_key": "hyp_post_hoc",
            "status": "proposed",
            "test_summary": {"post_hoc": True, "confirmation_status": "unconfirmed"},
            "supporting_evidence": ["asset_profile:1"],
            "contradictory_evidence": [],
        },
        "hypothesis_versions": [
            {
                "id": 34,
                "hypothesis_key": "hyp_post_hoc",
                "status": "testing",
                "test_summary": {"post_hoc": True, "confirmation_status": "unconfirmed", "campaign_id": 73},
                "supporting_evidence": ["asset_profile:1"],
                "contradictory_evidence": ["research_campaign_job:2"],
            }
        ],
        "dataset": {
            "id": 1,
            "dataset_key": "dataset_abc",
            "content_hash": "hash_abc",
            "candle_counts": {"AMD|1h": 1000, "GOOGL|1h": 1000},
        },
        "archives": [{"archive_key": "campaign_archive_73", "content_hash": "archive_hash"}],
        "evolution": [{"id": 5, "parent_candidate_id": "parent_a", "candidate_id": "child_a"}],
        "learning": {
            "research_failure_patterns": [],
            "research_success_patterns": [],
            "research_candidate_confidence": [],
            "research_evolution_history": [],
            "research_learning_recommendations": [],
        },
        "previous_campaigns": [
            {"id": 72, "promoted_candidates": 1, "analytics": {"strategies_tested": 10, "promoted": 1}}
        ],
        "analytics": campaign["analytics"],
    }


def test_scientific_report_preserves_post_hoc_unconfirmed_and_evidence_refs() -> None:
    report = build_scientific_report(bundle())

    assert report["report_version"] == PHASE_6_REPORT_VERSION
    assert report["dataset"]["dataset_id"] == 1
    assert report["hypothesis_lifecycle"]["confirmed"] == []
    assert report["hypothesis_lifecycle"]["inconclusive"][0]["confirmation_status"] == "unconfirmed"
    assert report["evolution_outcomes"]["classification"] == "Promising descendant - unconfirmed"
    assert report["next_campaign_recommendations"]
    assert all(row["supporting_evidence"] for row in report["next_campaign_recommendations"])
    assert "research_campaign:73" in report["executive_summary"]["evidence_refs"]


def test_scientific_report_marks_missing_observation_evidence_inconclusive() -> None:
    data = bundle()
    for job in data["jobs"]:
        job["candidate"] = {"parameters": {}}
    report = build_scientific_report(data)

    assert report["observation_contributions"][0]["statement"] == INSUFFICIENT
    assert "Inconclusive" in scientific_report_markdown(report)


def test_scientific_report_is_deterministic_for_same_inputs() -> None:
    first = build_scientific_report(bundle())
    second = build_scientific_report(bundle())

    assert first["reproducibility"]["report_input_hash"] == second["reproducibility"]["report_input_hash"]
    assert scientific_report_markdown(first) == scientific_report_markdown(second)
