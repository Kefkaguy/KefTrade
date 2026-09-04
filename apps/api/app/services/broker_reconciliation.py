from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from app.settings import settings

OPEN_ORDER_STATUSES = {
    "new",
    "accepted",
    "pending_new",
    "partially_filled",
    "pending_cancel",
    "accepted_for_bidding",
    "held",
    "calculated",
}
POSITION_PRECISION = Decimal("0.000000001")
AUTO_MANAGED_FINDING_TYPES = (
    "unexpected_position",
    "unexpected_open_order",
    "incomplete_snapshot",
    "account_blocked",
)


def reconcile_broker_snapshot(
    conn: psycopg.Connection,
    sync_run_id: int,
    *,
    trace_id: UUID | None = None,
) -> dict[str, Any]:
    if not settings.broker_reconciliation_enabled:
        return {
            "status": "disabled",
            "feature": "BROKER_RECONCILIATION_ENABLED",
            "paper_only": True,
        }
    sync = conn.execute(
        "SELECT * FROM broker_sync_runs WHERE id = %s", (sync_run_id,)
    ).fetchone()
    if not sync or sync["status"] != "complete" or not sync.get("broker_account_id"):
        raise ValueError("reconciliation requires one complete persisted broker sync")

    # Re-running the CLI against an already-reconciled snapshot is a harmless
    # read, not a second evidence set and not a unique-constraint error.
    existing = conn.execute(
        "SELECT * FROM broker_reconciliation_runs WHERE sync_run_id = %s FOR UPDATE",
        (sync_run_id,),
    ).fetchone()
    if existing and existing["status"] != "running":
        persisted = conn.execute(
            """SELECT * FROM broker_reconciliation_findings
               WHERE reconciliation_run_id=%s AND resolved_at IS NULL
               ORDER BY severity DESC, created_at, id""",
            (existing["id"],),
        ).fetchall()
        conn.commit()
        return {
            "status": existing["status"],
            "id": int(existing["id"]),
            "run": dict(existing),
            "findings": [dict(row) for row in persisted],
            "trace_id": str(existing["trace_id"]),
            "idempotent_replay": True,
            "paper_only": True,
        }

    trace_id = UUID(str(existing["trace_id"])) if existing else (trace_id or uuid4())
    run = (
        existing
        or conn.execute(
            """INSERT INTO broker_reconciliation_runs(
               broker_account_id, sync_run_id, trace_id, status
           ) VALUES (%s,%s,%s,'running') RETURNING *""",
            (sync["broker_account_id"], sync_run_id, trace_id),
        ).fetchone()
    )
    findings = build_findings(conn, int(sync["broker_account_id"]), sync_run_id)
    for current in findings:
        conn.execute(
            """INSERT INTO broker_reconciliation_findings(
                   reconciliation_run_id, trace_id, finding_key, finding_type,
                   severity, scope_type, scope_key, details
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(reconciliation_run_id, finding_key) DO UPDATE SET
                   trace_id=EXCLUDED.trace_id, finding_type=EXCLUDED.finding_type,
                   severity=EXCLUDED.severity, scope_type=EXCLUDED.scope_type,
                   scope_key=EXCLUDED.scope_key, details=EXCLUDED.details,
                   resolved_at=NULL, resolution=NULL""",
            (
                run["id"],
                trace_id,
                current["finding_key"],
                current["finding_type"],
                current["severity"],
                current["scope_type"],
                current["scope_key"],
                Jsonb(current["details"]),
            ),
        )
        if current["severity"] == "critical":
            upsert_halt(
                conn,
                trace_id,
                current["scope_type"],
                current["scope_key"],
                current["finding_type"],
                current["details"],
            )

    resolve_superseded_findings(
        conn,
        broker_account_id=int(sync["broker_account_id"]),
        reconciliation_run_id=int(run["id"]),
        current_findings=findings,
    )
    status = "findings" if findings else "clean"
    if status == "clean":
        # A clean account-level quantity comparison is the evidence sell guards
        # require; stamp every positive attribution with the run that proved it.
        conn.execute(
            """UPDATE strategy_owned_positions
               SET reconciliation_run_id=%s, as_of=NOW(), updated_at=NOW()
               WHERE broker_account_id=%s AND quantity > 0""",
            (run["id"], sync["broker_account_id"]),
        )
        clear_resolved_reconciliation_halts(
            conn,
            broker_account_id=int(sync["broker_account_id"]),
            trace_id=trace_id,
            reconciliation_run_id=int(run["id"]),
        )
    summary = {
        "finding_count": len(findings),
        "critical_count": sum(row["severity"] == "critical" for row in findings),
        "source_sync_run_id": sync_run_id,
    }
    completed = conn.execute(
        """UPDATE broker_reconciliation_runs
           SET status=%s, summary=%s, completed_at=NOW()
           WHERE id=%s RETURNING *""",
        (status, Jsonb(summary), run["id"]),
    ).fetchone()
    conn.commit()
    return {
        "status": status,
        "id": int(completed["id"]),
        "run": dict(completed),
        "findings": findings,
        "trace_id": str(trace_id),
        "idempotent_replay": False,
        "paper_only": True,
    }


def build_findings(
    conn: psycopg.Connection,
    broker_account_id: int,
    sync_run_id: int,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    account = conn.execute(
        "SELECT * FROM broker_account_state WHERE broker_account_id=%s AND sync_run_id=%s",
        (broker_account_id, sync_run_id),
    ).fetchone()
    clock = conn.execute(
        "SELECT * FROM broker_clock_state WHERE broker_account_id=%s AND sync_run_id=%s",
        (broker_account_id, sync_run_id),
    ).fetchone()
    if not account or not clock:
        findings.append(
            finding(
                "incomplete_latest_state",
                "incomplete_snapshot",
                "critical",
                "account",
                str(broker_account_id),
                {"account_present": bool(account), "clock_present": bool(clock)},
            )
        )
        return findings
    if (
        account["account_blocked"]
        or account["trading_blocked"]
        or account["trade_suspended_by_user"]
    ):
        findings.append(
            finding(
                "broker_account_blocked",
                "account_blocked",
                "critical",
                "account",
                str(broker_account_id),
                {"status": account["status"]},
            )
        )

    positions = conn.execute(
        """SELECT * FROM broker_positions
           WHERE broker_account_id=%s AND sync_run_id=%s AND quantity <> 0""",
        (broker_account_id, sync_run_id),
    ).fetchall()
    owned = conn.execute(
        """SELECT symbol, SUM(quantity) AS owned_quantity
           FROM strategy_owned_positions
           WHERE broker_account_id=%s AND quantity > 0
           GROUP BY symbol""",
        (broker_account_id,),
    ).fetchall()
    findings.extend(
        position_ownership_findings(
            positions,
            owned,
            broker_account_id=broker_account_id,
        )
    )

    orders = conn.execute(
        """SELECT o.*,
                  a.id AS strategy_attribution_id,
                  e.id AS execution_attempt_id
           FROM broker_orders o
           LEFT JOIN strategy_order_attributions a
             ON a.broker_account_id=o.broker_account_id
            AND a.client_order_id=o.client_order_id
           LEFT JOIN broker_execution_attempts e
             ON e.client_order_id=o.client_order_id
           WHERE o.broker_account_id=%s AND o.sync_run_id=%s""",
        (broker_account_id, sync_run_id),
    ).fetchall()
    for order in orders:
        if open_order_is_unexpected(order):
            findings.append(
                finding(
                    f"unexpected_open_order:{order['broker_order_id']}",
                    "unexpected_open_order",
                    "critical",
                    "account",
                    str(broker_account_id),
                    {
                        "broker_order_id": order["broker_order_id"],
                        "client_order_id": order["client_order_id"],
                        "symbol": order["symbol"],
                        "status": order["status"],
                    },
                )
            )
    return findings


def open_order_is_unexpected(order: dict[str, Any]) -> bool:
    expected = bool(
        order.get("strategy_attribution_id") or order.get("execution_attempt_id")
    )
    return str(order.get("status") or "") in OPEN_ORDER_STATUSES and not expected


def position_ownership_findings(
    broker_positions: Iterable[dict[str, Any]],
    owned_positions: Iterable[dict[str, Any]],
    *,
    broker_account_id: int,
) -> list[dict[str, Any]]:
    """Return only broker quantities not exactly explained by ownership."""
    broker_by_symbol = {str(row["symbol"]).upper(): row for row in broker_positions}
    owned_by_symbol: dict[str, Decimal] = {}
    for row in owned_positions:
        symbol = str(row["symbol"]).upper()
        owned_by_symbol[symbol] = owned_by_symbol.get(symbol, Decimal(0)) + _quantity(
            row.get("owned_quantity", row.get("quantity", 0))
        )

    findings: list[dict[str, Any]] = []
    for symbol in sorted(set(broker_by_symbol) | set(owned_by_symbol)):
        position = broker_by_symbol.get(symbol) or {}
        broker_quantity = _quantity(position.get("quantity", 0))
        owned_quantity = owned_by_symbol.get(symbol, Decimal(0))
        difference = (broker_quantity - owned_quantity).quantize(POSITION_PRECISION)
        if difference == 0:
            continue
        mismatch_kind = (
            "unexpected_broker_excess"
            if difference > 0
            else "strategy_ownership_exceeds_broker"
        )
        findings.append(
            finding(
                f"unexpected_position:{symbol}",
                "unexpected_position",
                "critical",
                "account",
                str(broker_account_id),
                {
                    "symbol": symbol,
                    "broker_quantity": str(broker_quantity),
                    "owned_quantity": str(owned_quantity),
                    "unexplained_quantity": str(difference),
                    "mismatch_kind": mismatch_kind,
                    "market_value": str(position.get("market_value") or 0),
                },
            )
        )
    return findings


def resolve_superseded_findings(
    conn: psycopg.Connection,
    *,
    broker_account_id: int,
    reconciliation_run_id: int,
    current_findings: Iterable[dict[str, Any]],
) -> int:
    """Keep only this run's current managed findings unresolved."""
    current_keys = {str(row["finding_key"]) for row in current_findings}
    rows = conn.execute(
        """UPDATE broker_reconciliation_findings f
           SET resolved_at=NOW(),
               resolution=jsonb_build_object(
                   'reason', CASE WHEN f.finding_key=ANY(%s)
                                  THEN 'superseded_by_new_reconciliation'
                                  ELSE 'condition_cleared' END,
                   'reconciliation_run_id', %s,
                   'automatic', TRUE
               )
           FROM broker_reconciliation_runs r
           WHERE r.id=f.reconciliation_run_id
             AND r.broker_account_id=%s
             AND f.reconciliation_run_id<>%s
             AND f.resolved_at IS NULL
             AND f.finding_type=ANY(%s)
           RETURNING f.id""",
        (
            list(current_keys) or ["__no_current_finding__"],
            reconciliation_run_id,
            broker_account_id,
            reconciliation_run_id,
            list(AUTO_MANAGED_FINDING_TYPES),
        ),
    ).fetchall()
    return len(rows)


def clear_resolved_reconciliation_halts(
    conn: psycopg.Connection,
    *,
    broker_account_id: int,
    trace_id: UUID,
    reconciliation_run_id: int,
) -> dict[str, int]:
    """Clear emergency stops after clean reconciliation, without auto-trading."""
    cleared = conn.execute(
        """UPDATE execution_halts
           SET cleared_at=NOW(), cleared_by='broker-reconciliation:auto',
               clearance_reason='clean_reconciliation'
           WHERE cleared_at IS NULL AND scope_type='account' AND scope_key=%s
             AND reason_code=ANY(%s)
           RETURNING id""",
        (str(broker_account_id), list(AUTO_MANAGED_FINDING_TYPES)),
    ).fetchall()
    deployments = conn.execute(
        """SELECT * FROM external_paper_deployments
           WHERE broker_account_id=%s AND state='reconciliation_halted'
           ORDER BY id FOR UPDATE""",
        (broker_account_id,),
    ).fetchall()
    for deployment in deployments:
        blocker = "EXPLICIT_REAPPROVAL_REQUIRED_AFTER_RECONCILIATION_HALT"
        conn.execute(
            """UPDATE external_paper_deployments
               SET state='readiness_blocked', latest_blockers=%s, updated_at=NOW()
               WHERE id=%s""",
            (Jsonb([blocker]), deployment["id"]),
        )
        conn.execute(
            """INSERT INTO external_deployment_transitions(
                   external_deployment_id, execution_epoch_id, trace_id,
                   from_state, to_state, reason_code, details, operator
               ) VALUES (%s,NULL,%s,'reconciliation_halted','readiness_blocked',%s,%s,%s)""",
            (
                deployment["id"],
                trace_id,
                "reconciliation_issue_resolved_reapproval_required",
                Jsonb(
                    {"reconciliation_run_id": reconciliation_run_id, "automatic": True}
                ),
                "broker-reconciliation:auto",
            ),
        )
    return {"halts_cleared": len(cleared), "deployments_released": len(deployments)}


def finding(
    key: str,
    finding_type: str,
    severity: str,
    scope_type: str,
    scope_key: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    return {
        "finding_key": key,
        "finding_type": finding_type,
        "severity": severity,
        "scope_type": scope_type,
        "scope_key": scope_key,
        "details": details,
    }


def _quantity(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(POSITION_PRECISION)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"invalid broker or ownership quantity {value!r}") from error


def upsert_halt(
    conn: psycopg.Connection,
    trace_id: UUID,
    scope_type: str,
    scope_key: str,
    reason_code: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    current = conn.execute(
        """SELECT * FROM execution_halts
           WHERE scope_type=%s AND scope_key=%s AND reason_code=%s
             AND cleared_at IS NULL FOR UPDATE""",
        (scope_type, scope_key, reason_code),
    ).fetchone()
    if current:
        row = conn.execute(
            """UPDATE execution_halts SET trace_id=%s, evidence=%s,
                   occurrence_count=occurrence_count+1, last_seen_at=NOW()
               WHERE id=%s RETURNING *""",
            (trace_id, Jsonb(evidence), current["id"]),
        ).fetchone()
    else:
        row = conn.execute(
            """INSERT INTO execution_halts(
                   trace_id, scope_type, scope_key, reason_code, severity, evidence
               ) VALUES (%s,%s,%s,%s,'critical',%s) RETURNING *""",
            (trace_id, scope_type, scope_key, reason_code, Jsonb(evidence)),
        ).fetchone()
    close_affected_epochs(conn, scope_type, scope_key, reason_code)
    return dict(row)


def close_affected_epochs(
    conn: psycopg.Connection,
    scope_type: str,
    scope_key: str,
    reason_code: str,
) -> None:
    if scope_type == "account":
        deployment_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM external_paper_deployments WHERE broker_account_id=%s",
                (int(scope_key),),
            ).fetchall()
        ]
    elif scope_type == "deployment":
        deployment_ids = [int(scope_key)]
    elif scope_type == "asset":
        deployment_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM external_paper_deployments WHERE symbol=%s",
                (scope_key,),
            ).fetchall()
        ]
    else:
        deployment_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM external_paper_deployments"
            ).fetchall()
        ]
    if not deployment_ids:
        return
    conn.execute(
        """UPDATE external_execution_epochs
           SET closed_at=NOW(), closing_state='halted', closing_reason=%s
           WHERE external_deployment_id=ANY(%s) AND closed_at IS NULL""",
        (reason_code, deployment_ids),
    )
    target_state = (
        "reconciliation_halted"
        if any(token in reason_code for token in ("position", "order", "snapshot"))
        else "risk_halted"
    )
    conn.execute(
        """UPDATE external_paper_deployments
           SET state=%s, latest_blockers=%s, updated_at=NOW()
           WHERE id=ANY(%s) AND state <> 'invalidated'""",
        (target_state, Jsonb([reason_code]), deployment_ids),
    )
