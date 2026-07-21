from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb

from app.services.evidence_alerts import (
    DISCLAIMER,
    candle_is_stale,
    current_regime,
    evidence_conditions_pass,
    evidence_failure_reasons,
    latest_feature,
    research_metrics_for_deployment,
    tsla_momentum_bull_rules,
)
from app.services.features import load_candles
from app.services.strategy import StrategyDecision, get_strategy_definition
from app.services.strategy_diagnostics import enrich_decision
from app.services.strategy_research import PAPER_READY_THRESHOLDS, finite_metric

SIGNAL_REVIEW_DISCLAIMER = "Research-only setup review. Not financial advice. No trade is executed."


def generate_signal_review(conn: psycopg.Connection, deployment: dict[str, Any]) -> dict[str, Any]:
    if not deployment.get("simulation_only"):
        raise ValueError("signal reviews require a simulation-only deployment")
    if deployment.get("symbol") != "TSLA" or deployment.get("timeframe") != "1h":
        raise ValueError("signal review currently supports TSLA 1h only")
    if deployment.get("strategy_name") != "momentum" or deployment.get("strategy_version") != "bull_v2":
        raise ValueError("signal review currently supports momentum_bull_v2_007 only")

    candles = load_candles(conn, symbol=deployment["symbol"], timeframe=deployment["timeframe"])
    if not candles:
        raise ValueError("No candles available for signal review")

    candle = candles[-1]
    strategy = get_strategy_definition(deployment["strategy_name"], deployment["strategy_version"])
    params = {**strategy.parameters, **dict(deployment.get("parameters") or {})}
    strategy_id = f"{deployment['strategy_name']}_{deployment['strategy_version']}_007"
    timestamp = candle["timestamp"]
    data_freshness = freshness_label(timestamp)

    if candle_is_stale(candle):
        return upsert_signal_review(
            conn,
            deployment,
            {
                "symbol": deployment["symbol"],
                "timeframe": deployment["timeframe"],
                "strategy_id": strategy_id,
                "status": "Stale Data Blocked",
                "verdict": "Stale Data Blocked",
                "regime": "unknown",
                "evidence_score": "0/1",
                "matched_rules": [],
                "failed_rules": ["Stored candle freshness is outside the expected monitoring window."],
                "latest_candle_timestamp": timestamp,
                "data_freshness": data_freshness,
                "max_holding_bars": int(params.get("max_holding_bars") or 0),
            },
        )

    feature = latest_feature(conn, deployment["symbol"], deployment["timeframe"], timestamp)
    if not feature:
        return upsert_signal_review(
            conn,
            deployment,
            {
                "symbol": deployment["symbol"],
                "timeframe": deployment["timeframe"],
                "strategy_id": strategy_id,
                "status": "No Setup",
                "verdict": "No Setup",
                "regime": "unknown",
                "evidence_score": "0/1",
                "matched_rules": [],
                "failed_rules": ["Feature data is unavailable for the latest candle."],
                "latest_candle_timestamp": timestamp,
                "data_freshness": data_freshness,
                "max_holding_bars": int(params.get("max_holding_bars") or 0),
            },
        )

    decision = enrich_decision(strategy.decide(candle, feature, candles, params), candle, feature, candles, params)
    metrics = safe_research_metrics(conn, deployment)
    rule_result = tsla_momentum_bull_rules(candle, feature, decision, params)
    regime = rule_result["regime"] or current_regime(candle, feature)
    evidence_failures = evidence_failure_reasons(metrics, regime)
    evidence_ready = evidence_conditions_pass(metrics, regime)
    position_quantity = current_position_quantity(conn, int(deployment["account_id"]), deployment["symbol"])
    status, verdict = review_status(decision, rule_result, evidence_ready, evidence_failures, position_quantity)
    levels = setup_levels(candle, decision, params) if status in {"Setup Worth Reviewing", "In Paper Position", "Setup Forming"} else {}
    failed_rules = [*rule_result["failed"], *evidence_failures]

    return upsert_signal_review(
        conn,
        deployment,
        {
            "symbol": deployment["symbol"],
            "timeframe": deployment["timeframe"],
            "strategy_id": strategy_id,
            "status": status,
            "verdict": verdict,
            "regime": regime,
            "evidence_score": f"{len(rule_result['matched'])}/{len(rule_result['matched']) + len(failed_rules)}",
            "matched_rules": rule_result["matched"],
            "failed_rules": failed_rules or ["No active setup review alert fired."],
            "profit_factor": decimal_or_none(metrics.get("profit_factor")),
            "expectancy": decimal_or_none(metrics.get("expectancy_per_trade")),
            "trade_count": int(metrics["number_of_trades"]) if metrics.get("number_of_trades") is not None else None,
            "max_drawdown": decimal_or_none(metrics.get("max_drawdown")),
            "latest_candle_timestamp": timestamp,
            "data_freshness": data_freshness,
            "max_holding_bars": int(params.get("max_holding_bars") or 0),
            **levels,
        },
    )


def review_status(
    decision: StrategyDecision,
    rule_result: dict[str, Any],
    evidence_ready: bool,
    evidence_failures: list[str],
    position_quantity: Decimal,
) -> tuple[str, str]:
    if position_quantity > 0 and decision.signal != "setup":
        return "Exit Risk Worth Reviewing", "Exit Risk Worth Reviewing"
    if position_quantity > 0:
        return "In Paper Position", "Setup Worth Reviewing"
    if decision.signal == "setup" and rule_result["passed"] and evidence_ready:
        return "Setup Worth Reviewing", "Setup Worth Reviewing"
    if decision.signal == "setup" and (rule_result["matched"] or not evidence_failures):
        return "Setup Forming", "No Setup"
    return "No Setup", "No Setup"


def setup_levels(candle: dict[str, Any], decision: StrategyDecision, params: dict[str, Any]) -> dict[str, Any]:
    if decision.stop_loss is None or decision.take_profit is None:
        return {}
    entry = Decimal(candle["close"])
    invalidation = Decimal(decision.stop_loss)
    target = Decimal(decision.take_profit)
    if not (invalidation < entry < target):
        return {}
    risk = entry - invalidation
    reward = target - entry
    if risk <= 0 or reward <= 0:
        return {}
    return {
        "possible_entry_price": entry,
        "invalidation_level": invalidation,
        "risk_target": target,
        "exit_zone": target,
        "risk_per_share": risk,
        "reward_per_share": reward,
        "risk_reward_ratio": reward / risk,
        "max_holding_bars": int(params.get("max_holding_bars") or 0),
    }


def upsert_signal_review(conn: psycopg.Connection, deployment: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute(
        """
        INSERT INTO signal_reviews(
            account_id, deployment_id, symbol, timeframe, strategy_id, status, verdict, regime,
            evidence_score, matched_rules, failed_rules, profit_factor, expectancy, trade_count,
            max_drawdown, latest_candle_timestamp, data_freshness, possible_entry_price,
            invalidation_level, risk_target, exit_zone, risk_per_share, reward_per_share,
            risk_reward_ratio, max_holding_bars, simulation_only
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
        ON CONFLICT (deployment_id, latest_candle_timestamp)
        DO UPDATE SET
            status = EXCLUDED.status,
            verdict = EXCLUDED.verdict,
            regime = EXCLUDED.regime,
            evidence_score = EXCLUDED.evidence_score,
            matched_rules = EXCLUDED.matched_rules,
            failed_rules = EXCLUDED.failed_rules,
            profit_factor = EXCLUDED.profit_factor,
            expectancy = EXCLUDED.expectancy,
            trade_count = EXCLUDED.trade_count,
            max_drawdown = EXCLUDED.max_drawdown,
            data_freshness = EXCLUDED.data_freshness,
            possible_entry_price = EXCLUDED.possible_entry_price,
            invalidation_level = EXCLUDED.invalidation_level,
            risk_target = EXCLUDED.risk_target,
            exit_zone = EXCLUDED.exit_zone,
            risk_per_share = EXCLUDED.risk_per_share,
            reward_per_share = EXCLUDED.reward_per_share,
            risk_reward_ratio = EXCLUDED.risk_reward_ratio,
            max_holding_bars = EXCLUDED.max_holding_bars,
            updated_at = NOW()
        RETURNING *
        """,
        (
            int(deployment["account_id"]),
            int(deployment["id"]),
            payload["symbol"],
            payload["timeframe"],
            payload["strategy_id"],
            payload["status"],
            payload["verdict"],
            payload.get("regime"),
            payload["evidence_score"],
            Jsonb(jsonable_encoder(payload.get("matched_rules") or [])),
            Jsonb(jsonable_encoder(payload.get("failed_rules") or [])),
            payload.get("profit_factor"),
            payload.get("expectancy"),
            payload.get("trade_count"),
            payload.get("max_drawdown"),
            payload.get("latest_candle_timestamp"),
            payload["data_freshness"],
            payload.get("possible_entry_price"),
            payload.get("invalidation_level"),
            payload.get("risk_target"),
            payload.get("exit_zone"),
            payload.get("risk_per_share"),
            payload.get("reward_per_share"),
            payload.get("risk_reward_ratio"),
            payload.get("max_holding_bars"),
        ),
    ).fetchone()
    conn.commit()
    return with_disclaimer(dict(row))


def list_signal_reviews(conn: psycopg.Connection, account_id: int | None = None, limit: int = 25) -> list[dict[str, Any]]:
    if account_id is not None:
        rows = conn.execute(
            "SELECT * FROM signal_reviews WHERE account_id = %s ORDER BY created_at DESC LIMIT %s",
            (account_id, limit),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM signal_reviews ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
    return [with_disclaimer(dict(row)) for row in rows]


def latest_signal_review(conn: psycopg.Connection, account_id: int | None = None) -> dict[str, Any] | None:
    rows = list_signal_reviews(conn, account_id=account_id, limit=1)
    return rows[0] if rows else None


def mark_signal_review(conn: psycopg.Connection, review_id: int, action: str, note: str | None = None) -> dict[str, Any]:
    if action == "reviewed":
        set_clause = "reviewed_at = NOW()"
        message = "Signal review was manually marked reviewed."
    elif action == "ignored":
        set_clause = "ignored_at = NOW()"
        message = "Signal review was manually ignored."
    elif action == "sent_to_paper_simulation":
        set_clause = "sent_to_paper_simulation_at = NOW()"
        message = "Signal review was sent to the internal paper simulation queue. No order was created."
    else:
        raise ValueError("unsupported signal review action")
    row = conn.execute(
        f"""
        UPDATE signal_reviews
        SET {set_clause},
            note = COALESCE(%s, note),
            updated_at = NOW()
        WHERE id = %s
          AND simulation_only = TRUE
        RETURNING *
        """,
        (note, review_id),
    ).fetchone()
    if not row:
        raise ValueError("signal review not found")
    row_dict = dict(row)
    log_signal_review_action(conn, row_dict, action, message)
    conn.commit()
    return with_disclaimer(row_dict)


def add_signal_review_note(conn: psycopg.Connection, review_id: int, note: str) -> dict[str, Any]:
    row = conn.execute(
        """
        UPDATE signal_reviews
        SET note = %s,
            updated_at = NOW()
        WHERE id = %s
          AND simulation_only = TRUE
        RETURNING *
        """,
        (note, review_id),
    ).fetchone()
    if not row:
        raise ValueError("signal review not found")
    row_dict = dict(row)
    log_signal_review_action(conn, row_dict, "note_added", "Signal review note was saved.")
    conn.commit()
    return with_disclaimer(row_dict)


def log_signal_review_action(conn: psycopg.Connection, review: dict[str, Any], action: str, message: str) -> None:
    conn.execute(
        """
        INSERT INTO execution_logs(account_id, deployment_id, order_id, event_type, message, payload, simulation_only)
        VALUES (%s, %s, NULL, %s, %s, %s, TRUE)
        """,
        (
            review.get("account_id"),
            review.get("deployment_id"),
            f"signal_review_{action}",
            message,
            Jsonb(jsonable_encoder({"signal_review_id": review["id"], "simulation_only": True, "disclaimer": SIGNAL_REVIEW_DISCLAIMER})),
        ),
    )


def current_position_quantity(conn: psycopg.Connection, account_id: int, symbol: str) -> Decimal:
    row = conn.execute(
        "SELECT quantity FROM paper_positions WHERE account_id = %s AND symbol = %s",
        (account_id, symbol),
    ).fetchone()
    if not row:
        return Decimal("0")
    return Decimal(row.get("quantity") or 0)


def safe_research_metrics(conn: psycopg.Connection, deployment: dict[str, Any]) -> dict[str, Any]:
    selected = selected_candidate_metrics(conn, deployment)
    if selected:
        return selected
    try:
        return research_metrics_for_deployment(conn, deployment)
    except Exception:
        return {}


def selected_candidate_metrics(conn: psycopg.Connection, deployment: dict[str, Any]) -> dict[str, Any] | None:
    strategy_ids = [
        f"{deployment['strategy_name']}_{deployment['strategy_version']}_007",
        f"{deployment['strategy_name']}_{deployment['strategy_version']}",
    ]
    rows = conn.execute(
        """
        SELECT profit_factor, expectancy, trade_count, max_drawdown, created_at
        FROM evidence_alerts
        WHERE symbol = %s
          AND timeframe = %s
          AND strategy_id = ANY(%s)
          AND profit_factor IS NOT NULL
          AND expectancy IS NOT NULL
          AND trade_count IS NOT NULL
          AND max_drawdown IS NOT NULL
          AND simulation_only = TRUE
        ORDER BY created_at DESC
        LIMIT 25
        """,
        (deployment["symbol"], deployment["timeframe"], strategy_ids),
    ).fetchall()
    for row in rows:
        metrics = {
            "profit_factor": row["profit_factor"],
            "expectancy_per_trade": row["expectancy"],
            "number_of_trades": row["trade_count"],
            "max_drawdown": row["max_drawdown"],
        }
        if candidate_metrics_pass(metrics):
            return metrics
    return None


def candidate_metrics_pass(metrics: dict[str, Any]) -> bool:
    return (
        finite_metric(metrics.get("profit_factor")) >= PAPER_READY_THRESHOLDS["profit_factor"]
        and finite_metric(metrics.get("expectancy_per_trade")) > PAPER_READY_THRESHOLDS["expectancy_per_trade"]
        and finite_metric(metrics.get("number_of_trades")) >= PAPER_READY_THRESHOLDS["number_of_trades"]
        and finite_metric(metrics.get("max_drawdown")) <= PAPER_READY_THRESHOLDS["max_drawdown"]
    )


def freshness_label(timestamp: Any) -> str:
    if not timestamp:
        return "No candle timestamp"
    parsed = timestamp
    if isinstance(parsed, str):
        parsed = datetime.fromisoformat(parsed.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    age_hours = max(0, (datetime.now(UTC) - parsed).total_seconds() / 3600)
    if age_hours < 48:
        return f"{age_hours:.1f}h old"
    return f"{age_hours / 24:.1f}d old"


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def with_disclaimer(row: dict[str, Any]) -> dict[str, Any]:
    row["disclaimer"] = SIGNAL_REVIEW_DISCLAIMER
    row["alert_disclaimer"] = DISCLAIMER
    return row
