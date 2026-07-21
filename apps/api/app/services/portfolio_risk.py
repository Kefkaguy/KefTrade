from __future__ import annotations

from decimal import Decimal
from math import sqrt
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from app.settings import settings


OPEN_STATUSES = ["new", "accepted", "pending_new", "partially_filled"]


def evaluate_portfolio_risk(
    conn: psycopg.Connection,
    *,
    external: dict[str, Any],
    strategy_evaluation: dict[str, Any],
    model_decision: dict[str, Any] | None,
    trace_id: UUID,
    requested_risk_pct: Decimal,
) -> dict[str, Any]:
    account_id = int(external["broker_account_id"])
    symbol = str(external["symbol"])
    positions = conn.execute("SELECT * FROM broker_positions WHERE broker_account_id=%s AND quantity>0", (account_id,)).fetchall()
    orders = conn.execute("SELECT * FROM broker_orders WHERE broker_account_id=%s AND status=ANY(%s)", (account_id, OPEN_STATUSES)).fetchall()
    active_count = int((conn.execute("SELECT COUNT(*) AS count FROM external_paper_deployments WHERE broker_account_id=%s AND state IN ('enabled_observe_only','enabled_execution')", (account_id,)).fetchone() or {}).get("count") or 1)
    total_heat_limit = Decimal(str(settings.max_broker_total_exposure_pct))
    per_strategy = min(Decimal(str(settings.max_broker_risk_per_trade_pct)), total_heat_limit / Decimal(max(1, active_count)))
    prior_heat = Decimal(str((conn.execute("""
        SELECT COALESCE(SUM(p.allocated_risk_pct),0) AS heat
        FROM portfolio_risk_decisions p
        JOIN external_paper_deployments x ON x.id=p.external_deployment_id
        WHERE p.approved=TRUE AND p.created_at >= CURRENT_DATE AND x.broker_account_id=%s
    """, (account_id,)).fetchone() or {}).get("heat") or 0))
    duplicate_position = any(str(row["symbol"]) == symbol for row in positions)
    duplicate_order = any(str(row["symbol"]) == symbol for row in orders)
    prior_winner = conn.execute("""
        SELECT p.* FROM portfolio_risk_decisions p
        JOIN external_paper_deployments x ON x.id=p.external_deployment_id
        WHERE p.approved=TRUE AND p.symbol=%s AND p.created_at>=CURRENT_DATE AND x.broker_account_id=%s
        ORDER BY p.id LIMIT 1
    """, (symbol, account_id)).fetchone()
    correlations = [correlation_for_symbols(conn, symbol, str(row["symbol"])) for row in positions if str(row["symbol"]) != symbol]
    finite_correlations = [value for value in correlations if value is not None]
    correlation_max = max((abs(value) for value in finite_correlations), default=None)
    allocated = min(requested_risk_pct, per_strategy, max(Decimal("0"), total_heat_limit - prior_heat))
    checks = [
        check("STRATEGY_SETUP", strategy_evaluation.get("signal_type") == "setup"),
        check("MODEL_BOUND", model_decision is None or Decimal(str(model_decision.get("bounded_risk_pct") or 0)) <= Decimal("0.01")),
        check("NO_DUPLICATE_POSITION", not duplicate_position),
        check("NO_DUPLICATE_OPEN_ORDER", not duplicate_order),
        check("NO_PRIOR_SYMBOL_WINNER", prior_winner is None),
        check("CORRELATION_LIMIT", correlation_max is None or correlation_max <= float(settings.portfolio_correlation_limit)),
        check("PORTFOLIO_HEAT", allocated > 0 and prior_heat + allocated <= total_heat_limit),
    ]
    approved = all(item["passed"] for item in checks)
    row = conn.execute(
        """
        INSERT INTO portfolio_risk_decisions(
          external_deployment_id, strategy_evaluation_id, model_risk_decision_id, trace_id,
          symbol, approved, requested_risk_pct, allocated_risk_pct, portfolio_heat_pct,
          correlation_max, winner_external_deployment_id, checks, decision
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(strategy_evaluation_id) DO UPDATE SET checks=EXCLUDED.checks, decision=EXCLUDED.decision RETURNING *
        """,
        (
            external["id"], strategy_evaluation["id"], model_decision.get("id") if model_decision else None,
            trace_id, symbol, approved, requested_risk_pct, allocated if approved else Decimal("0"),
            prior_heat + (allocated if approved else Decimal("0")), correlation_max,
            prior_winner["external_deployment_id"] if prior_winner else external["id"], Jsonb(checks),
            Jsonb({"active_elites": active_count, "per_strategy_risk_cap": str(per_strategy), "same_symbol_policy": "first-ranked-wins", "correlations": finite_correlations}),
        ),
    ).fetchone()
    return dict(row)


def portfolio_readiness(conn: psycopg.Connection) -> dict[str, Any]:
    positions = [dict(row) for row in conn.execute("SELECT * FROM broker_positions WHERE quantity>0 ORDER BY symbol").fetchall()]
    orders = [dict(row) for row in conn.execute("SELECT * FROM broker_orders WHERE status=ANY(%s) ORDER BY submitted_at DESC", (OPEN_STATUSES,)).fetchall()]
    decisions = [dict(row) for row in conn.execute("SELECT * FROM portfolio_risk_decisions ORDER BY id DESC LIMIT 100").fetchall()]
    heat_row = conn.execute("SELECT COALESCE(SUM(allocated_risk_pct),0) AS heat FROM portfolio_risk_decisions WHERE approved=TRUE AND created_at>=CURRENT_DATE").fetchone()
    heat = Decimal(str((heat_row or {}).get("heat") or 0))
    return {"positions": positions, "open_orders": orders, "recent_decisions": decisions, "portfolio_heat_pct": heat, "heat_limit_pct": settings.max_broker_total_exposure_pct, "same_symbol_limit": 1, "correlation_limit": settings.portfolio_correlation_limit}


def correlation_for_symbols(conn: psycopg.Connection, left: str, right: str) -> float | None:
    rows = conn.execute(
        """
        WITH l AS (SELECT timestamp, close, lag(close) OVER (ORDER BY timestamp) AS prior FROM candles WHERE symbol=%s ORDER BY timestamp DESC LIMIT 61),
             r AS (SELECT timestamp, close, lag(close) OVER (ORDER BY timestamp) AS prior FROM candles WHERE symbol=%s ORDER BY timestamp DESC LIMIT 61)
        SELECT (l.close/l.prior)-1 AS lret, (r.close/r.prior)-1 AS rret FROM l JOIN r USING(timestamp)
        WHERE l.prior IS NOT NULL AND r.prior IS NOT NULL ORDER BY timestamp
        """,
        (left, right),
    ).fetchall()
    x = [float(row["lret"]) for row in rows]
    y = [float(row["rret"]) for row in rows]
    if len(x) < 20:
        return None
    xm, ym = sum(x) / len(x), sum(y) / len(y)
    numerator = sum((a - xm) * (b - ym) for a, b in zip(x, y, strict=True))
    denominator = sqrt(sum((a - xm) ** 2 for a in x) * sum((b - ym) ** 2 for b in y))
    return numerator / denominator if denominator else None


def check(code: str, passed: bool) -> dict[str, Any]:
    return {"code": code, "passed": bool(passed)}
