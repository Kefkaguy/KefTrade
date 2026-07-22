from __future__ import annotations

from typing import Any, Callable

import psycopg
from psycopg.types.json import Jsonb

from app.services.external_execution import feature_flags
from app.services.paper_trading import create_deployment
from app.services.research_campaigns import ensure_candidate_forward_account


TERMINAL_MEMBER_STATES = {"internal_active", "external_record_created", "external_approval_required"}


class PortfolioActivationError(ValueError):
    pass


def activation_worklist(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in members if row.get("activation_state") not in TERMINAL_MEMBER_STATES]


def authorization_instruction(member: dict[str, Any], snapshot_hash: str) -> dict[str, Any] | None:
    if str(member.get("strategy_direction") or "long") != "long":
        return None
    if str(member.get("execution_capability") or "external_observe") == "internal_only":
        return None
    deployment_id = int(member["internal_deployment_id"])
    return {
        "portfolio_snapshot_hash": snapshot_hash,
        "internal_deployment_id": deployment_id,
        "candidate_id": member["candidate_id"],
        "symbol": member["symbol"],
        "timeframe": member["timeframe"],
        "expected_effect": "Explicitly approve Alpaca Paper observe-only state; this command does not enable order submission.",
        "command": (
            "docker compose -f docker-compose.prod.yml exec -T api "
            f"python -m app.cli.deployments enable-external-paper {deployment_id} "
            f"--confirm-deployment-id {deployment_id}"
        ),
        "execution_flags": feature_flags(),
        "live_money_supported": False,
    }


def activate_internal(
    conn: psycopg.Connection,
    portfolio_run_id: int,
    idempotency_key: str,
    requested_snapshot_hash: str,
    *,
    failure_injector: Callable[[dict[str, Any], str], None] | None = None,
) -> dict[str, Any]:
    run = conn.execute("SELECT * FROM elite_portfolio_runs WHERE id=%s FOR UPDATE", (portfolio_run_id,)).fetchone()
    if not run:
        raise PortfolioActivationError("elite portfolio run not found")
    if run["status"] not in {"approved", "activated_internal"}:
        raise PortfolioActivationError("internal activation requires an approved portfolio")
    if requested_snapshot_hash != run.get("approved_snapshot_hash") or requested_snapshot_hash != run.get("snapshot_hash"):
        raise PortfolioActivationError("activation snapshot does not match the approved immutable snapshot")
    existing_attempt = conn.execute("SELECT * FROM elite_portfolio_activation_attempts WHERE idempotency_key=%s FOR UPDATE", (idempotency_key,)).fetchone()
    if existing_attempt:
        if int(existing_attempt["portfolio_run_id"]) != portfolio_run_id or existing_attempt["requested_snapshot_hash"] != requested_snapshot_hash:
            raise PortfolioActivationError("idempotency key is already bound to another activation request")
        if existing_attempt["status"] == "complete":
            return dict(existing_attempt.get("result") or {})
        attempt = conn.execute("UPDATE elite_portfolio_activation_attempts SET status='running', error=NULL, completed_at=NULL WHERE id=%s RETURNING *", (existing_attempt["id"],)).fetchone()
    else:
        attempt = conn.execute(
            """
            INSERT INTO elite_portfolio_activation_attempts(portfolio_run_id,idempotency_key,status,requested_snapshot_hash)
            VALUES (%s,%s,'running',%s) RETURNING *
            """,
            (portfolio_run_id, idempotency_key, requested_snapshot_hash),
        ).fetchone()
    conn.commit()
    members = [dict(row) for row in conn.execute("SELECT * FROM elite_portfolio_members WHERE portfolio_run_id=%s ORDER BY rank", (portfolio_run_id,)).fetchall()]
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for member in activation_worklist(members):
        try:
            conn.execute("UPDATE elite_portfolio_members SET activation_state='internal_activation_pending',latest_error=NULL,updated_at=NOW() WHERE id=%s", (member["id"],))
            conn.commit()
            activated = _activate_member(conn, member)
            if failure_injector:
                failure_injector(activated, "after_internal_deployment")
            results.append(activated)
        except Exception as error:
            conn.rollback()
            conn.execute("UPDATE elite_portfolio_members SET activation_state='failed',latest_error=%s,updated_at=NOW() WHERE id=%s", (str(error), member["id"]))
            conn.commit()
            errors.append({"member_id": member["id"], "candidate_id": member["candidate_id"], "error_class": error.__class__.__name__, "error": str(error)})
    refreshed = [dict(row) for row in conn.execute("SELECT * FROM elite_portfolio_members WHERE portfolio_run_id=%s ORDER BY rank", (portfolio_run_id,)).fetchall()]
    instructions = [item for item in (authorization_instruction(member, requested_snapshot_hash) for member in refreshed) if item is not None]
    complete = not activation_worklist(refreshed)
    attempt_status = "complete" if complete else ("partial" if results else "failed")
    result = {
        "portfolio_run_id": portfolio_run_id,
        "idempotency_key": idempotency_key,
        "status": attempt_status,
        "members": refreshed,
        "activated_this_attempt": results,
        "errors": errors,
        "authorization_instructions": instructions,
        "portfolio_snapshot_hash": requested_snapshot_hash,
        "execution_flags": feature_flags(),
        "order_submission_changed": False,
        "live_money_supported": False,
    }
    conn.execute(
        "UPDATE elite_portfolio_activation_attempts SET status=%s,result=%s,error=%s,completed_at=NOW() WHERE id=%s",
        (attempt_status, Jsonb(result), Jsonb(errors) if errors else None, attempt["id"]),
    )
    if complete:
        conn.execute("UPDATE elite_portfolio_runs SET status='activated_internal',activated_at=COALESCE(activated_at,NOW()),updated_at=NOW() WHERE id=%s", (portfolio_run_id,))
    conn.commit()
    return result


def _activate_member(conn: psycopg.Connection, member: dict[str, Any]) -> dict[str, Any]:
    existing = conn.execute(
        """
        SELECT * FROM strategy_deployments
        WHERE campaign_id=%s AND candidate_id=%s AND symbol=%s AND timeframe=%s
          AND status='active' AND simulation_only=TRUE
        ORDER BY id LIMIT 1
        """,
        (member.get("campaign_id"), member["candidate_id"], member["symbol"], member["timeframe"]),
    ).fetchone()
    evidence = dict(member.get("evidence") or {})
    if existing:
        deployment = dict(existing)
    else:
        account = ensure_candidate_forward_account(conn, member["candidate_id"], 10_000)
        deployment = create_deployment(
            conn,
            int(account["id"]),
            str(evidence.get("strategy_name") or "autonomous_strategy_discovery"),
            member["symbol"],
            member["timeframe"],
            strategy_version=str(evidence.get("strategy_version") or member["candidate_id"]),
            parameters=dict(evidence.get("parameters") or {}),
            campaign_id=member.get("campaign_id"),
            candidate_id=member["candidate_id"],
            strategy_id=f"elite_portfolio:{member['candidate_id']}",
            evidence_version=str(evidence.get("data_snapshot_hash") or "elite-portfolio-v1"),
            lifecycle_state="portfolio_internal_validation",
            deployment_origin="elite_portfolio_builder",
            strategy_direction=member["strategy_direction"],
            execution_capability=member["execution_capability"],
        )
    external = None
    next_state = "internal_active"
    if member["strategy_direction"] == "long" and member["execution_capability"] != "internal_only":
        external = _ensure_disabled_external_candidate(conn, member, deployment)
        if external:
            next_state = "external_approval_required"
    conn.execute(
        """
        UPDATE elite_portfolio_members
        SET internal_deployment_id=%s,external_deployment_id=%s,activation_state=%s,latest_error=NULL,updated_at=NOW()
        WHERE id=%s
        """,
        (deployment["id"], external and external["id"], next_state, member["id"]),
    )
    conn.commit()
    return {
        "member_id": member["id"],
        "candidate_id": member["candidate_id"],
        "strategy_direction": member["strategy_direction"],
        "internal_deployment_id": deployment["id"],
        "external_deployment_id": external and external["id"],
        "activation_state": next_state,
    }


def _ensure_disabled_external_candidate(conn: psycopg.Connection, member: dict[str, Any], deployment: dict[str, Any]) -> dict[str, Any] | None:
    if member["strategy_direction"] != "long" or member["execution_capability"] == "internal_only":
        raise PortfolioActivationError("short and internal-only members cannot create external deployment records")
    account = conn.execute("SELECT * FROM broker_accounts ORDER BY last_successful_sync_at DESC NULLS LAST LIMIT 1").fetchone()
    if not account:
        return None
    existing = conn.execute("SELECT * FROM external_paper_deployments WHERE internal_deployment_id=%s AND broker_account_id=%s", (deployment["id"], account["id"])).fetchone()
    if existing:
        return dict(existing)
    row = conn.execute(
        """
        INSERT INTO external_paper_deployments(
            internal_deployment_id,broker_account_id,campaign_id,elite_candidate_id,candidate_id,
            strategy_version,symbol,timeframe,state,latest_blockers
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'disabled',%s) RETURNING *
        """,
        (
            deployment["id"], account["id"], member.get("campaign_id"), member.get("elite_candidate_id"), member["candidate_id"],
            deployment["strategy_version"], member["symbol"], member["timeframe"], Jsonb(["EXPLICIT_OBSERVE_APPROVAL_REQUIRED"]),
        ),
    ).fetchone()
    return dict(row)
