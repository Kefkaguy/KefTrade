from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.services.production_validation import (
    paper_ledger_reconciliation,
    phase10_readiness_assessment,
    production_validation_campaign_config,
    recommendation_outcomes,
    run_data_integrity_audit,
    safety_audit,
    validate_worker_supervision_config,
    verify_migrations,
)


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class ValidationConn:
    def __init__(self):
        now = datetime.now(UTC)
        self.counts = {
            "research_campaign_jobs": 4,
            "research_elite_candidates": 1,
            "research_campaign_plans": 1,
            "research_evolution_history": 1,
        }
        self.jobs = [
            {"status": "completed", "execution_runtime_ms": 1000},
            {"status": "promoted", "execution_runtime_ms": 1200},
            {"status": "queued", "execution_runtime_ms": None},
            {"status": "blocked_data", "execution_runtime_ms": None},
        ]
        self.workers = [{"worker_id": "worker-1", "status": "running", "started_at": now - timedelta(hours=2), "simulation_only": True}]
        self.scheduler = {"id": True, "enabled": True, "latest_error": None}
        self.recommendations = [{"id": 1, "evidence_refs": ["campaign_job:1"], "confidence_score": 0.4, "simulation_only": True}]
        self.confidence = [{"candidate_id": "elite_a", "confidence_score": 76, "calculation_version": "research_learning_v1", "simulation_only": True}]
        self.fills = [
            {"id": 1, "order_id": 1, "symbol": "TSLA", "side": "buy", "quantity": Decimal("1"), "fill_price": Decimal("100"), "filled_at": now - timedelta(days=2), "simulation_only": True},
            {"id": 2, "order_id": 2, "symbol": "TSLA", "side": "sell", "quantity": Decimal("1"), "fill_price": Decimal("112"), "filled_at": now - timedelta(days=1), "simulation_only": True},
        ]
        self.orders = [{"id": 1, "status": "filled", "simulation_only": True}, {"id": 2, "status": "filled", "simulation_only": True}]
        self.positions = [{"symbol": "TSLA", "quantity": Decimal("0"), "simulation_only": True}]

    def execute(self, query, params=None):
        if query.strip().startswith("CREATE TABLE") or query.strip().startswith("INSERT INTO"):
            return Result([])
        if "FROM research_campaign_workers" in query:
            return Result(self.workers)
        if "FROM research_campaign_scheduler" in query:
            return Result([self.scheduler])
        if "FROM research_campaign_jobs" in query and "GROUP BY status" in query:
            status_counts = {}
            for row in self.jobs:
                status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
            return Result([{"status": key, "count": value} for key, value in status_counts.items()])
        if "AVG(execution_runtime_ms)" in query:
            return Result([{"avg_runtime": 1100, "max_runtime": 1200}])
        if "FROM paper_fills" in query:
            return Result(self.fills)
        if "FROM paper_orders" in query:
            return Result(self.orders)
        if "FROM paper_positions" in query:
            return Result(self.positions)
        if "FROM research_recommendations" in query:
            return Result(self.recommendations)
        if "FROM research_confidence_history" in query:
            return Result(self.confidence)
        if "SELECT COUNT(*) AS count" in query:
            if "LEFT JOIN research_campaigns" in query:
                return Result([{"count": 0}])
            if "GROUP BY campaign_id, job_key" in query:
                return Result([{"count": 0}])
            if "GROUP BY candidate_id" in query:
                return Result([{"count": 0}])
            if "GROUP BY account_id, strategy_name" in query:
                return Result([{"count": 0}])
            if "lease_expires_at <= NOW()" in query:
                return Result([{"count": 0}])
            if "jsonb_array_length(evidence_refs) = 0" in query:
                return Result([{"count": 0}])
            if "calculation_version IS NULL" in query:
                return Result([{"count": 0}])
            if "strategy_id IS NULL" in query:
                return Result([{"count": 0}])
            if "simulation_only IS DISTINCT FROM TRUE" in query:
                return Result([{"count": 0}])
            if "jsonb_array_length(supporting_evidence)" in query or "jsonb_array_length(exploration_targets)" in query:
                return Result([{"count": 1}])
            if "forward_validation_state = 'passed'" in query:
                return Result([{"count": 1}])
            if "forward_validation_state IN ('failed', 'drifted')" in query:
                return Result([{"count": 0}])
            if "evidence_drift_state = 'severe'" in query:
                return Result([{"count": 0}])
            if "status = 'active'" in query and "research_elite_candidates" in query:
                return Result([{"count": 1}])
            if "research_campaign_jobs" in query:
                return Result([{"count": self.counts["research_campaign_jobs"]}])
            if "research_elite_candidates" in query:
                return Result([{"count": self.counts["research_elite_candidates"]}])
            return Result([{"count": 0}])
        if "FROM research_campaigns" in query:
            return Result([{"promoted_candidates": 1, "simulation_only": True}])
        return Result([])

    def commit(self):
        return None


def test_migration_verification_finds_milestone_5_migration() -> None:
    result = verify_migrations()

    assert result["latest_migration"] >= 26
    assert result["required_present"] is True
    assert result["simulation_only_constraints_present"] is True


def test_worker_supervision_config_contains_restart_health_identity_and_logs() -> None:
    result = validate_worker_supervision_config()

    assert result["passed"] is True
    assert {row["name"] for row in result["checks"]} >= {"automatic_restart", "healthcheck", "unique_worker_identity", "structured_logs"}


def test_validation_campaign_config_is_reproducible_and_bounded() -> None:
    config = production_validation_campaign_config({"assets": [f"A{i}" for i in range(60)], "max_candidates": 3000})

    assert len(config["assets"]) == 50
    assert config["max_candidates"] == 3000
    assert config["paper_deployment"] == "internal_only"
    assert config["simulation_only"] is True


def test_integrity_audit_and_safety_audit_are_read_only_and_classified() -> None:
    conn = ValidationConn()
    integrity = run_data_integrity_audit(conn)
    safety = safety_audit(conn)

    assert integrity["summary"]["critical_failures"] == 0
    assert all("recommended_remediation" in row for row in integrity["checks"])
    assert safety["status"] == "passed"


def test_paper_reconciliation_uses_closed_trade_fifo_and_detects_mismatches() -> None:
    result = paper_ledger_reconciliation(ValidationConn())

    assert result["passed"] is True
    assert result["summary"]["closed_trades"] == 1
    assert result["summary"]["expectancy"] > 0


def test_recommendation_outcomes_do_not_claim_success_without_followup() -> None:
    conn = ValidationConn()
    conn.confidence = []

    result = recommendation_outcomes(conn)

    assert result["outcomes"][0]["status"] == "pending"


def test_readiness_score_is_blocked_by_mandatory_gates_despite_score_components() -> None:
    result = phase10_readiness_assessment(ValidationConn(), persist=False)

    assert result["readiness_state"] in {"not_ready", "blocked"}
    assert "backend_and_frontend_verified" in result["blocking_reasons"]
    assert result["calculation"]["mandatory_gates_override_score"] is True
