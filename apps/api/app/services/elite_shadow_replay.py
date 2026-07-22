from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any, Callable

import psycopg
from psycopg.types.json import Jsonb

from app.services.paper_trading import candidate_payload_for_deployment, load_candles, strategy_definition_for_deployment
from app.services.strategy import StrategyDecision
from app.services.strategy_diagnostics import enrich_decision
from app.settings import settings


REPLAY_VERSION = "elite-historical-shadow-v1"
REPLAY_WARMUP_CANDLES = 500


def replay_risk_configuration(active_elites: int) -> dict[str, Any]:
    total_heat = Decimal(str(settings.max_broker_total_exposure_pct))
    strategy_cap = min(
        Decimal(str(settings.max_broker_risk_per_trade_pct)),
        total_heat / Decimal(max(1, active_elites)),
    )
    return {
        "allocated_capital": Decimal(str(settings.broker_allocated_capital)),
        "deterministic_risk_cap_pct": Decimal(str(settings.max_broker_risk_per_trade_pct)),
        "model_shadow_risk_cap_pct": Decimal(str(settings.model_risk_max_risk_pct)),
        "portfolio_strategy_cap_pct": strategy_cap,
        "total_exposure_cap_pct": total_heat,
        "active_elites": active_elites,
        "max_open_positions": settings.broker_max_open_positions,
        "max_open_orders": settings.broker_max_open_orders,
    }


def simulate_calculated_risk(
    *,
    reference_price: Decimal,
    stop_price: Decimal | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    allocated = Decimal(str(config["allocated_capital"]))
    risk_pct = min(
        Decimal(str(config["deterministic_risk_cap_pct"])),
        Decimal(str(config["model_shadow_risk_cap_pct"])),
        Decimal(str(config["portfolio_strategy_cap_pct"])),
    )
    risk_per_share = reference_price - stop_price if stop_price is not None else Decimal("0")
    risk_budget = allocated * risk_pct
    notional_budget = allocated * Decimal(str(config["total_exposure_cap_pct"]))
    risk_quantity = int(risk_budget // risk_per_share) if risk_per_share > 0 else 0
    exposure_quantity = int(notional_budget // reference_price) if reference_price > 0 else 0
    quantity = max(0, min(risk_quantity, exposure_quantity))
    expected_risk = Decimal(quantity) * max(Decimal("0"), risk_per_share)
    rejection_reasons = []
    if stop_price is None or risk_per_share <= 0:
        rejection_reasons.append("VALID_STOP_DISTANCE")
    if quantity < 1:
        rejection_reasons.append("WHOLE_SHARE_QUANTITY")
    return {
        "quantity": quantity,
        "expected_risk": expected_risk,
        "risk_pct": expected_risk / allocated if allocated > 0 else Decimal("0"),
        "risk_cap_pct": risk_pct,
        "risk_per_share": risk_per_share,
        "rejection_reasons": rejection_reasons,
        "would_submit": not rejection_reasons,
    }


def replay_decisions(
    *,
    decide: Callable[[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]], StrategyDecision],
    candles: list[dict[str, Any]],
    features: list[dict[str, Any]],
    params: dict[str, Any],
    risk_config: dict[str, Any],
    candle_limit: int,
) -> list[dict[str, Any]]:
    feature_by_time = {row["timestamp"]: row for row in features}
    eligible_indexes = [index for index, candle in enumerate(candles) if candle.get("timestamp") in feature_by_time]
    selected_indexes = eligible_indexes[-candle_limit:]
    results: list[dict[str, Any]] = []
    for index in selected_indexes:
        candle = dict(candles[index])
        feature = dict(feature_by_time[candle["timestamp"]])
        recent = candles[: index + 1]
        decision = enrich_decision(decide(candle, feature, recent, params), candle, feature, recent, params)
        reference_price = Decimal(str(candle["close"]))
        stop = Decimal(str(decision.stop_loss)) if decision.stop_loss is not None else None
        risk = simulate_calculated_risk(reference_price=reference_price, stop_price=stop, config=risk_config)
        failed_gates = [str(gate.get("code")) for gate in decision.gates if gate.get("status") == "failed"]
        is_setup = decision.signal == "setup" and stop is not None
        rejection_reasons = [] if is_setup else (failed_gates or ["NO_ACTIONABLE_SETUP"])
        if is_setup:
            rejection_reasons.extend(risk["rejection_reasons"])
        results.append(
            {
                "completed_bar_timestamp": candle["timestamp"],
                "reference_price": reference_price,
                "signal_type": decision.signal,
                "gates": decision.gates,
                "regime": decision.regime,
                "stop_price": stop,
                "target_price": Decimal(str(decision.take_profit)) if decision.take_profit is not None else None,
                "simulated_quantity": risk["quantity"] if is_setup else 0,
                "simulated_expected_risk": risk["expected_risk"] if is_setup else Decimal("0"),
                "simulated_risk_pct": risk["risk_pct"] if is_setup else Decimal("0"),
                "risk_cap_pct": risk["risk_cap_pct"],
                "would_submit": bool(is_setup and risk["would_submit"]),
                "rejection_reasons": sorted(set(rejection_reasons)),
                "decision": {
                    "signal": decision.signal,
                    "entry_zone": list(decision.entry_zone) if decision.entry_zone else None,
                    "stop_loss": decision.stop_loss,
                    "take_profit": decision.take_profit,
                    "risk_reward": decision.risk_reward,
                    "explanation": decision.explanation,
                    "calculated_risk_shadow": {
                        "risk_cap_pct": risk["risk_cap_pct"],
                        "risk_per_share": risk["risk_per_share"],
                        "model_bound_applied": True,
                        "portfolio_bound_applied": True,
                        "broker_mutation": False,
                    },
                },
            }
        )
    return results


def apply_portfolio_arbitration(rows: list[dict[str, Any]], risk_config: dict[str, Any]) -> None:
    grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["would_submit"]:
            grouped[row["completed_bar_timestamp"]].append(row)
    for candidates in grouped.values():
        candidates.sort(key=lambda row: (-float(row["research_score"]), int(row["external_deployment_id"])))
        allocated = Decimal(str(risk_config["allocated_capital"]))
        notional_limit = allocated * Decimal(str(risk_config["total_exposure_cap_pct"]))
        risk_limit = allocated * Decimal(str(risk_config["total_exposure_cap_pct"]))
        used_notional = Decimal("0")
        used_risk = Decimal("0")
        symbol_winners: dict[str, int] = {}
        for candidate in candidates:
            symbol = str(candidate["symbol"])
            rejection = None
            if symbol in symbol_winners:
                rejection = "SAME_SYMBOL_HIGHER_RANKED_WINNER"
            else:
                reference = Decimal(str(candidate["reference_price"]))
                stop = Decimal(str(candidate["stop_price"]))
                risk_per_share = reference - stop
                remaining_notional = max(Decimal("0"), notional_limit - used_notional)
                remaining_risk = max(Decimal("0"), risk_limit - used_risk)
                quantity = min(
                    int(candidate["simulated_quantity"]),
                    int(remaining_notional // reference) if reference > 0 else 0,
                    int(remaining_risk // risk_per_share) if risk_per_share > 0 else 0,
                )
                if quantity < 1:
                    rejection = "PORTFOLIO_TOTAL_EXPOSURE_BATCH"
                else:
                    candidate["simulated_quantity"] = quantity
                    candidate["simulated_expected_risk"] = Decimal(quantity) * risk_per_share
                    candidate["simulated_risk_pct"] = candidate["simulated_expected_risk"] / allocated
                    used_notional += Decimal(quantity) * reference
                    used_risk += candidate["simulated_expected_risk"]
                    symbol_winners[symbol] = int(candidate["external_deployment_id"])
            candidate["decision"]["portfolio_arbitration"] = {
                "winner_external_deployment_id": symbol_winners.get(symbol),
                "hierarchy": ["research_score_desc", "external_deployment_id_asc"],
                "batch_notional_limit": notional_limit,
                "batch_expected_risk_limit": risk_limit,
                "broker_mutation": False,
            }
            if rejection:
                candidate["would_submit"] = False
                candidate["simulated_quantity"] = 0
                candidate["simulated_expected_risk"] = Decimal("0")
                candidate["simulated_risk_pct"] = Decimal("0")
                candidate["rejection_reasons"] = sorted(set(candidate["rejection_reasons"] + [rejection]))


def replay_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    signals = Counter(str(row["signal_type"]) for row in rows)
    failures: Counter[str] = Counter()
    by_deployment: dict[int, Counter[str]] = defaultdict(Counter)
    for row in rows:
        deployment = int(row["external_deployment_id"])
        by_deployment[deployment]["evaluated"] += 1
        by_deployment[deployment][str(row["signal_type"])] += 1
        if row["would_submit"]:
            by_deployment[deployment]["would_submit"] += 1
        for gate in row["gates"]:
            if gate.get("status") == "failed":
                failures[str(gate.get("code") or "UNKNOWN")] += 1
                by_deployment[deployment][f"failed:{gate.get('code') or 'UNKNOWN'}"] += 1
    total = len(rows)
    setups = signals.get("setup", 0)
    would_submit = sum(bool(row["would_submit"]) for row in rows)
    opportunity_rate = setups / total if total else 0.0
    if total == 0:
        health = "insufficient_data"
    elif setups == 0:
        health = "dead" if total >= 100 else "too_restrictive"
    elif opportunity_rate > 0.20:
        health = "too_aggressive"
    else:
        health = "healthy"
    return {
        "evaluations": total,
        "setups": setups,
        "would_submit_true": would_submit,
        "opportunity_frequency": opportunity_rate,
        "health": health,
        "signals": dict(signals),
        "failed_gates": [{"code": code, "count": count, "rate": count / total if total else 0.0} for code, count in failures.most_common()],
        "deployments": {str(key): dict(value) for key, value in sorted(by_deployment.items())},
        "broker_mutation": False,
    }


def run_elite_shadow_replay(
    conn: psycopg.Connection,
    *,
    external_deployment_id: int | None = None,
    candle_limit: int = 2000,
) -> dict[str, Any]:
    if candle_limit < 1:
        raise ValueError("candle_limit must be positive")
    clauses = ["x.state IN ('enabled_observe_only','enabled_execution')", "d.simulation_only=TRUE", "e.simulation_only=TRUE"]
    params: list[Any] = []
    if external_deployment_id is not None:
        clauses.append("x.id=%s")
        params.append(external_deployment_id)
    deployments = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT x.id AS external_deployment_id, x.internal_deployment_id, x.elite_candidate_id,
                   x.campaign_id, x.candidate_id, x.symbol, x.timeframe, x.state,
                   d.parameters AS deployment_parameters, e.research_score,
                   epoch.candidate_fingerprint
            FROM external_paper_deployments x
            JOIN strategy_deployments d ON d.id=x.internal_deployment_id
            JOIN elite_research_candidates e ON e.id=x.elite_candidate_id
            JOIN external_execution_epochs epoch ON epoch.external_deployment_id=x.id AND epoch.closed_at IS NULL
            WHERE {' AND '.join(clauses)}
            ORDER BY e.research_score DESC, x.id
            """,
            tuple(params),
        ).fetchall()
    ]
    if not deployments:
        raise ValueError("no active elite external deployments matched the replay request")
    risk_config = replay_risk_configuration(len(deployments))
    run = conn.execute(
        """
        INSERT INTO elite_shadow_replay_runs(
          replay_version, requested_external_deployment_id, requested_candle_limit, configuration
        ) VALUES (%s,%s,%s,%s) RETURNING *
        """,
        (REPLAY_VERSION, external_deployment_id, candle_limit, Jsonb(json_safe(risk_config))),
    ).fetchone()
    conn.commit()
    all_rows: list[dict[str, Any]] = []
    try:
        for item in deployments:
            internal = dict(conn.execute("SELECT * FROM strategy_deployments WHERE id=%s", (item["internal_deployment_id"],)).fetchone())
            strategy = strategy_definition_for_deployment(conn, internal)
            candidate = candidate_payload_for_deployment(conn, internal)
            candles = [
                dict(row)
                for row in load_candles(
                    conn,
                    symbol=item["symbol"],
                    timeframe=item["timeframe"],
                    limit=candle_limit + REPLAY_WARMUP_CANDLES,
                )
            ]
            features = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM features WHERE symbol=%s AND timeframe=%s ORDER BY timestamp ASC",
                    (item["symbol"], item["timeframe"]),
                ).fetchall()
            ]
            candidate_parameters = dict(candidate.get("parameters") or {})
            if candidate_parameters.get("strategy_architecture") == "relative_strength_continuation_v2" or any(
                str(key).startswith("phase_9_11_") for key in candidate_parameters
            ):
                from app.services.research_campaigns import enrich_phase_9_11_context

                features = enrich_phase_9_11_context(conn, item["symbol"], item["timeframe"], features)
            combined_params = {**strategy.parameters, **dict(internal.get("parameters") or {})}
            replayed = replay_decisions(
                decide=strategy.decide,
                candles=candles,
                features=features,
                params=combined_params,
                risk_config=risk_config,
                candle_limit=candle_limit,
            )
            for row in replayed:
                row.update(item)
                all_rows.append(row)
        apply_portfolio_arbitration(all_rows, risk_config)
        for row in all_rows:
            conn.execute(
                """
                INSERT INTO elite_shadow_replay_decisions(
                  replay_run_id, internal_deployment_id, external_deployment_id, elite_candidate_id,
                  candidate_id, configuration_fingerprint, symbol, timeframe, completed_bar_timestamp,
                  signal_type, gates, regime, stop_price, target_price, reference_price,
                  simulated_quantity, simulated_expected_risk, simulated_risk_pct,
                  model_bound_applied, portfolio_bound_applied, would_submit, rejection_reasons,
                  decision, broker_mutation
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,TRUE,%s,%s,%s,FALSE)
                """,
                (
                    run["id"], row["internal_deployment_id"], row["external_deployment_id"], row["elite_candidate_id"],
                    row["candidate_id"], row["candidate_fingerprint"], row["symbol"], row["timeframe"],
                    row["completed_bar_timestamp"], row["signal_type"], Jsonb(json_safe(row["gates"])),
                    Jsonb(json_safe(row["regime"])), row["stop_price"], row["target_price"], row["reference_price"],
                    row["simulated_quantity"], row["simulated_expected_risk"], row["simulated_risk_pct"],
                    row["would_submit"], Jsonb(row["rejection_reasons"]), Jsonb(json_safe(row["decision"])),
                ),
            )
        summary = replay_summary(all_rows)
        completed = conn.execute(
            "UPDATE elite_shadow_replay_runs SET status='complete', summary=%s, completed_at=clock_timestamp() WHERE id=%s RETURNING *",
            (Jsonb(json_safe(summary)), run["id"]),
        ).fetchone()
        conn.commit()
        return {"run": dict(completed), "summary": summary, "replay_version": REPLAY_VERSION, "simulation_only": True, "broker_mutation": False}
    except Exception as error:
        conn.rollback()
        conn.execute(
            "UPDATE elite_shadow_replay_runs SET status='failed', summary=%s, completed_at=clock_timestamp() WHERE id=%s",
            (Jsonb({"error_class": error.__class__.__name__, "error": str(error), "broker_mutation": False}), run["id"]),
        )
        conn.commit()
        raise


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value
