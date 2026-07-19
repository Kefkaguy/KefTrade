from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from app.settings import settings

OPEN_ORDER_STATUSES = {"new", "accepted", "pending_new", "partially_filled", "pending_cancel", "accepted_for_bidding", "held", "calculated"}


def reconcile_broker_snapshot(conn: psycopg.Connection, sync_run_id: int, *, trace_id: UUID | None = None) -> dict[str, Any]:
    if not settings.broker_reconciliation_enabled:
        return {"status": "disabled", "feature": "BROKER_RECONCILIATION_ENABLED", "paper_only": True}
    sync = conn.execute("SELECT * FROM broker_sync_runs WHERE id = %s", (sync_run_id,)).fetchone()
    if not sync or sync["status"] != "complete" or not sync.get("broker_account_id"):
        raise ValueError("reconciliation requires one complete persisted broker sync")
    trace_id = trace_id or uuid4()
    run = conn.execute(
        "INSERT INTO broker_reconciliation_runs(broker_account_id, sync_run_id, trace_id, status) VALUES (%s,%s,%s,'running') RETURNING *",
        (sync["broker_account_id"], sync_run_id, trace_id),
    ).fetchone()
    findings = build_findings(conn, int(sync["broker_account_id"]), sync_run_id)
    for finding in findings:
        conn.execute(
            """
            INSERT INTO broker_reconciliation_findings(reconciliation_run_id, trace_id, finding_key, finding_type, severity, scope_type, scope_key, details)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (run["id"], trace_id, finding["finding_key"], finding["finding_type"], finding["severity"], finding["scope_type"], finding["scope_key"], Jsonb(finding["details"])),
        )
        if finding["severity"] == "critical":
            upsert_halt(conn, trace_id, finding["scope_type"], finding["scope_key"], finding["finding_type"], finding["details"])
    status = "findings" if findings else "clean"
    summary = {"finding_count": len(findings), "critical_count": sum(row["severity"] == "critical" for row in findings), "source_sync_run_id": sync_run_id}
    completed = conn.execute("UPDATE broker_reconciliation_runs SET status=%s, summary=%s, completed_at=NOW() WHERE id=%s RETURNING *", (status, Jsonb(summary), run["id"])).fetchone()
    conn.commit()
    return {"status": status, "run": dict(completed), "findings": findings, "trace_id": str(trace_id), "paper_only": True}


def build_findings(conn: psycopg.Connection, broker_account_id: int, sync_run_id: int) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    account = conn.execute("SELECT * FROM broker_account_state WHERE broker_account_id=%s AND sync_run_id=%s", (broker_account_id, sync_run_id)).fetchone()
    clock = conn.execute("SELECT * FROM broker_clock_state WHERE broker_account_id=%s AND sync_run_id=%s", (broker_account_id, sync_run_id)).fetchone()
    if not account or not clock:
        findings.append(finding("incomplete_latest_state", "incomplete_snapshot", "critical", "account", str(broker_account_id), {"account_present": bool(account), "clock_present": bool(clock)}))
        return findings
    if account["account_blocked"] or account["trading_blocked"] or account["trade_suspended_by_user"]:
        findings.append(finding("broker_account_blocked", "account_blocked", "critical", "account", str(broker_account_id), {"status": account["status"]}))
    positions = conn.execute("SELECT * FROM broker_positions WHERE broker_account_id=%s AND sync_run_id=%s AND quantity > 0", (broker_account_id, sync_run_id)).fetchall()
    for position in positions:
        findings.append(finding(f"unexpected_position:{position['symbol']}", "unexpected_position", "critical", "account", str(broker_account_id), {"symbol": position["symbol"], "quantity": str(position["quantity"]), "market_value": str(position["market_value"])}))
    orders = conn.execute("SELECT * FROM broker_orders WHERE broker_account_id=%s AND sync_run_id=%s", (broker_account_id, sync_run_id)).fetchall()
    for order in orders:
        if str(order["status"]) in OPEN_ORDER_STATUSES:
            findings.append(finding(f"unexpected_open_order:{order['broker_order_id']}", "unexpected_open_order", "critical", "account", str(broker_account_id), {"broker_order_id": order["broker_order_id"], "client_order_id": order["client_order_id"], "symbol": order["symbol"], "status": order["status"]}))
    return findings


def finding(key: str, finding_type: str, severity: str, scope_type: str, scope_key: str, details: dict[str, Any]) -> dict[str, Any]:
    return {"finding_key": key, "finding_type": finding_type, "severity": severity, "scope_type": scope_type, "scope_key": scope_key, "details": details}


def upsert_halt(conn: psycopg.Connection, trace_id: UUID, scope_type: str, scope_key: str, reason_code: str, evidence: dict[str, Any]) -> dict[str, Any]:
    current = conn.execute(
        "SELECT * FROM execution_halts WHERE scope_type=%s AND scope_key=%s AND reason_code=%s AND cleared_at IS NULL FOR UPDATE",
        (scope_type, scope_key, reason_code),
    ).fetchone()
    if current:
        row = conn.execute("UPDATE execution_halts SET trace_id=%s, evidence=%s, occurrence_count=occurrence_count+1, last_seen_at=NOW() WHERE id=%s RETURNING *", (trace_id, Jsonb(evidence), current["id"])).fetchone()
    else:
        row = conn.execute("INSERT INTO execution_halts(trace_id, scope_type, scope_key, reason_code, severity, evidence) VALUES (%s,%s,%s,%s,'critical',%s) RETURNING *", (trace_id, scope_type, scope_key, reason_code, Jsonb(evidence))).fetchone()
    close_affected_epochs(conn, scope_type, scope_key, reason_code)
    return dict(row)


def close_affected_epochs(conn: psycopg.Connection, scope_type: str, scope_key: str, reason_code: str) -> None:
    if scope_type == "account":
        deployment_ids = [row["id"] for row in conn.execute("SELECT id FROM external_paper_deployments WHERE broker_account_id=%s", (int(scope_key),)).fetchall()]
    elif scope_type == "deployment":
        deployment_ids = [int(scope_key)]
    elif scope_type == "asset":
        deployment_ids = [row["id"] for row in conn.execute("SELECT id FROM external_paper_deployments WHERE symbol=%s", (scope_key,)).fetchall()]
    else:
        deployment_ids = [row["id"] for row in conn.execute("SELECT id FROM external_paper_deployments").fetchall()]
    if not deployment_ids:
        return
    conn.execute("UPDATE external_execution_epochs SET closed_at=NOW(), closing_state='halted', closing_reason=%s WHERE external_deployment_id=ANY(%s) AND closed_at IS NULL", (reason_code, deployment_ids))
    target_state = "reconciliation_halted" if "position" in reason_code or "order" in reason_code or "snapshot" in reason_code else "risk_halted"
    conn.execute("UPDATE external_paper_deployments SET state=%s, latest_blockers=%s, updated_at=NOW() WHERE id=ANY(%s) AND state <> 'invalidated'", (target_state, Jsonb([reason_code]), deployment_ids))
