from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import psycopg
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb

from app.services.mission_control import classify_candle_freshness

SETUP_ALERT_TYPES = {"entry_setup_review"}
STALE_ALERT_TYPES = {"stale_data_warning"}
SCHEDULER_ERROR_ALERT_TYPES = {"scheduler_error"}
NO_SETUP_VERDICTS = {"No Setup", "Stale Data Blocked", "Invalidated"}
SETUP_REVIEW_STATUSES = {"Setup Worth Reviewing", "Setup Forming", "In Paper Position", "Exit Risk Worth Reviewing"}
SCAN_EVENT_TYPES = {"paper_scan_completed", "paper_scan_stale_data_skipped", "paper_scan_duplicate_candle_skipped", "paper_scheduler_scan_result"}
SCHEDULER_ERROR_EVENTS = {"paper_scheduler_scan_error", "paper_scheduler_loop_error"}
STALE_EVENTS = {"paper_scan_stale_data_skipped"}


def generate_daily_research_report(conn: psycopg.Connection, report_date: date | None = None, *, regenerate: bool = True) -> dict[str, Any]:
    report_date = report_date or datetime.now(UTC).date()
    if not regenerate:
        existing = get_daily_research_report(conn, report_date)
        if existing:
            return {**existing, "created": False}
    summary = build_daily_summary(conn, report_date)
    markdown = build_markdown_report(summary)
    row = conn.execute(
        """
        INSERT INTO daily_research_reports(report_date, summary, markdown_report, generated_at, simulation_only)
        VALUES (%s, %s, %s, NOW(), TRUE)
        ON CONFLICT(report_date) DO UPDATE SET
            summary = EXCLUDED.summary,
            markdown_report = EXCLUDED.markdown_report,
            generated_at = NOW(),
            simulation_only = TRUE
        RETURNING *
        """,
        (report_date, Jsonb(jsonable_encoder(summary)), markdown),
    ).fetchone()
    conn.commit()
    return {**dict(row), "created": True}


def auto_generate_daily_report_after_scheduler_run(
    conn: psycopg.Connection,
    *,
    completed_at: datetime | None = None,
    next_run_at: datetime | None = None,
) -> dict[str, Any]:
    completed_at = completed_at or datetime.now(UTC)
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=UTC)
    else:
        completed_at = completed_at.astimezone(UTC)
    if next_run_at is None:
        return {"status": "skipped", "reason": "next scheduled run is unknown", "simulation_only": True}
    if next_run_at.tzinfo is None:
        next_run_at = next_run_at.replace(tzinfo=UTC)
    else:
        next_run_at = next_run_at.astimezone(UTC)
    if next_run_at.date() <= completed_at.date():
        return {"status": "skipped", "reason": "scheduled run was not the final run of the UTC day", "simulation_only": True}

    report = generate_daily_research_report(conn, completed_at.date(), regenerate=False)
    status = "created" if report.get("created") else "exists"
    log_daily_report_event(
        conn,
        "daily_research_report_auto_generated" if status == "created" else "daily_research_report_duplicate_prevented",
        f"Daily research report for {completed_at.date().isoformat()} {status}.",
        {"report_date": completed_at.date().isoformat(), "status": status, "simulation_only": True},
    )
    conn.commit()
    return {"status": status, "report": report, "simulation_only": True}


def list_daily_research_reports(conn: psycopg.Connection, limit: int = 30) -> list[dict[str, Any]]:
    return list(
        conn.execute(
            """
            SELECT *
            FROM daily_research_reports
            WHERE simulation_only = TRUE
            ORDER BY report_date DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    )


def get_daily_research_report(conn: psycopg.Connection, report_date: date) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM daily_research_reports
        WHERE report_date = %s
          AND simulation_only = TRUE
        """,
        (report_date,),
    ).fetchone()
    return dict(row) if row else None


def build_daily_report_analytics(conn: psycopg.Connection) -> dict[str, Any]:
    reports = list(
        conn.execute(
            """
            SELECT *
            FROM daily_research_reports
            WHERE simulation_only = TRUE
            ORDER BY report_date ASC
            """
        ).fetchall()
    )
    series = [analytics_row(row) for row in reports]
    return {
        "simulation_only": True,
        "generated_at": datetime.now(UTC),
        "series": series,
        "windows": {
            "7d": aggregate_window(series[-7:]),
            "30d": aggregate_window(series[-30:]),
            "all_time": aggregate_window(series),
        },
        "asset_comparison": compare_assets(reports),
        "strategy_comparison": compare_strategies(reports),
        "recurring_operational_failures": recurring_operational_failures(reports),
        "weekly_summary": weekly_research_summary(series[-7:], reports[-7:]),
    }


def build_daily_summary(conn: psycopg.Connection, report_date: date) -> dict[str, Any]:
    start, end = utc_day_bounds(report_date)
    logs = rows_between(conn, "execution_logs", "created_at", start, end)
    alerts = rows_between(conn, "evidence_alerts", "created_at", start, end)
    reviews = rows_between(conn, "signal_reviews", "created_at", start, end)
    orders = rows_between(conn, "paper_orders", "submitted_at", start, end)
    fills = rows_between(conn, "paper_fills", "filled_at", start, end)
    equity = equity_until(conn, end)
    freshness = data_freshness_snapshot(conn, end)
    scheduler = scheduler_status(conn)
    research_intelligence = stored_research_intelligence_snapshot(conn, start, end)

    scanned_assets = sorted(asset_symbols_from(logs, alerts, reviews))
    important_alerts = important_alert_rows(alerts)
    scheduler_errors = [*filter_by_type(logs, SCHEDULER_ERROR_EVENTS), *filter_by_alert_type(alerts, SCHEDULER_ERROR_ALERT_TYPES)]
    stale_blocks = [*filter_by_type(logs, STALE_EVENTS), *filter_by_alert_type(alerts, STALE_ALERT_TYPES)]
    setup_alerts = filter_by_alert_type(alerts, SETUP_ALERT_TYPES)
    setup_reviews = [row for row in reviews if row.get("status") in SETUP_REVIEW_STATUSES]
    no_setup_reviews = [row for row in reviews if row.get("verdict") in NO_SETUP_VERDICTS]
    scan_logs = filter_by_type(logs, SCAN_EVENT_TYPES)
    scheduler_uptime = scheduler_uptime_percent(logs, scheduler)

    latest_equity = latest_equity_by_account(equity)
    realized = sum_decimal(latest_equity.values(), "realized_pnl")
    unrealized = sum_decimal(latest_equity.values(), "unrealized_pnl")
    total_equity = sum_decimal(latest_equity.values(), "equity")

    return {
        "report_date": report_date.isoformat(),
        "period": {"start": start.isoformat(), "end": end.isoformat(), "timezone": "UTC"},
        "assets_scanned": {"count": len(scanned_assets), "symbols": scanned_assets},
        "setups_found": {"count": len(setup_alerts) + len(setup_reviews), "alerts": compact_alerts(setup_alerts), "reviews": compact_reviews(setup_reviews)},
        "no_setup_decisions": {"count": len(no_setup_reviews), "reviews": compact_reviews(no_setup_reviews[:25])},
        "stale_data_blocks": {"count": len(stale_blocks), "items": compact_events(stale_blocks[:25])},
        "scheduler_errors": {"count": len(scheduler_errors), "items": compact_events(scheduler_errors[:25])},
        "paper_orders": {"count": len(orders), "items": compact_orders(orders[:25])},
        "paper_fills": {"count": len(fills), "items": compact_fills(fills[:25])},
        "pnl": {"realized": realized, "unrealized": unrealized, "equity": total_equity, "label": "Simulated paper P&L only"},
        "data_freshness": freshness,
        "scheduler_uptime": scheduler_uptime,
        "research_intelligence": research_intelligence,
        "important_alerts": {"count": len(important_alerts), "items": compact_alerts(important_alerts[:25])},
        "scan_activity": {"count": len(scan_logs), "items": compact_events(scan_logs[:25])},
        "simulation_only": True,
        "safety": "Research-only daily report. Live routing is physically disabled.",
    }


def rows_between(conn: psycopg.Connection, table: str, timestamp_column: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    allowed = {
        ("execution_logs", "created_at"),
        ("evidence_alerts", "created_at"),
        ("signal_reviews", "created_at"),
        ("paper_orders", "submitted_at"),
        ("paper_fills", "filled_at"),
    }
    if (table, timestamp_column) not in allowed:
        raise ValueError("unsupported daily report table")
    return list(
        conn.execute(
            f"""
            SELECT *
            FROM {table}
            WHERE simulation_only = TRUE
              AND {timestamp_column} >= %s
              AND {timestamp_column} < %s
            ORDER BY {timestamp_column} DESC
            """,
            (start, end),
        ).fetchall()
    )


def equity_until(conn: psycopg.Connection, end: datetime) -> list[dict[str, Any]]:
    return list(
        conn.execute(
            """
            SELECT DISTINCT ON (account_id) *
            FROM paper_equity_curve
            WHERE simulation_only = TRUE
              AND timestamp < %s
            ORDER BY account_id, timestamp DESC
            """,
            (end,),
        ).fetchall()
    )


def scheduler_status(conn: psycopg.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM paper_scan_scheduler WHERE id = TRUE").fetchone()
    return dict(row) if row else None


def data_freshness_snapshot(conn: psycopg.Connection, end: datetime) -> dict[str, Any]:
    rows = conn.execute(
        """
        WITH monitored AS (
            SELECT symbol, timeframe FROM strategy_deployments WHERE simulation_only = TRUE
            UNION
            SELECT symbol, timeframe FROM evidence_alerts WHERE simulation_only = TRUE AND symbol <> 'SYSTEM'
            UNION
            SELECT symbol, timeframe FROM signal_reviews WHERE simulation_only = TRUE
            UNION
            SELECT symbol, CASE WHEN asset_class ILIKE '%%equity%%' THEN '1d' ELSE '4h' END AS timeframe FROM symbols WHERE is_active = TRUE
        ),
        latest AS (
            SELECT DISTINCT ON (c.symbol, c.timeframe) c.symbol, c.timeframe, c.timestamp
            FROM candles c
            JOIN monitored m ON m.symbol = c.symbol AND m.timeframe = c.timeframe
            WHERE c.timestamp < %s
            ORDER BY c.symbol, c.timeframe, c.timestamp DESC
        )
        SELECT m.symbol, m.timeframe, s.asset_class, latest.timestamp
        FROM monitored m
        LEFT JOIN latest ON latest.symbol = m.symbol AND latest.timeframe = m.timeframe
        LEFT JOIN symbols s ON s.symbol = m.symbol
        ORDER BY m.symbol, m.timeframe
        """,
        (end,),
    ).fetchall()
    assets = []
    counts = {"Healthy": 0, "Warning": 0, "Stale": 0}
    for row in rows:
        freshness = classify_candle_freshness(row.get("timestamp"), row["timeframe"], row.get("asset_class"), end)
        counts[freshness["classification"]] = counts.get(freshness["classification"], 0) + 1
        assets.append(
            {
                "symbol": row["symbol"],
                "timeframe": row["timeframe"],
                "asset_class": row.get("asset_class"),
                "latest_candle_timestamp": row.get("timestamp"),
                "classification": freshness["classification"],
                "detail": freshness["detail"],
                "age_hours": freshness["age_hours"],
            }
        )
    return {"counts": counts, "assets": assets}


def scheduler_uptime_percent(logs: list[dict[str, Any]], scheduler: dict[str, Any] | None) -> Decimal | None:
    if scheduler and (not scheduler.get("enabled") or scheduler.get("cadence") == "manual"):
        return None
    completed = sum(1 for row in logs if row.get("event_type") in {"paper_scheduler_scan_result", "paper_scheduler_run_finished", "paper_scan_completed"})
    errors = sum(1 for row in logs if row.get("event_type") in SCHEDULER_ERROR_EVENTS)
    total = completed + errors
    if total == 0:
        return Decimal("100") if scheduler and not scheduler.get("latest_error") else Decimal("0")
    return (Decimal(completed) / Decimal(total) * Decimal("100")).quantize(Decimal("0.01"))


def build_markdown_report(summary: dict[str, Any]) -> str:
    alerts = summary["important_alerts"]["items"]
    freshness_counts = summary["data_freshness"]["counts"]
    return "\n".join(
        [
            f"# Daily Research Report — {summary['report_date']}",
            "",
            "Research-only daily operations summary. No live trading, broker routing, leverage, margin, shorting, or real-money execution.",
            "",
            "## Summary",
            f"- Assets scanned: {summary['assets_scanned']['count']} ({', '.join(summary['assets_scanned']['symbols']) or 'none'})",
            f"- Setups found: {summary['setups_found']['count']}",
            f"- No-setup decisions: {summary['no_setup_decisions']['count']}",
            f"- Stale-data blocks: {summary['stale_data_blocks']['count']}",
            f"- Scheduler errors: {summary['scheduler_errors']['count']}",
            f"- Paper orders: {summary['paper_orders']['count']}",
            f"- Paper fills: {summary['paper_fills']['count']}",
            f"- Simulated realized P&L: {summary['pnl']['realized']}",
            f"- Simulated unrealized P&L: {summary['pnl']['unrealized']}",
            f"- Scheduler uptime: {format_uptime(summary['scheduler_uptime'])}",
            f"- Top research candidate: {summary.get('research_intelligence', {}).get('top_ranked_candidate') or 'none'}",
            f"- Highest review priority: {summary.get('research_intelligence', {}).get('highest_review_priority') or 'none'}",
            f"- Strongest strategy: {summary.get('research_intelligence', {}).get('strongest_strategy') or 'none'}",
            "",
            "## Data Freshness",
            f"- Healthy: {freshness_counts.get('Healthy', 0)}",
            f"- Warning: {freshness_counts.get('Warning', 0)}",
            f"- Stale: {freshness_counts.get('Stale', 0)}",
            "",
            "## Important Alerts",
            *(f"- {alert['symbol']} / {alert['severity']} / {alert['alert_type']}: {alert['verdict']}" for alert in alerts),
        ]
    )


def analytics_row(report: dict[str, Any]) -> dict[str, Any]:
    summary = report.get("summary") or {}
    freshness = summary.get("data_freshness", {}).get("counts", {})
    return {
        "report_date": str(report.get("report_date")),
        "scheduler_uptime": numeric(summary.get("scheduler_uptime")),
        "stale_data_blocks": int(summary.get("stale_data_blocks", {}).get("count") or 0),
        "setups_found": int(summary.get("setups_found", {}).get("count") or 0),
        "no_setup_decisions": int(summary.get("no_setup_decisions", {}).get("count") or 0),
        "realized_pnl": numeric(summary.get("pnl", {}).get("realized")),
        "unrealized_pnl": numeric(summary.get("pnl", {}).get("unrealized")),
        "equity": numeric(summary.get("pnl", {}).get("equity")),
        "scheduler_errors": int(summary.get("scheduler_errors", {}).get("count") or 0),
        "paper_orders": int(summary.get("paper_orders", {}).get("count") or 0),
        "paper_fills": int(summary.get("paper_fills", {}).get("count") or 0),
        "important_alerts": int(summary.get("important_alerts", {}).get("count") or 0),
        "top_research_candidate": summary.get("research_intelligence", {}).get("top_ranked_candidate"),
        "fresh_assets": int(freshness.get("Healthy") or 0),
        "warning_assets": int(freshness.get("Warning") or 0),
        "stale_assets": int(freshness.get("Stale") or 0),
    }


def aggregate_window(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "report_count": 0,
            "avg_scheduler_uptime": None,
            "stale_data_blocks": 0,
            "setups_found": 0,
            "no_setup_decisions": 0,
            "scheduler_errors": 0,
            "paper_orders": 0,
            "paper_fills": 0,
            "realized_pnl_change": 0,
            "unrealized_pnl_change": 0,
        }
    uptime_values = [row["scheduler_uptime"] for row in rows if row["scheduler_uptime"] is not None]
    return {
        "report_count": len(rows),
        "avg_scheduler_uptime": round(sum(uptime_values) / len(uptime_values), 2) if uptime_values else None,
        "stale_data_blocks": sum(row["stale_data_blocks"] for row in rows),
        "setups_found": sum(row["setups_found"] for row in rows),
        "no_setup_decisions": sum(row["no_setup_decisions"] for row in rows),
        "scheduler_errors": sum(row["scheduler_errors"] for row in rows),
        "paper_orders": sum(row["paper_orders"] for row in rows),
        "paper_fills": sum(row["paper_fills"] for row in rows),
        "realized_pnl_change": rows[-1]["realized_pnl"] - rows[0]["realized_pnl"],
        "unrealized_pnl_change": rows[-1]["unrealized_pnl"] - rows[0]["unrealized_pnl"],
    }


def compare_assets(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    assets: dict[str, dict[str, Any]] = {}
    for report in reports:
        summary = report.get("summary") or {}
        for symbol in summary.get("assets_scanned", {}).get("symbols") or []:
            row = assets.setdefault(symbol, {"symbol": symbol, "scanned_days": 0, "setups": 0, "stale_blocks": 0, "important_alerts": 0})
            row["scanned_days"] += 1
        for item in summary.get("setups_found", {}).get("alerts", []) + summary.get("setups_found", {}).get("reviews", []):
            symbol = item.get("symbol")
            if symbol:
                assets.setdefault(symbol, {"symbol": symbol, "scanned_days": 0, "setups": 0, "stale_blocks": 0, "important_alerts": 0})["setups"] += 1
        for item in summary.get("stale_data_blocks", {}).get("items", []):
            symbol = item.get("symbol")
            if symbol:
                assets.setdefault(symbol, {"symbol": symbol, "scanned_days": 0, "setups": 0, "stale_blocks": 0, "important_alerts": 0})["stale_blocks"] += 1
        for item in summary.get("important_alerts", {}).get("items", []):
            symbol = item.get("symbol")
            if symbol and symbol != "SYSTEM":
                assets.setdefault(symbol, {"symbol": symbol, "scanned_days": 0, "setups": 0, "stale_blocks": 0, "important_alerts": 0})["important_alerts"] += 1
    return sorted(assets.values(), key=lambda row: (-row["stale_blocks"], -row["setups"], row["symbol"]))


def compare_strategies(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    strategies: dict[str, dict[str, Any]] = {}
    for report in reports:
        summary = report.get("summary") or {}
        for item in summary.get("setups_found", {}).get("alerts", []) + summary.get("setups_found", {}).get("reviews", []):
            strategy = item.get("strategy_id") or "unknown"
            row = strategies.setdefault(strategy, {"strategy": strategy, "setups": 0, "no_setup": 0, "important_alerts": 0})
            row["setups"] += 1
        for item in summary.get("no_setup_decisions", {}).get("reviews", []):
            strategy = item.get("strategy_id") or "unknown"
            row = strategies.setdefault(strategy, {"strategy": strategy, "setups": 0, "no_setup": 0, "important_alerts": 0})
            row["no_setup"] += 1
        for item in summary.get("important_alerts", {}).get("items", []):
            strategy = item.get("strategy_id") or "unknown"
            row = strategies.setdefault(strategy, {"strategy": strategy, "setups": 0, "no_setup": 0, "important_alerts": 0})
            row["important_alerts"] += 1
    return sorted(strategies.values(), key=lambda row: (-row["important_alerts"], -row["setups"], row["strategy"]))


def recurring_operational_failures(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures: dict[tuple[str, str, str], dict[str, Any]] = {}
    for report in reports:
        summary = report.get("summary") or {}
        report_date = str(report.get("report_date"))
        for item in summary.get("stale_data_blocks", {}).get("items", []) + summary.get("scheduler_errors", {}).get("items", []):
            symbol = item.get("symbol") or "SYSTEM"
            event_type = item.get("event_type") or "unknown"
            message = (item.get("message") or "").strip()[:160]
            key = (symbol, event_type, message)
            row = failures.setdefault(key, {"symbol": symbol, "event_type": event_type, "message": message, "count": 0, "dates": []})
            row["count"] += 1
            if report_date not in row["dates"]:
                row["dates"].append(report_date)
    return sorted(failures.values(), key=lambda row: (-row["count"], row["symbol"], row["event_type"]))[:20]


def weekly_research_summary(series: list[dict[str, Any]], reports: list[dict[str, Any]]) -> dict[str, Any]:
    window = aggregate_window(series)
    failures = recurring_operational_failures(reports)[:5]
    assets = compare_assets(reports)[:5]
    strategies = compare_strategies(reports)[:5]
    return {
        "window": "last_7_reports",
        "summary": window,
        "top_assets": assets,
        "top_strategies": strategies,
        "research_intelligence": [report.get("summary", {}).get("research_intelligence", {}) for report in reports if report.get("summary", {}).get("research_intelligence")],
        "recurring_failures": failures,
        "narrative": weekly_narrative(window, failures),
        "simulation_only": True,
    }


def weekly_narrative(window: dict[str, Any], failures: list[dict[str, Any]]) -> str:
    if not window["report_count"]:
        return "No stored daily reports are available for the weekly summary."
    parts = [
        f"{window['report_count']} stored daily report(s) reviewed.",
        f"{window['setups_found']} setup review item(s), {window['no_setup_decisions']} no-setup decision(s), and {window['stale_data_blocks']} stale-data block(s) were recorded.",
    ]
    if failures:
        parts.append(f"Most recurring operational issue: {failures[0]['event_type']} on {failures[0]['symbol']} ({failures[0]['count']} occurrence(s)).")
    return " ".join(parts)


def numeric(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def log_daily_report_event(conn: psycopg.Connection, event_type: str, message: str, payload: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO execution_logs(account_id, deployment_id, order_id, event_type, message, payload, simulation_only)
        VALUES (NULL, NULL, NULL, %s, %s, %s, TRUE)
        """,
        (event_type, message, Jsonb(jsonable_encoder(payload))),
    )


def utc_day_bounds(report_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(report_date, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def asset_symbols_from(*groups: list[dict[str, Any]]) -> set[str]:
    symbols: set[str] = set()
    for rows in groups:
        for row in rows:
            symbol = symbol_from_row(row)
            if symbol and symbol != "SYSTEM":
                symbols.add(symbol)
    return symbols


def symbol_from_row(row: dict[str, Any]) -> str | None:
    if row.get("symbol"):
        return row["symbol"]
    payload = row.get("payload")
    if isinstance(payload, dict):
        if payload.get("symbol"):
            return str(payload["symbol"])
        deployment = payload.get("deployment")
        if isinstance(deployment, dict) and deployment.get("symbol"):
            return str(deployment["symbol"])
    return None


def filter_by_type(rows: list[dict[str, Any]], event_types: set[str]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("event_type") in event_types]


def filter_by_alert_type(rows: list[dict[str, Any]], alert_types: set[str]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("alert_type") in alert_types]


def important_alert_rows(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in alerts
        if row.get("severity") in {"warning", "critical"} or row.get("alert_type") in {"entry_setup_review", "exit_risk_review", "scheduler_error", "stale_data_warning"}
    ]


def compact_alerts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": row.get("id"),
            "symbol": row.get("symbol"),
            "timeframe": row.get("timeframe"),
            "strategy_id": row.get("strategy_id"),
            "alert_type": row.get("alert_type"),
            "severity": row.get("severity"),
            "verdict": row.get("verdict"),
            "created_at": row.get("created_at"),
            "evidence_summary": row.get("evidence_summary"),
        }
        for row in rows
    ]


def compact_reviews(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": row.get("id"),
            "symbol": row.get("symbol"),
            "timeframe": row.get("timeframe"),
            "strategy_id": row.get("strategy_id"),
            "status": row.get("status"),
            "verdict": row.get("verdict"),
            "created_at": row.get("created_at"),
        }
        for row in rows
    ]


def compact_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": row.get("id"),
            "symbol": symbol_from_row(row),
            "event_type": row.get("event_type") or row.get("alert_type"),
            "message": row.get("message") or row.get("evidence_summary"),
            "created_at": row.get("created_at"),
            "severity": row.get("severity"),
        }
        for row in rows
    ]


def compact_orders(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": row.get("id"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "order_type": row.get("order_type"),
            "status": row.get("status"),
            "submitted_at": row.get("submitted_at"),
            "simulation_only": row.get("simulation_only"),
        }
        for row in rows
    ]


def compact_fills(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": row.get("id"),
            "order_id": row.get("order_id"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "quantity": row.get("quantity"),
            "fill_price": row.get("fill_price"),
            "filled_at": row.get("filled_at"),
            "simulation_only": row.get("simulation_only"),
        }
        for row in rows
    ]


def latest_equity_by_account(rows: list[dict[str, Any]]) -> dict[Any, dict[str, Any]]:
    result = {}
    for row in rows:
        result[row.get("account_id")] = row
    return result


def sum_decimal(rows, key: str) -> Decimal:
    return sum((Decimal(row.get(key) or 0) for row in rows), Decimal("0"))


def format_uptime(value: Any) -> str:
    if value is None:
        return "manual or disabled"
    return f"{value}%"


def stored_research_intelligence_snapshot(conn: psycopg.Connection, start: datetime, end: datetime) -> dict[str, Any]:
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM research_ranking_snapshots
            WHERE created_at >= %s
              AND created_at < %s
            ORDER BY rank ASC, created_at DESC
            """,
            (start, end),
        ).fetchall()
    except Exception:
        return {
            "available": False,
            "reason": "No stored research ranking snapshots exist for this report period.",
            "top_ranked_candidate": None,
            "highest_review_priority": None,
            "strongest_strategy": None,
            "score_changes": [],
            "entries_leaving_top_ranks": [],
            "new_blocking_issues": [],
            "concentration_changes": [],
        }
    if not rows:
        return {
            "available": False,
            "reason": "No stored research ranking snapshots exist for this report period.",
            "top_ranked_candidate": None,
            "highest_review_priority": None,
            "strongest_strategy": None,
            "score_changes": [],
            "entries_leaving_top_ranks": [],
            "new_blocking_issues": [],
            "concentration_changes": [],
        }
    top = rows[0]
    strategies = Counter(strategy_from_candidate(row.get("candidate_id")) for row in rows)
    return {
        "available": True,
        "snapshot_count": len(rows),
        "top_ranked_candidate": top.get("candidate_id"),
        "top_ranked_score": top.get("research_score"),
        "highest_review_priority": next((row.get("candidate_id") for row in rows if row.get("review_priority") == "Review first"), top.get("candidate_id")),
        "strongest_strategy": strategies.most_common(1)[0][0] if strategies else None,
        "score_changes": [],
        "candidates_entering_top_ranks": [row.get("candidate_id") for row in rows[:5]],
        "candidates_leaving_top_ranks": [],
        "new_blocking_issues": [],
        "concentration_changes": [],
        "calculation_version": top.get("calculation_version"),
    }


def strategy_from_candidate(candidate_id: Any) -> str:
    text = str(candidate_id or "unknown")
    parts = text.split("_")
    return "_".join(parts[:2]) if len(parts) >= 2 else text
