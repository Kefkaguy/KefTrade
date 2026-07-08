from datetime import UTC, datetime

from app.services import candidate_lifecycle
from app.services.candidate_lifecycle import build_research_notebook, detect_evidence_drift, infer_lifecycle_status


def make_candidate(**overrides):
    row = {
        "rank": 1,
        "candidate_id": "trend_pullback_001",
        "experiment_id": "trend_pullback_rsi_ema_exit_sweep",
        "strategy_name": "trend_pullback_v1",
        "title": "Trend pullback RSI/EMA exit sweep",
        "parameters": {"rsi_min": 35, "take_profit_pct": 0.05},
        "aggregate_metrics": {
            "profit_factor": 1.2,
            "expectancy_per_trade": 15,
            "number_of_trades": 42,
            "max_drawdown": 0.08,
        },
        "research_score": 58.5,
        "stability_score": 0.55,
        "cross_asset_consistency": 0.5,
        "timeframe_consistency": 0.5,
        "out_of_sample_score": 0.4,
        "dataset_results": [],
        "train_test_results": [],
        "walk_forward": {"fold_count": 2},
        "assets_worked": ["BTCUSDT 4h"],
        "assets_failed": ["ETHUSDT 4h"],
        "validation_status": "Needs more evidence",
        "evidence_summary": "Worked on BTCUSDT 4h and failed on ETHUSDT 4h.",
        "recommended_next_experiment": "Increase cross-asset sample before validation.",
        "research_report": "Cross-asset sweep found one profitable pocket.",
    }
    row.update(overrides)
    return row


def test_lifecycle_status_maps_research_evidence_without_changing_thresholds() -> None:
    assert infer_lifecycle_status(make_candidate(validation_status="Research candidate for alpha validation")) == "Alpha Validation"
    assert infer_lifecycle_status(make_candidate(validation_status="Needs more evidence")) == "Needs More Evidence"
    assert infer_lifecycle_status(make_candidate(validation_status="Reject for now", aggregate_metrics={"profit_factor": 1.1, "number_of_trades": 5})) == "Promising"
    assert infer_lifecycle_status(make_candidate(validation_status="Reject for now", aggregate_metrics={"profit_factor": 0.7, "number_of_trades": 5}, research_score=9)) == "Experimenting"


def test_evidence_drift_flags_weaker_current_snapshot() -> None:
    events = [
        {
            "metrics": {
                "research_score": 72,
                "out_of_sample_score": 0.75,
            }
        }
    ]
    current = make_candidate(research_score=48, out_of_sample_score=0.3)

    drift = detect_evidence_drift(events, current)

    assert drift["status"] == "Drifting"
    assert drift["score_delta"] < 0
    assert "weakened" in drift["message"]


def test_research_notebook_contains_required_sections() -> None:
    notebook = build_research_notebook(make_candidate(), "Needs More Evidence", "More sample required.")

    assert "## What Was Tested" in notebook
    assert "## What Changed" in notebook
    assert "## What Improved" in notebook
    assert "## What Failed" in notebook
    assert "## Next Research Recommendation" in notebook


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self):
        self.events = []
        self.committed = False

    def execute(self, query, params=None):
        if "SELECT to_state" in query:
            rows = sorted(self.events, key=lambda row: row["id"], reverse=True)
            return FakeResult(rows[:1])
        if "INSERT INTO candidate_lifecycle_events" in query:
            candidate_id, from_state, to_state, reason, metrics = params
            self.events.append(
                {
                    "id": len(self.events) + 1,
                    "candidate_id": candidate_id,
                    "from_state": from_state,
                    "to_state": to_state,
                    "reason": reason,
                    "metrics": getattr(metrics, "obj", metrics),
                    "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                }
            )
            return FakeResult([])
        if "SELECT id, candidate_id, from_state" in query:
            candidate_id = params[0]
            return FakeResult([row for row in self.events if row["candidate_id"] == candidate_id])
        return FakeResult([])

    def commit(self):
        self.committed = True


def test_build_research_portfolio_records_lifecycle_and_timeline(monkeypatch) -> None:
    candidate = make_candidate()

    def fake_build_promising_research_candidates(conn, max_candidates, max_runs_per_experiment, fold_count):
        return {
            "summary": {"candidate_count": 1},
            "datasets": [],
            "thresholds": {},
            "rank_metrics": [],
            "markdown_report": "report",
            "candidates": [candidate],
        }

    monkeypatch.setattr(candidate_lifecycle, "build_promising_research_candidates", fake_build_promising_research_candidates)
    monkeypatch.setattr(candidate_lifecycle, "ensure_lifecycle_tables", lambda conn: None)
    conn = FakeConnection()

    portfolio = candidate_lifecycle.build_research_portfolio(conn, max_candidates=1)

    assert portfolio["summary"]["total_candidates"] == 1
    assert portfolio["summary"]["active_candidates"] == 1
    assert conn.committed is True
    assert portfolio["candidates"][0]["lifecycle_status"] == "Needs More Evidence"
    assert portfolio["candidates"][0]["lifecycle_events"]
    assert portfolio["timeline"]
    assert {"experiment_created", "parameter_changes", "validation_run", "cross_asset_results", "research_notes", "promotion_rejection_decision"}.issubset(
        {event["event_type"] for event in portfolio["timeline"]}
    )
    assert portfolio["comparison"][0]["profit_factor"] == 1.2
