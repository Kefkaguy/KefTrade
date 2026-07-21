from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Callable

import psycopg

from app.services.research_campaigns import campaign_mission_control_summary
from app.services.production_validation import validation_mission_control_summary
from app.services.research_learning import research_learning_summary
from app.services.broker_read_models import broker_status

FRESHNESS_BY_TIMEFRAME_HOURS = {
    "15m": 2,
    "30m": 4,
    "60m": 8,
    "1h": 8,
    "4h": 24,
    "1d": 72,
}

REVIEW_PRIORITY = {
    "scheduler_error": 10,
    "stale_data_warning": 20,
    "entry_setup_review": 30,
    "exit_risk_review": 40,
    "avoid_condition": 60,
    "duplicate_candle_skip": 70,
}

ERROR_EVENT_TYPES = {"paper_scheduler_scan_error", "paper_scheduler_loop_error"}
STALE_EVENT_TYPES = {"paper_scan_stale_data_skipped"}
DUPLICATE_EVENT_TYPES = {"paper_scan_duplicate_candle_skipped"}
SCAN_EVENT_TYPES = {"paper_scan_completed", "paper_scan_stale_data_skipped", "paper_scan_duplicate_candle_skipped", "paper_scheduler_scan_result"}
ORDER_EVENT_TYPES = {"paper_order_submitted", "paper_order_rejected", "paper_order_filled", "paper_order_canceled", "protective_order_created", "protective_order_canceled"}
FILL_EVENT_TYPES = {"paper_order_filled"}


def get_mission_control(conn: psycopg.Connection) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []

    def section(name: str, fn: Callable[[], Any], fallback: Any) -> Any:
        try:
            return fn()
        except Exception as error:  # noqa: BLE001 - endpoint must return partial dashboard data
            rollback_after_section_error(conn)
            errors.append(diagnostic_error(name, error))
            return fallback

    now = datetime.now(UTC)
    scheduler = section("scheduler", lambda: scheduler_status(conn), None)
    deployments = section("deployments", lambda: simulation_deployments(conn), [])
    accounts = section("paper_accounts", lambda: paper_accounts(conn), [])
    positions = section("paper_positions", lambda: paper_positions(conn), [])
    orders = section("paper_orders", lambda: paper_orders(conn), [])
    fills = section("paper_fills", lambda: paper_fills(conn), [])
    equity = section("paper_equity", lambda: paper_equity(conn), [])
    alerts = section("evidence_alerts", lambda: evidence_alerts(conn), [])
    reviews = section("signal_reviews", lambda: signal_reviews(conn), [])
    logs = section("execution_logs", lambda: execution_logs(conn), [])
    symbols = section("symbols", lambda: active_symbols(conn), [])
    campaigns = section("research_campaigns", lambda: campaign_mission_control_summary(conn), {})
    learning = section("research_learning", lambda: research_learning_summary(conn), {})
    validation = section("production_validation", lambda: validation_mission_control_summary(conn), {})
    external_broker_paper = section("external_broker_paper", lambda: broker_status(conn), {})

    asset_keys = monitored_asset_keys(symbols, deployments, alerts, reviews, positions)
    latest_candles = section("market_data", lambda: latest_candles_for(conn, asset_keys), {})
    assets = build_assets(
        now=now,
        asset_keys=asset_keys,
        symbols=symbols,
        deployments=deployments,
        alerts=alerts,
        reviews=reviews,
        positions=positions,
        latest_candles=latest_candles,
    )
    review_queue = build_review_queue(alerts, reviews, assets)
    active_deployments = build_active_deployments(deployments, alerts, positions)
    recent_activity = build_recent_activity(logs, alerts, orders, fills, reviews)
    paper = build_paper_account(accounts, positions, orders, fills, equity)
    summary = build_research_summary(assets, deployments, alerts, logs, paper, campaigns)
    health = build_system_health(now, scheduler, assets, deployments, alerts, logs, errors)
    daily = build_daily_summary(now, logs, alerts, assets, orders, positions)
    diagnostics = build_diagnostics(errors, now, validation)
    readiness = authoritative_readiness(validation, now)
    campaign = authoritative_campaign(campaigns, validation)
    workers = authoritative_workers(campaigns, validation)
    market_data = authoritative_market_data(assets, logs)
    forward_evidence = authoritative_forward_evidence(validation)
    invariants = consistency_invariants(readiness, campaign, workers, forward_evidence, diagnostics)

    return {
        "generated_at": now,
        "snapshot_version": "mission_control_v2",
        "simulation_only": True,
        "safety": {
            "status": "Simulation protected",
            "detail": "Live-money routing is disabled; Alpaca Paper execution is explicitly gated" if external_broker_paper.get("execution_enabled") else "Live-money routing is disabled; Alpaca Paper is in observation mode",
            "simulation_only": True,
            "live_routing_enabled": False,
            "broker_order_routing": "alpaca_paper_enabled" if external_broker_paper.get("execution_enabled") else "disabled",
        },
        "system_health": health,
        "health": {
            "engineering_status": health["overall_status"],
            "checks": validation.get("health_checks") or [],
            "status": validation.get("health_check_status") or ("warning" if errors else "unknown"),
        },
        "readiness": readiness,
        "campaign": campaign,
        "workers": workers,
        "market_data": market_data,
        "forward_evidence": forward_evidence,
        "diagnostics": diagnostics,
        "invariants": invariants,
        "research_summary": summary,
        "assets": assets,
        "review_queue": review_queue,
        "deployments": active_deployments,
        "paper_account": paper,
        "research_campaigns": campaigns,
        "research_learning": learning,
        "production_validation": validation,
        "external_broker_paper": external_broker_paper,
        "recent_activity": recent_activity,
        "daily_summary": daily,
        "subsystem_errors": diagnostics["active"],
    }


def compact_campaign_operations(campaigns: dict[str, Any]) -> dict[str, Any]:
    """Keep operational counters while excluding multi-megabyte campaign payloads."""
    keys = (
        "active_worker_count", "healthy_worker_count", "stale_worker_count",
        "active_campaigns", "queued_campaigns", "queue_depth", "queued_jobs",
        "running_jobs", "completed_jobs", "rejected_jobs", "failed_jobs",
        "blocked_data_jobs", "deferred_jobs", "retrying_jobs", "claimed_jobs",
        "generated_candidates", "promoted_candidates", "scheduler_enabled",
        "worker_utilization", "average_job_runtime_ms", "queue_throughput",
        "oldest_queued_job_age_hours", "campaign_eta", "current_experiment",
        "simulation_only",
    )
    return {key: campaigns.get(key) for key in keys if key in campaigns}


def compact_mission_control_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded dashboard contract without mutating the full snapshot."""
    assets = list(snapshot.get("assets") or [])
    return {
        **snapshot,
        "asset_count": len(assets),
        "assets": [],
        "research_campaigns": compact_campaign_operations(dict(snapshot.get("research_campaigns") or {})),
    }


def authoritative_readiness(validation: dict[str, Any], now: datetime) -> dict[str, Any]:
    gates = list(validation.get("readiness_gates") or [])
    blocking = [gate for gate in gates if gate.get("mandatory") and not gate.get("passed")]
    state = validation.get("phase10_readiness_state") or ("unknown" if not gates else "not_ready")
    score = validation.get("phase10_readiness_score")
    return {
        "state": state,
        "score": score,
        "phase_10_allowed": state == "ready_for_phase_10" and not blocking,
        "blocking_gate_count": len(blocking),
        "blocking_gates": blocking,
        "passed_gates": [gate for gate in gates if gate.get("passed")],
        "gates": gates,
        "last_assessed_at": validation.get("last_readiness_assessment_at") or now,
        "snapshot_source": "production_validation_readiness_service",
    }


def authoritative_campaign(campaigns: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
    current = validation.get("current_validation_campaign") or {}
    config = current.get("config") or {}
    state = current.get("status") or campaigns.get("current_experiment") or "unavailable"
    return {
        "id": current.get("id"),
        "state": state,
        "configuration_version": config.get("strategy_generation_version") or current.get("strategy_generation_version"),
        "name": config.get("name") or current.get("name"),
        "started_at": current.get("started_at"),
        "queue_depth": campaigns.get("queue_depth"),
        "running_jobs": campaigns.get("running_jobs"),
        "blocked_data_jobs": campaigns.get("blocked_data_jobs"),
        "completed_jobs": campaigns.get("completed_jobs"),
        "rejected_jobs": campaigns.get("rejected_jobs"),
        "completed_or_rejected_jobs": campaigns.get("completed_or_rejected_jobs"),
        "failed_jobs": campaigns.get("failed_jobs"),
        "genuine_failed_jobs": campaigns.get("genuine_failed_jobs"),
        "recovered_stale_leases": campaigns.get("recovered_stale_leases"),
        "count_reconciliation": campaigns.get("count_reconciliation"),
        "completed_last_24h": campaigns.get("jobs_completed_last_24h"),
        "throughput": validation.get("throughput") or campaigns.get("queue_throughput"),
        "last_progress_at": campaigns.get("last_scheduler_cycle") or current.get("updated_at") or current.get("started_at"),
        "eta": campaigns.get("campaign_eta"),
    }


def authoritative_workers(campaigns: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
    uptime = validation.get("worker_uptime") or {}
    return {
        "active": campaigns.get("active_worker_count", uptime.get("active_workers")),
        "healthy": campaigns.get("healthy_worker_count"),
        "stale": campaigns.get("stale_worker_count"),
        "uptime": uptime,
        "utilization": campaigns.get("worker_utilization"),
        "workers": campaigns.get("workers") or [],
    }


def authoritative_market_data(assets: list[dict[str, Any]], logs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "monitored_assets": len(assets),
        "stale_monitored_assets": sum(1 for asset in assets if asset.get("data_freshness") == "Stale"),
        "warning_monitored_assets": sum(1 for asset in assets if asset.get("data_freshness") == "Warning"),
        "market_closed_assets": sum(1 for asset in assets if "Market closed" in str(asset.get("data_freshness_detail"))),
        "missing_candle_datasets": sum(1 for asset in assets if asset.get("latest_candle_timestamp") is None),
        "paper_scans_blocked_by_stale_data": count_events(logs, STALE_EVENT_TYPES),
        "provider_failures": count_events(logs, ERROR_EVENT_TYPES),
    }


def authoritative_forward_evidence(validation: dict[str, Any]) -> dict[str, Any]:
    evidence = dict(validation.get("forward_evidence") or {})
    closed = int(evidence.get("closed_trades") or 0)
    expectancy = evidence.get("expectancy")
    return {
        **evidence,
        "closed_trades": closed,
        "expectancy": expectancy,
        "has_data": closed > 0 or int(evidence.get("paper_fills") or 0) > 0,
        "gate_status": "failed" if closed > 0 and (expectancy is not None and Decimal(str(expectancy)) <= 0) else "pending",
        "source": "paper_ledger_reconciliation",
    }


def build_diagnostics(errors: list[dict[str, Any]], now: datetime, validation: dict[str, Any] | None = None) -> dict[str, Any]:
    active = []
    for error in errors:
        active.append({
            **error,
            "active": True,
            "first_seen_at": error.get("timestamp") or now,
            "last_seen_at": error.get("timestamp") or now,
            "resolved_at": None,
            "occurrence_count": 1,
        })
    for check in (validation or {}).get("health_checks") or []:
        if check.get("passed"):
            continue
        metadata = check.get("metadata") or {}
        active.append({
            "subsystem": check.get("source"),
            "source": check.get("source"),
            "severity": check.get("severity") or "warning",
            "timestamp": check.get("timestamp") or now,
            "error": check.get("detail"),
            "last_error": (metadata.get("latest_worker") or {}).get("latest_error"),
            "recommended_fix": check.get("recommended_fix"),
            "active": True,
            "first_seen_at": check.get("timestamp") or now,
            "last_seen_at": check.get("timestamp") or now,
            "resolved_at": None,
            "occurrence_count": 1,
            "metadata": metadata,
        })
    return {"active": active, "resolved": [], "history": active, "active_count": len(active)}


def consistency_invariants(readiness: dict[str, Any], campaign: dict[str, Any], workers: dict[str, Any], forward_evidence: dict[str, Any], diagnostics: dict[str, Any]) -> list[dict[str, Any]]:
    running_with_queue_without_worker = (
        campaign.get("state") == "running"
        and int(campaign.get("queue_depth") or 0) > 0
        and int(workers.get("healthy") or workers.get("active") or 0) == 0
    )
    count_reconciliation = campaign.get("count_reconciliation") or {}
    checks = [
        ("readiness_not_allowed_when_not_ready", readiness["state"] == "ready_for_phase_10" or readiness["phase_10_allowed"] is False),
        ("blocking_gate_count_matches", readiness["blocking_gate_count"] == len(readiness["blocking_gates"])),
        ("blocking_gates_present_when_count_positive", readiness["blocking_gate_count"] == 0 or bool(readiness["blocking_gates"])),
        ("campaign_count_reconciliation", bool(count_reconciliation.get("passed", True))),
        ("running_campaign_has_started_at", campaign["state"] != "running" or bool(campaign.get("started_at"))),
        ("running_campaign_has_healthy_worker_when_queue_open", not running_with_queue_without_worker),
        ("forward_evidence_visible_when_present", not forward_evidence["has_data"] or forward_evidence.get("closed_trades") is not None),
        ("active_diagnostics_match_count", diagnostics["active_count"] == len(diagnostics["active"])),
    ]
    return [{"name": name, "passed": passed, "severity": "critical" if not passed else "info"} for name, passed in checks]


def rollback_after_section_error(conn: psycopg.Connection) -> None:
    rollback = getattr(conn, "rollback", None)
    if callable(rollback):
        try:
            rollback()
        except Exception:
            pass


def diagnostic_error(subsystem: str, error: Exception) -> dict[str, Any]:
    message = str(error)
    if "current transaction is aborted" in message.lower():
        message = "A previous database operation failed and the transaction was rolled back before continuing."
    return {
        "subsystem": subsystem,
        "source": subsystem,
        "severity": "critical" if subsystem in {"market_data", "scheduler", "paper_orders", "paper_fills"} else "warning",
        "timestamp": datetime.now(UTC),
        "error": message,
        "last_error": str(error),
        "recommended_fix": recommended_fix_for(subsystem, str(error)),
    }


def recommended_fix_for(subsystem: str, error: str) -> str:
    lowered = error.lower()
    if "current transaction is aborted" in lowered:
        return "Rollback the failed transaction and inspect the first database error in the service logs."
    if subsystem == "market_data":
        return "Verify candle table access, provider ingestion, and latest completed candle queries."
    if subsystem == "scheduler":
        return "Check scheduler state, latest_error, and recent paper_scheduler_* execution logs."
    if subsystem == "research_campaigns":
        return "Check campaign scheduler, worker leases, and failed campaign job classifications."
    if subsystem == "production_validation":
        return "Run readiness health checks and inspect failed validation gates."
    return "Review the subsystem query and service logs for the first failing operation."


def scheduler_status(conn: psycopg.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM paper_scan_scheduler WHERE id = TRUE").fetchone()
    return dict(row) if row else None


def simulation_deployments(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return list(
        conn.execute(
            """
            SELECT *
            FROM strategy_deployments
            WHERE simulation_only = TRUE
            ORDER BY status = 'active' DESC, created_at DESC, id DESC
            """
        ).fetchall()
    )


def paper_accounts(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return list(conn.execute("SELECT * FROM paper_accounts WHERE simulation_only = TRUE ORDER BY created_at DESC").fetchall())


def paper_positions(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return list(
        conn.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (symbol) symbol, close
                FROM candles
                ORDER BY symbol, timestamp DESC
            )
            SELECT p.*,
                   COALESCE(latest.close, 0) AS last_price,
                   CASE WHEN p.quantity > 0 THEN p.quantity * COALESCE(latest.close, 0) ELSE 0 END AS market_value,
                   CASE WHEN p.quantity > 0 THEN (COALESCE(latest.close, p.average_price) - p.average_price) * p.quantity ELSE 0 END AS unrealized_pnl
            FROM paper_positions p
            LEFT JOIN latest ON latest.symbol = p.symbol
            WHERE p.simulation_only = TRUE
            ORDER BY p.account_id, p.symbol
            """
        ).fetchall()
    )


def paper_orders(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return list(conn.execute("SELECT * FROM paper_orders WHERE simulation_only = TRUE ORDER BY submitted_at DESC LIMIT 100").fetchall())


def paper_fills(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return list(conn.execute("SELECT * FROM paper_fills WHERE simulation_only = TRUE ORDER BY filled_at DESC LIMIT 100").fetchall())


def paper_equity(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return list(conn.execute("SELECT * FROM paper_equity_curve WHERE simulation_only = TRUE ORDER BY timestamp DESC LIMIT 200").fetchall())


def evidence_alerts(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return list(conn.execute("SELECT * FROM evidence_alerts WHERE simulation_only = TRUE ORDER BY created_at DESC LIMIT 250").fetchall())


def signal_reviews(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return list(conn.execute("SELECT * FROM signal_reviews WHERE simulation_only = TRUE ORDER BY created_at DESC LIMIT 250").fetchall())


def execution_logs(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return list(conn.execute("SELECT * FROM execution_logs WHERE simulation_only = TRUE ORDER BY created_at DESC LIMIT 300").fetchall())


def active_symbols(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return list(conn.execute("SELECT * FROM symbols WHERE is_active = TRUE ORDER BY symbol").fetchall())


def monitored_asset_keys(
    symbols: list[dict[str, Any]],
    deployments: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    positions: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for deployment in deployments:
        keys.add((deployment["symbol"], deployment["timeframe"]))
    for alert in alerts:
        if alert["symbol"] != "SYSTEM":
            keys.add((alert["symbol"], alert["timeframe"]))
    for review in reviews:
        keys.add((review["symbol"], review["timeframe"]))
    for position in positions:
        if Decimal(position.get("quantity") or 0) > 0:
            keys.add((position["symbol"], "1d"))
    for symbol in symbols:
        if symbol.get("symbol"):
            keys.add((symbol["symbol"], "1d" if symbol.get("asset_class") == "equity" else "4h"))
    return sorted(keys)


def latest_candles_for(conn: psycopg.Connection, asset_keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    if not asset_keys:
        return {}
    values = ", ".join(["(%s, %s)"] * len(asset_keys))
    params: list[Any] = []
    for symbol, timeframe in asset_keys:
        params.extend([symbol, timeframe])
    rows = conn.execute(
        f"""
        WITH monitored(symbol, timeframe) AS (VALUES {values}),
        ranked AS (
            SELECT c.symbol, c.timeframe, c.timestamp, c.close, c.source,
                   ROW_NUMBER() OVER (PARTITION BY c.symbol, c.timeframe ORDER BY c.timestamp DESC) AS row_number
            FROM candles c
            JOIN monitored m ON m.symbol = c.symbol AND m.timeframe = c.timeframe
        )
        SELECT symbol, timeframe, timestamp, close, source
        FROM ranked
        WHERE row_number = 1
        """,
        params,
    ).fetchall()
    candles: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        candles[(row["symbol"], row["timeframe"])] = dict(row)
    return candles


def build_assets(
    *,
    now: datetime,
    asset_keys: list[tuple[str, str]],
    symbols: list[dict[str, Any]],
    deployments: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    latest_candles: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    symbol_meta = {row["symbol"]: row for row in symbols}
    latest_alert = latest_by_key(alerts, lambda row: (row["symbol"], row["timeframe"]))
    latest_review = latest_by_key(reviews, lambda row: (row["symbol"], row["timeframe"]))
    active_deployments = defaultdict(list)
    for deployment in deployments:
        if deployment.get("status") == "active" and deployment.get("simulation_only") is True:
            active_deployments[(deployment["symbol"], deployment["timeframe"])].append(deployment)
    positions_by_symbol = defaultdict(list)
    for position in positions:
        positions_by_symbol[position["symbol"]].append(position)

    rows = []
    for symbol, timeframe in asset_keys:
        meta = symbol_meta.get(symbol, {})
        deployment = active_deployments[(symbol, timeframe)][0] if active_deployments[(symbol, timeframe)] else None
        alert = latest_alert.get((symbol, timeframe))
        review = latest_review.get((symbol, timeframe))
        candle = latest_candles.get((symbol, timeframe))
        freshness = classify_candle_freshness(
            timestamp=candle.get("timestamp") if candle else None,
            timeframe=timeframe,
            asset_class=meta.get("asset_class"),
            now=now,
        )
        position = next((row for row in positions_by_symbol.get(symbol, []) if Decimal(row.get("quantity") or 0) > 0), None)
        status = asset_status(freshness["classification"], deployment, alert, review)
        metrics_source = review or alert or {}
        rows.append(
            {
                "symbol": symbol,
                "asset_class": meta.get("asset_class") or "unknown",
                "timeframe": timeframe,
                "selected_strategy": strategy_label(deployment, alert, review),
                "deployment_status": deployment.get("status") if deployment else "not_deployed",
                "status": status,
                "latest_verdict": (review or alert or {}).get("verdict") or "No Setup",
                "evidence_score": (review or {}).get("evidence_score") or evidence_score(alert),
                "profit_factor": metrics_source.get("profit_factor"),
                "expectancy": metrics_source.get("expectancy"),
                "trade_count": metrics_source.get("trade_count"),
                "max_drawdown": metrics_source.get("max_drawdown"),
                "current_regime": metrics_source.get("regime"),
                "latest_candle_timestamp": candle.get("timestamp") if candle else None,
                "data_age_hours": freshness["age_hours"],
                "data_freshness": freshness["classification"],
                "data_freshness_detail": freshness["detail"],
                "latest_scan_timestamp": deployment.get("last_scan_at") if deployment else None,
                "alert_severity": alert.get("severity") if alert else None,
                "paper_position_status": "open_simulated_position" if position else "no_simulated_position",
                "simulated_unrealized_pnl": position.get("unrealized_pnl") if position else Decimal("0"),
                "links": {
                    "signal_review": f"/paper#signal-review",
                    "paper_lab": "/paper",
                    "candidate_detail": "/promising",
                    "alert_detail": "/paper#evidence-alerts",
                    "asset_research": f"/assets/{symbol}",
                },
            }
        )
    return rows


def classify_candle_freshness(timestamp: Any, timeframe: str, asset_class: str | None, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    parsed = parse_datetime(timestamp)
    if parsed is None:
        return {"classification": "Warning", "age_hours": None, "detail": "Provider unavailable: no completed candle stored"}
    age_hours = max(0, (now - parsed).total_seconds() / 3600)
    max_age = FRESHNESS_BY_TIMEFRAME_HOURS.get(timeframe, 24)
    if is_equity_asset(asset_class) and within_equity_market_closed_grace(parsed, now):
        return {"classification": "Healthy", "age_hours": round(age_hours, 2), "detail": "Market closed: latest completed candle is expected"}
    if age_hours <= max_age:
        return {"classification": "Healthy", "age_hours": round(age_hours, 2), "detail": f"Healthy: latest completed candle within {max_age}h"}
    if age_hours <= max_age * 2:
        return {"classification": "Warning", "age_hours": round(age_hours, 2), "detail": f"Needs attention: candle older than {max_age}h"}
    return {"classification": "Stale", "age_hours": round(age_hours, 2), "detail": f"Stale data: candle older than {max_age * 2}h"}


def is_equity_asset(asset_class: str | None) -> bool:
    return bool(asset_class and "equity" in asset_class.lower())


def within_equity_market_closed_grace(candle_time: datetime, now: datetime) -> bool:
    if now.weekday() == 5:  # Saturday
        return candle_time.weekday() == 4 and (now - candle_time) <= timedelta(hours=48)
    if now.weekday() == 6:  # Sunday
        return candle_time.weekday() == 4 and (now - candle_time) <= timedelta(hours=72)
    if now.weekday() == 0 and now.time() < time(14, 30):
        return candle_time.weekday() == 4 and (now - candle_time) <= timedelta(hours=90)
    return False


def asset_status(classification: str, deployment: dict[str, Any] | None, alert: dict[str, Any] | None, review: dict[str, Any] | None) -> str:
    if alert and alert.get("alert_type") == "scheduler_error":
        return "Scheduler Error"
    if classification == "Stale":
        return "Stale Data"
    verdict = (review or alert or {}).get("verdict")
    status = (review or {}).get("status")
    if verdict == "Setup Worth Reviewing" or status in {"Setup Worth Reviewing", "Setup Forming"}:
        return "Setup Review"
    if verdict == "Research Opportunity":
        return "Research Opportunity"
    if verdict == "Avoid":
        return "Avoid"
    if deployment is None:
        return "Not Deployed"
    return "No Setup"


def strategy_label(deployment: dict[str, Any] | None, alert: dict[str, Any] | None, review: dict[str, Any] | None) -> str:
    if deployment:
        return f"{deployment['strategy_name']}_{deployment['strategy_version']}"
    strategy_id = (review or alert or {}).get("strategy_id")
    return strategy_id or "unassigned"


def evidence_score(alert: dict[str, Any] | None) -> str:
    if not alert:
        return "0/0"
    matched = len(alert.get("matched_rules") or [])
    failed = len(alert.get("failed_rules") or [])
    return f"{matched}/{matched + failed}"


def build_review_queue(alerts: list[dict[str, Any]], reviews: list[dict[str, Any]], assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for alert in alerts:
        if alert.get("acknowledged_at") and alert.get("alert_type") not in {"scheduler_error", "stale_data_warning"}:
            continue
        items.append(
            {
                "symbol": alert["symbol"],
                "reason": alert["alert_type"].replace("_", " "),
                "severity": alert["severity"],
                "timestamp": alert["created_at"],
                "strategy": alert["strategy_id"],
                "current_verdict": alert["verdict"],
                "priority": REVIEW_PRIORITY.get(alert["alert_type"], 50),
                "action": {"label": "Open Signal Review", "href": "/paper#signal-review"},
            }
        )
    for review in reviews:
        if review.get("status") in {"Setup Worth Reviewing", "Setup Forming", "Exit Risk Worth Reviewing", "Stale Data Blocked"} and not review.get("reviewed_at") and not review.get("ignored_at"):
            items.append(
                {
                    "symbol": review["symbol"],
                    "reason": review["status"],
                    "severity": "warning" if review["status"] == "Stale Data Blocked" else "info",
                    "timestamp": review["created_at"],
                    "strategy": review["strategy_id"],
                    "current_verdict": review["verdict"],
                    "priority": 20 if review["status"] == "Stale Data Blocked" else 30,
                    "action": {"label": "Open Signal Review", "href": "/paper#signal-review"},
                }
            )
    if not items:
        for asset in assets[:8]:
            items.append(
                {
                    "symbol": asset["symbol"],
                    "reason": "Informational no-setup result",
                    "severity": "info",
                    "timestamp": asset.get("latest_scan_timestamp") or asset.get("latest_candle_timestamp"),
                    "strategy": asset["selected_strategy"],
                    "current_verdict": asset["latest_verdict"],
                    "priority": 70,
                    "action": {"label": "Open Signal Review", "href": "/paper#signal-review"},
                }
            )
    return sorted(items, key=lambda row: (row["priority"], reverse_time(row.get("timestamp"))))[:20]


def build_active_deployments(deployments: list[dict[str, Any]], alerts: list[dict[str, Any]], positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest_alert = latest_by_key(alerts, lambda row: (row["symbol"], row["timeframe"], row["strategy_id"]))
    positions_by_symbol = defaultdict(list)
    for position in positions:
        positions_by_symbol[position["symbol"]].append(position)
    rows = []
    for deployment in deployments:
        if deployment.get("status") != "active" or deployment.get("simulation_only") is not True:
            continue
        strategy_id = f"{deployment['strategy_name']}_{deployment['strategy_version']}"
        alert = latest_alert.get((deployment["symbol"], deployment["timeframe"], strategy_id)) or latest_alert.get((deployment["symbol"], deployment["timeframe"], f"{strategy_id}_007"))
        position = next((row for row in positions_by_symbol.get(deployment["symbol"], []) if Decimal(row.get("quantity") or 0) > 0), None)
        rows.append(
            {
                "id": deployment["id"],
                "asset": deployment["symbol"],
                "timeframe": deployment["timeframe"],
                "strategy": strategy_id,
                "candidate_identifier": f"{strategy_id}_007" if strategy_id == "momentum_bull_v2" else strategy_id,
                "deployment_state": deployment["status"],
                "last_scanned_candle": deployment.get("last_scanned_candle_timestamp"),
                "last_decision": deployment.get("last_signal"),
                "last_successful_scan": deployment.get("last_scan_at"),
                "latest_alert": alert,
                "paper_position": position,
                "simulated_unrealized_pnl": position.get("unrealized_pnl") if position else Decimal("0"),
                "links": {
                    "run_scan": f"/paper",
                    "signal_review": "/paper#signal-review",
                    "paper_lab": "/paper",
                    "execution_logs": "/paper#execution-logs",
                },
            }
        )
    return rows


def build_paper_account(accounts: list[dict[str, Any]], positions: list[dict[str, Any]], orders: list[dict[str, Any]], fills: list[dict[str, Any]], equity: list[dict[str, Any]]) -> dict[str, Any]:
    cash = sum_decimal(accounts, "cash_balance")
    realized = sum_decimal(accounts, "realized_pnl")
    market_value = sum((Decimal(row.get("market_value") or 0) for row in positions), Decimal("0"))
    unrealized = sum((Decimal(row.get("unrealized_pnl") or 0) for row in positions), Decimal("0"))
    stored_equity = latest_equity_by_account(equity)
    total_equity = sum((Decimal(row.get("equity") or 0) for row in stored_equity.values()), Decimal("0"))
    if not total_equity:
        total_equity = cash + market_value
    return {
        "simulation_only": True,
        "account_count": len(accounts),
        "equity": total_equity,
        "cash": cash,
        "open_positions": sum(1 for row in positions if Decimal(row.get("quantity") or 0) > 0),
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "recent_simulated_orders": orders[:12],
        "recent_simulated_fills": fills[:12],
        "recent_equity_curve": list(reversed(equity[:40])),
        "label": "All values are simulated.",
    }


def build_recent_activity(logs: list[dict[str, Any]], alerts: list[dict[str, Any]], orders: list[dict[str, Any]], fills: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for log in logs[:100]:
        items.append(
            {
                "event_type": log["event_type"],
                "symbol": symbol_from_payload(log.get("payload")),
                "description": log["message"],
                "timestamp": log["created_at"],
                "status": activity_status(log["event_type"]),
                "link": "/paper",
            }
        )
    for alert in alerts[:50]:
        items.append(
            {
                "event_type": alert["alert_type"],
                "symbol": alert["symbol"],
                "description": alert["evidence_summary"],
                "timestamp": alert["created_at"],
                "status": alert["severity"],
                "link": "/paper#evidence-alerts",
            }
        )
    for review in reviews[:25]:
        items.append(
            {
                "event_type": "signal_review",
                "symbol": review["symbol"],
                "description": review["status"],
                "timestamp": review["created_at"],
                "status": review["verdict"],
                "link": "/paper#signal-review",
            }
        )
    for order in orders[:25]:
        items.append(
            {
                "event_type": "paper_order",
                "symbol": order["symbol"],
                "description": f"Simulated {order['side']} {order['order_type']} order {order['status']}",
                "timestamp": order.get("submitted_at") or order.get("filled_at"),
                "status": order["status"],
                "link": "/paper/orders",
            }
        )
    for fill in fills[:25]:
        items.append(
            {
                "event_type": "paper_fill",
                "symbol": fill["symbol"],
                "description": f"Simulated {fill['side']} fill",
                "timestamp": fill["filled_at"],
                "status": "filled",
                "link": "/paper/orders",
            }
        )
    return sorted(items, key=lambda row: reverse_time(row.get("timestamp")))[:50]


def build_research_summary(
    assets: list[dict[str, Any]],
    deployments: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    logs: list[dict[str, Any]],
    paper: dict[str, Any],
    campaigns: dict[str, Any] | None = None,
) -> dict[str, Any]:
    campaigns = campaigns or {}
    return {
        "assets_monitored": len(assets),
        "active_deployments": sum(1 for row in deployments if row.get("status") == "active" and row.get("simulation_only") is True),
        "research_opportunities": sum(1 for row in assets if row["status"] == "Research Opportunity"),
        "setups_requiring_review": sum(1 for row in assets if row["status"] == "Setup Review"),
        "no_setup_results": sum(1 for row in assets if row["status"] == "No Setup"),
        "stale_data_blocks": sum(1 for row in assets if row["status"] == "Stale Data") + count_events(logs, STALE_EVENT_TYPES),
        "scheduler_failures": sum(1 for row in alerts if row.get("alert_type") == "scheduler_error") + count_events(logs, ERROR_EVENT_TYPES),
        "open_simulated_positions": paper["open_positions"],
        "total_paper_account_equity": paper["equity"],
        "total_unrealized_pnl": paper["unrealized_pnl"],
        "total_realized_pnl": paper["realized_pnl"],
        "active_research_campaigns": campaigns.get("active_campaigns", 0),
        "queued_research_campaigns": campaigns.get("queued_campaigns", 0),
        "research_campaign_jobs_queued": campaigns.get("queued_jobs", 0),
        "elite_candidates_promoted": campaigns.get("promoted_candidates", 0),
        "campaign_rejection_rate": campaigns.get("rejection_rate", 0.0),
    }


def build_system_health(
    now: datetime,
    scheduler: dict[str, Any] | None,
    assets: list[dict[str, Any]],
    deployments: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    logs: list[dict[str, Any]],
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    active_deployment_count = sum(1 for row in deployments if row.get("status") == "active" and row.get("simulation_only") is True)
    unack_alert_count = sum(1 for row in alerts if not row.get("acknowledged_at"))
    stale_assets = [row for row in assets if row["data_freshness"] == "Stale"]
    candle_times = [parsed for parsed in (parse_datetime(row.get("latest_candle_timestamp")) for row in assets) if parsed is not None]
    scan_times = [parsed for parsed in (parse_datetime(row.get("latest_scan_timestamp")) for row in assets) if parsed is not None]
    latest_candle = max(candle_times, default=None)
    latest_successful_scan = max(scan_times, default=None)
    if scheduler and scheduler.get("latest_error"):
        overall = "Error"
    elif errors:
        overall = "Warning"
    elif stale_assets:
        overall = "Stale"
    elif scheduler and not scheduler.get("enabled"):
        overall = "Disabled"
    else:
        overall = "Healthy"
    return {
        "overall_status": overall,
        "research_engine_status": "Healthy" if not errors else "Warning",
        "scheduler_status": scheduler_classification(scheduler),
        "scheduler_cadence": scheduler.get("cadence") if scheduler else None,
        "last_successful_scan": latest_successful_scan,
        "last_successful_scheduler_run": scheduler.get("last_run_at") if scheduler and not scheduler.get("latest_error") else None,
        "next_scheduled_scan": scheduler.get("next_run_at") if scheduler else None,
        "latest_completed_candle": latest_candle,
        "overall_data_freshness": "Stale" if stale_assets else "Healthy",
        "active_deployment_count": active_deployment_count,
        "unacknowledged_alert_count": unack_alert_count,
        "simulation_safety_status": "Simulation protected. Live routing is physically disabled.",
        "scheduler_failures": count_events(logs, ERROR_EVENT_TYPES),
        "duplicate_candle_skips": count_events(logs, DUPLICATE_EVENT_TYPES),
        "generated_at": now,
    }


def build_daily_summary(now: datetime, logs: list[dict[str, Any]], alerts: list[dict[str, Any]], assets: list[dict[str, Any]], orders: list[dict[str, Any]], positions: list[dict[str, Any]]) -> dict[str, Any]:
    today = now.date()
    todays_logs = [row for row in logs if same_day(row.get("created_at"), today)]
    todays_alerts = [row for row in alerts if same_day(row.get("created_at"), today)]
    todays_orders = [row for row in orders if same_day(row.get("submitted_at"), today)]
    return {
        "label": "Today",
        "scans_completed": count_events(todays_logs, SCAN_EVENT_TYPES),
        "assets_evaluated": len({row["symbol"] for row in assets if row.get("latest_scan_timestamp")}),
        "research_opportunities": sum(1 for row in todays_alerts if row.get("alert_type") == "entry_setup_review"),
        "no_setup_decisions": sum(1 for row in assets if row["latest_verdict"] == "No Setup"),
        "stale_data_blocks": sum(1 for row in todays_alerts if row.get("alert_type") == "stale_data_warning") + count_events(todays_logs, STALE_EVENT_TYPES),
        "scheduler_errors": sum(1 for row in todays_alerts if row.get("alert_type") == "scheduler_error") + count_events(todays_logs, ERROR_EVENT_TYPES),
        "simulated_orders": len(todays_orders),
        "open_simulated_positions": sum(1 for row in positions if Decimal(row.get("quantity") or 0) > 0),
    }


def scheduler_classification(scheduler: dict[str, Any] | None) -> str:
    if not scheduler:
        return "Warning"
    if scheduler.get("latest_error"):
        return "Error"
    if not scheduler.get("enabled") or scheduler.get("cadence") == "manual":
        return "Disabled"
    if scheduler.get("is_running"):
        return "Healthy"
    return "Healthy"


def latest_by_key(rows: list[dict[str, Any]], key_fn: Callable[[dict[str, Any]], Any]) -> dict[Any, dict[str, Any]]:
    result = {}
    for row in rows:
        key = key_fn(row)
        if key not in result:
            result[key] = row
    return result


def latest_equity_by_account(rows: list[dict[str, Any]]) -> dict[Any, dict[str, Any]]:
    result = {}
    for row in rows:
        result.setdefault(row.get("account_id"), row)
    return result


def parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def reverse_time(value: Any) -> float:
    parsed = parse_datetime(value)
    return -(parsed.timestamp() if parsed else 0)


def same_day(value: Any, day) -> bool:
    parsed = parse_datetime(value)
    return parsed is not None and parsed.date() == day


def count_events(logs: list[dict[str, Any]], event_types: set[str]) -> int:
    return sum(1 for row in logs if row.get("event_type") in event_types)


def sum_decimal(rows: list[dict[str, Any]], key: str) -> Decimal:
    return sum((Decimal(row.get(key) or 0) for row in rows), Decimal("0"))


def symbol_from_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("symbol", "asset"):
        if payload.get(key):
            return str(payload[key])
    deployment = payload.get("deployment")
    if isinstance(deployment, dict) and deployment.get("symbol"):
        return str(deployment["symbol"])
    return None


def activity_status(event_type: str) -> str:
    if event_type in ERROR_EVENT_TYPES:
        return "Error"
    if event_type in STALE_EVENT_TYPES:
        return "Stale"
    if event_type in DUPLICATE_EVENT_TYPES:
        return "Warning"
    if event_type in ORDER_EVENT_TYPES or event_type in FILL_EVENT_TYPES:
        return "Simulated"
    return "Healthy"
