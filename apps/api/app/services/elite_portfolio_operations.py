"""Step 04: the activation workspace behind the Elite Builder.

Everything a portfolio needs between "approved snapshot" and "Alpaca Paper is
submitting orders" already existed as services and CLI commands. What did not
exist was a way to *see* the state and drive it without reading database rows
or discovering terminal commands, which is what this module provides.

It adds no authority. Every state change here calls the same function the CLI
calls (`external_execution.enable_observe_only`,
`external_execution.enable_paper_execution`), so every guard, audit event,
epoch, and frozen configuration behaves identically whichever entry point is
used. The three approvals stay separate and explicit:

    portfolio approved  ->  internal deployments activated
                        ->  each member approved for Alpaca Paper (observe only)
                        ->  each member's execution preflight passes
                        ->  execution explicitly enabled per member

Live money is not reachable from any of these paths.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg

from app.services.external_execution import (
    bar_is_complete,
    enable_observe_only,
    enable_paper_execution,
    feature_flags,
)
from app.settings import settings

OPERATOR_PREFIX = "elite-builder-ui"

# Deployment states, ordered from "nothing granted" to "submitting orders", with
# the halt states kept separate because they are exits rather than steps.
EXTERNAL_STATE_SEQUENCE = ("disabled", "enabled_observe_only", "enabled_execution")
EXTERNAL_HALT_STATES = ("readiness_blocked", "manually_halted", "risk_halted", "reconciliation_halted", "invalidated")

EXTERNAL_STATE_LABELS: dict[str, str] = {
    "disabled": "Disabled",
    "external_approval_required": "External approval required",
    "enabled_observe_only": "Enabled — observe only",
    "readiness_blocked": "Readiness blocked",
    "enabled_execution": "Enabled — execution",
    "manually_halted": "Manually halted",
    "risk_halted": "Risk halted",
    "reconciliation_halted": "Reconciliation halted",
    "invalidated": "Invalidated",
}


class PortfolioOperationError(ValueError):
    """A requested activation step is not valid for the current state."""


def _operator(actor: str | None = None) -> str:
    return f"{OPERATOR_PREFIX}:{actor or 'operator'}"


def _check(code: str, label: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"code": code, "label": label, "passed": bool(passed), "detail": detail}


def _fingerprint_state(conn: psycopg.Connection, external: dict[str, Any]) -> dict[str, Any]:
    """Does the candidate still hash to what was frozen at approval time?

    `enable_observe_only` freezes a `candidate_fingerprint` into the active
    configuration version. If the underlying deployment, elite row or candidate
    object has changed since, the approval no longer describes what would
    actually trade, and execution must not be enabled on it.

    A missing piece is reported as a failed check with the reason, never as a
    pass: "we could not verify the fingerprint" is not "the fingerprint is fine".
    """
    from app.services.external_execution import candidate_fingerprint, candidate_object_for

    version_id = external.get("active_configuration_version_id")
    if not version_id:
        return {"matches": False, "detail": "No active configuration version; re-approve this member for Alpaca Paper."}
    try:
        version = conn.execute(
            "SELECT candidate_fingerprint FROM deployment_configuration_versions WHERE id=%s", (version_id,)
        ).fetchone()
        deployment = conn.execute(
            "SELECT * FROM strategy_deployments WHERE id=%s", (external["internal_deployment_id"],)
        ).fetchone()
        elite = conn.execute(
            "SELECT * FROM elite_research_candidates WHERE campaign_id=%s AND candidate_id=%s AND simulation_only=TRUE",
            (external.get("campaign_id"), external.get("candidate_id")),
        ).fetchone()
        if not version or not deployment or not elite:
            return {"matches": False, "detail": "The frozen configuration, deployment or elite record is missing."}
        candidate_object = candidate_object_for(conn, int(deployment["campaign_id"]), str(deployment["candidate_id"]))
        current = candidate_fingerprint(dict(deployment), dict(elite), candidate_object)
    except Exception as error:  # noqa: BLE001 - see docstring
        conn.rollback()
        return {"matches": False, "detail": f"Fingerprint could not be recomputed: {type(error).__name__}: {error}"}
    stored = str(version["candidate_fingerprint"])
    if current != stored:
        return {
            "matches": False,
            "detail": f"Candidate changed since approval (frozen {stored[:12]}…, current {current[:12]}…). Re-approve before enabling execution.",
        }
    return {"matches": True, "detail": f"Matches the configuration frozen at approval ({stored[:12]}…)."}


def execution_preflight(conn: psycopg.Connection, external_deployment_id: int) -> dict[str, Any]:
    """Read-only checklist of everything `enable_paper_execution` will require.

    This is the same set of conditions that function enforces, evaluated
    without mutating anything, so the operator sees which requirement is
    outstanding instead of discovering it as a rejected request.

    Deliberately named "preflight" rather than "shadow trading". The two
    decision records it looks for (`shadow_executions.would_submit` and an
    approved `portfolio_risk_decisions` row) are produced automatically by the
    observe-only runner as it evaluates bars; they are a transition check, not
    a separate workspace the operator has to go and operate.
    """
    external = conn.execute("SELECT * FROM external_paper_deployments WHERE id=%s", (external_deployment_id,)).fetchone()
    if not external:
        raise PortfolioOperationError("external deployment not found")
    external = dict(external)

    epoch = conn.execute(
        "SELECT * FROM external_execution_epochs WHERE external_deployment_id=%s AND closed_at IS NULL ORDER BY id DESC LIMIT 1",
        (external_deployment_id,),
    ).fetchone()
    shadow = conn.execute(
        "SELECT * FROM shadow_executions WHERE external_deployment_id=%s AND would_submit=TRUE ORDER BY created_at DESC LIMIT 1",
        (external_deployment_id,),
    ).fetchone()
    portfolio_decision = conn.execute(
        "SELECT * FROM portfolio_risk_decisions WHERE external_deployment_id=%s AND approved=TRUE ORDER BY created_at DESC LIMIT 1",
        (external_deployment_id,),
    ).fetchone()
    account = conn.execute("SELECT * FROM broker_accounts WHERE id=%s", (external["broker_account_id"],)).fetchone()
    sync = conn.execute(
        "SELECT * FROM broker_sync_runs WHERE broker_account_id=%s AND status='complete' ORDER BY completed_at DESC LIMIT 1",
        (external["broker_account_id"],),
    ).fetchone()
    reconciliation = conn.execute(
        "SELECT * FROM broker_reconciliation_runs WHERE broker_account_id=%s ORDER BY completed_at DESC NULLS LAST LIMIT 1",
        (external["broker_account_id"],),
    ).fetchone()
    halts = [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM execution_halts
            WHERE cleared_at IS NULL
              AND ((scope_type='deployment' AND scope_key=%s) OR (scope_type='account' AND scope_key=%s) OR scope_type='global')
            ORDER BY severity DESC, last_seen_at DESC
            """,
            (str(external["id"]), str(external["broker_account_id"])),
        ).fetchall()
    ]
    candle = conn.execute(
        "SELECT * FROM candles WHERE symbol=%s AND timeframe=%s ORDER BY timestamp DESC LIMIT 1",
        (external["symbol"], external["timeframe"]),
    ).fetchone()
    bar_fresh = False
    if candle is not None and candle.get("timestamp") is not None:
        try:
            bar_fresh = bar_is_complete(candle["timestamp"], str(external["timeframe"]))
        except Exception:  # noqa: BLE001 - an unparseable bar is simply "not fresh"
            bar_fresh = False

    adapter = conn.execute(
        "SELECT * FROM broker_adapter_releases WHERE provider='alpaca' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    fingerprint = _fingerprint_state(conn, external)

    flags = feature_flags()
    checks = [
        _check(
            "PAPER_ACCOUNT_DETECTED",
            "Alpaca paper account detected",
            bool(account and str(account.get("environment")) == "paper"),
            (
                f"{account.get('provider')} {account.get('environment')} account {account.get('account_number_masked')}."
                if account
                else "No broker account has been synced."
            ),
        ),
        _check(
            "OBSERVE_ONLY_APPROVED",
            "Approved for Alpaca Paper (observe only)",
            external["state"] in {"enabled_observe_only", "enabled_execution"},
            f"Deployment state is {EXTERNAL_STATE_LABELS.get(str(external['state']), external['state'])}.",
        ),
        _check(
            "OPEN_EXECUTION_EPOCH",
            "Open approved execution epoch",
            epoch is not None,
            "An execution epoch is open." if epoch else "No open epoch; re-approve the deployment to open one.",
        ),
        _check(
            "BROKER_SYNC_COMPLETE",
            "Successful broker sync",
            sync is not None,
            f"Last complete sync {sync['completed_at']}." if sync else "No completed broker sync run.",
        ),
        _check(
            "RECONCILIATION_CLEAN",
            "Clean reconciliation",
            bool(reconciliation and reconciliation["status"] == "clean"),
            f"Reconciliation status {reconciliation['status']}." if reconciliation else "No reconciliation run.",
        ),
        _check(
            "ADAPTER_COMPATIBLE",
            "Compatible broker adapter release",
            bool(adapter and adapter["change_class"] == "compatible_patch"),
            (
                f"Adapter release {adapter.get('version') or adapter.get('id')} is {adapter['change_class']}."
                if adapter
                else "No Alpaca adapter release is registered."
            ),
        ),
        _check(
            "NO_ACTIVE_HALTS",
            "No active execution halts",
            not halts,
            "No active halts." if not halts else f"{len(halts)} active halt(s): " + ", ".join(sorted({str(row['reason_code']) for row in halts})),
        ),
        _check(
            "CANDIDATE_FINGERPRINT_MATCH",
            "Candidate fingerprint matches the approved configuration",
            fingerprint["matches"],
            fingerprint["detail"],
        ),
        _check(
            "FRESH_COMPLETED_BAR",
            "Fresh completed bar",
            bar_fresh,
            f"Latest {external['timeframe']} bar {candle['timestamp']}." if candle else f"No {external['timeframe']} candles for {external['symbol']}.",
        ),
        _check(
            "WOULD_SUBMIT_DECISION",
            "Execution preflight decision recorded",
            shadow is not None,
            (
                "The observe-only runner recorded a decision that would have submitted an order."
                if shadow
                else "Waiting for the observe-only runner to produce a would-submit decision. This happens automatically on a qualifying bar."
            ),
        ),
        _check(
            "PORTFOLIO_RISK_APPROVED",
            "Portfolio risk decision approved",
            portfolio_decision is not None,
            "A portfolio-level risk decision has been approved." if portfolio_decision else "Waiting for an approved portfolio-risk decision.",
        ),
        # Reported separately rather than as one combined flag: when execution
        # is blocked it matters which of the two an operator still has to set.
        _check(
            "ORDER_SUBMISSION_FLAG",
            "Broker order submission flag enabled",
            bool(flags["broker_order_submission_enabled"]),
            (
                "BROKER_ORDER_SUBMISSION_ENABLED is on."
                if flags["broker_order_submission_enabled"]
                else "BROKER_ORDER_SUBMISSION_ENABLED is off. This is an API environment setting, not a UI toggle."
            ),
        ),
        _check(
            "PAPER_EXECUTION_FLAG",
            "External paper execution flag enabled",
            bool(flags["external_paper_execution_enabled"]),
            (
                "EXTERNAL_PAPER_EXECUTION_ENABLED is on."
                if flags["external_paper_execution_enabled"]
                else "EXTERNAL_PAPER_EXECUTION_ENABLED is off. This is an API environment setting, not a UI toggle."
            ),
        ),
    ]
    outstanding = [row for row in checks if not row["passed"]]
    return {
        "external_deployment_id": external_deployment_id,
        "state": external["state"],
        "state_label": EXTERNAL_STATE_LABELS.get(str(external["state"]), str(external["state"])),
        "checks": checks,
        "passed": not outstanding,
        "outstanding": [row["code"] for row in outstanding],
        "next_action": (
            "Enable Alpaca Paper execution"
            if not outstanding
            else outstanding[0]["label"]
        ),
        "account_environment": "paper",
        "live_money_supported": False,
        "active_halts": halts,
        "generated_at": datetime.now(UTC),
    }


def _member_deployment_view(conn: psycopg.Connection, member: dict[str, Any]) -> dict[str, Any]:
    internal = None
    if member.get("internal_deployment_id"):
        row = conn.execute("SELECT * FROM strategy_deployments WHERE id=%s", (member["internal_deployment_id"],)).fetchone()
        internal = dict(row) if row else None
    external = None
    preflight = None
    if member.get("external_deployment_id"):
        row = conn.execute("SELECT * FROM external_paper_deployments WHERE id=%s", (member["external_deployment_id"],)).fetchone()
        if row:
            external = dict(row)
            preflight = execution_preflight(conn, int(external["id"]))
    activity = _member_activity(conn, member, external)
    external_state = str((external or {}).get("state") or "not_created")
    return {
        **{key: member[key] for key in member},
        "internal_deployment": internal,
        "internal_deployment_state": (internal or {}).get("status") or "not_created",
        "external_deployment": external,
        "external_deployment_state": external_state,
        "external_deployment_state_label": EXTERNAL_STATE_LABELS.get(external_state, external_state),
        "preflight": preflight,
        "activity": activity,
        "available_actions": _available_actions(member, external, preflight),
    }


def _available_actions(member: dict[str, Any], external: dict[str, Any] | None, preflight: dict[str, Any] | None) -> list[dict[str, Any]]:
    """What the operator may do to this member right now, and why not otherwise."""
    actions: list[dict[str, Any]] = []
    direction = str(member.get("strategy_direction") or "long")
    capability = str(member.get("execution_capability") or "external_observe")
    if direction != "long" or capability == "internal_only":
        actions.append({
            "action": "approve_external_paper",
            "enabled": False,
            "reason": "Short and internal-only members have no external broker path at all. This is structural, not a setting.",
        })
        return actions
    state = str((external or {}).get("state") or "not_created")
    actions.append({
        "action": "approve_external_paper",
        "enabled": state in {"disabled", "readiness_blocked", "manually_halted", "risk_halted", "reconciliation_halted"},
        "reason": (
            "Approve this member for Alpaca Paper in observe-only state. No orders are submitted by this step."
            if state in {"disabled", "readiness_blocked", "manually_halted", "risk_halted", "reconciliation_halted"}
            else f"Already {EXTERNAL_STATE_LABELS.get(state, state)}."
            if external
            else "Activate internal deployments first; the external record is created there."
        ),
    })
    actions.append({
        "action": "enable_paper_execution",
        "enabled": bool(preflight and preflight["passed"] and state == "enabled_observe_only"),
        "reason": (
            "Every preflight check passes. This authorises Alpaca Paper order submission for this member."
            if preflight and preflight["passed"] and state == "enabled_observe_only"
            else f"Preflight outstanding: {', '.join(preflight['outstanding'])}." if preflight and preflight["outstanding"]
            else "Approve for Alpaca Paper first."
        ),
    })
    return actions


def _member_activity(conn: psycopg.Connection, member: dict[str, Any], external: dict[str, Any] | None) -> dict[str, Any]:
    """Most recent evidence of this member actually doing something.

    `broker_orders` and `broker_fills` are synced from the broker and keyed by
    account, not deployment, so they are reached through the execution attempt
    that produced them rather than filtered directly -- an account-wide "last
    order" would be some other deployment's order half the time.
    """
    activity: dict[str, Any] = {
        "last_scan": None,
        "last_signal": None,
        "last_risk_decision": None,
        "last_proposed_order": None,
        "last_submitted_order": None,
        "last_fill": None,
        "halt_reason": None,
    }
    if not external:
        return activity
    external_id = external["id"]
    # The observe-only runner evaluates every qualifying bar and records the
    # decision, so its latest row is both "last scan" and, when it fired,
    # "last signal".
    activity["last_scan"] = _first(
        conn,
        "SELECT id, created_at, would_submit, decision FROM shadow_executions WHERE external_deployment_id=%s ORDER BY created_at DESC LIMIT 1",
        (external_id,),
    )
    activity["last_signal"] = _first(
        conn,
        "SELECT id, created_at, would_submit, decision FROM shadow_executions WHERE external_deployment_id=%s AND would_submit=TRUE ORDER BY created_at DESC LIMIT 1",
        (external_id,),
    )
    activity["last_risk_decision"] = _first(
        conn,
        "SELECT * FROM portfolio_risk_decisions WHERE external_deployment_id=%s ORDER BY created_at DESC LIMIT 1",
        (external_id,),
    )
    activity["last_proposed_order"] = _first(
        conn,
        "SELECT * FROM proposed_broker_orders WHERE external_deployment_id=%s ORDER BY created_at DESC LIMIT 1",
        (external_id,),
    )
    activity["last_submitted_order"] = _first(
        conn,
        """
        SELECT attempt.id, attempt.status, attempt.created_at, attempt.error, attempt.broker_order_id,
               orders.symbol, orders.side, orders.requested_quantity, orders.filled_quantity,
               orders.filled_average_price, orders.status AS broker_status, orders.submitted_at
        FROM broker_execution_attempts attempt
        LEFT JOIN broker_orders orders
          ON orders.broker_order_id = attempt.broker_order_id
         AND orders.broker_account_id = %s
        WHERE attempt.external_deployment_id = %s
        ORDER BY attempt.created_at DESC
        LIMIT 1
        """,
        (external["broker_account_id"], external_id),
    )
    activity["last_fill"] = _first(
        conn,
        """
        SELECT fills.*
        FROM broker_fills fills
        JOIN broker_execution_attempts attempt
          ON attempt.broker_order_id = fills.broker_order_id
         AND attempt.external_deployment_id = %s
        WHERE fills.broker_account_id = %s
        ORDER BY fills.transaction_at DESC
        LIMIT 1
        """,
        (external_id, external["broker_account_id"]),
    )
    blockers = list(external.get("latest_blockers") or [])
    activity["halt_reason"] = blockers[0] if blockers else None
    return activity


def _first(conn: psycopg.Connection, query: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    """Best-effort read of an observability table.

    A missing table means that part of the pipeline has not been provisioned in
    this environment, which is a reason to show nothing for it -- not a reason
    to fail the whole activation view.
    """
    try:
        row = conn.execute(query, params).fetchone()
    except Exception:  # noqa: BLE001 - see docstring
        conn.rollback()
        return None
    return dict(row) if row else None


def safety_panel(conn: psycopg.Connection) -> dict[str, Any]:
    """Everything that should be checked before authorising order submission."""
    from app.services.broker_read_models import broker_clock, broker_status
    from app.services.external_execution import default_risk_policy

    status = broker_status(conn)
    account = dict(status.get("account") or {})
    try:
        clock = broker_clock(conn)
    except Exception:  # noqa: BLE001 - clock is informational, never a gate here
        conn.rollback()
        clock = {}
    policy = default_risk_policy()
    return {
        "provider": "alpaca",
        "environment": "paper",
        "account_is_paper": True,
        "live_money_supported": False,
        "account": account,
        "broker_sync": status.get("latest_sync"),
        "reconciliation": status.get("latest_reconciliation"),
        "market_clock": clock,
        "active_halts": status.get("active_halts") or [],
        "feature_flags": status.get("feature_flags") or feature_flags(),
        "risk_limits": {
            "allocated_capital": policy["allocated_capital"],
            "max_risk_per_trade_pct": policy["max_risk_per_trade_pct"],
            "max_total_exposure_pct": policy["max_total_exposure_pct"],
            "max_open_positions": policy["max_open_positions"],
            "max_open_orders": policy["max_open_orders"],
            "daily_loss_limit_pct": policy["daily_loss_limit_pct"],
            "weekly_loss_limit_pct": policy["weekly_loss_limit_pct"],
            "long_only": policy["long_only"],
        },
        "generated_at": datetime.now(UTC),
    }


def portfolio_activation_view(conn: psycopg.Connection, portfolio_run_id: int) -> dict[str, Any]:
    """The whole of Step 04 in one read: portfolio, members, readiness, safety."""
    run = conn.execute("SELECT * FROM elite_portfolio_runs WHERE id=%s", (portfolio_run_id,)).fetchone()
    if not run:
        raise PortfolioOperationError("elite portfolio run not found")
    run = dict(run)
    members = [
        _member_deployment_view(conn, dict(row))
        for row in conn.execute("SELECT * FROM elite_portfolio_members WHERE portfolio_run_id=%s ORDER BY rank", (portfolio_run_id,)).fetchall()
    ]
    attempts = [
        dict(row)
        for row in conn.execute(
            "SELECT id,portfolio_run_id,idempotency_key,status,error,started_at,completed_at FROM elite_portfolio_activation_attempts WHERE portfolio_run_id=%s ORDER BY id DESC LIMIT 20",
            (portfolio_run_id,),
        ).fetchall()
    ]
    external_members = [row for row in members if row.get("external_deployment")]
    source_configuration = dict(run.get("source_configuration") or {})
    return {
        "portfolio_run_id": portfolio_run_id,
        "run_key": run.get("run_key"),
        "status": run.get("status"),
        "snapshot_hash": run.get("snapshot_hash"),
        "approved_snapshot_hash": run.get("approved_snapshot_hash"),
        "approved_at": run.get("approved_at"),
        "activated_at": run.get("activated_at"),
        "objective": run.get("objective"),
        "profile": source_configuration.get("profile"),
        # `mode`/`diversified`/`warning` are only ever set on a paper lab run's
        # source_configuration (see `paper_lab_preview`); a diversified run's
        # `diversified` therefore defaults True here, never the other way
        # around, so the UI can never mistake one for the other.
        "mode": source_configuration.get("mode"),
        "diversified": bool(source_configuration.get("diversified", True)),
        "warning": source_configuration.get("warning"),
        "constraints": run.get("constraints"),
        "members": members,
        "activation_attempts": attempts,
        "summary": {
            "member_count": len(members),
            "internally_active": sum(1 for row in members if row.get("activation_state") == "internal_active" or row.get("internal_deployment_id")),
            "external_records": len(external_members),
            "observe_only": sum(1 for row in external_members if row["external_deployment_state"] == "enabled_observe_only"),
            "execution_enabled": sum(1 for row in external_members if row["external_deployment_state"] == "enabled_execution"),
            "preflight_ready": sum(1 for row in external_members if (row.get("preflight") or {}).get("passed")),
        },
        "safety": safety_panel(conn),
        "requires_approved_snapshot": True,
        "live_money_supported": False,
    }


def _member_for(conn: psycopg.Connection, portfolio_run_id: int, member_id: int) -> dict[str, Any]:
    member = conn.execute(
        "SELECT * FROM elite_portfolio_members WHERE id=%s AND portfolio_run_id=%s",
        (member_id, portfolio_run_id),
    ).fetchone()
    if not member:
        raise PortfolioOperationError("portfolio member not found")
    return dict(member)


def approve_member_external_paper(
    conn: psycopg.Connection,
    portfolio_run_id: int,
    member_id: int,
    *,
    actor: str | None = None,
    reapprove: bool = False,
) -> dict[str, Any]:
    """Approve one member for Alpaca Paper in observe-only state.

    Thin wrapper over `external_execution.enable_observe_only`, which is the
    same function the CLI calls: identical guards, identical audit trail,
    identical frozen configuration and epoch. This does not enable order
    submission -- that is a separate, later approval.
    """
    member = _member_for(conn, portfolio_run_id, member_id)
    run = conn.execute("SELECT status, approved_snapshot_hash FROM elite_portfolio_runs WHERE id=%s", (portfolio_run_id,)).fetchone()
    if not run or run["status"] not in {"approved", "activated_internal"}:
        raise PortfolioOperationError("external approval requires an approved, internally activated portfolio")
    if not member.get("internal_deployment_id"):
        raise PortfolioOperationError("activate internal deployments before approving external paper")
    if str(member.get("strategy_direction") or "long") != "long" or str(member.get("execution_capability")) == "internal_only":
        raise PortfolioOperationError("short and internal-only members have no external broker path")
    result = enable_observe_only(
        conn,
        int(member["internal_deployment_id"]),
        operator=_operator(actor),
        reapprove=reapprove,
    )
    external_id = int(result["deployment"]["id"])
    conn.execute(
        "UPDATE elite_portfolio_members SET external_deployment_id=%s, activation_state='external_record_created', latest_error=NULL, updated_at=NOW() WHERE id=%s",
        (external_id, member_id),
    )
    conn.commit()
    return {
        "portfolio_run_id": portfolio_run_id,
        "member_id": member_id,
        "external_deployment_id": external_id,
        "state": result["deployment"]["state"],
        "state_label": EXTERNAL_STATE_LABELS.get(str(result["deployment"]["state"]), str(result["deployment"]["state"])),
        "order_submission_enabled": False,
        "live_money_supported": False,
        "trace_id": result.get("trace_id"),
        "preflight": execution_preflight(conn, external_id),
    }


def enable_member_paper_execution(
    conn: psycopg.Connection,
    portfolio_run_id: int,
    member_id: int,
    *,
    actor: str | None = None,
) -> dict[str, Any]:
    """Authorise Alpaca Paper order submission for one approved member.

    Thin wrapper over `external_execution.enable_paper_execution`. The
    preflight is re-read first purely so a refusal explains itself; the
    authoritative checks are the ones inside that function, which run
    regardless of what this reported.
    """
    member = _member_for(conn, portfolio_run_id, member_id)
    if not member.get("external_deployment_id"):
        raise PortfolioOperationError("approve this member for Alpaca Paper before enabling execution")
    external_id = int(member["external_deployment_id"])
    preflight = execution_preflight(conn, external_id)
    if not preflight["passed"]:
        raise PortfolioOperationError(
            "execution preflight is not complete: " + ", ".join(preflight["outstanding"])
        )
    result = enable_paper_execution(conn, external_id, operator=_operator(actor))
    return {
        "portfolio_run_id": portfolio_run_id,
        "member_id": member_id,
        "external_deployment_id": external_id,
        "state": result["deployment"]["state"],
        "state_label": EXTERNAL_STATE_LABELS.get(str(result["deployment"]["state"]), str(result["deployment"]["state"])),
        "order_submission_enabled": True,
        "environment": "paper",
        "live_money_supported": False,
        "trace_id": result.get("trace_id"),
        "preflight": execution_preflight(conn, external_id),
    }


def approve_all_members_for_alpaca_paper(
    conn: psycopg.Connection,
    portfolio_run_id: int,
    *,
    actor: str | None = None,
    reapprove: bool = False,
) -> dict[str, Any]:
    """Approve every eligible member of a run for Alpaca Paper, one at a time.

    Not specific to the paper lab -- any approved run's members can be
    bulk-approved -- but the paper lab (potentially thirteen members at once)
    is the mode this exists for. Each member goes through the exact same
    `approve_member_external_paper` a single per-member click would use, which
    itself calls `enable_observe_only`; nothing here bypasses those guards or
    grants any authority the per-member path does not already grant. One
    member's failure never blocks the rest, and a member already approved
    (the common case on a retried bulk call) is reported as skipped rather
    than as an error.
    """
    members = [
        dict(row)
        for row in conn.execute(
            "SELECT id, strategy_direction, execution_capability, internal_deployment_id FROM elite_portfolio_members WHERE portfolio_run_id=%s ORDER BY rank",
            (portfolio_run_id,),
        ).fetchall()
    ]
    approved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for member in members:
        member_id = int(member["id"])
        if str(member.get("strategy_direction") or "long") != "long" or str(member.get("execution_capability")) == "internal_only":
            skipped.append({"member_id": member_id, "reason": "no external broker path (short or internal-only)"})
            continue
        if not member.get("internal_deployment_id"):
            skipped.append({"member_id": member_id, "reason": "internal deployment not activated yet"})
            continue
        try:
            approved.append(approve_member_external_paper(conn, portfolio_run_id, member_id, actor=actor, reapprove=reapprove))
        except PortfolioOperationError as error:
            errors.append({"member_id": member_id, "error": str(error)})
        except ValueError as error:
            if "already enabled or approved" in str(error):
                skipped.append({"member_id": member_id, "reason": str(error)})
            else:
                errors.append({"member_id": member_id, "error": str(error)})
    return {
        "portfolio_run_id": portfolio_run_id,
        "approved": approved,
        "skipped": skipped,
        "errors": errors,
        "summary": {"approved": len(approved), "skipped": len(skipped), "errors": len(errors), "total": len(members)},
        "live_money_supported": False,
    }


def enable_all_ready_members_paper_execution(
    conn: psycopg.Connection,
    portfolio_run_id: int,
    *,
    actor: str | None = None,
) -> dict[str, Any]:
    """Enable Alpaca Paper execution for every member whose full preflight passes.

    A member with any outstanding preflight check is left completely
    unchanged -- this never partially satisfies `enable_paper_execution`'s own
    requirements, it only ever calls that function for a member that already
    passes every one of them. Blocked members are reported, not silently
    dropped, so a bulk call always accounts for the whole run.
    """
    members = [
        dict(row)
        for row in conn.execute(
            "SELECT id, external_deployment_id FROM elite_portfolio_members WHERE portfolio_run_id=%s ORDER BY rank",
            (portfolio_run_id,),
        ).fetchall()
    ]
    enabled: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for member in members:
        member_id = int(member["id"])
        external_id = member.get("external_deployment_id")
        if not external_id:
            blocked.append({"member_id": member_id, "reason": "not approved for Alpaca Paper yet"})
            continue
        preflight = execution_preflight(conn, int(external_id))
        if not preflight["passed"]:
            blocked.append({"member_id": member_id, "reason": f"preflight outstanding: {', '.join(preflight['outstanding'])}"})
            continue
        try:
            enabled.append(enable_member_paper_execution(conn, portfolio_run_id, member_id, actor=actor))
        except (PortfolioOperationError, ValueError) as error:
            errors.append({"member_id": member_id, "error": str(error)})
    return {
        "portfolio_run_id": portfolio_run_id,
        "enabled": enabled,
        "blocked": blocked,
        "errors": errors,
        "summary": {"enabled": len(enabled), "blocked": len(blocked), "errors": len(errors), "total": len(members)},
        "live_money_supported": False,
    }
