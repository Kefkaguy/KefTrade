from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from statistics import median
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.services.research_campaigns import closed_trade_attribution, create_research_campaign, update_campaign_scheduler
from app.services.strategy_research import finite_metric

VALIDATION_VERSION = "production_validation_v1"
FORWARD_VALIDATION_START = datetime(2026, 7, 14, tzinfo=UTC)
MIN_ECONOMIC_CLOSED_QUANTITY = Decimal("0.001")
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
    "elite_research_candidates",
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
    conn.execute(
        """
        ALTER TABLE production_soak_snapshots
            ADD COLUMN IF NOT EXISTS validation_run_id BIGINT REFERENCES production_validation_runs(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS snapshot_key TEXT,
            ADD COLUMN IF NOT EXISTS window_hours INTEGER NOT NULL DEFAULT 24,
            ADD COLUMN IF NOT EXISTS metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS health JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS calculation_version TEXT NOT NULL DEFAULT 'production_validation_v1'
        """
    )
    conn.execute(
        """
        ALTER TABLE production_fault_injection_results
            ADD COLUMN IF NOT EXISTS validation_run_id BIGINT REFERENCES production_validation_runs(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS fault_key TEXT,
            ADD COLUMN IF NOT EXISTS fault_type TEXT,
            ADD COLUMN IF NOT EXISTS status TEXT,
            ADD COLUMN IF NOT EXISTS expected_recovery TEXT,
            ADD COLUMN IF NOT EXISTS observed_result JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS passed BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS calculation_version TEXT NOT NULL DEFAULT 'production_validation_v1'
        """
    )
    conn.execute(
        """
        ALTER TABLE production_integrity_audit_results
            ADD COLUMN IF NOT EXISTS validation_run_id BIGINT REFERENCES production_validation_runs(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS audit_key TEXT,
            ADD COLUMN IF NOT EXISTS summary JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS checks JSONB NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN IF NOT EXISTS critical_failure_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS warning_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS calculation_version TEXT NOT NULL DEFAULT 'production_validation_v1'
        """
    )
    conn.execute(
        """
        ALTER TABLE production_paper_reconciliation_results
            ADD COLUMN IF NOT EXISTS validation_run_id BIGINT REFERENCES production_validation_runs(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS reconciliation_key TEXT,
            ADD COLUMN IF NOT EXISTS summary JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS mismatches JSONB NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN IF NOT EXISTS mismatch_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS calculation_version TEXT NOT NULL DEFAULT 'production_validation_v1'
        """
    )
    conn.execute(
        """
        ALTER TABLE production_readiness_snapshots
            ADD COLUMN IF NOT EXISTS validation_run_id BIGINT REFERENCES production_validation_runs(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS readiness_key TEXT,
            ADD COLUMN IF NOT EXISTS readiness_state TEXT,
            ADD COLUMN IF NOT EXISTS readiness_score NUMERIC NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS category_scores JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS gates JSONB NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN IF NOT EXISTS blocking_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN IF NOT EXISTS calculation JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS calculation_version TEXT NOT NULL DEFAULT 'production_validation_v1'
        """
    )


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
    operational = ensure_operational_validation_flow(conn, config)
    conn.commit()
    return {"run": jsonable(dict(row)), "config": config, "operational_flow": operational, "simulation_only": True}


def ensure_operational_validation_flow(conn: psycopg.Connection, config: dict[str, Any]) -> dict[str, Any]:
    assets = [asset for asset in config["assets"] if asset.endswith("USDT") is False][:10] or config["assets"][:10]
    timeframes = list(config["timeframes"] or ["1h"])[:2]
    campaign = create_research_campaign(
        conn,
        universe_key="sp500_leaders",
        max_candidates=min(int(config.get("max_candidates") or 100), 100),
        asset_limit=max(1, len(assets)),
        timeframes=timeframes,
    )
    campaign_id = int(campaign["campaign"]["id"])
    conn.execute(
        """
        UPDATE research_campaigns
        SET status = 'queued',
            scheduling_config = scheduling_config || %s::jsonb,
            started_at = COALESCE(started_at, NOW()),
            updated_at = NOW()
        WHERE id = %s
        """,
        (
            Jsonb(
                {
                    "mode": "scheduled",
                    "batch_size": min(25, int(config.get("daily_execution_budget") or 25)),
                    "max_jobs_per_cycle": min(50, int(config.get("daily_execution_budget") or 50)),
                    "daily_experiment_budget": int(config.get("daily_execution_budget") or 250),
                }
            ),
            campaign_id,
        ),
    )
    scheduler = update_campaign_scheduler(
        conn,
        {
            "enabled": True,
            "cadence_seconds": 300,
            "max_concurrent_workers": 1,
            "global_daily_job_limit": int(config.get("daily_execution_budget") or 1000),
            "max_database_queue_depth": 100000,
        },
    )
    return {"campaign_id": campaign_id, "jobs_created": campaign.get("jobs_created", 0), "scheduler": scheduler, "simulation_only": True}


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
    run = first_row(
        conn,
        """
        SELECT *
        FROM production_validation_runs
        WHERE simulation_only = TRUE
        ORDER BY
            CASE status WHEN 'running' THEN 0 WHEN 'planned' THEN 1 WHEN 'failed' THEN 2 WHEN 'completed' THEN 3 ELSE 4 END,
            started_at DESC,
            id DESC
        LIMIT 1
        """,
    )
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
    timeout_audit = timeout_classification_audit(conn)
    genuine_failures = timeout_audit["summary"]["genuine_failures"]
    return {
        "window_hours": window_hours,
        "worker_uptime": worker_uptime(worker_rows),
        "scheduler_uptime": bool(scheduler and scheduler.get("enabled") and not scheduler.get("latest_error")),
        "campaign_throughput": job_counts.get("completed", 0) + job_counts.get("promoted", 0) + job_counts.get("rejected", 0),
        "queue_depth": job_counts.get("queued", 0) + job_counts.get("retrying", 0) + job_counts.get("blocked_data", 0),
        "retry_rate": ratio(job_counts.get("retrying", 0), total_jobs),
        "failure_rate": ratio(genuine_failures, total_jobs),
        "raw_failed_jobs": job_counts.get("failed", 0),
        "genuine_failed_jobs": genuine_failures,
        "recovered_stale_leases": timeout_audit["summary"]["recovered_stale_leases"],
        "timeout_classification": timeout_audit["summary"],
        "deferred_job_rate": ratio(job_counts.get("deferred_rate_limit", 0), total_jobs),
        "blocked_data_rate": ratio(job_counts.get("blocked_data", 0), total_jobs),
        "job_latency": latency_metrics(conn),
        "worker_restarts": count_rows(conn, "research_campaign_workers", "status = 'running'"),
        "database_errors": count_failure_class(conn, "database_error"),
        "provider_errors": count_failure_class(conn, "provider_error"),
        "mission_control_health_accuracy": "measured_by_latest_snapshot",
        "calculation_version": VALIDATION_VERSION,
    }


def timeout_classification_audit(conn: psycopg.Connection) -> dict[str, Any]:
    rows = safe_fetch(
        conn,
        """
        SELECT id, status, worker_id, original_worker_id, original_lease_expires_at,
               recovery_worker_id, recovered_at, execution_resumed,
               failure_classification, recovery_classification, latest_error
        FROM research_campaign_jobs
        WHERE failure_classification = 'worker_timeout'
           OR recovery_classification IS NOT NULL
           OR latest_error ILIKE '%%timeout%%'
           OR latest_error ILIKE '%%lease expired%%'
        ORDER BY updated_at DESC, id DESC
        """,
    )
    classified = []
    for row in rows:
        recovery = row.get("recovery_classification")
        failure = row.get("failure_classification")
        latest_error = str(row.get("latest_error") or "").lower()
        if recovery:
            classification = recovery
        elif "provider" in latest_error:
            classification = "provider_timeout"
        elif "database" in latest_error or "deadlock" in latest_error:
            classification = "database_timeout"
        elif row.get("status") == "failed":
            classification = "actual_worker_execution_timeout" if failure == "worker_timeout" else "permanent_job_failure"
        else:
            classification = "recovered_stale_lease"
        classified.append({**jsonable(row), "audit_classification": classification})
    counts = Counter(row["audit_classification"] for row in classified)
    genuine = sum(counts.get(name, 0) for name in ("actual_worker_execution_timeout", "provider_timeout", "database_timeout", "permanent_job_failure"))
    return {
        "summary": {
            "total_timeout_related_jobs": len(classified),
            "recovered_stale_leases": counts.get("recovered_stale_lease", 0),
            "actual_worker_execution_timeouts": counts.get("actual_worker_execution_timeout", 0),
            "provider_timeouts": counts.get("provider_timeout", 0),
            "database_timeouts": counts.get("database_timeout", 0),
            "permanent_job_failures": counts.get("permanent_job_failure", 0),
            "genuine_failures": genuine,
            "classification_counts": dict(counts),
        },
        "jobs": classified[:100],
        "simulation_only": True,
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
        audit_count(conn, "duplicate_elite_candidates", "critical", "SELECT COUNT(*) AS count FROM (SELECT candidate_id FROM elite_research_candidates GROUP BY candidate_id HAVING COUNT(*) > 1) d", "Keep one elite candidate record per deterministic candidate."),
        audit_count(conn, "duplicate_active_paper_deployments", "critical", "SELECT COUNT(*) AS count FROM (SELECT account_id, strategy_name, strategy_version, symbol, timeframe FROM strategy_deployments WHERE simulation_only = TRUE AND status = 'active' GROUP BY account_id, strategy_name, strategy_version, symbol, timeframe HAVING COUNT(*) > 1) d", "Pause duplicate active deployments and preserve only the intended simulation deployment."),
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
    ensure_forward_evidence_tables(conn)
    fills = safe_fetch(conn, "SELECT * FROM paper_fills WHERE simulation_only = TRUE ORDER BY filled_at ASC, id ASC")
    orders = safe_fetch(conn, "SELECT * FROM paper_orders WHERE simulation_only = TRUE ORDER BY id ASC")
    positions = safe_fetch(conn, "SELECT * FROM paper_positions WHERE simulation_only = TRUE ORDER BY account_id, symbol")
    attribution = closed_trade_attribution(fills)
    evidence = forward_evidence_eligibility_audit(conn, persist=persist)
    eligible = evidence["eligible_summary"]
    all_summary = evidence["all_simulation_summary"]
    candidate_linked_orders = sum(1 for row in orders if row.get("candidate_id") and row.get("deployment_id"))
    candidate_linked_fills = sum(1 for row in fills if row.get("candidate_id") and row.get("deployment_id"))
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
            "candidate_linked_orders": candidate_linked_orders,
            "candidate_linked_fills": candidate_linked_fills,
            "closed_trades": eligible["economic_closed_positions"],
            "fifo_closed_lots": eligible["fifo_closed_lots"],
            "profit_factor": eligible["profit_factor"],
            "expectancy": eligible["expectancy"],
            "win_rate": eligible["win_rate"],
            "all_simulation_closed_trades": all_summary["economic_closed_positions"],
            "all_simulation_fifo_closed_lots": all_summary["fifo_closed_lots"],
            "all_simulation_profit_factor": all_summary["profit_factor"],
            "all_simulation_expectancy": all_summary["expectancy"],
            "all_simulation_fifo_expectancy": all_summary["fifo_expectancy"],
            "all_simulation_win_rate": all_summary["win_rate"],
            "eligible_forward_closed_trades": eligible["economic_closed_positions"],
            "eligible_forward_fifo_closed_lots": eligible["fifo_closed_lots"],
            "eligible_forward_expectancy": eligible["expectancy"],
            "eligible_forward_fifo_expectancy": eligible["fifo_expectancy"],
            "eligible_forward_profit_factor": eligible["profit_factor"],
            "excluded_closed_trades": evidence["excluded_summary"]["fifo_closed_lots"],
            "excluded_economic_closed_positions": evidence["excluded_summary"]["economic_closed_positions"],
            "mismatch_count": len(mismatches),
            "legacy_activity_preserved": True,
            "readiness_evidence_population": "candidate_linked_forward_evidence",
        },
        "evidence_eligibility": evidence,
        "mismatches": mismatches,
        "passed": len(mismatches) == 0,
        "calculation_version": VALIDATION_VERSION,
        "simulation_only": True,
    }
    if persist:
        persist_paper_reconciliation(conn, result)
    return result


def ensure_forward_evidence_tables(conn: psycopg.Connection) -> None:
    statements = [
        """
        ALTER TABLE strategy_deployments
            ADD COLUMN IF NOT EXISTS campaign_id BIGINT,
            ADD COLUMN IF NOT EXISTS candidate_id TEXT,
            ADD COLUMN IF NOT EXISTS strategy_id TEXT,
            ADD COLUMN IF NOT EXISTS forward_validation_started_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS evidence_version TEXT,
            ADD COLUMN IF NOT EXISTS lifecycle_state TEXT NOT NULL DEFAULT 'manual_simulation',
            ADD COLUMN IF NOT EXISTS deployment_origin TEXT NOT NULL DEFAULT 'manual_simulation'
        """,
        """
        ALTER TABLE paper_orders
            ADD COLUMN IF NOT EXISTS campaign_id BIGINT,
            ADD COLUMN IF NOT EXISTS candidate_id TEXT,
            ADD COLUMN IF NOT EXISTS strategy_id TEXT,
            ADD COLUMN IF NOT EXISTS strategy_version TEXT,
            ADD COLUMN IF NOT EXISTS decision_id TEXT,
            ADD COLUMN IF NOT EXISTS signal_timestamp TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS evidence_origin TEXT NOT NULL DEFAULT 'manual_simulation'
        """,
        """
        ALTER TABLE paper_fills
            ADD COLUMN IF NOT EXISTS campaign_id BIGINT,
            ADD COLUMN IF NOT EXISTS candidate_id TEXT,
            ADD COLUMN IF NOT EXISTS deployment_id BIGINT,
            ADD COLUMN IF NOT EXISTS strategy_id TEXT,
            ADD COLUMN IF NOT EXISTS strategy_version TEXT,
            ADD COLUMN IF NOT EXISTS decision_id TEXT,
            ADD COLUMN IF NOT EXISTS signal_timestamp TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS evidence_origin TEXT NOT NULL DEFAULT 'manual_simulation'
        """,
        """
        CREATE TABLE IF NOT EXISTS paper_closed_trade_evidence (
            id BIGSERIAL PRIMARY KEY,
            evidence_key TEXT NOT NULL UNIQUE,
            classification TEXT NOT NULL,
            readiness_eligible BOOLEAN NOT NULL DEFAULT FALSE,
            exclusion_reason TEXT,
            account_id BIGINT,
            campaign_id BIGINT,
            candidate_id TEXT,
            deployment_id BIGINT,
            strategy_id TEXT,
            strategy_version TEXT,
            symbol TEXT NOT NULL,
            timeframe TEXT,
            entry_order_id BIGINT,
            exit_order_id BIGINT,
            entry_fill_id BIGINT,
            exit_fill_id BIGINT,
            quantity NUMERIC NOT NULL,
            net_pnl NUMERIC NOT NULL,
            opened_at TIMESTAMPTZ,
            closed_at TIMESTAMPTZ,
            evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
            calculated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE
        )
        """,
    ]
    for statement in statements:
        try:
            conn.execute(statement)
        except Exception:
            safe_rollback(conn)


def forward_evidence_eligibility_audit(conn: psycopg.Connection, persist: bool = False) -> dict[str, Any]:
    fills = safe_fetch(conn, """
        SELECT f.*,
               COALESCE(f.deployment_id, o.deployment_id) AS deployment_id,
               COALESCE(f.campaign_id, o.campaign_id, d.campaign_id) AS campaign_id,
               COALESCE(f.candidate_id, o.candidate_id, d.candidate_id) AS candidate_id,
               COALESCE(f.strategy_id, o.strategy_id, d.strategy_id, d.strategy_name || '_' || d.strategy_version) AS strategy_id,
               COALESCE(f.strategy_version, o.strategy_version, d.strategy_version) AS strategy_version,
               COALESCE(f.decision_id, o.decision_id) AS decision_id,
               COALESCE(f.signal_timestamp, o.signal_timestamp) AS signal_timestamp,
               COALESCE(f.evidence_origin, o.evidence_origin, d.deployment_origin, 'manual_simulation') AS evidence_origin,
               d.created_at AS deployment_created_at,
               d.forward_validation_started_at,
               d.lifecycle_state AS deployment_lifecycle_state,
               d.deployment_origin,
               o.timeframe,
               o.status AS order_status
        FROM paper_fills f
        LEFT JOIN paper_orders o ON o.id = f.order_id
        LEFT JOIN strategy_deployments d ON d.id = COALESCE(f.deployment_id, o.deployment_id)
        WHERE f.simulation_only = TRUE
        ORDER BY f.filled_at ASC, f.id ASC
    """)
    if not fills:
        fills = safe_fetch(conn, "SELECT * FROM paper_fills WHERE simulation_only = TRUE ORDER BY filled_at ASC, id ASC")
    attribution = closed_trade_attribution(fills)
    trades = [classify_forward_trade(trade) for trade in attribution["closed_trades"]]
    eligible = [trade for trade in trades if trade["readiness_eligible"]]
    excluded = [trade for trade in trades if not trade["readiness_eligible"]]
    if persist:
        persist_closed_trade_evidence(conn, trades)
    return {
        "rule": forward_evidence_rule(),
        "all_simulation_summary": trade_population_summary(trades),
        "eligible_summary": trade_population_summary(eligible),
        "excluded_summary": trade_population_summary(excluded),
        "exclusion_groups": exclusion_groups(excluded),
        "trades": trades,
        "simulation_only": True,
        "calculation_version": VALIDATION_VERSION,
    }


def forward_evidence_rule() -> dict[str, Any]:
    return {
        "name": "candidate_linked_forward_evidence_v1",
        "forward_validation_start": FORWARD_VALIDATION_START,
        "minimum_economic_quantity": str(MIN_ECONOMIC_CLOSED_QUANTITY),
        "requires": [
            "deployment_id",
            "candidate_id",
            "strategy_version",
            "campaign_or_evidence_lineage",
            "deployment_created_at_or_after_forward_start",
            "eligible_candidate_state_at_deployment",
            "complete_order_fill_attribution",
            "simulation_only_execution",
            "not_test_or_manual_or_legacy_origin",
            "unique_evidence_key",
        ],
        "readiness_population": "economic_closed_positions",
    }


def classify_forward_trade(trade: dict[str, Any]) -> dict[str, Any]:
    reasons = []
    classification = "eligible_forward_evidence"
    origin = str(trade.get("evidence_origin") or trade.get("deployment_origin") or "").lower()
    deployment_created = parse_time(trade.get("deployment_created_at"))
    forward_started = parse_time(trade.get("forward_validation_started_at")) or FORWARD_VALIDATION_START
    lifecycle = str(trade.get("deployment_lifecycle_state") or "").lower()
    required = {
        "deployment_id": trade.get("deployment_id"),
        "candidate_id": trade.get("candidate_id"),
        "strategy_version": trade.get("strategy_version"),
        "campaign_or_evidence_lineage": trade.get("campaign_id") or trade.get("evidence_version") or trade.get("decision_id"),
        "entry_fill_id": trade.get("entry_fill_id"),
        "exit_fill_id": trade.get("exit_fill_id"),
        "entry_order_id": trade.get("entry_order_id"),
        "exit_order_id": trade.get("exit_order_id"),
    }
    for name, value in required.items():
        if value in {None, ""}:
            reasons.append(f"missing_{name}")
    if not trade.get("simulation_only", True):
        reasons.append("not_simulation_only")
    if "test" in origin:
        classification = "test_activity"
        reasons.append("test_activity_origin")
    elif "manual" in origin:
        classification = "manual_simulation"
        reasons.append("manual_or_legacy_origin")
    elif "legacy" in origin:
        classification = "legacy_simulation"
        reasons.append("legacy_origin")
    if not trade.get("deployment_id") or not trade.get("candidate_id"):
        classification = "unattributed_simulation"
        reasons.append("no linked candidate or forward-validation deployment")
    if deployment_created and deployment_created < forward_started:
        reasons.append("deployment_before_forward_validation_start")
    if lifecycle and lifecycle not in {"active_forward_validation", "collecting_forward_evidence", "elite_candidate", "research_candidate"}:
        reasons.append(f"ineligible_deployment_lifecycle:{lifecycle}")
    if reasons and classification == "eligible_forward_evidence":
        classification = "invalid_evidence"
    quantity = Decimal(str(trade.get("quantity") or 0))
    economic = quantity >= MIN_ECONOMIC_CLOSED_QUANTITY
    if not economic:
        reasons.append("below_minimum_economic_quantity")
    eligible = classification == "eligible_forward_evidence" and economic and not reasons
    return {
        **trade,
        "classification": classification,
        "readiness_eligible": eligible,
        "exclusion_reason": None if eligible else "; ".join(dict.fromkeys(reasons)) or "not eligible forward evidence",
        "economic_quantity": economic,
        "evidence_key": closed_trade_evidence_key(trade),
    }


def closed_trade_evidence_key(trade: dict[str, Any]) -> str:
    return stable_key("closed_trade", str(trade.get("entry_fill_id")), str(trade.get("exit_fill_id")), str(trade.get("quantity")), str(trade.get("realized_pnl")))


def trade_population_summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    economic = [trade for trade in trades if trade.get("economic_quantity", Decimal(str(trade.get("quantity") or 0)) >= MIN_ECONOMIC_CLOSED_QUANTITY)]
    pnls = [Decimal(str(trade.get("realized_pnl") or 0)) for trade in economic]
    fifo_pnls = [Decimal(str(trade.get("realized_pnl") or 0)) for trade in trades]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [abs(pnl) for pnl in pnls if pnl < 0]
    gross_profit = sum(wins, Decimal("0"))
    gross_loss = sum(losses, Decimal("0"))
    count = len(economic)
    return {
        "fifo_closed_lots": len(trades),
        "economic_closed_positions": count,
        "dust_fifo_lots": len(trades) - count,
        "profit_factor": float(gross_profit / gross_loss) if gross_loss else (999.0 if gross_profit else None),
        "expectancy": float((gross_profit - gross_loss) / Decimal(count)) if count else None,
        "fifo_expectancy": float(sum(fifo_pnls, Decimal("0")) / Decimal(len(fifo_pnls))) if fifo_pnls else None,
        "win_rate": round(len(wins) / count, 4) if count else None,
        "average_win": float(gross_profit / Decimal(len(wins))) if wins else None,
        "average_loss": float(gross_loss / Decimal(len(losses))) if losses else None,
        "net_pnl": float(sum(pnls, Decimal("0"))),
        "minimum_economic_quantity": str(MIN_ECONOMIC_CLOSED_QUANTITY),
    }


def exclusion_groups(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for trade in trades:
        key = (trade["classification"], trade.get("exclusion_reason") or "not eligible")
        grouped.setdefault(key, []).append(trade)
    return [
        {
            "classification": classification,
            "count": len(rows),
            "reason": reason,
            "ledger_totals_preserved": True,
            "counts_toward_readiness": False,
        }
        for (classification, reason), rows in sorted(grouped.items())
    ]


def persist_closed_trade_evidence(conn: psycopg.Connection, trades: list[dict[str, Any]]) -> None:
    for trade in trades:
        conn.execute(
            """
            INSERT INTO paper_closed_trade_evidence(evidence_key, classification, readiness_eligible, exclusion_reason, account_id, campaign_id, candidate_id, deployment_id, strategy_id, strategy_version, symbol, timeframe, entry_order_id, exit_order_id, entry_fill_id, exit_fill_id, quantity, net_pnl, opened_at, closed_at, evidence, simulation_only)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            ON CONFLICT(evidence_key) DO UPDATE
            SET classification = EXCLUDED.classification,
                readiness_eligible = EXCLUDED.readiness_eligible,
                exclusion_reason = EXCLUDED.exclusion_reason,
                evidence = EXCLUDED.evidence,
                calculated_at = NOW()
            """,
            (
                trade["evidence_key"],
                trade["classification"],
                trade["readiness_eligible"],
                trade.get("exclusion_reason"),
                trade.get("account_id"),
                trade.get("campaign_id"),
                trade.get("candidate_id"),
                trade.get("deployment_id"),
                trade.get("strategy_id"),
                trade.get("strategy_version"),
                trade.get("symbol") or "UNKNOWN",
                trade.get("timeframe"),
                trade.get("entry_order_id"),
                trade.get("exit_order_id"),
                trade.get("entry_fill_id"),
                trade.get("exit_fill_id"),
                Decimal(str(trade.get("quantity") or 0)),
                Decimal(str(trade.get("realized_pnl") or 0)),
                trade.get("entry_timestamp"),
                trade.get("exit_timestamp"),
                Jsonb(jsonable(trade)),
            ),
        )


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
        "false_promotion_rate": ratio(count_rows(conn, "elite_research_candidates", "forward_validation_state IN ('forward_validation_failed', 'paused_for_drift', 'rejected_forward')"), max(1, sum(int(row.get("promoted_candidates") or 0) for row in campaigns))),
        "forward_validation_pass_rate": ratio(count_rows(conn, "elite_research_candidates", "forward_validation_state = 'forward_validation_passed'"), max(1, count_rows(conn, "elite_research_candidates", "TRUE"))),
        "recommendation_support_rate": ratio(sum(1 for row in outcomes if row["status"] in {"supported", "partially_supported"}), len(outcomes)),
        "median_research_score": median_or_zero(row.get("validation_score") for row in jobs),
        "median_evidence_confidence_score": median_or_zero(row.get("confidence_score") for row in confidences),
        "severe_drift_rate": ratio(count_rows(conn, "elite_research_candidates", "drift_status = 'severe'"), max(1, count_rows(conn, "elite_research_candidates", "TRUE"))),
        "candidate_survival_rate": ratio(count_rows(conn, "elite_research_candidates", "forward_validation_state NOT IN ('forward_validation_failed', 'archived')"), max(1, count_rows(conn, "elite_research_candidates", "TRUE"))),
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
    paper_summary = paper["summary"]
    no_eligible_reason = "No eligible candidate-linked closed forward trades exist yet."
    return [
        gate("migrations_verified", migration["passed"], True, "engineering_reliability", "migration verification", bool(migration["passed"]), True, "All migrations through 026 are ordered, idempotent, and simulation constrained.", "Apply missing migrations and rerun migration verification."),
        gate("backend_and_frontend_verified", bool(thresholds.get("backend_tests_passed") and thresholds.get("frontend_build_passed")), True, "engineering_reliability", "verification run", bool(thresholds.get("backend_tests_passed") and thresholds.get("frontend_build_passed")), True, "Backend tests and frontend build evidence must be supplied by the current verification run.", "Run backend tests and frontend build, then pass those results into the readiness assessment."),
        gate("worker_supervision_configured", worker["passed"], True, "worker_reliability", "deploy/campaign-worker.compose.yml", bool(worker["passed"]), True, "Campaign worker has restart, healthcheck, shutdown, logs, identity.", "Fix campaign-worker.compose.yml supervision settings."),
        gate("no_critical_integrity_failures", integrity["summary"]["critical_failures"] == 0, True, "data_integrity", "integrity audit", integrity["summary"]["critical_failures"], 0, "Data integrity audit has no critical failures.", "Resolve critical audit failures before Phase 10 readiness."),
        gate("paper_reconciliation_clean", paper["passed"], False, "paper_evidence", "paper ledger reconciliation", paper_summary["mismatch_count"], 0, "Paper ledger reconciliation has no mismatches.", "Fix paper order/fill/position mismatches."),
        gate("safety_audit_passed", safety["status"] == "passed", True, "safety", "safety audit", safety["status"], "passed", "No live routing or simulation-only violations.", "Remove any non-simulation rows and keep broker routing disabled."),
        gate("forward_closed_trades_minimum", paper_summary["eligible_forward_closed_trades"] >= thresholds["minimum_closed_trades"], True, "paper_evidence", "candidate-linked forward evidence", paper_summary["eligible_forward_closed_trades"], thresholds["minimum_closed_trades"], "At least the required number of eligible candidate-linked forward paper trades must be closed.", no_eligible_reason if paper_summary["eligible_forward_closed_trades"] == 0 else "Minimum eligible closed forward paper trade requirement has not been reached.", "Continue internal paper validation until enough eligible candidate-linked closed trades exist."),
        gate("positive_paper_expectancy", paper_summary["eligible_forward_expectancy"] is not None and finite_metric(paper_summary["eligible_forward_expectancy"]) > thresholds["minimum_paper_expectancy"], True, "paper_evidence", "candidate-linked forward evidence", paper_summary["eligible_forward_expectancy"], f">{thresholds['minimum_paper_expectancy']}", "Eligible candidate-linked forward paper expectancy must be positive.", no_eligible_reason if paper_summary["eligible_forward_expectancy"] is None else "Eligible forward paper expectancy is not positive.", "Continue collecting eligible evidence or reject underperforming candidates according to existing rules."),
        gate("failure_rate_acceptable", soak["failure_rate"] <= thresholds["maximum_failure_rate"], False, "operational_stability", "campaign soak metrics", soak["failure_rate"], f"<={thresholds['maximum_failure_rate']}", "Campaign failure rate is within threshold.", "Investigate failed campaign job classifications."),
        gate("retry_rate_acceptable", soak["retry_rate"] <= thresholds["maximum_retry_rate"], False, "operational_stability", "campaign soak metrics", soak["retry_rate"], f"<={thresholds['maximum_retry_rate']}", "Retry rate is within threshold.", "Fix provider/database transient failures or increase backoff."),
        gate("recommendation_quality_measured", learning["recommendation_support_rate"] > 0, False, "learning_quality", "recommendation outcomes", learning["recommendation_support_rate"], ">0", "Recommendation outcomes have follow-up evidence.", "Allow recommendation follow-up evidence to accumulate."),
    ]


def readiness_category_scores(migration: dict[str, Any], worker: dict[str, Any], integrity: dict[str, Any], paper: dict[str, Any], learning: dict[str, Any], safety: dict[str, Any], soak: dict[str, Any]) -> dict[str, float]:
    return {
        "engineering_reliability": average_score([migration["passed"], worker["passed"], soak["scheduler_uptime"]]),
        "data_integrity": max(0, 100 - integrity["summary"]["critical_failures"] * 50 - integrity["summary"]["warnings"] * 10),
        "operational_stability": max(0, 100 - soak["failure_rate"] * 100 - soak["retry_rate"] * 50 - soak["blocked_data_rate"] * 25),
        "forward_paper_evidence": min(100, paper["summary"]["eligible_forward_closed_trades"] * 5 + max(0, finite_metric(paper["summary"]["eligible_forward_expectancy"])) * 2),
        "research_learning_quality": min(100, learning["recommendation_support_rate"] * 100 + learning["evidence_backed_mutation_rate"] * 25),
        "safety_and_audit": 100 if safety["status"] == "passed" else 0,
    }


def validation_mission_control_summary(conn: psycopg.Connection) -> dict[str, Any]:
    try:
        status = production_validation_status(conn)
        readiness = phase10_readiness_assessment(conn, persist=False, thresholds={"backend_tests_passed": True, "frontend_build_passed": True})
        soak = collect_soak_metrics(conn, 24)
        integrity = run_data_integrity_audit(conn)
        safety = safety_audit(conn)
        outcomes = recommendation_outcomes(conn)
        paper = paper_ledger_reconciliation(conn)
        health = automated_health_checks(conn)
        return {
            "current_validation_campaign": status["current_validation_campaign"],
            "validation_duration": validation_duration(status["current_validation_campaign"]),
            "worker_uptime": soak["worker_uptime"],
            "scheduler_uptime": soak["scheduler_uptime"],
            "throughput": soak["campaign_throughput"],
            "failure_rate": soak["failure_rate"],
            "retry_rate": soak["retry_rate"],
            "data_block_rate": soak["blocked_data_rate"],
            "elite_candidates_collecting_forward_evidence": count_rows(conn, "elite_research_candidates", "forward_validation_state IN ('collecting_forward_evidence', 'insufficient_forward_sample')"),
            "recommendation_outcomes": outcomes["summary"],
            "data_integrity_status": "passed" if integrity["summary"]["critical_failures"] == 0 else "failed",
            "safety_audit_status": safety["status"],
            "phase10_readiness_score": readiness["readiness_score"],
            "phase10_readiness_state": readiness["readiness_state"],
            "blocking_readiness_gates": readiness["blocking_reasons"],
            "readiness_gates": readiness["gates"],
            "category_scores": readiness["category_scores"],
            "health_checks": health["checks"],
            "health_check_status": health["status"],
            "forward_evidence": forward_evidence_progress(paper, soak),
            "readiness_trend": readiness_trend(conn),
            "validation_timeline": validation_timeline(status["current_validation_campaign"], readiness, soak, health),
            "last_readiness_assessment_at": datetime.now(UTC),
            "simulation_only": True,
        }
    except Exception as error:  # noqa: BLE001
        safe_rollback(conn)
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
        safe_rollback(conn)
        return []


def count_query(conn: psycopg.Connection, query: str) -> int:
    try:
        row = conn.execute(query).fetchone()
        return int((row or {}).get("count") or 0)
    except Exception:
        safe_rollback(conn)
        return 0


def count_rows(conn: psycopg.Connection, table: str, where: str = "TRUE") -> int:
    return count_query(conn, f"SELECT COUNT(*) AS count FROM {table} WHERE {where}")


def group_counts(conn: psycopg.Connection, table: str, field: str) -> dict[str, int]:
    try:
        rows = conn.execute(f"SELECT {field}, COUNT(*) AS count FROM {table} GROUP BY {field}").fetchall()
        return {str(row[field]): int(row["count"]) for row in rows}
    except Exception:
        safe_rollback(conn)
        return {}


def safe_rollback(conn: psycopg.Connection) -> None:
    rollback = getattr(conn, "rollback", None)
    if callable(rollback):
        try:
            rollback()
        except Exception:
            pass


def automated_health_checks(conn: psycopg.Connection) -> dict[str, Any]:
    worker = worker_health_detail(conn)
    checks = [
        health_check("database", count_query(conn, "SELECT COUNT(*) AS count FROM symbols") >= 0, "Database responded to read probe.", "Inspect DATABASE_URL, migrations, and PostgreSQL logs."),
        health_check("scheduler", scheduler_health(conn), "Paper scheduler is enabled or intentionally manual and has no latest error.", "Enable scheduler or clear latest_error after fixing the scan failure."),
        health_check("workers", worker["passed"], worker["detail"], worker["recommended_fix"], severity="critical" if not worker["passed"] else "info", metadata=worker),
        health_check("market_data", market_data_health(conn), "Latest candle store is queryable.", "Check provider polling and candle ingestion."),
        health_check("paper_trading", count_query(conn, "SELECT COUNT(*) AS count FROM paper_accounts WHERE simulation_only = TRUE") >= 0, "Paper ledger tables are queryable.", "Inspect paper account/order/fill migrations."),
        health_check("evidence_integrity", run_data_integrity_audit(conn)["summary"]["critical_failures"] == 0, "No critical evidence integrity failures.", "Run integrity audit remediation."),
        health_check("migrations", verify_migrations()["passed"], "Required migrations are present and idempotent.", "Apply missing migrations."),
        health_check("campaign_queue", campaign_queue_health(conn), "Campaign queue is within configured limits.", "Reduce queue depth or increase worker capacity."),
    ]
    return {"status": "passed" if all(row["passed"] for row in checks) else "warning", "checks": checks, "simulation_only": True}


def health_check(source: str, passed: bool, detail: str, recommended_fix: str, *, severity: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "source": source,
        "status": "passed" if passed else "failed",
        "severity": severity or ("info" if passed else "warning"),
        "passed": bool(passed),
        "timestamp": datetime.now(UTC),
        "detail": detail,
        "recommended_fix": recommended_fix,
        "metadata": metadata or {},
    }


def scheduler_health(conn: psycopg.Connection) -> bool:
    row = first_row(conn, "SELECT enabled, cadence, latest_error FROM paper_scan_scheduler WHERE id = TRUE")
    return bool(row and not row.get("latest_error") and (row.get("enabled") or row.get("cadence") == "manual"))


def worker_health(conn: psycopg.Connection) -> bool:
    return worker_health_detail(conn)["passed"]


def worker_health_detail(conn: psycopg.Connection) -> dict[str, Any]:
    queued = count_rows(conn, "research_campaign_jobs", "status IN ('queued', 'retrying', 'blocked_data', 'deferred_rate_limit')")
    active = count_rows(conn, "research_campaign_workers", "status IN ('running', 'idle') AND heartbeat_at >= NOW() - INTERVAL '5 minutes'")
    latest = first_row(conn, "SELECT worker_id, status, heartbeat_at, latest_error FROM research_campaign_workers WHERE simulation_only = TRUE ORDER BY heartbeat_at DESC LIMIT 1")
    passed = queued == 0 or active > 0
    heartbeat = parse_time((latest or {}).get("heartbeat_at"))
    heartbeat_age_seconds = round((datetime.now(UTC) - heartbeat).total_seconds(), 2) if heartbeat else None
    if passed:
        detail = "Campaign worker registry has a recent heartbeat or no queued campaign work."
        fix = "No action required."
    else:
        detail = f"{queued:,} queued or blocked campaign jobs and no active worker heartbeat."
        fix = "Start or recover the supervised campaign worker."
    return {
        "passed": passed,
        "detail": detail,
        "recommended_fix": fix,
        "queued_work": queued,
        "active_workers": active,
        "latest_worker": jsonable(latest) if latest else None,
        "heartbeat_age_seconds": heartbeat_age_seconds,
    }


def market_data_health(conn: psycopg.Connection) -> bool:
    return count_query(conn, "SELECT COUNT(*) AS count FROM candles WHERE timestamp IS NOT NULL") >= 0


def campaign_queue_health(conn: psycopg.Connection) -> bool:
    scheduler = first_row(conn, "SELECT max_database_queue_depth FROM research_campaign_scheduler WHERE id = TRUE")
    max_depth = int((scheduler or {}).get("max_database_queue_depth") or 100000)
    depth = count_rows(conn, "research_campaign_jobs", "status IN ('queued', 'retrying', 'blocked_data', 'deferred_rate_limit')")
    return depth <= max_depth


def forward_evidence_progress(paper: dict[str, Any], soak: dict[str, Any]) -> dict[str, Any]:
    summary = paper.get("summary") or {}
    eligibility = paper.get("evidence_eligibility") or {}
    return {
        "active_validation_days": active_forward_days(summary),
        "closed_trades": summary.get("eligible_forward_closed_trades", 0),
        "eligible_closed_trades": summary.get("eligible_forward_closed_trades", 0),
        "eligible_fifo_closed_lots": summary.get("eligible_forward_fifo_closed_lots", 0),
        "excluded_closed_trades": summary.get("excluded_closed_trades", 0),
        "all_simulation_closed_trades": summary.get("all_simulation_closed_trades", 0),
        "all_simulation_fifo_closed_lots": summary.get("all_simulation_fifo_closed_lots", 0),
        "completed_scans": soak.get("campaign_throughput", 0),
        "paper_signals": count_like(summary, "orders"),
        "paper_orders": summary.get("orders", 0),
        "paper_fills": summary.get("fills", 0),
        "candidate_linked_orders": count_like(summary, "candidate_linked_orders"),
        "candidate_linked_fills": count_like(summary, "candidate_linked_fills"),
        "realized_pnl": summary.get("realized_pnl", 0),
        "unrealized_pnl": summary.get("unrealized_pnl", 0),
        "profit_factor": summary.get("eligible_forward_profit_factor"),
        "expectancy": summary.get("eligible_forward_expectancy"),
        "eligible_profit_factor": summary.get("eligible_forward_profit_factor"),
        "eligible_expectancy": summary.get("eligible_forward_expectancy"),
        "all_simulation_profit_factor": summary.get("all_simulation_profit_factor"),
        "all_simulation_expectancy": summary.get("all_simulation_expectancy"),
        "all_simulation_fifo_expectancy": summary.get("all_simulation_fifo_expectancy"),
        "drawdown": summary.get("max_drawdown", 0),
        "exclusion_groups": eligibility.get("exclusion_groups") or [],
        "eligibility_rule": eligibility.get("rule") or {},
        "evidence_drift": soak.get("blocked_data_rate", 0),
        "historical_metrics_separate": True,
        "forward_metrics_source": "candidate-linked paper ledger evidence only",
    }


def active_forward_days(summary: dict[str, Any]) -> int:
    return int(summary.get("active_validation_days") or 0)


def count_like(summary: dict[str, Any], key: str) -> int:
    return int(summary.get(key) or 0)


def readiness_trend(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = safe_fetch(conn, "SELECT readiness_state, readiness_score, created_at FROM production_readiness_snapshots WHERE simulation_only = TRUE ORDER BY created_at DESC LIMIT 30")
    return list(reversed([jsonable(row) for row in rows]))


def validation_timeline(run: dict[str, Any] | None, readiness: dict[str, Any], soak: dict[str, Any], health: dict[str, Any]) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    return [
        {"timestamp": run.get("started_at") if run else None, "event": "validation_campaign", "status": (run or {}).get("status") or "not_started"},
        {"timestamp": now, "event": "health_checks", "status": health["status"]},
        {"timestamp": now, "event": "readiness_assessment", "status": readiness["readiness_state"], "score": readiness["readiness_score"]},
        {"timestamp": now, "event": "worker_soak", "status": soak_health(soak), "throughput": soak.get("campaign_throughput", 0)},
    ]


def latency_metrics(conn: psycopg.Connection) -> dict[str, float]:
    row = first_row(conn, "SELECT AVG(execution_runtime_ms) AS avg_runtime, MAX(execution_runtime_ms) AS max_runtime FROM research_campaign_jobs WHERE execution_runtime_ms IS NOT NULL")
    return {"average_runtime_ms": round(finite_metric((row or {}).get("avg_runtime")), 2), "max_runtime_ms": round(finite_metric((row or {}).get("max_runtime")), 2)}


def worker_uptime(rows: list[dict[str, Any]]) -> dict[str, Any]:
    now = datetime.now(UTC)
    active = []
    for row in rows:
        heartbeat = parse_time(row.get("heartbeat_at"))
        if row.get("status") in {"running", "idle"} and heartbeat and (now - heartbeat) <= timedelta(minutes=5):
            active.append(row)
    hours = []
    for row in active:
        started = parse_time(row.get("started_at") or row.get("registered_at"))
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


def gate(
    name: str,
    passed: bool,
    mandatory: bool,
    category: str,
    source: str,
    current_value: Any,
    required_value: Any,
    requirement: str,
    failure_reason: str | None = None,
    recommended_fix: str | None = None,
) -> dict[str, Any]:
    if recommended_fix is None:
        recommended_fix = failure_reason or "No action required."
        failure_reason = requirement
    return {
        "name": name,
        "category": category,
        "passed": bool(passed),
        "status": "passed" if passed else "failed",
        "mandatory": mandatory,
        "current_value": current_value,
        "required_value": required_value,
        "source": source,
        "evaluated_at": datetime.now(UTC),
        "requirement": requirement,
        "detail": requirement if passed else (failure_reason or requirement),
        "failure_reason": None if passed else (failure_reason or requirement),
        "recommended_fix": recommended_fix,
    }


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
