from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

import psycopg

from app.services.external_execution import feature_flags
from app.settings import settings


def broker_status(conn: psycopg.Connection) -> dict[str, Any]:
    account = latest_account(conn)
    sync = latest_row(conn, "broker_sync_runs", "completed_at")
    reconciliation = latest_row(conn, "broker_reconciliation_runs", "completed_at")
    adapter = latest_row(conn, "broker_adapter_releases", "created_at")
    active_halts = rows(conn, "SELECT * FROM execution_halts WHERE cleared_at IS NULL ORDER BY severity DESC, last_seen_at DESC LIMIT 100")
    deployments = rows(conn, "SELECT * FROM external_paper_deployments ORDER BY updated_at DESC")
    epochs = rows(conn, "SELECT * FROM external_execution_epochs ORDER BY activated_at DESC LIMIT 100")
    shadows = rows(conn, "SELECT * FROM shadow_executions ORDER BY created_at DESC LIMIT 50")
    daily_summary = latest_row(conn, "broker_daily_summaries", "updated_at")
    elite_activity = elite_observability(conn)
    return {
        "provider": "alpaca",
        "environment": "paper",
        "feature_flags": feature_flags(),
        "execution_enabled": bool(settings.broker_order_submission_enabled and settings.external_paper_execution_enabled and any(row.get("state") == "enabled_execution" for row in deployments)),
        "order_submission_implemented": True,
        "account": account,
        "latest_sync": sync,
        "latest_reconciliation": reconciliation,
        "adapter": adapter,
        "active_halts": active_halts,
        "deployments": deployments,
        "epochs": epochs,
        "shadow_executions": shadows,
        "daily_summary": daily_summary,
        "elite_activity": elite_activity,
        "opportunity_coverage": opportunity_coverage(elite_activity),
        "generated_at": datetime.now(UTC),
    }


def opportunity_coverage(elites: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe deployment diversity without changing any trading authority."""
    active = [row for row in elites if row.get("state") in {"enabled_observe_only", "enabled_execution"}]
    symbols = Counter(str(row.get("symbol") or "unknown") for row in active)
    timeframes = Counter(str(row.get("timeframe") or "unknown") for row in active)
    active_count = len(active)
    dominant_symbol, dominant_count = symbols.most_common(1)[0] if symbols else (None, 0)
    dominant_share = dominant_count / active_count if active_count else 0.0
    evaluations = sum(int(row.get("evaluations_today") or 0) for row in active)
    setups = sum(int(row.get("setups_today") or 0) for row in active)
    concentrated = active_count > 1 and (len(symbols) < 3 or dominant_share >= 0.6)
    classification = "concentrated_long_only" if concentrated else "long_only"
    return {
        "classification": classification,
        "active_elites": active_count,
        "unique_symbols": len(symbols),
        "unique_timeframes": len(timeframes),
        "dominant_symbol": dominant_symbol,
        "dominant_symbol_share": round(dominant_share, 4),
        "symbol_distribution": dict(symbols),
        "timeframe_distribution": dict(timeframes),
        "setup_frequency_today": round(setups / evaluations, 6) if evaluations else 0.0,
        "long_only": True,
        "external_short_execution_enabled": False,
        "research_recommendations": [
            {
                "code": "INDEPENDENT_SYMBOLS",
                "status": "research_required" if concentrated else "monitor",
                "detail": "Validate elites on additional, less-correlated symbols before promotion.",
            },
            {
                "code": "MEAN_REVERSION_DEFENSIVE",
                "status": "research_required",
                "detail": "Research mean-reversion and defensive candidates separately from the frozen elites.",
            },
            {
                "code": "BEARISH_SHORT",
                "status": "research_required",
                "detail": "Validate short candidates in simulation before adding any external execution path.",
            },
        ],
    }


def elite_observability(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Build a bounded read model for every external elite deployment.

    Forward observation and historical replay are deliberately kept separate:
    observe-only decisions are not presented as earned P&L.
    """
    deployments = rows(
        conn,
        """
        SELECT
            x.id,
            x.internal_deployment_id,
            x.candidate_id,
            x.symbol,
            x.timeframe,
            x.state,
            x.updated_at,
            e.research_score,
            COALESCE(today_evaluations.evaluations, 0) AS evaluations_today,
            COALESCE(today_evaluations.setups, 0) AS setups_today,
            COALESCE(today_evaluations.avoids, 0) AS avoids_today,
            latest_evaluation.signal_type AS latest_signal,
            latest_evaluation.completed_bar_timestamp AS latest_bar,
            latest_evaluation.created_at AS latest_evaluation_at,
            latest_evaluation.gates AS latest_gates,
            COALESCE(today_shadows.shadow_decisions, 0) AS shadow_decisions_today,
            COALESCE(today_shadows.would_submit, 0) AS would_submit_today,
            latest_shadow.would_submit AS latest_would_submit,
            latest_shadow.rejection_reasons AS latest_rejection_reasons,
            latest_shadow.created_at AS latest_shadow_at,
            COALESCE(today_attempts.execution_attempts, 0) AS execution_attempts_today,
            COALESCE(today_attempts.submitted_attempts, 0) AS submitted_attempts_today
        FROM external_paper_deployments x
        JOIN elite_research_candidates e ON e.id = x.elite_candidate_id
        LEFT JOIN LATERAL (
            SELECT
                COUNT(*) AS evaluations,
                COUNT(*) FILTER (WHERE se.signal_type = 'setup') AS setups,
                COUNT(*) FILTER (WHERE se.signal_type = 'avoid') AS avoids
            FROM strategy_evaluations se
            WHERE se.external_deployment_id = x.id
              AND se.created_at >= ((NOW() AT TIME ZONE 'America/New_York')::date AT TIME ZONE 'America/New_York')
        ) today_evaluations ON TRUE
        LEFT JOIN LATERAL (
            SELECT se.signal_type, se.completed_bar_timestamp, se.created_at, se.gates
            FROM strategy_evaluations se
            WHERE se.external_deployment_id = x.id
            ORDER BY se.created_at DESC, se.id DESC
            LIMIT 1
        ) latest_evaluation ON TRUE
        LEFT JOIN LATERAL (
            SELECT
                COUNT(*) AS shadow_decisions,
                COUNT(*) FILTER (WHERE sx.would_submit) AS would_submit
            FROM shadow_executions sx
            WHERE sx.external_deployment_id = x.id
              AND sx.created_at >= ((NOW() AT TIME ZONE 'America/New_York')::date AT TIME ZONE 'America/New_York')
        ) today_shadows ON TRUE
        LEFT JOIN LATERAL (
            SELECT sx.would_submit, sx.rejection_reasons, sx.created_at
            FROM shadow_executions sx
            WHERE sx.external_deployment_id = x.id
            ORDER BY sx.created_at DESC, sx.id DESC
            LIMIT 1
        ) latest_shadow ON TRUE
        LEFT JOIN LATERAL (
            SELECT
                COUNT(*) AS execution_attempts,
                COUNT(*) FILTER (WHERE bea.status IN ('submitted', 'accepted', 'reconciled')) AS submitted_attempts
            FROM broker_execution_attempts bea
            WHERE bea.external_deployment_id = x.id
              AND bea.created_at >= ((NOW() AT TIME ZONE 'America/New_York')::date AT TIME ZONE 'America/New_York')
        ) today_attempts ON TRUE
        ORDER BY e.research_score DESC, x.id
        """,
    )
    replay = conn.execute(
        """
        SELECT id, completed_at, outcome_summary
        FROM elite_shadow_replay_runs
        WHERE status = 'complete' AND outcome_summary <> '{}'::jsonb
        ORDER BY completed_at DESC NULLS LAST, id DESC
        LIMIT 1
        """
    ).fetchone()
    replay_summary = dict((replay or {}).get("outcome_summary") or {})
    replay_by_deployment = dict(replay_summary.get("by_deployment") or {})
    for deployment in deployments:
        replay_metrics = dict(replay_by_deployment.get(str(deployment["id"])) or {})
        submitted = int(deployment.get("submitted_attempts_today") or 0)
        observe_only = deployment.get("state") != "enabled_execution"
        deployment["today_performance"] = {
            "realized_pnl": 0.0 if observe_only else None,
            "unrealized_pnl": 0.0 if observe_only else None,
            "submitted_orders": submitted,
            "attribution_status": "observation_only_no_paper_trades" if observe_only else "awaiting_broker_lifecycle_attribution",
            "simulation_only": True,
        }
        deployment["historical_replay"] = {
            "run_id": replay.get("id") if replay else None,
            "completed_at": replay.get("completed_at") if replay else None,
            **replay_metrics,
        }
    return deployments


def broker_account(conn: psycopg.Connection) -> dict[str, Any]:
    account = latest_account(conn)
    state = dict(conn.execute("SELECT * FROM broker_account_state ORDER BY updated_at DESC LIMIT 1").fetchone() or {})
    return {"account": account, "state": state, "allocated_capital": settings.broker_allocated_capital, "buying_power_used_for_sizing": False, "environment": "paper"}


def broker_clock(conn: psycopg.Connection) -> dict[str, Any]:
    return dict(conn.execute("SELECT * FROM broker_clock_state ORDER BY updated_at DESC LIMIT 1").fetchone() or {})


def broker_orders(conn: psycopg.Connection, limit: int = 100) -> list[dict[str, Any]]:
    return rows(conn, "SELECT * FROM broker_orders ORDER BY updated_at DESC LIMIT %s", (limit,))


def broker_positions(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return rows(conn, "SELECT * FROM broker_positions WHERE quantity > 0 ORDER BY symbol")


def broker_reconciliation(conn: psycopg.Connection) -> dict[str, Any]:
    run = latest_row(conn, "broker_reconciliation_runs", "completed_at")
    findings = rows(conn, "SELECT * FROM broker_reconciliation_findings WHERE resolved_at IS NULL ORDER BY severity DESC, created_at DESC LIMIT 200")
    return {"latest_run": run, "unresolved_findings": findings, "clean": bool(run and run.get("status") == "clean" and not findings)}


def execution_readiness(conn: psycopg.Connection) -> dict[str, Any]:
    status = broker_status(conn)
    deployments = status["deployments"]
    return {
        "execution_enabled": bool(status["execution_enabled"]),
        "highest_reachable_state": "enabled_execution" if settings.broker_order_submission_enabled and settings.external_paper_execution_enabled else "enabled_observe_only",
        "eligible_deployments": [row for row in deployments if row.get("state") in {"enabled_observe_only", "enabled_execution"}],
        "blocked_deployments": [row for row in deployments if row.get("state") in {"readiness_blocked", "risk_halted", "reconciliation_halted", "manually_halted", "invalidated"}],
        "active_halts": status["active_halts"],
        "feature_flags": status["feature_flags"],
        "submission_proof": {"broker_order_submission_enabled": settings.broker_order_submission_enabled, "external_paper_execution_enabled": settings.external_paper_execution_enabled, "paper_domain_required": True, "explicit_cli_approval_required": True, "live_money_supported": False},
    }


def execution_attempts(conn: psycopg.Connection, limit: int = 100) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("SELECT * FROM broker_execution_attempts ORDER BY created_at DESC, id DESC LIMIT %s", (limit,)).fetchall()]


def latest_account(conn: psycopg.Connection) -> dict[str, Any]:
    return dict(conn.execute("SELECT * FROM broker_accounts ORDER BY last_successful_sync_at DESC NULLS LAST, created_at DESC LIMIT 1").fetchone() or {})


def latest_row(conn: psycopg.Connection, table: str, timestamp_column: str) -> dict[str, Any]:
    return dict(conn.execute(f"SELECT * FROM {table} ORDER BY {timestamp_column} DESC NULLS LAST LIMIT 1").fetchone() or {})


def rows(conn: psycopg.Connection, query: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, params or ()).fetchall()]
