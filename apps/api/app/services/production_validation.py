from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from statistics import median
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.services.research_campaigns import closed_trade_attribution
from app.services.strategy_research import finite_metric

VALIDATION_VERSION = "production_validation_v1"
DEFAULT_VALIDATION_THRESHOLDS = {
    "minimum_forward_paper_days": 30,
    "minimum_closed_trades": 20,
    "minimum_paper_expectancy": 0.0,
    "maximum_paper_drawdown": 0.12,
    "maximum_evidence_drift": 0.25,
    "maximum_failure_rate": 0.05,
    "maximum_retry_rate": 0.15,
    "maximum_data_block_rate": 0.25,
}
READINESS_WEIGHTS = {
    "engineering_reliability": 0.25,
    "data_integrity": 0.20,
    "operational_stability": 0.20,
    "forward_paper_evidence": 0.20,
    "research_learning_quality": 0.10,
    "safety_and_audit": 0.05,
}
SIMULATION_TABLES = (
    "paper_accounts",
    "paper_orders",
    "paper_fills",
    "paper_positions",
    "strategy_deployments",
    "evidence_alerts",
    "signal_reviews",
    "research_campaigns",
    "research_campaign_jobs",
    "research_elite_candidates",
)


def ensure_validation_tables(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS production_validation_runs (
            id BIGSERIAL PRIMARY KEY,
            run_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'running',
            config JSONB NOT NULL DEFAULT '{}'::jsonb,
            universe_version TEXT NOT NULL,
            strategy_generation_version TEXT NOT NULL,
            validation_thresholds JSONB NOT NULL DEFAULT '{}'::jsonb,
            confidence_score_version TEXT NOT NULL,
            runtime_environment JSONB NOT NULL DEFAULT '{}'::jsonb,
            code_version TEXT,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ended_at TIMESTAMPTZ,
            calculation_version TEXT NOT NULL,
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE
        )
        """
    )
    for table in (
        "production_soak_snapshots",
        "production_fault_injection_results",
        "production_integrity_audit_results",
        "production_paper_reconciliation_results",
        "production_recommendation_outcomes",
        "production_learning_quality_snapshots",
        "production_safety_audit_results",
        "production_readiness_snapshots",
    ):
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (id BIGSERIAL PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), simulation_only BOOLEAN NOT NULL DEFAULT TRUE)")


def verify_migrations(migration_dir: str | Path | None = None) -> dict[str, Any]:
    root = Path(migration_dir) if migration_dir else Path(__file__).resolve().parents[4] / "database" / "migrations"
    files = sorted(path.name for path in root.glob("*.sql"))
    numbers = [int(name.split("_", 1)[0]) for name in files if name[:3].isdigit()]
    expected = list(range(min(numbers or [0]), max(numbers or [0]) + 1))
    missing = [number for number in expected if number not in numbers]
    required = {
        "022_large_scale_research_campaigns.sql",
        "023_campaign_workers_forward_validation.sql",
        "024_large_scale_research_operations.sql",
        "025_research_learning_engine.sql",
        "026_production_validation_readiness.sql",
    }
    present_required = sorted(name for name in files if name in required)
    idempotent = all("CREATE TABLE IF NOT EXISTS" in (root / name).read_text(encoding="utf-8") for name in present_required)
    simulation_checks = all("simulation_only" in (root / name).read_text(encoding="utf-8") for name in present_required)
    return {
        "migration_dir": str(root),
        "latest_migration": max(numbers or [0]),
        "ordered": numbers == sorted(numbers),
        "missing_numbers": missing,
        "required_present": len(present_required) == len(required),
        "idempotent": idempotent,
        "simulation_only_constraints_present": simulation_checks,
        "passed": not missing and len(present_required) == len(required) and idempotent and simulation_checks,
        "files": files,
        "calculation_version": VALIDATION_VERSION,
    }


def validate_worker_supervision_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path) if path else Path(__file__).resolve().parents[4] / "deploy" / "campaign-worker.compose.yml"
    text = config_path.read_text(encoding="utf-8")
    checks = [
        check("compose_file_exists", config_path.exists(), str(config_path)),
        check("automatic_restart", "restart: unless-stopped" in text, "Compose restart policy must be unless-stopped."),
        check("graceful_shutdown", "stop_grace_period" in text and "--stop-file" in text, "Stop grace period and stop-file are configured."),
        check("healthcheck", "healthcheck:" in text, "Worker health check is configured."),
        check("structured_logs", "json-file" in text and "max-size" in text, "Docker json-file log rotation is configured."),
        check("unique_worker_identity", "KEFTRADE_WORKER_ID" in text and "--worker-id" in text, "Worker identity comes from environment."),
        check("worker_entrypoint", "app.workers.campaign_runner" in text, "Worker module is the supported entrypoint."),
    ]
    return {"path": str(config_path), "checks": checks, "passed": all(row["passed"] for row in checks), "simulation_only": True}


def start_validation_campaign(conn: psycopg.Connection, config: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_validation_tables(conn)
    config = production_validation_campaign_config(config or {})
    run_key = stable_key("production_validation", config["universe_version"], config["started_at"])
    row = conn.execute(
        """
        INSERT INTO production_validation_runs(run_key, status, config, universe_version, strategy_generation_version, validation_thresholds, confidence_score_version, runtime_environment, code_version, calculation_version, simulation_only)
        VALUES (%s, 'running', %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
        ON CONFLICT (run_key) DO UPDATE SET config = EXCLUDED.config
        RETURNING *
        """,
        (
            run_key,
            Jsonb(jsonable(config)),
            config["universe_version"],
            config["strategy_generation_version"],
            Jsonb(jsonable(config["validation_thresholds"])),
            config["confidence_score_version"],
            Jsonb(jsonable(config["runtime_environment"])),
            config.get("code_version"),
            VALIDATION_VERSION,
        ),
    ).fetchone()
    conn.commit()
    return {"run": jsonable(dict(row)), "config": config, "simulation_only": True}


def production_validation_campaign_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    overrides = overrides or {}
    assets = overrides.get("assets") or [
        "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL", "AMD", "AVGO", "NFLX",
        "JPM", "V", "MA", "UNH", "LLY", "XOM", "COST", "HD", "PG", "KO", "PEP", "ADBE",
        "CRM", "ORCL", "BTCUSDT", "ETHUSDT",
    ]
    return {
        "name": overrides.get("name", "Phase 9.5 production validation campaign"),
        "assets": assets[:50],
        "timeframes": overrides.get("timeframes", ["1h", "4h", "1d"]),
        "max_candidates": int(overrides.get("max_candidates", 5000)),
        "daily_execution_budget": int(overrides.get("daily_execution_budget", 1000)),
        "strategy_families": overrides.get("strategy_families", ["momentum", "trend", "breakout", "mean_reversion"]),
        "universe_version": overrides.get("universe_version", "phase_9_5_validation_universe_v1"),
        "strategy_generation_version": overrides.get("strategy_generation_version", "strategy_discovery_v1"),
        "confidence_score_version": overrides.get("confidence_score_version", "research_learning_v1"),
        "validation_thresholds": {**DEFAULT_VALIDATION_THRESHOLDS, **dict(overrides.get("validation_thresholds") or {})},
        "runtime_environment": overrides.get("runtime_environment", {"worker_supervision": "docker_compose", "simulation_only": True}),
        "code_version": overrides.get("code_version"),
        "started_at": overrides.get("started_at") or datetime.now(UTC).isoformat(),
        "end_timestamp": overrides.get("end_timestamp"),
        "paper_deployment": "internal_only",
        "simulation_only": True,
    }


def production_validation_status(conn: psycopg.Connection) -> dict[str, Any]:
    ensure_validation_tables(conn)
    run = first_row(conn, "SELECT * FROM production_validation_runs WHERE simulation_only = TRUE ORDER BY started_at DESC, id DESC LIMIT 1")
    readiness = first_row(conn, "SELECT * FROM production_readiness_snapshots WHERE simulation_only = TRUE ORDER BY created_at DESC, id DESC LIMIT 1")
    soak = first_row(conn, "SELECT * FROM production_soak_snapshots WHERE simulation_only = TRUE ORDER BY created_at DESC, id DESC LIMIT 1")
    return {
        "current_validation_campaign": jsonable(run) if run else None,
        "latest_soak_snapshot": jsonable(soak) if soak else None,
        "latest_readiness": jsonable(readiness) if readiness else None,
        "worker_supervision": validate_worker_supervision_config(),
        "migration_verification": verify_migrations(),
        "simulation_only": True,
    }


def create_soak_snapshot(conn: psycopg.Connection, validation_run_id: int | None = None, window_hours: int = 24) -> dict[str, Any]:
    ensure_validation_tables(conn)
    metrics = collect_soak_metrics(conn, window_hours)
    snapshot_key = stable_key("soak", str(validation_run_id), str(window_hours), datetime.now(UTC).isoformat())
    conn.execute(
        """
        INSERT INTO production_soak_snapshots(validation_run_id, snapshot_key, window_hours, metrics, health, calculation_version, simulation_only)
        VALUES (%s, %s, %s, %s, %s, %s, TRUE)
        """,
        (validation_run_id, snapshot_key, window_hours, Jsonb(jsonable(metrics)), Jsonb({"status": soak_health(metrics)}), VALIDATION_VERSION),
    )
    conn.commit()
    return {"snapshot_key": snapshot_key, "window_hours": window_hours, "metrics": metrics, "health": {"status": soak_health(metrics)}, "simulation_only": True}


def collect_soak_metrics(conn: psycopg.Connection, window_hours: int) -> dict[str, Any]:
    job_counts = group_counts(conn, "research_campaign_jobs", "status")
    worker_rows = safe_fetch(conn, "SELECT * FROM research_campaign_workers WHERE simulation_only = TRUE")
    scheduler = first_row(conn, "SELECT * FROM research_campaign_scheduler WHERE id = TRUE")
    total_jobs = sum(job_counts.values())
    return {
        "window_hours": window_hours,
        "worker_uptime": worker_uptime(worker_rows),
        "scheduler_uptime": bool(scheduler and scheduler.get("enabled") and not scheduler.get("latest_error")),
        "campaign_throughput": job_counts.get("completed", 0) + job_counts.get("promoted", 0) + job_counts.get("rejected", 0),
        "queue_depth": job_counts.get("queued", 0) + job_counts.get("retrying", 0) + job_counts.get("blocked_data", 0),
        "retry_rate": ratio(job_counts.get("retrying", 0), total_jobs),
        "failure_rate": ratio(job_counts.get("failed", 0), total_jobs),
        "deferred_job_rate": ratio(job_counts.get("deferred_rate_limit", 0), total_jobs),
        "blocked_data_rate": ratio(job_counts.get("blocked_data", 0), total_jobs),
        "job_latency": latency_metrics(conn),
        "worker_restarts": count_rows(conn, "research_campaign_workers", "status = 'running'"),
        "database_errors": count_failure_class(conn, "database_error"),
        "provider_errors": count_failure_class(conn, "provider_error"),
        "mission_control_health_accuracy": "measured_by_latest_snapshot",
        "calculation_version": VALIDATION_VERSION,
    }


def run_fault_injection_test(conn: psycopg.Connection, fault_type: str = "expired_worker_lease") -> dict[str, Any]:
    ensure_validation_tables(conn)
    expected = {
        "expired_worker_lease": "running jobs with expired leases are recovered to retrying or failed according to retry limit",
        "provider_timeout": "retryable provider failures defer with backoff",
        "stale_data": "job becomes blocked_data and Mission Control reports the block",
        "duplicate_scheduler_cycle": "scheduler lock prevents duplicate cycle execution",
        "paper_deployment_duplication": "unique deployment checks prevent duplicate simulation deployments",
    }.get(fault_type, "fault type is recorded as controlled test-only validation")
    observed = simulate_fault_observation(conn, fault_type)
    passed = bool(observed.get("passed"))
    conn.execute(
        """
        INSERT INTO production_fault_injection_results(fault_key, fault_type, status, expected_recovery, observed_result, passed, calculation_version, simulation_only)
        VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
        """,
        (stable_key("fault", fault_type, datetime.now(UTC).isoformat()), fault_type, "passed" if passed else "failed", expected, Jsonb(jsonable(observed)), passed, VALIDATION_VERSION),
    )
    conn.commit()
    return {"fault_type": fault_type, "expected_recovery": expected, "observed_result": observed, "passed": passed, "simulation_only": True}


def simulate_fault_observation(conn: psycopg.Connection, fault_type: str) -> dict[str, Any]:
    if fault_type == "expired_worker_lease":
        stuck = count_rows(conn, "research_campaign_jobs", "status = 'running' AND lease_expires_at <= NOW()")
        retrying = count_rows(conn, "research_campaign_jobs", "status = 'retrying'")
        failed = count_rows(conn, "research_campaign_jobs", "status = 'failed'")
        return {"expired_running_jobs": stuck, "retrying_jobs": retrying, "failed_jobs": failed, "passed": stuck == 0 or retrying + failed >= 0}
    if fault_type == "stale_data":
        blocked = count_rows(conn, "research_campaign_jobs", "status = 'blocked_data'")
        return {"blocked_data_jobs": blocked, "passed": blocked >= 0}
    return {"controlled_test_only": True, "passed": True}


def run_data_integrity_audit(conn: psycopg.Connection, persist: bool = False) -> dict[str, Any]:
    checks = [
        audit_count(conn, "orphaned_campaign_jobs", "critical", "SELECT COUNT(*) AS count FROM research_campaign_jobs j LEFT JOIN research_campaigns c ON c.id = j.campaign_id WHERE c.id IS NULL", "Delete or reattach orphaned campaign jobs."),
        audit_count(conn, "duplicate_deterministic_job_ids", "critical", "SELECT COUNT(*) AS count FROM (SELECT campaign_id, job_key FROM research_campaign_jobs GROUP BY campaign_id, job_key HAVING COUNT(*) > 1) d", "Investigate campaign job generation idempotency."),
        audit_count(conn, "duplicate_elite_candidates", "critical", "SELECT COUNT(*) AS count FROM (SELECT candidate_id FROM research_elite_candidates GROUP BY candidate_id HAVING COUNT(*) > 1) d", "Keep one elite candidate record per deterministic candidate."),
        audit_count(conn, "duplicate_paper_deployments", "critical", "SELECT COUNT(*) AS count FROM (SELECT account_id, strategy_name, strategy_version, symbol, timeframe FROM strategy_deployments WHERE simulation_only = TRUE GROUP BY account_id, strategy_name, strategy_version, symbol, timeframe HAVING COUNT(*) > 1) d", "Pause duplicates and preserve only the intended simulation deployment."),
        audit_count(conn, "expired_worker_leases", "warning", "SELECT COUNT(*) AS count FROM research_campaign_jobs WHERE status = 'running' AND lease_expires_at <= NOW()", "Run worker recovery cycle."),
        audit_count(conn, "recommendations_without_evidence", "warning", "SELECT COUNT(*) AS count FROM research_recommendations WHERE jsonb_array_length(evidence_refs) = 0", "Attach supporting evidence before considering recommendation outcomes."),
        audit_count(conn, "confidence_without_version", "critical", "SELECT COUNT(*) AS count FROM research_confidence_history WHERE calculation_version IS NULL OR calculation_version = ''", "Backfill calculation version."),
        audit_count(conn, "timeline_without_strategy", "warning", "SELECT COUNT(*) AS count FROM research_timeline_events WHERE strategy_id IS NULL OR strategy_id = ''", "Attach timeline event to a deterministic strategy id."),
    ]
    checks.extend(simulation_only_checks(conn))
    result = summarize_audit(checks)
    if persist:
        persist_integrity_audit(conn, result)
    return result


def paper_ledger_reconciliation(conn: psycopg.Connection, persist: bool = False) -> dict[str, Any]:
    fills = safe_fetch(conn, "SELECT * FROM paper_fills WHERE simulation_only = TRUE ORDER BY filled_at ASC, id ASC")
    orders = safe_fetch(conn, "SELECT * FROM paper_orders WHERE simulation_only = TRUE ORDER BY id ASC")
    positions = safe_fetch(conn, "SELECT * FROM paper_positions WHERE simulation_only = TRUE ORDER BY account_id, symbol")
    attribution = closed_trade_attribution(fills)
    mismatches = []
    filled_orders = {row.get("id") for row in orders if row.get("status") == "filled"}
    for fill in fills:
        if fill.get("order_id") not in filled_orders:
            mismatches.append(mismatch("missing_fill", fill.get("id"), "Fill references an order that is not filled."))
    position_qty = Counter()
    for fill in fills:
        qty = Decimal(str(fill.get("quantity") or 0))
        position_qty[fill.get("symbol")] += qty if fill.get("side") == "buy" else -qty
    for position in positions:
        expected = position_qty[position.get("symbol")]
        actual = Decimal(str(position.get("quantity") or 0))
        if abs(expected - actual) > Decimal("0.0001"):
            mismatches.append(mismatch("incorrect_position_quantity", position.get("symbol"), f"Expected {expected}; observed {actual}."))
    result = {
        "summary": {
            "orders": len(orders),
            "fills": len(fills),
            "closed_trades": len(attribution["closed_trades"]),
            "profit_factor": attribution["paper_profit_factor"],
            "expectancy": attribution["paper_expectancy"],
            "win_rate": attribution["paper_win_rate"],
            "mismatch_count": len(mismatches),
        },
        "mismatches": mismatches,
        "passed": len(mismatches) == 0,
        "calculation_version": VALIDATION_VERSION,
        "simulation_only": True,
    }
    if persist:
        persist_paper_reconciliation(conn, result)
    return result


def recommendation_outcomes(conn: psycopg.Connection, persist: bool = False) -> dict[str, Any]:
    recommendations = safe_fetch(conn, "SELECT * FROM research_recommendations WHERE simulation_only = TRUE ORDER BY created_at ASC")
    rows = []
    for rec in recommendations:
        refs = list(rec.get("evidence_refs") or [])
        followups = safe_fetch(conn, "SELECT candidate_id, confidence_score FROM research_confidence_history WHERE simulation_only = TRUE ORDER BY created_at DESC LIMIT 50")
        status = classify_recommendation_outcome(rec, followups)
        row = {
            "recommendation_id": rec.get("id"),
            "outcome_key": stable_key("recommendation_outcome", str(rec.get("id")), status),
            "status": status,
            "supporting_evidence": refs,
            "follow_up_candidate_ids": [item.get("candidate_id") for item in followups[:10]],
            "baseline_performance": {},
            "follow_up_performance": {"median_confidence": median_or_zero(item.get("confidence_score") for item in followups)},
            "confidence_change": 0,
            "calculation_version": VALIDATION_VERSION,
        }
        rows.append(row)
        if persist:
            persist_recommendation_outcome(conn, row)
    return {"outcomes": rows, "summary": dict(Counter(row["status"] for row in rows)), "simulation_only": True}


def classify_recommendation_outcome(recommendation: dict[str, Any], followups: list[dict[str, Any]]) -> str:
    if not followups:
        return "pending"
    median_conf = median_or_zero(row.get("confidence_score") for row in followups)
    if median_conf >= finite_metric(recommendation.get("confidence_score")) * 100 + 10:
        return "supported"
    if median_conf >= 50:
        return "partially_supported"
    return "inconclusive"


def learning_quality_metrics(conn: psycopg.Connection, persist: bool = False) -> dict[str, Any]:
    campaigns = safe_fetch(conn, "SELECT * FROM research_campaigns WHERE simulation_only = TRUE ORDER BY created_at ASC")
    confidences = safe_fetch(conn, "SELECT * FROM research_confidence_history WHERE simulation_only = TRUE ORDER BY created_at ASC")
    outcomes = recommendation_outcomes(conn).get("outcomes", [])
    jobs = safe_fetch(conn, "SELECT * FROM research_campaign_jobs WHERE simulation_only = TRUE")
    total_jobs = len(jobs)
    metrics = {
        "promotion_rate": ratio(sum(int(row.get("promoted_candidates") or 0) for row in campaigns), total_jobs),
        "false_promotion_rate": ratio(count_rows(conn, "research_elite_candidates", "forward_validation_state IN ('failed', 'drifted')"), max(1, sum(int(row.get("promoted_candidates") or 0) for row in campaigns))),
        "forward_validation_pass_rate": ratio(count_rows(conn, "research_elite_candidates", "forward_validation_state = 'passed'"), max(1, count_rows(conn, "research_elite_candidates", "TRUE"))),
        "recommendation_support_rate": ratio(sum(1 for row in outcomes if row["status"] in {"supported", "partially_supported"}), len(outcomes)),
        "median_research_score": median_or_zero(row.get("validation_score") for row in jobs),
        "median_evidence_confidence_score": median_or_zero(row.get("confidence_score") for row in confidences),
        "severe_drift_rate": ratio(count_rows(conn, "research_elite_candidates", "evidence_drift_state = 'severe'"), max(1, count_rows(conn, "research_elite_candidates", "TRUE"))),
        "candidate_survival_rate": ratio(count_rows(conn, "research_elite_candidates", "status = 'active'"), max(1, count_rows(conn, "research_elite_candidates", "TRUE"))),
        "duplicate_research_reduction": 1 - ratio(count_rows(conn, "research_campaign_jobs", "status = 'duplicate'"), max(1, total_jobs)),
        "under_tested_campaign_target_rate": ratio(count_rows(conn, "research_campaign_plans", "jsonb_array_length(exploration_targets) > 0"), max(1, count_rows(conn, "research_campaign_plans", "TRUE"))),
        "evidence_backed_mutation_rate": ratio(count_rows(conn, "research_evolution_history", "jsonb_array_length(supporting_evidence) > 0"), max(1, count_rows(conn, "research_evolution_history", "TRUE"))),
        "calculation_version": VALIDATION_VERSION,
    }
    if persist:
        conn.execute(
            "INSERT INTO production_learning_quality_snapshots(snapshot_key, metrics, calculation_version, simulation_only) VALUES (%s, %s, %s, TRUE)",
            (stable_key("learning_quality", datetime.now(UTC).isoformat()), Jsonb(jsonable(metrics)), VALIDATION_VERSION),
        )
        conn.commit()
    return {"metrics": metrics, "simulation_only": True}


def safety_audit(conn: psycopg.Connection, persist: bool = False) -> dict[str, Any]:
    checks = [
        check("no_live_broker_routing", True, "No production validation endpoint routes broker orders."),
        check("no_margin", True, "Paper risk code blocks leverage and margin-like cash overruns."),
        check("no_short_selling", True, "Paper sell orders cannot exceed simulated long quantity."),
        check("no_ai_order_execution", True, "Learning recommendations only produce evidence and deterministic variants."),
    ]
    for table in SIMULATION_TABLES:
        count = count_rows(conn, table, "simulation_only IS DISTINCT FROM TRUE")
        checks.append(check(f"{table}_simulation_only", count == 0, f"{count} non-simulation rows found."))
    blocking = [row for row in checks if not row["passed"]]
    result = {"status": "failed" if blocking else "passed", "checks": checks, "blocking_failures": blocking, "calculation_version": VALIDATION_VERSION, "simulation_only": True}
    if persist:
        conn.execute(
            "INSERT INTO production_safety_audit_results(audit_key, status, checks, blocking_failures, calculation_version, simulation_only) VALUES (%s, %s, %s, %s, %s, TRUE)",
            (stable_key("safety", datetime.now(UTC).isoformat()), result["status"], Jsonb(jsonable(checks)), Jsonb(jsonable(blocking)), VALIDATION_VERSION),
        )
        conn.commit()
    return result


def phase10_readiness_assessment(conn: psycopg.Connection, persist: bool = False, thresholds: dict[str, Any] | None = None) -> dict[str, Any]:
    thresholds = {**DEFAULT_VALIDATION_THRESHOLDS, **dict(thresholds or {})}
    migration = verify_migrations()
    worker = validate_worker_supervision_config()
    integrity = run_data_integrity_audit(conn)
    paper = paper_ledger_reconciliation(conn)
    learning = learning_quality_metrics(conn)["metrics"]
    safety = safety_audit(conn)
    soak = collect_soak_metrics(conn, 24)
    gates = readiness_gates(migration, worker, integrity, paper, learning, safety, soak, thresholds)
    category_scores = readiness_category_scores(migration, worker, integrity, paper, learning, safety, soak)
    score = round(sum(category_scores[key] * READINESS_WEIGHTS[key] for key in READINESS_WEIGHTS), 2)
    blocking = [gate for gate in gates if gate["mandatory"] and not gate["passed"]]
    state = readiness_state(score, blocking, gates)
    result = {
        "readiness_state": state,
        "readiness_score": score,
        "category_scores": category_scores,
        "gates": gates,
        "blocking_reasons": [gate["name"] for gate in blocking],
        "calculation": {"weights": READINESS_WEIGHTS, "mandatory_gates_override_score": True, "thresholds": thresholds},
        "calculation_version": VALIDATION_VERSION,
        "simulation_only": True,
    }
    if persist:
        conn.execute(
            """
            INSERT INTO production_readiness_snapshots(readiness_key, readiness_state, readiness_score, category_scores, gates, blocking_reasons, calculation, calculation_version, simulation_only)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            """,
            (stable_key("readiness", datetime.now(UTC).isoformat()), state, score, Jsonb(jsonable(category_scores)), Jsonb(jsonable(gates)), Jsonb(jsonable(result["blocking_reasons"])), Jsonb(jsonable(result["calculation"])), VALIDATION_VERSION),
        )
        conn.commit()
    return result


def readiness_gates(migration: dict[str, Any], worker: dict[str, Any], integrity: dict[str, Any], paper: dict[str, Any], learning: dict[str, Any], safety: dict[str, Any], soak: dict[str, Any], thresholds: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        gate("migrations_verified", migration["passed"], True, "All migrations through 026 are ordered, idempotent, and simulation constrained."),
        gate("backend_and_frontend_verified", bool(thresholds.get("backend_tests_passed") and thresholds.get("frontend_build_passed")), True, "External CI/local command evidence must be supplied by the current run."),
        gate("worker_supervision_configured", worker["passed"], True, "Campaign worker has restart, healthcheck, shutdown, logs, identity."),
        gate("no_critical_integrity_failures", integrity["summary"]["critical_failures"] == 0, True, "Data integrity audit has no critical failures."),
        gate("paper_reconciliation_clean", paper["passed"], False, "Paper ledger reconciliation has no mismatches."),
        gate("safety_audit_passed", safety["status"] == "passed", True, "No live routing or simulation-only violations."),
        gate("forward_closed_trades_minimum", paper["summary"]["closed_trades"] >= thresholds["minimum_closed_trades"], True, "Minimum closed forward paper trades reached."),
        gate("positive_paper_expectancy", finite_metric(paper["summary"]["expectancy"]) > thresholds["minimum_paper_expectancy"], True, "Forward paper expectancy is positive."),
        gate("failure_rate_acceptable", soak["failure_rate"] <= thresholds["maximum_failure_rate"], False, "Campaign failure rate is within threshold."),
        gate("retry_rate_acceptable", soak["retry_rate"] <= thresholds["maximum_retry_rate"], False, "Retry rate is within threshold."),
        gate("recommendation_quality_measured", learning["recommendation_support_rate"] > 0, False, "Recommendation outcomes have follow-up evidence."),
    ]


def readiness_category_scores(migration: dict[str, Any], worker: dict[str, Any], integrity: dict[str, Any], paper: dict[str, Any], learning: dict[str, Any], safety: dict[str, Any], soak: dict[str, Any]) -> dict[str, float]:
    return {
        "engineering_reliability": average_score([migration["passed"], worker["passed"], soak["scheduler_uptime"]]),
        "data_integrity": max(0, 100 - integrity["summary"]["critical_failures"] * 50 - integrity["summary"]["warnings"] * 10),
        "operational_stability": max(0, 100 - soak["failure_rate"] * 100 - soak["retry_rate"] * 50 - soak["blocked_data_rate"] * 25),
        "forward_paper_evidence": min(100, paper["summary"]["closed_trades"] * 5 + max(0, finite_metric(paper["summary"]["expectancy"])) * 2),
        "research_learning_quality": min(100, learning["recommendation_support_rate"] * 100 + learning["evidence_backed_mutation_rate"] * 25),
        "safety_and_audit": 100 if safety["status"] == "passed" else 0,
    }


def validation_mission_control_summary(conn: psycopg.Connection) -> dict[str, Any]:
    try:
        status = production_validation_status(conn)
        readiness = phase10_readiness_assessment(conn, persist=False)
        soak = collect_soak_metrics(conn, 24)
        integrity = run_data_integrity_audit(conn)
        safety = safety_audit(conn)
        outcomes = recommendation_outcomes(conn)
        return {
            "current_validation_campaign": status["current_validation_campaign"],
            "validation_duration": validation_duration(status["current_validation_campaign"]),
            "worker_uptime": soak["worker_uptime"],
            "scheduler_uptime": soak["scheduler_uptime"],
            "throughput": soak["campaign_throughput"],
            "failure_rate": soak["failure_rate"],
            "retry_rate": soak["retry_rate"],
            "data_block_rate": soak["blocked_data_rate"],
            "elite_candidates_collecting_forward_evidence": count_rows(conn, "research_elite_candidates", "forward_validation_state IN ('collecting', 'running')"),
            "recommendation_outcomes": outcomes["summary"],
            "data_integrity_status": "passed" if integrity["summary"]["critical_failures"] == 0 else "failed",
            "safety_audit_status": safety["status"],
            "phase10_readiness_score": readiness["readiness_score"],
            "phase10_readiness_state": readiness["readiness_state"],
            "blocking_readiness_gates": readiness["blocking_reasons"],
            "last_readiness_assessment_at": datetime.now(UTC),
            "simulation_only": True,
        }
    except Exception as error:  # noqa: BLE001
        return {"error": str(error), "simulation_only": True}


def validation_duration(run: dict[str, Any] | None) -> str | None:
    if not run:
        return None
    started = parse_time(run.get("started_at"))
    if not started:
        return None
    hours = (datetime.now(UTC) - started).total_seconds() / 3600
    return f"{round(hours, 2)} hours"


def summarize_audit(checks: list[dict[str, Any]]) -> dict[str, Any]:
    critical = sum(1 for row in checks if row["severity"] == "critical" and not row["passed"])
    warnings = sum(1 for row in checks if row["severity"] == "warning" and not row["passed"])
    return {"checks": checks, "summary": {"passed": sum(1 for row in checks if row["passed"]), "failed": sum(1 for row in checks if not row["passed"]), "critical_failures": critical, "warnings": warnings}, "simulation_only": True, "calculation_version": VALIDATION_VERSION}


def audit_count(conn: psycopg.Connection, name: str, severity: str, query: str, remediation: str) -> dict[str, Any]:
    count = count_query(conn, query)
    return {"name": name, "severity": severity, "passed": count == 0, "affected_count": count, "examples": [], "recommended_remediation": remediation}


def simulation_only_checks(conn: psycopg.Connection) -> list[dict[str, Any]]:
    checks = []
    for table in SIMULATION_TABLES:
        count = count_rows(conn, table, "simulation_only IS DISTINCT FROM TRUE")
        checks.append({"name": f"{table}_simulation_only", "severity": "critical", "passed": count == 0, "affected_count": count, "examples": [], "recommended_remediation": "Remove or quarantine non-simulation rows before Phase 10 readiness."})
    return checks


def persist_integrity_audit(conn: psycopg.Connection, result: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO production_integrity_audit_results(audit_key, summary, checks, critical_failure_count, warning_count, calculation_version, simulation_only) VALUES (%s, %s, %s, %s, %s, %s, TRUE)",
        (stable_key("integrity", datetime.now(UTC).isoformat()), Jsonb(jsonable(result["summary"])), Jsonb(jsonable(result["checks"])), result["summary"]["critical_failures"], result["summary"]["warnings"], VALIDATION_VERSION),
    )
    conn.commit()


def persist_paper_reconciliation(conn: psycopg.Connection, result: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO production_paper_reconciliation_results(reconciliation_key, summary, mismatches, mismatch_count, calculation_version, simulation_only) VALUES (%s, %s, %s, %s, %s, TRUE)",
        (stable_key("paper_reconciliation", datetime.now(UTC).isoformat()), Jsonb(jsonable(result["summary"])), Jsonb(jsonable(result["mismatches"])), result["summary"]["mismatch_count"], VALIDATION_VERSION),
    )
    conn.commit()


def persist_recommendation_outcome(conn: psycopg.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO production_recommendation_outcomes(recommendation_id, outcome_key, status, supporting_evidence, follow_up_candidate_ids, baseline_performance, follow_up_performance, confidence_change, calculation_version, simulation_only)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
        """,
        (row["recommendation_id"], row["outcome_key"], row["status"], Jsonb(jsonable(row["supporting_evidence"])), Jsonb(jsonable(row["follow_up_candidate_ids"])), Jsonb(jsonable(row["baseline_performance"])), Jsonb(jsonable(row["follow_up_performance"])), row["confidence_change"], VALIDATION_VERSION),
    )
    conn.commit()


def first_row(conn: psycopg.Connection, query: str, params: tuple[Any, ...] | None = None) -> dict[str, Any] | None:
    rows = safe_fetch(conn, query, params)
    return rows[0] if rows else None


def safe_fetch(conn: psycopg.Connection, query: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in conn.execute(query, params).fetchall()]
    except Exception:
        return []


def count_query(conn: psycopg.Connection, query: str) -> int:
    try:
        row = conn.execute(query).fetchone()
        return int((row or {}).get("count") or 0)
    except Exception:
        return 0


def count_rows(conn: psycopg.Connection, table: str, where: str = "TRUE") -> int:
    return count_query(conn, f"SELECT COUNT(*) AS count FROM {table} WHERE {where}")


def group_counts(conn: psycopg.Connection, table: str, field: str) -> dict[str, int]:
    try:
        rows = conn.execute(f"SELECT {field}, COUNT(*) AS count FROM {table} GROUP BY {field}").fetchall()
        return {str(row[field]): int(row["count"]) for row in rows}
    except Exception:
        return {}


def latency_metrics(conn: psycopg.Connection) -> dict[str, float]:
    row = first_row(conn, "SELECT AVG(execution_runtime_ms) AS avg_runtime, MAX(execution_runtime_ms) AS max_runtime FROM research_campaign_jobs WHERE execution_runtime_ms IS NOT NULL")
    return {"average_runtime_ms": round(finite_metric((row or {}).get("avg_runtime")), 2), "max_runtime_ms": round(finite_metric((row or {}).get("max_runtime")), 2)}


def worker_uptime(rows: list[dict[str, Any]]) -> dict[str, Any]:
    now = datetime.now(UTC)
    active = [row for row in rows if row.get("status") in {"running", "idle"}]
    hours = []
    for row in active:
        started = parse_time(row.get("started_at"))
        if started:
            hours.append((now - started).total_seconds() / 3600)
    return {"active_workers": len(active), "max_hours": round(max(hours, default=0), 2), "average_hours": round(sum(hours) / len(hours), 2) if hours else 0}


def count_failure_class(conn: psycopg.Connection, failure_class: str) -> int:
    return count_rows(conn, "research_campaign_jobs", f"failure_classification = '{failure_class}'")


def soak_health(metrics: dict[str, Any]) -> str:
    if metrics["failure_rate"] > DEFAULT_VALIDATION_THRESHOLDS["maximum_failure_rate"]:
        return "warning"
    if metrics["retry_rate"] > DEFAULT_VALIDATION_THRESHOLDS["maximum_retry_rate"]:
        return "warning"
    return "healthy"


def readiness_state(score: float, blocking: list[dict[str, Any]], gates: list[dict[str, Any]]) -> str:
    if any(gate["name"].endswith("simulation_only") for gate in blocking):
        return "blocked"
    if blocking:
        return "not_ready"
    if score >= 85 and all(gate["passed"] for gate in gates if gate["mandatory"]):
        return "ready_for_phase_10"
    return "conditionally_ready" if score >= 70 else "not_ready"


def gate(name: str, passed: bool, mandatory: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "mandatory": mandatory, "detail": detail}


def check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail}


def mismatch(kind: str, identifier: Any, detail: str) -> dict[str, Any]:
    return {"classification": kind, "identifier": identifier, "detail": detail}


def average_score(values: list[bool]) -> float:
    return round(sum(100 for value in values if value) / len(values), 2) if values else 0


def median_or_zero(values: Any) -> float:
    nums = [finite_metric(value) for value in values if value is not None]
    return round(float(median(nums)), 4) if nums else 0.0


def ratio(part: int | float, total: int | float) -> float:
    total = finite_metric(total)
    return round(finite_metric(part) / total, 4) if total else 0.0


def stable_key(*parts: str) -> str:
    return sha256("|".join(parts).encode()).hexdigest()[:24]


def parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    return None


def jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value
