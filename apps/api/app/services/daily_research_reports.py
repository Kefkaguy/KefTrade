from __future__ import annotations

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


def generate_daily_research_report(conn: psycopg.Connection, report_date: date | None = None) -> dict[str, Any]:
    report_date = report_date or datetime.now(UTC).date()
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
    return dict(row)


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
