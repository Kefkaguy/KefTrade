from __future__ import annotations

from collections import Counter
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from app.services.strategy import StrategyDecision, calculate_ema_from_candles


DECISION_VERSION = "structured-gates-v1"


def enrich_decision(
    decision: StrategyDecision,
    candle: dict[str, Any],
    feature: dict[str, Any],
    recent_candles: list[dict[str, Any]],
    params: dict[str, Any],
) -> StrategyDecision:
    """Attach non-mutating, measurable diagnostics to an existing strategy result.

    Signal semantics stay owned by the frozen strategy. The diagnostic layer reports
    every independently measurable configured rule and never turns an avoid into a setup.
    """
    gates: list[dict[str, Any]] = []
    close = number(candle.get("close"))
    fast_period = integer(params.get("ema_fast") or params.get("trend_fast"))
    slow_period = integer(params.get("ema_slow") or params.get("trend_slow"))
    fast = ema_value(feature, recent_candles, fast_period)
    slow = ema_value(feature, recent_candles, slow_period)

    if slow_period:
        gates.append(compare_gate("TREND_PRICE_ABOVE_SLOW", "trend", close, ">", slow, f"Price must be above EMA{slow_period}."))
    if fast_period and slow_period:
        gates.append(compare_gate("TREND_FAST_ABOVE_SLOW", "trend", fast, ">", slow, f"EMA{fast_period} must be above EMA{slow_period}."))

    rsi = number(feature.get("rsi_14"))
    rsi_min = number(params.get("rsi_min") or params.get("pullback_rsi_min"))
    rsi_max = number(params.get("rsi_max") or params.get("pullback_rsi_max"))
    if rsi_min is not None or rsi_max is not None:
        gates.append(range_gate("MOMENTUM_RSI_RANGE", "momentum", rsi, rsi_min, rsi_max, "RSI must be within the configured range."))

    volume_min = number(params.get("volume_change_min"))
    if volume_min is not None:
        gates.append(compare_gate("VOLUME_CHANGE_MIN", "volume", number(feature.get("volume_change")), ">=", volume_min, "Volume change must meet its configured minimum."))

    distance_max = number(params.get("entry_distance_to_ema20_max") or params.get("entry_distance_max"))
    if distance_max is not None:
        distance = abs(close / fast - Decimal("1")) if close is not None and fast not in {None, Decimal("0")} else None
        gates.append(compare_gate("ENTRY_DISTANCE_MAX", "entry", distance, "<=", distance_max, "Price must be close enough to the fast EMA."))

    returns_min = number(params.get("returns_5_min") or params.get("momentum_short_min"))
    if returns_min is not None:
        gates.append(compare_gate("MOMENTUM_RETURN_MIN", "momentum", number(feature.get("returns_5")), ">=", returns_min, "Return momentum must meet its configured minimum."))

    volatility_min = number(params.get("volatility_min"))
    if volatility_min is not None:
        gates.append(compare_gate("VOLATILITY_MIN", "volatility", number(feature.get("volatility_20")), ">=", volatility_min, "Volatility must meet its configured minimum."))

    if decision.stop_loss is not None and close is not None:
        gates.append(compare_gate("STOP_BELOW_ENTRY", "stop_geometry", number(decision.stop_loss), "<", close, "The long stop must be below the entry reference price."))
    else:
        gates.append(not_evaluated_gate("STOP_BELOW_ENTRY", "stop_geometry", "No stop was produced because entry prerequisites did not pass."))

    # Preserve useful strategy-specific messages without pretending that unparsed text
    # contains a numeric threshold.
    for index, explanation in enumerate(decision.explanation):
        if any(token in explanation.lower() for token in ("failed", "not enough", "wait for", "lacks", "invalid")):
            gates.append({
                "code": f"STRATEGY_REASON_{index + 1}",
                "group": "strategy",
                "status": "failed" if decision.signal != "setup" else "passed",
                "actual": None,
                "required": None,
                "reason": explanation,
            })

    if not gates:
        gates.append({"code": "STRATEGY_SIGNAL", "group": "strategy", "status": "passed" if decision.signal == "setup" else "failed", "actual": decision.signal, "required": "setup", "reason": decision.explanation[-1] if decision.explanation else "Strategy returned no explanation."})

    regime = {
        key: serializable(feature.get(key))
        for key in ("trend_regime", "volatility_regime", "trend_strength", "market_regime")
        if feature.get(key) is not None
    }
    return replace(decision, decision_version=DECISION_VERSION, gates=gates, regime=regime)


def persist_strategy_evaluation(
    conn: psycopg.Connection,
    *,
    internal_deployment_id: int,
    decision: StrategyDecision,
    candle: dict[str, Any],
    trace_id: UUID,
    external_deployment_id: int | None = None,
    execution_epoch_id: int | None = None,
    configuration_fingerprint: str | None = None,
) -> dict[str, Any]:
    payload = decision_payload(decision)
    row = conn.execute(
        """
        INSERT INTO strategy_evaluations(
            internal_deployment_id, external_deployment_id, execution_epoch_id, trace_id,
            decision_version, configuration_fingerprint, symbol, timeframe,
            completed_bar_timestamp, signal_type, regime, gates, decision
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(internal_deployment_id, completed_bar_timestamp, decision_version) DO NOTHING
        RETURNING *
        """,
        (
            internal_deployment_id, external_deployment_id, execution_epoch_id, trace_id,
            decision.decision_version, configuration_fingerprint, candle["symbol"], candle["timeframe"],
            candle["timestamp"], decision.signal, Jsonb(decision.regime), Jsonb(decision.gates), Jsonb(payload),
        ),
    ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT * FROM strategy_evaluations WHERE internal_deployment_id=%s AND completed_bar_timestamp=%s AND decision_version=%s",
            (internal_deployment_id, candle["timestamp"], decision.decision_version),
        ).fetchone()
    return dict(row)


def list_evaluations(conn: psycopg.Connection, *, deployment_id: int | None, limit: int, cursor: int | None) -> dict[str, Any]:
    clauses: list[str] = []
    values: list[Any] = []
    if deployment_id is not None:
        clauses.append("internal_deployment_id=%s")
        values.append(deployment_id)
    if cursor is not None:
        clauses.append("id < %s")
        values.append(cursor)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    values.append(limit + 1)
    rows = conn.execute(f"SELECT * FROM strategy_evaluations {where} ORDER BY id DESC LIMIT %s", values).fetchall()
    has_more = len(rows) > limit
    items = [dict(row) for row in rows[:limit]]
    return {"items": items, "next_cursor": items[-1]["id"] if has_more and items else None, "has_more": has_more}


def diagnostics_summary(conn: psycopg.Connection, *, deployment_id: int | None = None) -> dict[str, Any]:
    if deployment_id is None:
        rows = conn.execute("SELECT signal_type, regime, gates FROM strategy_evaluations ORDER BY id DESC LIMIT 5000").fetchall()
    else:
        rows = conn.execute("SELECT signal_type, regime, gates FROM strategy_evaluations WHERE internal_deployment_id=%s ORDER BY id DESC LIMIT 5000", (deployment_id,)).fetchall()
    signals = Counter(str(row["signal_type"]) for row in rows)
    failed = Counter()
    not_evaluated = 0
    supported_regime_rows = 0
    for row in rows:
        if dict(row.get("regime") or {}):
            supported_regime_rows += 1
        for gate in list(row.get("gates") or []):
            if gate.get("status") == "failed":
                failed[str(gate.get("code") or "UNKNOWN")] += 1
            elif gate.get("status") == "not_evaluated":
                not_evaluated += 1
    total = len(rows)
    setup_rate = signals.get("setup", 0) / total if total else 0.0
    dominant_code, dominant_count = failed.most_common(1)[0] if failed else (None, 0)
    dominant_rate = dominant_count / total if total else 0.0
    health = classify_health(total, signals.get("setup", 0), not_evaluated, supported_regime_rows, dominant_rate)
    score = health_score(total, setup_rate, not_evaluated, dominant_rate, supported_regime_rows)
    return {
        "evaluated": total,
        "signals": dict(signals),
        "setup_frequency": round(setup_rate, 6),
        "failed_gates": [{"code": code, "count": count, "rate": round(count / total, 6) if total else 0} for code, count in failed.most_common()],
        "most_common_rejection": dominant_code,
        "health": {"label": health, "score": score},
        "sample_limited_to": 5000,
    }


def elite_deployment_audit(conn: psycopg.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT e.id AS elite_id, e.campaign_id, e.candidate_id, e.strategy_name, e.strategy_version,
               e.forward_validation_state, e.promoted_to_paper_at,
               d.id AS internal_deployment_id, d.symbol, d.timeframe, d.status AS internal_status,
               x.id AS external_deployment_id, x.state AS external_state, x.latest_blockers,
               x.approval_ref, x.approved_at,
               ep.id AS execution_epoch_id, ep.closed_at AS epoch_closed_at
        FROM elite_research_candidates e
        LEFT JOIN LATERAL (
            SELECT * FROM strategy_deployments sd
            WHERE sd.campaign_id=e.campaign_id AND sd.candidate_id=e.candidate_id AND sd.simulation_only=TRUE
            ORDER BY sd.created_at DESC LIMIT 1
        ) d ON TRUE
        LEFT JOIN external_paper_deployments x ON x.internal_deployment_id=d.id
        LEFT JOIN LATERAL (
            SELECT * FROM external_execution_epochs xe
            WHERE xe.external_deployment_id=x.id ORDER BY xe.sequence_number DESC LIMIT 1
        ) ep ON TRUE
        WHERE e.simulation_only=TRUE
        ORDER BY e.research_score DESC, e.id
        """
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        blockers = list(item.get("latest_blockers") or [])
        if not item.get("internal_deployment_id"):
            blockers.append("INTERNAL_DEPLOYMENT_MISSING")
        elif item.get("internal_status") != "active":
            blockers.append("INTERNAL_DEPLOYMENT_INACTIVE")
        if not item.get("external_deployment_id"):
            blockers.append("EXTERNAL_APPROVAL_NOT_PERFORMED")
        if item.get("external_deployment_id") and not item.get("execution_epoch_id"):
            blockers.append("EXECUTION_EPOCH_MISSING")
        item["blockers"] = sorted(set(blockers))
        item["ready_for_observe_approval"] = bool(item.get("internal_deployment_id") and item.get("internal_status") == "active" and not item["blockers"])
        items.append(item)
    return {"items": items, "counts": {"elites": len(items), "internal_deployments": sum(bool(row.get("internal_deployment_id")) for row in items), "external_deployments": sum(bool(row.get("external_deployment_id")) for row in items)}}


def decision_payload(decision: StrategyDecision) -> dict[str, Any]:
    return {
        "signal": decision.signal,
        "entry_zone": [str(value) for value in decision.entry_zone] if decision.entry_zone else None,
        "stop_loss": str(decision.stop_loss) if decision.stop_loss is not None else None,
        "take_profit": str(decision.take_profit) if decision.take_profit is not None else None,
        "risk_reward": str(decision.risk_reward) if decision.risk_reward is not None else None,
        "explanation": decision.explanation,
        "decision_version": decision.decision_version,
        "gates": decision.gates,
        "regime": decision.regime,
    }


def compare_gate(code: str, group: str, actual: Decimal | None, comparator: str, required: Decimal | None, description: str) -> dict[str, Any]:
    if actual is None or required is None:
        return not_evaluated_gate(code, group, f"{description} Required input is unavailable.")
    passed = {">": actual > required, ">=": actual >= required, "<": actual < required, "<=": actual <= required}[comparator]
    relation = "passes" if passed else "fails"
    return {"code": code, "group": group, "status": "passed" if passed else "failed", "actual": str(actual), "required": f"{comparator} {required}", "reason": f"Actual {actual} {relation} requirement {comparator} {required}. {description}"}


def range_gate(code: str, group: str, actual: Decimal | None, minimum: Decimal | None, maximum: Decimal | None, description: str) -> dict[str, Any]:
    if actual is None:
        return not_evaluated_gate(code, group, f"{description} Required input is unavailable.")
    passed = (minimum is None or actual >= minimum) and (maximum is None or actual <= maximum)
    required = f"{minimum if minimum is not None else '-inf'} <= value <= {maximum if maximum is not None else 'inf'}"
    return {"code": code, "group": group, "status": "passed" if passed else "failed", "actual": str(actual), "required": required, "reason": f"Actual {actual} {'is' if passed else 'is not'} within {required}. {description}"}


def not_evaluated_gate(code: str, group: str, reason: str) -> dict[str, Any]:
    return {"code": code, "group": group, "status": "not_evaluated", "actual": None, "required": None, "reason": reason}


def ema_value(feature: dict[str, Any], candles: list[dict[str, Any]], period: int | None) -> Decimal | None:
    if not period:
        return None
    stored = number(feature.get(f"ema_{period}"))
    return stored if stored is not None else calculate_ema_from_candles(candles, period)


def number(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def integer(value: Any) -> int | None:
    try:
        return int(value) if value not in {None, ""} else None
    except (TypeError, ValueError):
        return None


def serializable(value: Any) -> Any:
    return str(value) if isinstance(value, Decimal) else value


def classify_health(total: int, setups: int, not_evaluated: int, regime_rows: int, dominant_rate: float) -> str:
    if total and not_evaluated / total > 1:
        return "broken"
    if total < 100:
        return "insufficient_data"
    if regime_rows / total < 0.2:
        return "market_inactive"
    if setups == 0:
        return "dead"
    if dominant_rate > 0.6:
        return "too_restrictive"
    if setups / total > 0.3:
        return "too_aggressive"
    return "healthy"


def health_score(total: int, setup_rate: float, not_evaluated: int, dominant_rate: float, regime_rows: int) -> int:
    if not total:
        return 0
    integrity = max(0.0, 1.0 - min(1.0, not_evaluated / max(1, total)))
    opportunity = min(1.0, setup_rate / 0.05) if setup_rate <= 0.05 else max(0.0, 1.0 - (setup_rate - 0.05) / 0.25)
    concentration = max(0.0, 1.0 - dominant_rate)
    regime = min(1.0, regime_rows / total)
    risk_viability = 1.0 if setup_rate > 0 else 0.0
    return round(100 * (0.25 * integrity + 0.30 * opportunity + 0.20 * concentration + 0.15 * regime + 0.10 * risk_viability))

