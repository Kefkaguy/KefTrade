from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb

from app.services.features import load_candles
from app.services.regimes import load_regimes, sync_market_regimes
from app.services.strategy import StrategyDecision, get_strategy_definition
from app.services.strategy_diagnostics import enrich_decision
from app.services.strategy_research import PAPER_READY_THRESHOLDS, finite_metric, run_strategy_research
from app.settings import settings

DISCLAIMER = "Research-only. Not financial advice. No trade is executed."
ALERT_TYPES = {
    "entry_setup_review",
    "exit_risk_review",
    "avoid_condition",
    "stale_data_warning",
    "scheduler_error",
    "duplicate_candle_skip",
    "evidence_drift_warning",
}
WEAK_REGIMES = {"bear_trend", "unknown"}


def create_evidence_alert(
    conn: psycopg.Connection,
    *,
    symbol: str,
    timeframe: str,
    strategy_id: str,
    alert_type: str,
    severity: str,
    verdict: str,
    evidence_summary: str,
    matched_rules: list[str] | None = None,
    failed_rules: list[str] | None = None,
    profit_factor: Any = None,
    expectancy: Any = None,
    trade_count: Any = None,
    max_drawdown: Any = None,
    regime: str | None = None,
    candle_timestamp: Any = None,
) -> dict[str, Any]:
    if alert_type not in ALERT_TYPES:
        raise ValueError(f"Unsupported evidence alert type: {alert_type}")
    existing = conn.execute(
        """
        SELECT *
        FROM evidence_alerts
        WHERE symbol = %s
          AND timeframe = %s
          AND strategy_id = %s
          AND alert_type = %s
          AND candle_timestamp IS NOT DISTINCT FROM %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (symbol.upper(), timeframe, strategy_id, alert_type, candle_timestamp),
    ).fetchone()
    if existing:
        return dict(existing)
    summary = evidence_summary if DISCLAIMER in evidence_summary else f"{evidence_summary} {DISCLAIMER}"
    row = conn.execute(
        """
        INSERT INTO evidence_alerts(
            symbol, timeframe, strategy_id, alert_type, severity, verdict, evidence_summary,
            matched_rules, failed_rules, profit_factor, expectancy, trade_count, max_drawdown,
            regime, candle_timestamp, simulation_only
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
        RETURNING *
        """,
        (
            symbol.upper(),
            timeframe,
            strategy_id,
            alert_type,
            severity,
            verdict,
            summary,
            Jsonb(jsonable_encoder(matched_rules or [])),
            Jsonb(jsonable_encoder(failed_rules or [])),
            decimal_or_none(profit_factor),
            decimal_or_none(expectancy),
            int(trade_count) if trade_count is not None else None,
            decimal_or_none(max_drawdown),
            regime,
            candle_timestamp,
        ),
    ).fetchone()
    return dict(row)


def list_evidence_alerts(conn: psycopg.Connection, limit: int = 100, include_acknowledged: bool = True) -> list[dict[str, Any]]:
    if include_acknowledged:
        return list(conn.execute("SELECT * FROM evidence_alerts ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall())
    return list(conn.execute("SELECT * FROM evidence_alerts WHERE acknowledged_at IS NULL ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall())


def acknowledge_evidence_alert(conn: psycopg.Connection, alert_id: int) -> dict[str, Any]:
    row = conn.execute(
        "UPDATE evidence_alerts SET acknowledged_at = NOW() WHERE id = %s RETURNING *",
        (alert_id,),
    ).fetchone()
    if not row:
        raise ValueError("evidence alert not found")
    conn.commit()
    return dict(row)


def detect_paper_scan_alert(
    conn: psycopg.Connection,
    *,
    deployment: dict[str, Any],
    decision: StrategyDecision | dict[str, Any],
    candle: dict[str, Any],
    action: str,
    message: str,
    duplicate_candle: bool = False,
) -> dict[str, Any] | None:
    symbol = deployment["symbol"]
    timeframe = deployment["timeframe"]
    strategy_id = f"{deployment['strategy_name']}_{deployment['strategy_version']}"
    candle_timestamp = candle.get("timestamp")
    if duplicate_candle:
        duplicate_alert = create_evidence_alert(
            conn,
            symbol=symbol,
            timeframe=timeframe,
            strategy_id=strategy_id,
            alert_type="duplicate_candle_skip",
            severity="info",
            verdict="No Setup",
            evidence_summary="Duplicate completed candle scan was skipped by the one-candle safety gate.",
            matched_rules=["One-candle dedupe prevented repeated action."],
            failed_rules=[],
            candle_timestamp=candle_timestamp,
        )
        if candle_is_stale(candle):
            return create_evidence_alert(
                conn,
                symbol=symbol,
                timeframe=timeframe,
                strategy_id=strategy_id,
                alert_type="stale_data_warning",
                severity="warning",
                verdict="No Setup",
                evidence_summary="Duplicate scan was skipped and the latest stored candle is stale.",
                matched_rules=["One-candle dedupe prevented repeated action."],
                failed_rules=["Stored candle freshness is outside the expected monitoring window."],
                candle_timestamp=candle_timestamp,
            )
        return duplicate_alert

    if candle_is_stale(candle):
        return create_evidence_alert(
            conn,
            symbol=symbol,
            timeframe=timeframe,
            strategy_id=strategy_id,
            alert_type="stale_data_warning",
            severity="warning",
            verdict="No Setup",
            evidence_summary="Latest stored candle is stale relative to the expected timeframe.",
            matched_rules=[],
            failed_rules=["Stored candle freshness is outside the expected monitoring window."],
            regime=None,
            candle_timestamp=candle_timestamp,
        )

    features = latest_feature(conn, symbol, timeframe, candle_timestamp)
    if not features:
        return create_evidence_alert(
            conn,
            symbol=symbol,
            timeframe=timeframe,
            strategy_id=strategy_id,
            alert_type="stale_data_warning",
            severity="warning",
            verdict="No Setup",
            evidence_summary="No matching feature row exists for the latest stored candle.",
            matched_rules=[],
            failed_rules=["Feature data is unavailable for the latest candle."],
            candle_timestamp=candle_timestamp,
        )

    metrics = research_metrics_for_deployment(conn, deployment)
    rule_result = tsla_momentum_bull_rules(candle, features, decision, deployment.get("parameters") or {})
    evidence_ready = evidence_conditions_pass(metrics, rule_result["regime"])
    signal = decision.get("signal") if isinstance(decision, dict) else decision.signal

    if signal == "setup" and rule_result["passed"] and evidence_ready:
        return create_evidence_alert(
            conn,
            symbol=symbol,
            timeframe=timeframe,
            strategy_id=strategy_id,
            alert_type="entry_setup_review",
            severity="info",
            verdict="Bullish Setup Detected",
            evidence_summary="Research-backed setup is worth reviewing. No order is created by this alert.",
            matched_rules=rule_result["matched"],
            failed_rules=[],
            profit_factor=metrics["profit_factor"],
            expectancy=metrics["expectancy_per_trade"],
            trade_count=metrics["number_of_trades"],
            max_drawdown=metrics["max_drawdown"],
            regime=rule_result["regime"],
            candle_timestamp=candle_timestamp,
        )

    failed = [*rule_result["failed"], *evidence_failure_reasons(metrics, rule_result["regime"])]
    alert_type = "avoid_condition" if signal == "avoid" else "exit_risk_review"
    verdict = "Avoid" if signal == "avoid" else "Setup Worth Reviewing"
    return create_evidence_alert(
        conn,
        symbol=symbol,
        timeframe=timeframe,
        strategy_id=strategy_id,
        alert_type=alert_type,
        severity="warning" if failed else "info",
        verdict=verdict,
        evidence_summary=message or "Current evidence did not satisfy every alert gate.",
        matched_rules=rule_result["matched"],
        failed_rules=failed or ["No setup alert fired."],
        profit_factor=metrics["profit_factor"],
        expectancy=metrics["expectancy_per_trade"],
        trade_count=metrics["number_of_trades"],
        max_drawdown=metrics["max_drawdown"],
        regime=rule_result["regime"],
        candle_timestamp=candle_timestamp,
    )


def create_scheduler_error_alert(conn: psycopg.Connection, deployment: dict[str, Any] | None, message: str) -> dict[str, Any]:
    return create_evidence_alert(
        conn,
        symbol=(deployment or {}).get("symbol", "SYSTEM"),
        timeframe=(deployment or {}).get("timeframe", "scheduler"),
        strategy_id=f"{(deployment or {}).get('strategy_name', 'paper')}_{(deployment or {}).get('strategy_version', 'scheduler')}",
        alert_type="scheduler_error",
        severity="critical",
        verdict="Avoid",
        evidence_summary=f"Scheduler error: {message}",
        matched_rules=[],
        failed_rules=[message],
        candle_timestamp=None,
    )


def detect_research_report_alert(conn: psycopg.Connection, symbol: str, timeframe: str, report: dict[str, Any]) -> dict[str, Any] | None:
    ranked = report.get("ranking_table") or []
    if not ranked:
        return None
    top = ranked[0]
    strategy_id = f"{top.get('strategy_name')}_{top.get('strategy_version')}"
    candles = load_candles(conn, symbol=symbol, timeframe=timeframe)
    if not candles:
        return None
    candle = candles[-1]
    feature = latest_feature(conn, symbol, timeframe, candle["timestamp"])
    if not feature:
        return create_evidence_alert(
            conn,
            symbol=symbol,
            timeframe=timeframe,
            strategy_id=strategy_id,
            alert_type="stale_data_warning",
            severity="warning",
            verdict="No Setup",
            evidence_summary="Research scan completed, but latest feature data is missing for alert evaluation.",
            failed_rules=["Latest feature data is unavailable."],
            candle_timestamp=candle["timestamp"],
        )
    strategy = get_strategy_definition(top["strategy_name"], top["strategy_version"])
    params = top.get("parameters") or strategy.parameters
    decision = enrich_decision(strategy.decide(candle, feature, candles, params), candle, feature, candles, params)
    metrics = top.get("metrics") or {}
    regime = current_regime(candle, feature)
    if top.get("recommendation") == "Candidate for Paper Trading" and decision.signal == "setup" and evidence_conditions_pass(metrics, regime):
        return create_evidence_alert(
            conn,
            symbol=symbol,
            timeframe=timeframe,
            strategy_id=strategy_id,
            alert_type="entry_setup_review",
            severity="info",
            verdict="Research Opportunity",
            evidence_summary="Research scan found a candidate setup worth reviewing.",
            matched_rules=[*decision.explanation, "Research candidate passed paper-readiness gates."],
            failed_rules=[],
            profit_factor=metrics.get("profit_factor"),
            expectancy=metrics.get("expectancy_per_trade"),
            trade_count=metrics.get("number_of_trades"),
            max_drawdown=metrics.get("max_drawdown"),
            regime=regime,
            candle_timestamp=candle["timestamp"],
        )
    return create_evidence_alert(
        conn,
        symbol=symbol,
        timeframe=timeframe,
        strategy_id=strategy_id,
        alert_type="avoid_condition",
        severity="info",
        verdict="No Setup" if decision.signal != "avoid" else "Avoid",
        evidence_summary="Research scan did not satisfy every evidence alert gate.",
        matched_rules=decision.explanation,
        failed_rules=evidence_failure_reasons(metrics, regime) or ["Current setup rules did not produce a research opportunity alert."],
        profit_factor=metrics.get("profit_factor"),
        expectancy=metrics.get("expectancy_per_trade"),
        trade_count=metrics.get("number_of_trades"),
        max_drawdown=metrics.get("max_drawdown"),
        regime=regime,
        candle_timestamp=candle["timestamp"],
    )


def latest_feature(conn: psycopg.Connection, symbol: str, timeframe: str, timestamp: Any) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM features
        WHERE symbol = %s AND timeframe = %s AND timestamp = %s
        LIMIT 1
        """,
        (symbol, timeframe, timestamp),
    ).fetchone()
    return dict(row) if row else None


def research_metrics_for_deployment(conn: psycopg.Connection, deployment: dict[str, Any]) -> dict[str, Any]:
    candles = load_candles(conn, symbol=deployment["symbol"], timeframe=deployment["timeframe"])
    features = list(
        conn.execute(
            """
            SELECT *
            FROM features
            WHERE symbol = %s AND timeframe = %s
            ORDER BY timestamp ASC
            """,
            (deployment["symbol"], deployment["timeframe"]),
        ).fetchall()
    )
    try:
        sync_market_regimes(conn, symbol=deployment["symbol"], timeframe=deployment["timeframe"])
        regimes = load_regimes(conn, symbol=deployment["symbol"], timeframe=deployment["timeframe"])
    except Exception:
        regimes = []
    report = run_strategy_research(
        candles=candles,
        features=features,
        regimes=regimes,
        strategy_name=deployment["strategy_name"],
        strategy_version=deployment["strategy_version"],
        base_params=dict(deployment.get("parameters") or {}),
    )
    top = report["ranking_table"][0] if report["ranking_table"] else {"metrics": {}}
    return top["metrics"]


def tsla_momentum_bull_rules(candle: dict[str, Any], feature: dict[str, Any], decision: StrategyDecision | dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    close = Decimal(candle["close"])
    returns_min = Decimal(str(params.get("returns_5_min", "0.008")))
    matched = []
    failed = []
    ema50 = feature.get("ema_50")
    returns_5 = feature.get("returns_5")
    macd = feature.get("macd")
    macd_signal = feature.get("macd_signal")
    rule(matched, failed, "Close above EMA50.", ema50 is not None and close > Decimal(ema50))
    rule(matched, failed, "Five-candle return condition passes.", returns_5 is not None and Decimal(returns_5) >= returns_min)
    rule(matched, failed, "MACD above signal.", macd is not None and macd_signal is not None and Decimal(macd) > Decimal(macd_signal))
    regime = current_regime(candle, feature)
    rule(matched, failed, "Bull-trend regime filter passes.", regime == "bull_trend")
    stop_loss = decision.get("stop_loss") if isinstance(decision, dict) else decision.stop_loss
    take_profit = decision.get("take_profit") if isinstance(decision, dict) else decision.take_profit
    valid_geometry = stop_loss is not None and take_profit is not None and Decimal(str(stop_loss)) < close < Decimal(str(take_profit))
    rule(matched, failed, "Stop/target logic is valid.", valid_geometry)
    return {"matched": matched, "failed": failed, "passed": not failed, "regime": regime}


def rule(matched: list[str], failed: list[str], label: str, passed: bool) -> None:
    if passed:
        matched.append(label)
    else:
        failed.append(label)


def current_regime(candle: dict[str, Any], feature: dict[str, Any]) -> str:
    if feature.get("ema_50") is None or feature.get("returns_5") is None:
        return "unknown"
    close = Decimal(candle["close"])
    ema50 = Decimal(feature["ema_50"])
    returns_5 = Decimal(feature["returns_5"])
    if close > ema50 and returns_5 > 0:
        return "bull_trend"
    if close < ema50 and returns_5 < 0:
        return "bear_trend"
    return "sideways"


def evidence_conditions_pass(metrics: dict[str, Any], regime: str) -> bool:
    return not evidence_failure_reasons(metrics, regime)


def evidence_failure_reasons(metrics: dict[str, Any], regime: str) -> list[str]:
    reasons = []
    if finite_metric(metrics.get("profit_factor")) < PAPER_READY_THRESHOLDS["profit_factor"]:
        reasons.append(f"Profit factor is below paper threshold {PAPER_READY_THRESHOLDS['profit_factor']}.")
    if finite_metric(metrics.get("expectancy_per_trade")) <= PAPER_READY_THRESHOLDS["expectancy_per_trade"]:
        reasons.append("Expectancy is not positive.")
    if finite_metric(metrics.get("max_drawdown")) > PAPER_READY_THRESHOLDS["max_drawdown"]:
        reasons.append(f"Max drawdown is above allowed threshold {PAPER_READY_THRESHOLDS['max_drawdown']}.")
    if finite_metric(metrics.get("number_of_trades")) < PAPER_READY_THRESHOLDS["number_of_trades"]:
        reasons.append(f"Trade count is below minimum threshold {PAPER_READY_THRESHOLDS['number_of_trades']}.")
    if regime in WEAK_REGIMES:
        reasons.append(f"Current regime is historically weak or unknown: {regime}.")
    return reasons


def candle_is_stale(candle: dict[str, Any]) -> bool:
    timestamp = candle.get("timestamp")
    if not timestamp:
        return True
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return datetime.now(UTC) - timestamp > timedelta(hours=settings.paper_scan_max_candle_age_hours)


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
