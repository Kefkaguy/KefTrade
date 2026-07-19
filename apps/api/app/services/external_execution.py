from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from app.services.broker_reconciliation import upsert_halt
from app.services.broker_sync import canonical_json
from app.services.evidence_alerts import candle_is_stale
from app.services.paper_trading import decision_payload, evaluate_deployment_decision, latest_candle
from app.settings import settings

STARTABLE_FORWARD_STATES = {"awaiting_paper_deployment", "insufficient_forward_sample", "forward_validation_passed"}


def feature_flags() -> dict[str, bool]:
    return {
        "broker_sync_enabled": settings.broker_sync_enabled,
        "broker_reconciliation_enabled": settings.broker_reconciliation_enabled,
        "broker_shadow_execution_enabled": settings.broker_shadow_execution_enabled,
        "broker_order_submission_enabled": settings.broker_order_submission_enabled,
        "external_paper_execution_enabled": settings.external_paper_execution_enabled,
    }


def default_risk_policy() -> dict[str, Any]:
    return {
        "allocated_capital": settings.broker_allocated_capital,
        "max_risk_per_trade_pct": settings.max_broker_risk_per_trade_pct,
        "max_total_exposure_pct": settings.max_broker_total_exposure_pct,
        "daily_loss_limit_pct": settings.broker_daily_loss_limit_pct,
        "weekly_loss_limit_pct": settings.broker_weekly_loss_limit_pct,
        "max_open_positions": settings.broker_max_open_positions,
        "max_open_orders": settings.broker_max_open_orders,
        "long_only": True,
        "whole_shares": True,
        "use_broker_buying_power": False,
    }


def default_eligibility_policy() -> dict[str, Any]:
    return {
        "allowed_forward_states": sorted(STARTABLE_FORWARD_STATES),
        "require_elite_record": True,
        "require_candidate_object": True,
        "require_candidate_fingerprint_match": True,
        "require_complete_sync": True,
        "require_clean_reconciliation": True,
        "require_open_broker_clock": True,
        "require_fresh_completed_bar": True,
        "allow_automatic_resume": False,
    }


def ensure_policy_versions(conn: psycopg.Connection) -> tuple[dict[str, Any], dict[str, Any]]:
    risk = persist_policy(conn, "risk_policy_versions", "phase10-risk-v1", default_risk_policy())
    eligibility = persist_policy(conn, "eligibility_policy_versions", "phase10-eligibility-v1", default_eligibility_policy())
    conn.commit()
    return risk, eligibility


def persist_policy(conn: psycopg.Connection, table: str, version: str, policy: dict[str, Any]) -> dict[str, Any]:
    policy_hash = hashlib.sha256(canonical_json(policy).encode("utf-8")).hexdigest()
    conn.execute(f"INSERT INTO {table}(version, policy, policy_hash) VALUES (%s,%s,%s) ON CONFLICT(version) DO NOTHING", (version, Jsonb(policy), policy_hash))
    row = conn.execute(f"SELECT * FROM {table} WHERE version=%s", (version,)).fetchone()
    if not row or row["policy_hash"] != policy_hash:
        raise RuntimeError(f"{table} version collision with different policy")
    return dict(row)


def enable_observe_only(conn: psycopg.Connection, internal_deployment_id: int, *, operator: str, reapprove: bool = False) -> dict[str, Any]:
    assert_execution_disabled()
    trace_id = uuid4()
    audit(conn, trace_id, "reapprove_external_paper" if reapprove else "enable_external_paper", operator, "before", details={"internal_deployment_id": internal_deployment_id})
    conn.commit()
    deployment = conn.execute("SELECT * FROM strategy_deployments WHERE id=%s AND simulation_only=TRUE FOR UPDATE", (internal_deployment_id,)).fetchone()
    if not deployment or not deployment.get("campaign_id") or not deployment.get("candidate_id"):
        raise ValueError("external paper requires a candidate-linked internal simulation deployment")
    elite = conn.execute("SELECT * FROM elite_research_candidates WHERE campaign_id=%s AND candidate_id=%s AND simulation_only=TRUE FOR UPDATE", (deployment["campaign_id"], deployment["candidate_id"])).fetchone()
    if not elite:
        raise ValueError("external paper requires an authoritative elite candidate")
    if str(elite["forward_validation_state"]) not in STARTABLE_FORWARD_STATES:
        raise ValueError(f"candidate cannot start forward validation from {elite['forward_validation_state']}")
    candidate_object = candidate_object_for(conn, int(deployment["campaign_id"]), str(deployment["candidate_id"]))
    account = conn.execute("SELECT * FROM broker_accounts ORDER BY last_successful_sync_at DESC NULLS LAST LIMIT 1 FOR UPDATE").fetchone()
    if not account:
        raise ValueError("run a successful broker sync before external deployment approval")
    latest_sync = conn.execute("SELECT * FROM broker_sync_runs WHERE broker_account_id=%s AND status='complete' ORDER BY completed_at DESC LIMIT 1", (account["id"],)).fetchone()
    latest_reconciliation = conn.execute("SELECT * FROM broker_reconciliation_runs WHERE broker_account_id=%s ORDER BY completed_at DESC NULLS LAST LIMIT 1", (account["id"],)).fetchone()
    if not latest_sync or not latest_reconciliation or latest_reconciliation["status"] != "clean":
        raise ValueError("external deployment approval requires a complete sync and clean reconciliation")
    adapter = conn.execute("SELECT * FROM broker_adapter_releases WHERE provider='alpaca' ORDER BY created_at DESC LIMIT 1").fetchone()
    if not adapter or adapter["change_class"] not in {"compatible_patch"}:
        raise ValueError("a compatible read-only adapter release is required")
    risk_policy, eligibility_policy = ensure_policy_versions(conn)
    fingerprint = candidate_fingerprint(dict(deployment), dict(elite), candidate_object)
    current = conn.execute("SELECT * FROM external_paper_deployments WHERE internal_deployment_id=%s AND broker_account_id=%s FOR UPDATE", (internal_deployment_id, account["id"])).fetchone()
    if current and not reapprove and current["state"] not in {"disabled", "readiness_blocked", "manually_halted", "risk_halted", "reconciliation_halted"}:
        raise ValueError("deployment is already enabled or approved")
    next_version = int((conn.execute("SELECT COALESCE(MAX(version),0)+1 AS version FROM deployment_configuration_versions WHERE internal_deployment_id=%s", (internal_deployment_id,)).fetchone() or {}).get("version") or 1)
    frozen = frozen_configuration(dict(deployment), dict(elite), candidate_object, fingerprint, risk_policy, eligibility_policy, dict(adapter))
    config = conn.execute(
        """
        INSERT INTO deployment_configuration_versions(internal_deployment_id, version, campaign_id, elite_candidate_id, candidate_id, candidate_fingerprint, strategy_name, strategy_version, symbol, timeframe, frozen_configuration, approved_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """,
        (internal_deployment_id, next_version, deployment["campaign_id"], elite["id"], deployment["candidate_id"], fingerprint, deployment["strategy_name"], deployment["strategy_version"], deployment["symbol"], deployment["timeframe"], Jsonb(frozen), operator),
    ).fetchone()
    if current:
        close_epoch(conn, int(current["id"]), "reapproved" if reapprove else "reenabled")
        external = conn.execute("UPDATE external_paper_deployments SET state='enabled_observe_only', active_configuration_version_id=%s, approval_ref=%s, approved_at=NOW(), latest_blockers='[]'::jsonb, updated_at=NOW() WHERE id=%s RETURNING *", (config["id"], f"cli:{operator}:{trace_id}", current["id"])).fetchone()
    else:
        external = conn.execute(
            """
            INSERT INTO external_paper_deployments(internal_deployment_id, broker_account_id, campaign_id, elite_candidate_id, candidate_id, strategy_version, symbol, timeframe, state, active_configuration_version_id, approval_ref, approved_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'enabled_observe_only',%s,%s,NOW()) RETURNING *
            """,
            (internal_deployment_id, account["id"], deployment["campaign_id"], elite["id"], deployment["candidate_id"], deployment["strategy_version"], deployment["symbol"], deployment["timeframe"], config["id"], f"cli:{operator}:{trace_id}"),
        ).fetchone()
    epoch = create_epoch(conn, dict(external), dict(config), risk_policy, eligibility_policy, dict(adapter), fingerprint, operator, latest_sync, latest_reconciliation)
    conn.execute("INSERT INTO external_deployment_transitions(external_deployment_id, execution_epoch_id, trace_id, from_state, to_state, reason_code, details, operator) VALUES (%s,%s,%s,%s,'enabled_observe_only',%s,%s,%s)", (external["id"], epoch["id"], trace_id, current["state"] if current else "disabled", "explicit_reapproval" if reapprove else "explicit_enable", Jsonb({"configuration_version": next_version}), operator))
    audit(conn, trace_id, "reapprove_external_paper" if reapprove else "enable_external_paper", operator, "after", broker_account_id=account["id"], external_deployment_id=external["id"], execution_epoch_id=epoch["id"], details={"state": "enabled_observe_only", "configuration_version": next_version})
    conn.commit()
    return {"deployment": dict(external), "configuration": dict(config), "epoch": dict(epoch), "trace_id": str(trace_id), "execution_enabled": False}


def disable_external_deployment(conn: psycopg.Connection, external_deployment_id: int, *, operator: str) -> dict[str, Any]:
    trace_id = uuid4()
    row = conn.execute("SELECT * FROM external_paper_deployments WHERE id=%s FOR UPDATE", (external_deployment_id,)).fetchone()
    if not row:
        raise ValueError("external deployment not found")
    audit(conn, trace_id, "disable_external_paper", operator, "before", broker_account_id=row["broker_account_id"], external_deployment_id=row["id"])
    close_epoch(conn, external_deployment_id, "explicit_disable")
    updated = conn.execute("UPDATE external_paper_deployments SET state='disabled', updated_at=NOW() WHERE id=%s RETURNING *", (external_deployment_id,)).fetchone()
    audit(conn, trace_id, "disable_external_paper", operator, "after", broker_account_id=row["broker_account_id"], external_deployment_id=row["id"], details={"state": "disabled"})
    conn.commit()
    return {"deployment": dict(updated), "trace_id": str(trace_id)}


def resume_observe_only(conn: psycopg.Connection, external_deployment_id: int, *, operator: str) -> dict[str, Any]:
    assert_execution_disabled()
    row = conn.execute("SELECT * FROM external_paper_deployments WHERE id=%s FOR UPDATE", (external_deployment_id,)).fetchone()
    if not row:
        raise ValueError("external deployment not found")
    blockers = conn.execute("SELECT * FROM execution_halts WHERE cleared_at IS NULL AND reason_code <> 'manual_halt' AND ((scope_type='deployment' AND scope_key=%s) OR (scope_type='account' AND scope_key=%s) OR scope_type='global')", (str(row["id"]), str(row["broker_account_id"]))).fetchall()
    if blockers:
        raise ValueError("cannot resume while execution halts remain unresolved")
    conn.execute("UPDATE execution_halts SET cleared_at=NOW(), cleared_by=%s, clearance_reason='explicit_cli_resume' WHERE cleared_at IS NULL AND reason_code='manual_halt' AND scope_type='deployment' AND scope_key=%s", (operator, str(row["id"])))
    return enable_observe_only(conn, int(row["internal_deployment_id"]), operator=operator, reapprove=True)


def manual_halt(conn: psycopg.Connection, external_deployment_id: int, *, operator: str, reason: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM external_paper_deployments WHERE id=%s FOR UPDATE", (external_deployment_id,)).fetchone()
    if not row:
        raise ValueError("external deployment not found")
    trace_id = uuid4()
    audit(conn, trace_id, "manual_halt", operator, "before", broker_account_id=row["broker_account_id"], external_deployment_id=row["id"], details={"reason": reason})
    halt = upsert_halt(conn, trace_id, "deployment", str(row["id"]), "manual_halt", {"reason": reason, "operator": operator})
    conn.execute("UPDATE external_paper_deployments SET state='manually_halted', latest_blockers=%s, updated_at=NOW() WHERE id=%s", (Jsonb(["manual_halt"]), row["id"]))
    audit(conn, trace_id, "manual_halt", operator, "after", broker_account_id=row["broker_account_id"], external_deployment_id=row["id"], details={"halt_id": halt["id"], "state": "manually_halted"})
    conn.commit()
    return {"halt": halt, "trace_id": str(trace_id), "state": "manually_halted"}


def validate_adapter_compatibility(conn: psycopg.Connection, *, operator: str) -> dict[str, Any]:
    releases = conn.execute("SELECT * FROM broker_adapter_releases WHERE provider='alpaca' ORDER BY created_at DESC LIMIT 2").fetchall()
    if not releases:
        raise ValueError("no persisted adapter release exists")
    current = dict(releases[0])
    previous = dict(releases[1]) if len(releases) > 1 else current
    status = "passed" if current["change_class"] == "compatible_patch" and current["adapter_contract_version"] == previous["adapter_contract_version"] and current["normalization_version"] == previous["normalization_version"] and current["behavior_version"] == previous["behavior_version"] else "blocked"
    trace_id = uuid4()
    comparison = {"from": previous["adapter_version"], "to": current["adapter_version"], "change_class": current["change_class"], "contract_match": current["adapter_contract_version"] == previous["adapter_contract_version"], "normalization_match": current["normalization_version"] == previous["normalization_version"], "behavior_match": current["behavior_version"] == previous["behavior_version"]}
    row = conn.execute("INSERT INTO adapter_compatibility_validations(trace_id, from_release_id, to_release_id, status, comparison, validated_by) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *", (trace_id, previous["id"], current["id"], status, Jsonb(comparison), operator)).fetchone()
    if status != "passed":
        upsert_halt(conn, trace_id, "global", "alpaca-paper", "adapter_incompatible", comparison)
    conn.commit()
    return {"validation": dict(row), "comparison": comparison, "trace_id": str(trace_id)}


def evaluate_eligibility(conn: psycopg.Connection, external: dict[str, Any], epoch: dict[str, Any], sync_run_id: int, trace_id: UUID, *, bar_fresh: bool) -> dict[str, Any]:
    config = conn.execute("SELECT * FROM deployment_configuration_versions WHERE id=%s", (epoch["deployment_configuration_version_id"],)).fetchone()
    internal = conn.execute("SELECT * FROM strategy_deployments WHERE id=%s", (external["internal_deployment_id"],)).fetchone()
    elite = conn.execute("SELECT * FROM elite_research_candidates WHERE id=%s", (external["elite_candidate_id"],)).fetchone()
    candidate_object = candidate_object_for(conn, int(external["campaign_id"]), str(external["candidate_id"]))
    current_fingerprint = candidate_fingerprint(dict(internal), dict(elite), candidate_object)
    account = conn.execute("SELECT * FROM broker_account_state WHERE broker_account_id=%s", (external["broker_account_id"],)).fetchone()
    clock = conn.execute("SELECT * FROM broker_clock_state WHERE broker_account_id=%s", (external["broker_account_id"],)).fetchone()
    sync = conn.execute("SELECT * FROM broker_sync_runs WHERE id=%s", (sync_run_id,)).fetchone()
    reconciliation = conn.execute("SELECT * FROM broker_reconciliation_runs WHERE sync_run_id=%s", (sync_run_id,)).fetchone()
    halts = conn.execute("SELECT COUNT(*) AS count FROM execution_halts WHERE cleared_at IS NULL AND ((scope_type='deployment' AND scope_key=%s) OR (scope_type='asset' AND scope_key=%s) OR (scope_type='account' AND scope_key=%s) OR scope_type='global')", (str(external["id"]), external["symbol"], str(external["broker_account_id"]))).fetchone()
    checks = [
        check("DEPLOYMENT_OBSERVE_ONLY", external["state"] == "enabled_observe_only"),
        check("EPOCH_OPEN", epoch.get("closed_at") is None),
        check("CANDIDATE_FINGERPRINT_MATCH", current_fingerprint == config["candidate_fingerprint"]),
        check("FORWARD_START_STATE", str(elite["forward_validation_state"]) in STARTABLE_FORWARD_STATES),
        check("SYNC_COMPLETE", bool(sync and sync["status"] == "complete")),
        check("RECONCILIATION_CLEAN", bool(reconciliation and reconciliation["status"] == "clean")),
        check("ACCOUNT_HEALTHY", bool(account and not account["trading_blocked"] and not account["account_blocked"] and not account["trade_suspended_by_user"])),
        check("MARKET_OPEN", bool(clock and clock["is_open"])),
        check("BAR_FRESH_COMPLETE", bar_fresh),
        check("NO_ACTIVE_HALTS", int((halts or {}).get("count") or 0) == 0),
        check("SHADOW_FEATURE_ENABLED", settings.broker_shadow_execution_enabled),
        check("ORDER_SUBMISSION_DISABLED", not settings.broker_order_submission_enabled and not settings.external_paper_execution_enabled),
    ]
    eligible = all(item["passed"] for item in checks)
    phase = "forward_validated" if elite["forward_validation_state"] == "forward_validation_passed" else ("forward_validation_in_progress" if eligible else "blocked")
    row = conn.execute(
        """
        INSERT INTO eligibility_decisions(external_deployment_id, execution_epoch_id, trace_id, sync_run_id, eligibility_policy_version_id, risk_policy_version_id, deployment_configuration_version_id, adapter_release_id, eligible, operational_phase, checks, candidate_fingerprint)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """,
        (external["id"], epoch["id"], trace_id, sync_run_id, epoch["eligibility_policy_version_id"], epoch["risk_policy_version_id"], epoch["deployment_configuration_version_id"], epoch["adapter_release_id"], eligible, phase, Jsonb(checks), current_fingerprint),
    ).fetchone()
    if current_fingerprint != config["candidate_fingerprint"]:
        close_epoch(conn, int(external["id"]), "research_drift")
        conn.execute("UPDATE external_paper_deployments SET state='readiness_blocked', latest_blockers=%s, updated_at=NOW() WHERE id=%s", (Jsonb(["CANDIDATE_FINGERPRINT_MATCH"]), external["id"]))
    return dict(row)


def risk_decision(conn: psycopg.Connection, external: dict[str, Any], epoch: dict[str, Any], eligibility: dict[str, Any], trace_id: UUID, *, reference_price: Decimal, stop_price: Decimal) -> dict[str, Any]:
    policy_row = conn.execute("SELECT * FROM risk_policy_versions WHERE id=%s", (epoch["risk_policy_version_id"],)).fetchone()
    policy = dict(policy_row["policy"])
    allocated = Decimal(str(policy["allocated_capital"]))
    risk_per_share = reference_price - stop_price
    positions = conn.execute("SELECT * FROM broker_positions WHERE broker_account_id=%s AND quantity > 0", (external["broker_account_id"],)).fetchall()
    open_orders = conn.execute("SELECT * FROM broker_orders WHERE broker_account_id=%s AND status=ANY(%s)", (external["broker_account_id"], ["new", "accepted", "pending_new", "partially_filled"])).fetchall()
    exposure = sum((Decimal(str(row["market_value"])) for row in positions), Decimal("0"))
    losses = conn.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN closed_at >= CURRENT_DATE AND (evidence->>'realized_pnl') ~ '^-?[0-9]+([.][0-9]+)?$' THEN (evidence->>'realized_pnl')::numeric ELSE 0 END), 0) AS daily_pnl,
          COALESCE(SUM(CASE WHEN closed_at >= date_trunc('week', NOW()) AND (evidence->>'realized_pnl') ~ '^-?[0-9]+([.][0-9]+)?$' THEN (evidence->>'realized_pnl')::numeric ELSE 0 END), 0) AS weekly_pnl
        FROM external_paper_closed_trade_evidence
        WHERE broker_account_id=%s
        """,
        (external["broker_account_id"],),
    ).fetchone() or {"daily_pnl": 0, "weekly_pnl": 0}
    daily_pnl = Decimal(str(losses["daily_pnl"] or 0))
    weekly_pnl = Decimal(str(losses["weekly_pnl"] or 0))
    daily_limit = allocated * Decimal(str(policy["daily_loss_limit_pct"]))
    weekly_limit = allocated * Decimal(str(policy["weekly_loss_limit_pct"]))
    max_risk = allocated * Decimal(str(policy["max_risk_per_trade_pct"]))
    remaining_exposure = max(Decimal("0"), allocated * Decimal(str(policy["max_total_exposure_pct"])) - exposure)
    risk_qty = math.floor(max_risk / risk_per_share) if risk_per_share > 0 else 0
    exposure_qty = math.floor(remaining_exposure / reference_price) if reference_price > 0 else 0
    requested = max(0, min(risk_qty, exposure_qty))
    checks = [
        check("ELIGIBILITY_APPROVED", bool(eligibility["eligible"])),
        check("VALID_STOP_DISTANCE", risk_per_share > 0),
        check("WHOLE_SHARE_QUANTITY", requested >= 1),
        check("MAX_OPEN_POSITIONS", len(positions) < int(policy["max_open_positions"])),
        check("MAX_OPEN_ORDERS", len(open_orders) < int(policy["max_open_orders"])),
        check("DAILY_LOSS_LIMIT", daily_pnl > -daily_limit),
        check("WEEKLY_LOSS_LIMIT", weekly_pnl > -weekly_limit),
        check("TOTAL_EXPOSURE", remaining_exposure >= reference_price),
        check("BROKER_BUYING_POWER_IGNORED", policy.get("use_broker_buying_power") is False),
        check("ORDER_SUBMISSION_DISABLED", not settings.broker_order_submission_enabled and not settings.external_paper_execution_enabled),
    ]
    approved = all(item["passed"] for item in checks)
    approved_qty = requested if approved else 0
    expected_risk = Decimal(approved_qty) * max(Decimal("0"), risk_per_share)
    projected_exposure = exposure + Decimal(approved_qty) * reference_price
    row = conn.execute(
        """
        INSERT INTO execution_risk_decisions(external_deployment_id, execution_epoch_id, trace_id, eligibility_decision_id, risk_policy_version_id, eligibility_policy_version_id, deployment_configuration_version_id, adapter_release_id, approved, requested_quantity, approved_quantity, expected_risk, projected_exposure, checks)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """,
        (external["id"], epoch["id"], trace_id, eligibility["id"], epoch["risk_policy_version_id"], epoch["eligibility_policy_version_id"], epoch["deployment_configuration_version_id"], epoch["adapter_release_id"], approved, requested, approved_qty, expected_risk, projected_exposure, Jsonb(checks)),
    ).fetchone()
    if daily_pnl <= -daily_limit:
        upsert_halt(conn, trace_id, "account", str(external["broker_account_id"]), "daily_loss_limit", {"realized_pnl": str(daily_pnl), "limit": str(daily_limit), "risk_policy_version_id": epoch["risk_policy_version_id"]})
    if weekly_pnl <= -weekly_limit:
        upsert_halt(conn, trace_id, "account", str(external["broker_account_id"]), "weekly_loss_limit", {"realized_pnl": str(weekly_pnl), "limit": str(weekly_limit), "risk_policy_version_id": epoch["risk_policy_version_id"]})
    return dict(row)


def run_shadow_cycle(conn: psycopg.Connection, external_deployment_id: int, sync_run_id: int) -> dict[str, Any]:
    assert_execution_disabled()
    if not settings.broker_shadow_execution_enabled:
        return {"status": "disabled", "feature": "BROKER_SHADOW_EXECUTION_ENABLED"}
    trace_id = uuid4()
    external = conn.execute("SELECT * FROM external_paper_deployments WHERE id=%s FOR UPDATE", (external_deployment_id,)).fetchone()
    if not external or external["state"] != "enabled_observe_only":
        raise ValueError("shadow execution requires an observe-only deployment")
    epoch = conn.execute("SELECT * FROM external_execution_epochs WHERE external_deployment_id=%s AND closed_at IS NULL FOR UPDATE", (external_deployment_id,)).fetchone()
    if not epoch:
        raise ValueError("observe-only deployment has no open execution epoch")
    internal = conn.execute("SELECT * FROM strategy_deployments WHERE id=%s", (external["internal_deployment_id"],)).fetchone()
    candle = latest_candle(conn, internal["symbol"], internal["timeframe"])
    decision = evaluate_deployment_decision(conn, dict(internal))
    execution_key = f"{external_deployment_id}:{epoch['id']}:{internal['symbol']}:{internal['timeframe']}:{candle['timestamp'].isoformat()}:{decision.signal}"
    existing = conn.execute("SELECT * FROM external_execution_signals WHERE execution_key=%s", (execution_key,)).fetchone()
    if existing:
        return {"status": "duplicate_skipped", "signal": dict(existing), "trace_id": str(trace_id)}
    signal = conn.execute("INSERT INTO external_execution_signals(external_deployment_id, execution_epoch_id, trace_id, execution_key, symbol, timeframe, completed_bar_timestamp, signal_type, signal) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *", (external["id"], epoch["id"], trace_id, execution_key, internal["symbol"], internal["timeframe"], candle["timestamp"], decision.signal, Jsonb(decision_payload(decision)))).fetchone()
    eligibility = evaluate_eligibility(conn, dict(external), dict(epoch), sync_run_id, trace_id, bar_fresh=not candle_is_stale(candle) and bar_is_complete(candle["timestamp"], internal["timeframe"]))
    proposed = None
    risk = None
    would_submit = False
    reasons: list[str] = []
    if decision.signal == "setup" and decision.stop_loss is not None:
        reference_price = Decimal(str(candle["close"]))
        risk = risk_decision(conn, dict(external), dict(epoch), eligibility, trace_id, reference_price=reference_price, stop_price=Decimal(decision.stop_loss))
        if Decimal(risk["requested_quantity"]) > 0:
            client_order_id = f"keftrade-shadow-{hashlib.sha256(execution_key.encode()).hexdigest()[:24]}"
            proposed = conn.execute(
                """
                INSERT INTO proposed_broker_orders(external_deployment_id, execution_epoch_id, signal_id, eligibility_decision_id, risk_decision_id, trace_id, client_order_id, symbol, side, quantity, reference_price, stop_price, target_price, expected_risk)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'buy',%s,%s,%s,%s,%s) RETURNING *
                """,
                (external["id"], epoch["id"], signal["id"], eligibility["id"], risk["id"], trace_id, client_order_id, internal["symbol"], risk["requested_quantity"], reference_price, decision.stop_loss, decision.take_profit, risk["expected_risk"]),
            ).fetchone()
        would_submit = bool(risk["approved"])
        reasons = [item["code"] for item in risk["checks"] if not item["passed"]]
    else:
        reasons = ["NO_ACTIONABLE_SETUP"]
    shadow = conn.execute("INSERT INTO shadow_executions(external_deployment_id, execution_epoch_id, proposed_order_id, trace_id, would_submit, rejection_reasons, decision) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *", (external["id"], epoch["id"], proposed["id"] if proposed else None, trace_id, would_submit, Jsonb(reasons), Jsonb({"signal": decision_payload(decision), "eligibility_decision_id": eligibility["id"], "risk_decision_id": risk["id"] if risk else None, "broker_mutation": False}))).fetchone()
    audit(conn, trace_id, "shadow_execution", "broker_worker", "automatic", broker_account_id=external["broker_account_id"], external_deployment_id=external["id"], execution_epoch_id=epoch["id"], details={"shadow_execution_id": shadow["id"], "would_submit": would_submit, "broker_mutation": False})
    conn.commit()
    return {"status": "shadow_complete", "signal": dict(signal), "eligibility": eligibility, "risk": risk, "proposed_order": dict(proposed) if proposed else None, "shadow": dict(shadow), "trace_id": str(trace_id), "broker_mutation": False}


def candidate_object_for(conn: psycopg.Connection, campaign_id: int, candidate_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM research_candidate_objects WHERE candidate_id=%s AND campaign_ids @> %s ORDER BY updated_at DESC LIMIT 1", (candidate_id, Jsonb([campaign_id]))).fetchone()
    if not row:
        raise ValueError("authoritative persisted candidate object was not found")
    return dict(row)


def candidate_fingerprint(deployment: dict[str, Any], elite: dict[str, Any], candidate_object: dict[str, Any]) -> str:
    evidence = {
        "campaign_id": deployment.get("campaign_id"),
        "candidate_id": deployment.get("candidate_id"),
        "strategy_name": deployment.get("strategy_name"),
        "strategy_version": deployment.get("strategy_version"),
        "symbol": deployment.get("symbol"),
        "timeframe": deployment.get("timeframe"),
        "parameters": deployment.get("parameters") or {},
        "elite_state": elite.get("forward_validation_state"),
        "elite_validation_history": elite.get("validation_history") or {},
        "elite_thresholds": elite.get("forward_validation_thresholds") or {},
        "candidate_lineage": candidate_object.get("lineage") or {},
        "candidate_validation_history": candidate_object.get("validation_history") or [],
        "candidate_state": candidate_object.get("state"),
        "candidate_calculation_version": candidate_object.get("calculation_version"),
    }
    return hashlib.sha256(canonical_json(evidence).encode("utf-8")).hexdigest()


def frozen_configuration(deployment: dict[str, Any], elite: dict[str, Any], candidate_object: dict[str, Any], fingerprint: str, risk_policy: dict[str, Any], eligibility_policy: dict[str, Any], adapter: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_fingerprint": fingerprint,
        "strategy_name": deployment["strategy_name"],
        "strategy_version": deployment["strategy_version"],
        "parameters": deployment.get("parameters") or {},
        "symbol": deployment["symbol"],
        "timeframe": deployment["timeframe"],
        "forward_thresholds": elite.get("forward_validation_thresholds") or {},
        "lineage": candidate_object.get("lineage") or {},
        "risk_policy_version": risk_policy["version"],
        "eligibility_policy_version": eligibility_policy["version"],
        "adapter_version": adapter["adapter_version"],
        "adapter_contract_version": adapter["adapter_contract_version"],
        "normalization_version": adapter["normalization_version"],
        "order_submission_enabled": False,
    }


def create_epoch(conn: psycopg.Connection, external: dict[str, Any], config: dict[str, Any], risk_policy: dict[str, Any], eligibility_policy: dict[str, Any], adapter: dict[str, Any], fingerprint: str, operator: str, sync: dict[str, Any], reconciliation: dict[str, Any]) -> dict[str, Any]:
    sequence = int((conn.execute("SELECT COALESCE(MAX(sequence_number),0)+1 AS sequence FROM external_execution_epochs WHERE external_deployment_id=%s", (external["id"],)).fetchone() or {}).get("sequence") or 1)
    row = conn.execute(
        """
        INSERT INTO external_execution_epochs(external_deployment_id, sequence_number, deployment_configuration_version_id, eligibility_policy_version_id, risk_policy_version_id, adapter_release_id, candidate_fingerprint, activation_operator, feature_flags, starting_state)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """,
        (external["id"], sequence, config["id"], eligibility_policy["id"], risk_policy["id"], adapter["id"], fingerprint, operator, Jsonb(feature_flags()), Jsonb({"sync_run_id": sync["id"], "reconciliation_run_id": reconciliation["id"], "allocated_capital": settings.broker_allocated_capital})),
    ).fetchone()
    return dict(row)


def close_epoch(conn: psycopg.Connection, external_deployment_id: int, reason: str) -> None:
    conn.execute("UPDATE external_execution_epochs SET closed_at=NOW(), closing_state='closed', closing_reason=%s WHERE external_deployment_id=%s AND closed_at IS NULL", (reason, external_deployment_id))


def audit(conn: psycopg.Connection, trace_id: UUID, event_type: str, operator: str, phase: str, *, broker_account_id: int | None = None, external_deployment_id: int | None = None, execution_epoch_id: int | None = None, details: dict[str, Any] | None = None) -> None:
    conn.execute("INSERT INTO broker_audit_events(trace_id, event_type, operator, phase, broker_account_id, external_deployment_id, execution_epoch_id, details) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", (trace_id, event_type, operator, phase, broker_account_id, external_deployment_id, execution_epoch_id, Jsonb(details or {})))


def check(code: str, passed: bool) -> dict[str, Any]:
    return {"code": code, "passed": bool(passed)}


def assert_execution_disabled() -> None:
    if settings.broker_order_submission_enabled or settings.external_paper_execution_enabled:
        raise RuntimeError("broker execution flags must remain disabled during Phase 10 first pass")


def bar_is_complete(opened_at: datetime, timeframe: str, now: datetime | None = None) -> bool:
    seconds = {"15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}.get(timeframe)
    if seconds is None:
        return False
    parsed = opened_at if opened_at.tzinfo else opened_at.replace(tzinfo=UTC)
    return parsed + timedelta(seconds=seconds) <= (now or datetime.now(UTC))
