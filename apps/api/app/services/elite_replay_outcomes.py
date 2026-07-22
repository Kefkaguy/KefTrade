from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from math import sqrt
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from app.services.elite_shadow_replay import json_safe


OUTCOME_VERSION = "elite-replay-outcomes-v1"
DEFAULT_FEE_RATE = Decimal("0.001")
DEFAULT_SLIPPAGE_RATE = Decimal("0.0005")
NEW_YORK = ZoneInfo("America/New_York")
TIMEFRAME_SECONDS = {"15m": 900, "30m": 1800, "60m": 3600, "1h": 3600, "4h": 14400, "1d": 86400}


def simulate_replay_outcome(
    decision: dict[str, Any],
    candles: list[dict[str, Any]],
    *,
    fee_rate: Decimal,
    slippage_rate: Decimal,
    max_holding_bars: int,
    allocated_capital: Decimal,
    risk_cap_pct: Decimal,
    total_exposure_cap_pct: Decimal,
) -> tuple[dict[str, Any], int | None]:
    index_by_time = {row["timestamp"]: index for index, row in enumerate(candles)}
    signal_index = index_by_time.get(decision["completed_bar_timestamp"])
    base = {
        "replay_decision_id": decision["id"],
        "replay_run_id": decision["replay_run_id"],
        "external_deployment_id": decision["external_deployment_id"],
        "symbol": decision["symbol"],
        "timeframe": decision["timeframe"],
        "quantity": 0,
        "gross_pnl": Decimal("0"),
        "fees": Decimal("0"),
        "net_pnl": Decimal("0"),
        "net_return_on_allocated_capital": Decimal("0"),
        "holding_bars": 0,
        "holding_hours": 0.0,
        "regime": regime_name(decision.get("regime") or {}),
        "assumptions": {
            "outcome_version": OUTCOME_VERSION,
            "entry": "next_bar_open",
            "same_candle_exit_policy": "stop_first",
            "fee_rate": str(fee_rate),
            "slippage_rate": str(slippage_rate),
            "max_holding_bars": max_holding_bars,
            "broker_mutation": False,
        },
    }
    if signal_index is None or signal_index + 1 >= len(candles):
        return {**base, "status": "no_next_bar", "exit_reason": "no_next_bar"}, None
    entry_index = signal_index + 1
    entry_candle = candles[entry_index]
    entry_price = Decimal(str(entry_candle["open"])) * (Decimal("1") + slippage_rate)
    stop = Decimal(str(decision["stop_price"])) if decision.get("stop_price") is not None else None
    reference = Decimal(str(decision["reference_price"]))
    original_target = Decimal(str(decision["target_price"])) if decision.get("target_price") is not None else None
    if stop is None or original_target is None or stop >= entry_price or reference <= stop:
        return {
            **base,
            "status": "invalid_geometry",
            "entry_time": entry_candle["timestamp"],
            "entry_price": entry_price,
            "stop_price": stop,
            "target_price": original_target,
            "exit_reason": "invalid_geometry_after_next_bar_entry",
        }, None
    risk_reward = (original_target - reference) / (reference - stop)
    target = entry_price + ((entry_price - stop) * risk_reward)
    risk_per_share = entry_price - stop
    risk_budget = allocated_capital * risk_cap_pct
    notional_budget = allocated_capital * total_exposure_cap_pct
    quantity = min(
        int(decision.get("simulated_quantity") or 0),
        int(risk_budget // risk_per_share) if risk_per_share > 0 else 0,
        int(notional_budget // entry_price) if entry_price > 0 else 0,
    )
    if quantity < 1:
        return {
            **base,
            "status": "invalid_geometry",
            "entry_time": entry_candle["timestamp"],
            "entry_price": entry_price,
            "stop_price": stop,
            "target_price": target,
            "exit_reason": "whole_share_quantity_unavailable_after_entry_gap",
        }, None
    final_index = len(candles) - 1
    search_end = min(final_index, entry_index + max_holding_bars) if max_holding_bars > 0 else final_index
    exit_index = None
    exit_reason = None
    for index in range(entry_index, search_end + 1):
        candle = candles[index]
        stop_touched = Decimal(str(candle["low"])) <= stop
        target_touched = Decimal(str(candle["high"])) >= target
        if stop_touched:
            exit_index = index
            exit_reason = "stop_loss_same_candle" if target_touched else "stop_loss"
            break
        if target_touched:
            exit_index = index
            exit_reason = "take_profit"
            break
    if exit_index is None:
        if max_holding_bars > 0 and search_end < final_index:
            exit_index = search_end
            exit_reason = "time_exit"
        else:
            return {
                **base,
                "status": "unresolved",
                "entry_time": entry_candle["timestamp"],
                "entry_price": entry_price,
                "quantity": quantity,
                "stop_price": stop,
                "target_price": target,
                "exit_reason": "end_of_available_data",
            }, None
    exit_candle = candles[exit_index]
    if str(exit_reason).startswith("stop_loss"):
        exit_price = stop * (Decimal("1") - slippage_rate)
    elif exit_reason == "take_profit":
        exit_price = target * (Decimal("1") - slippage_rate)
    else:
        exit_price = Decimal(str(exit_candle["close"])) * (Decimal("1") - slippage_rate)
    gross_pnl = (exit_price - entry_price) * Decimal(quantity)
    fees = (entry_price * Decimal(quantity) * fee_rate) + (exit_price * Decimal(quantity) * fee_rate)
    net_pnl = gross_pnl - fees
    return {
        **base,
        "status": "completed",
        "entry_time": entry_candle["timestamp"],
        "exit_time": exit_candle["timestamp"],
        "entry_price": entry_price,
        "exit_price": exit_price,
        "quantity": quantity,
        "stop_price": stop,
        "target_price": target,
        "gross_pnl": gross_pnl,
        "fees": fees,
        "net_pnl": net_pnl,
        "net_return_on_allocated_capital": net_pnl / allocated_capital if allocated_capital > 0 else Decimal("0"),
        "exit_reason": exit_reason,
        "holding_bars": exit_index - entry_index + 1,
        "holding_hours": (exit_candle["timestamp"] - entry_candle["timestamp"]).total_seconds() / 3600,
    }, exit_index


def outcome_metrics(rows: list[dict[str, Any]], allocated_capital: Decimal) -> dict[str, Any]:
    completed = [row for row in rows if row["status"] == "completed"]
    wins = [row for row in completed if row["net_pnl"] > 0]
    losses = [row for row in completed if row["net_pnl"] <= 0]
    gross_profit = sum((row["net_pnl"] for row in wins), Decimal("0"))
    gross_loss = abs(sum((row["net_pnl"] for row in losses), Decimal("0")))
    win_rate = len(wins) / len(completed) if completed else 0.0
    lower, upper = wilson_interval(len(wins), len(completed))
    cumulative = Decimal("0")
    peak = Decimal("0")
    max_drawdown = Decimal("0")
    for row in sorted(completed, key=lambda item: (item.get("exit_time") or item.get("entry_time"), item["external_deployment_id"])):
        cumulative += Decimal(str(row["net_pnl"]))
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    metrics = {
        "signals_considered": len(rows),
        "completed_trades": len(completed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "win_rate_confidence_95": {"lower": lower, "upper": upper, "method": "wilson"},
        "profit_factor": float(gross_profit / gross_loss) if gross_loss > 0 else None,
        "expectancy": float(sum((row["net_pnl"] for row in completed), Decimal("0")) / Decimal(len(completed))) if completed else 0.0,
        "net_pnl": float(sum((row["net_pnl"] for row in completed), Decimal("0"))),
        "fees": float(sum((row["fees"] for row in completed), Decimal("0"))),
        "max_drawdown": float(max_drawdown),
        "max_drawdown_pct_allocated": float(max_drawdown / allocated_capital) if allocated_capital > 0 else 0.0,
        "average_holding_hours": mean([row["holding_hours"] for row in completed]) if completed else 0.0,
        "status_counts": dict(count_values(str(row["status"]) for row in rows)),
        "exit_reasons": dict(count_values(str(row.get("exit_reason") or "unknown") for row in completed)),
    }
    metrics["health"] = classify_outcome_health(metrics)
    return metrics


def outcome_summary(rows: list[dict[str, Any]], allocated_capital: Decimal, timing: dict[str, Any]) -> dict[str, Any]:
    by_deployment: dict[str, Any] = {}
    by_regime: dict[str, Any] = {}
    for deployment_id, items in group_by(rows, lambda row: str(row["external_deployment_id"])).items():
        by_deployment[deployment_id] = outcome_metrics(items, allocated_capital)
    for regime, items in group_by(rows, lambda row: str(row["regime"])).items():
        by_regime[regime] = outcome_metrics(items, allocated_capital)
    overall = outcome_metrics(rows, allocated_capital)
    return {
        "outcome_version": OUTCOME_VERSION,
        "overall": overall,
        "by_deployment": by_deployment,
        "by_regime": by_regime,
        "execution_timing": timing,
        "simulation_only": True,
        "broker_mutation": False,
    }


def execution_timing_diagnostics(candles_by_market: dict[tuple[str, str], list[dict[str, Any]]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, int]] = defaultdict(lambda: {"bars": 0, "complete_while_market_open": 0, "complete_at_or_after_close": 0})
    for (_symbol, timeframe), candles in candles_by_market.items():
        seconds = TIMEFRAME_SECONDS.get(timeframe)
        if not seconds:
            continue
        for candle in candles:
            completed = candle["timestamp"] + timedelta(seconds=seconds)
            local = completed.astimezone(NEW_YORK)
            grouped[timeframe]["bars"] += 1
            if local.weekday() < 5 and time(9, 30) <= local.time() < time(16, 0):
                grouped[timeframe]["complete_while_market_open"] += 1
            elif local.weekday() < 5 and local.time() >= time(16, 0):
                grouped[timeframe]["complete_at_or_after_close"] += 1
    result: dict[str, Any] = {}
    for timeframe, counts in sorted(grouped.items()):
        compatible = counts["complete_while_market_open"]
        result[timeframe] = {
            **counts,
            "market_open_compatibility_rate": compatible / counts["bars"] if counts["bars"] else 0.0,
            "classification": "compatible" if compatible else "market_open_conflict",
            "policy": "MARKET_OPEN and BAR_FRESH_COMPLETE must both pass",
        }
    return result


def run_replay_outcomes(conn: psycopg.Connection, *, replay_run_id: int | None = None) -> dict[str, Any]:
    if replay_run_id is None:
        latest = conn.execute("SELECT id FROM elite_shadow_replay_runs WHERE status='complete' ORDER BY id DESC LIMIT 1").fetchone()
        if not latest:
            raise ValueError("no completed elite replay exists")
        replay_run_id = int(latest["id"])
    run = conn.execute("SELECT * FROM elite_shadow_replay_runs WHERE id=%s AND status='complete'", (replay_run_id,)).fetchone()
    if not run:
        raise ValueError("completed elite replay run not found")
    decisions = [dict(row) for row in conn.execute(
        """
        SELECT d.*, e.research_score
        FROM elite_shadow_replay_decisions d
        JOIN external_paper_deployments x ON x.id=d.external_deployment_id
        JOIN elite_research_candidates e ON e.id=x.elite_candidate_id
        WHERE d.replay_run_id=%s AND d.would_submit=TRUE
        ORDER BY d.completed_bar_timestamp, e.research_score DESC, d.external_deployment_id
        """,
        (replay_run_id,),
    ).fetchall()]
    if not decisions:
        raise ValueError("replay contains no would_submit decisions")
    configuration = dict(run.get("configuration") or {})
    allocated = Decimal(str(configuration.get("allocated_capital") or 10000))
    risk_cap = min(
        Decimal(str(configuration.get("model_shadow_risk_cap_pct") or "0.005")),
        Decimal(str(configuration.get("portfolio_strategy_cap_pct") or "0.01")),
        Decimal(str(configuration.get("deterministic_risk_cap_pct") or "0.01")),
    )
    exposure_cap = Decimal(str(configuration.get("total_exposure_cap_pct") or "0.03"))
    max_open_positions = int(configuration.get("max_open_positions") or 2)
    deployment_ids = sorted({int(row["external_deployment_id"]) for row in decisions})
    deployment_rows = {
        int(row["external_deployment_id"]): dict(row)
        for row in conn.execute(
            """
            SELECT x.id AS external_deployment_id, d.parameters
            FROM external_paper_deployments x
            JOIN strategy_deployments d ON d.id=x.internal_deployment_id
            WHERE x.id=ANY(%s)
            """,
            (deployment_ids,),
        ).fetchall()
    }
    candles_by_market: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for market in sorted({(str(row["symbol"]), str(row["timeframe"])) for row in decisions}):
        candles_by_market[market] = [dict(row) for row in conn.execute(
            "SELECT symbol,timeframe,timestamp,open,high,low,close,volume FROM candles WHERE symbol=%s AND timeframe=%s ORDER BY timestamp",
            market,
        ).fetchall()]
    outcomes: list[dict[str, Any]] = []
    for decision in decisions:
        deployment_id = int(decision["external_deployment_id"])
        market = (str(decision["symbol"]), str(decision["timeframe"]))
        params = dict(deployment_rows.get(deployment_id, {}).get("parameters") or {})
        outcome, _exit_index = simulate_replay_outcome(
            decision,
            candles_by_market[market],
            fee_rate=Decimal(str(params.get("fee_rate", DEFAULT_FEE_RATE))),
            slippage_rate=Decimal(str(params.get("slippage_rate", DEFAULT_SLIPPAGE_RATE))),
            max_holding_bars=max(0, int(params.get("max_holding_bars") or 0)),
            allocated_capital=allocated,
            risk_cap_pct=risk_cap,
            total_exposure_cap_pct=exposure_cap,
        )
        outcome["_research_score"] = decision.get("research_score") or 0
        outcomes.append(outcome)
    outcomes = apply_historical_portfolio_constraints(
        outcomes,
        allocated_capital=allocated,
        total_exposure_cap_pct=exposure_cap,
        max_open_positions=max_open_positions,
    )
    for row in outcomes:
        conn.execute(
            """
            INSERT INTO elite_shadow_replay_outcomes(
              replay_run_id,replay_decision_id,external_deployment_id,symbol,timeframe,status,
              entry_time,exit_time,entry_price,exit_price,quantity,stop_price,target_price,
              gross_pnl,fees,net_pnl,net_return_on_allocated_capital,exit_reason,holding_bars,
              holding_hours,regime,assumptions,broker_mutation
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE)
            ON CONFLICT(replay_decision_id) DO NOTHING
            """,
            (
                replay_run_id,row["replay_decision_id"],row["external_deployment_id"],row["symbol"],row["timeframe"],row["status"],
                row.get("entry_time"),row.get("exit_time"),row.get("entry_price"),row.get("exit_price"),row["quantity"],row.get("stop_price"),row.get("target_price"),
                row["gross_pnl"],row["fees"],row["net_pnl"],row["net_return_on_allocated_capital"],row.get("exit_reason"),row["holding_bars"],
                row["holding_hours"],row["regime"],Jsonb(json_safe(row["assumptions"])),
            ),
        )
    summary = outcome_summary(outcomes, allocated, execution_timing_diagnostics(candles_by_market))
    conn.execute(
        "UPDATE elite_shadow_replay_runs SET outcome_summary=%s WHERE id=%s",
        (Jsonb(json_safe(summary)), replay_run_id),
    )
    conn.commit()
    return {"replay_run_id": replay_run_id, "summary": summary, "simulation_only": True, "broker_mutation": False}


def skipped_outcome(outcome: dict[str, Any], reason: str) -> dict[str, Any]:
    assumptions = {**dict(outcome.get("assumptions") or {}), "portfolio_skip_reason": reason}
    return {
        **outcome,
        "status": "skipped_overlap",
        "quantity": 0,
        "gross_pnl": Decimal("0"),
        "fees": Decimal("0"),
        "net_pnl": Decimal("0"),
        "net_return_on_allocated_capital": Decimal("0"),
        "exit_reason": reason,
        "holding_bars": 0,
        "holding_hours": 0.0,
        "assumptions": assumptions,
    }


def apply_historical_portfolio_constraints(
    outcomes: list[dict[str, Any]],
    *,
    allocated_capital: Decimal,
    total_exposure_cap_pct: Decimal,
    max_open_positions: int,
) -> list[dict[str, Any]]:
    ordered = sorted(
        outcomes,
        key=lambda row: (
            row.get("entry_time") or datetime.max.replace(tzinfo=UTC),
            -float(row.get("_research_score") or 0),
            int(row["external_deployment_id"]),
        ),
    )
    active_positions: list[dict[str, Any]] = []
    constrained: list[dict[str, Any]] = []
    for original in ordered:
        outcome = dict(original)
        entry_time = outcome.get("entry_time")
        if entry_time is not None and outcome["status"] in {"completed", "unresolved"}:
            active_positions = [
                position for position in active_positions
                if position.get("exit_time") is None or position["exit_time"] >= entry_time
            ]
            existing_symbol = any(position["symbol"] == outcome["symbol"] for position in active_positions)
            used_notional = sum((position["notional"] for position in active_positions), Decimal("0"))
            remaining_notional = max(Decimal("0"), (allocated_capital * total_exposure_cap_pct) - used_notional)
            resize_quantity = int(remaining_notional // Decimal(str(outcome["entry_price"]))) if outcome.get("entry_price") else 0
            if existing_symbol:
                outcome = skipped_outcome(outcome, "existing_portfolio_symbol_position")
            elif len(active_positions) >= max_open_positions:
                outcome = skipped_outcome(outcome, "maximum_open_positions")
            elif resize_quantity < 1:
                outcome = skipped_outcome(outcome, "portfolio_total_exposure")
            else:
                if resize_quantity < int(outcome["quantity"]):
                    resize_outcome(outcome, resize_quantity, allocated_capital)
                active_positions.append({
                    "symbol": outcome["symbol"],
                    "exit_time": outcome.get("exit_time"),
                    "notional": Decimal(str(outcome["entry_price"])) * Decimal(int(outcome["quantity"])),
                })
        outcome.pop("_research_score", None)
        constrained.append(outcome)
    return constrained


def resize_outcome(outcome: dict[str, Any], quantity: int, allocated_capital: Decimal) -> None:
    prior_quantity = int(outcome["quantity"])
    if prior_quantity <= 0 or quantity >= prior_quantity:
        return
    scale = Decimal(quantity) / Decimal(prior_quantity)
    outcome["quantity"] = quantity
    outcome["gross_pnl"] = Decimal(str(outcome["gross_pnl"])) * scale
    outcome["fees"] = Decimal(str(outcome["fees"])) * scale
    outcome["net_pnl"] = Decimal(str(outcome["net_pnl"])) * scale
    outcome["net_return_on_allocated_capital"] = outcome["net_pnl"] / allocated_capital if allocated_capital > 0 else Decimal("0")
    outcome["assumptions"] = {**dict(outcome.get("assumptions") or {}), "portfolio_resized_from_quantity": prior_quantity}


def regime_name(regime: dict[str, Any]) -> str:
    for key in ("market_regime", "trend_regime", "volatility_regime"):
        if regime.get(key):
            return str(regime[key])
    return "unknown"


def wilson_interval(wins: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 0.0
    p = wins / total
    denominator = 1 + (z * z / total)
    center = (p + z * z / (2 * total)) / denominator
    margin = z * sqrt((p * (1 - p) / total) + (z * z / (4 * total * total))) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def classify_outcome_health(metrics: dict[str, Any]) -> str:
    trades = int(metrics["completed_trades"])
    if trades < 30:
        return "insufficient_data"
    if metrics["profit_factor"] is None and metrics["net_pnl"] > 0:
        return "healthy"
    if metrics["profit_factor"] is not None and metrics["profit_factor"] >= 1.2 and metrics["expectancy"] > 0:
        return "healthy"
    if metrics["expectancy"] <= 0:
        return "broken"
    return "broken"


def group_by(rows: list[dict[str, Any]], key) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[key(row)].append(row)
    return grouped


def count_values(values) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for value in values:
        counts[value] += 1
    return dict(sorted(counts.items()))
