from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from app.services.mission_control import classify_candle_freshness
from app.services.strategy_research import finite_metric

CALCULATION_VERSION = "research_score_v1"
SCORE_WEIGHTS = {
    "performance_quality": 20,
    "out_of_sample_quality": 20,
    "stability_consistency": 15,
    "sample_confidence": 10,
    "drawdown_quality": 10,
    "regime_robustness": 10,
    "cross_asset_timeframe_proof": 10,
    "freshness_health": 5,
}
SETUP_STATUSES = {"Setup Worth Reviewing", "Setup Forming", "In Paper Position", "Exit Risk Worth Reviewing"}
UNHEALTHY_DEPLOYMENT_STATES = {"Error", "Warning", "Paused"}


def build_research_intelligence(
    hypotheses: list[dict[str, Any]],
    experiments: list[dict[str, Any]],
    journal_entries: list[dict[str, Any]],
    validation_runs: list[dict[str, Any]],
    *,
    alerts: list[dict[str, Any]] | None = None,
    reviews: list[dict[str, Any]] | None = None,
    deployments: list[dict[str, Any]] | None = None,
    symbols: list[dict[str, Any]] | None = None,
    latest_candles: list[dict[str, Any]] | None = None,
    previous_snapshots: list[dict[str, Any]] | None = None,
    snapshot_timestamp: datetime | None = None,
) -> dict[str, Any]:
    evidence = collect_evidence(experiments, validation_runs)
    meta = build_meta_analysis(evidence)
    recommendations = generate_research_recommendations(evidence, meta)
    graph = build_knowledge_graph(hypotheses, experiments, validation_runs, evidence, recommendations)
    timeline = build_research_timeline(hypotheses, experiments, journal_entries, validation_runs, recommendations)
    conclusions = build_research_conclusions(evidence, meta, recommendations)
    ranking = build_research_ranking_layer(
        evidence=evidence,
        alerts=alerts or [],
        reviews=reviews or [],
        deployments=deployments or [],
        symbols=symbols or [],
        latest_candles=latest_candles or [],
        previous_snapshots=previous_snapshots or [],
        generated_at=snapshot_timestamp or datetime.now(UTC),
    )
    return {
        "summary": {
            "hypothesis_count": len(hypotheses),
            "experiment_count": len(experiments),
            "validation_run_count": len(validation_runs),
            "evidence_item_count": len(evidence),
            "recommendation_count": len(recommendations),
            **ranking["summary"],
        },
        "rankings": ranking["rankings"],
        "review_priorities": ranking["review_priorities"],
        "strategy_leaderboard": ranking["strategy_leaderboard"],
        "asset_leaderboard": ranking["asset_leaderboard"],
        "candidate_comparisons": ranking["candidate_comparisons"],
        "portfolio_intelligence": ranking["portfolio_intelligence"],
        "score_methodology": score_methodology(),
        "safety": {
            "simulation_only": True,
            "live_routing_enabled": False,
            "broker_order_routing": "disabled",
            "statement": "Research rankings are based on historical and stored evidence. They are not trading recommendations.",
        },
        "subsystem_errors": ranking["subsystem_errors"],
        "knowledge_engine": build_knowledge_engine(hypotheses, evidence),
        "meta_analysis": meta,
        "recommendations": recommendations,
        "knowledge_graph": graph,
        "confidence": conclusions,
        "timeline": timeline,
        "archive": build_archive(evidence),
        "markdown_report": build_markdown_report(meta, recommendations, conclusions),
    }


def collect_evidence(experiments: list[dict[str, Any]], validation_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence = []
    for experiment in experiments:
        result = experiment.get("result") or {}
        for candidate in result.get("leaderboard", []):
            evidence.append(
                normalize_candidate_evidence(
                    source_type="experiment",
                    source_id=experiment["id"],
                    hypothesis_id=experiment.get("hypothesis_id"),
                    created_at=experiment.get("created_at"),
                    candidate=candidate,
                    fallback_recommendation=experiment.get("recommendation"),
                )
            )
    for run in validation_runs:
        report = run.get("report") or {}
        for candidate in report.get("leaderboard", []):
            evidence.append(
                normalize_candidate_evidence(
                    source_type="validation_run",
                    source_id=run["id"],
                    hypothesis_id=None,
                    created_at=run.get("created_at"),
                    candidate=candidate,
                    fallback_recommendation=candidate.get("recommendation"),
                )
            )
    return evidence


def build_research_ranking_layer(
    *,
    evidence: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    deployments: list[dict[str, Any]],
    symbols: list[dict[str, Any]],
    latest_candles: list[dict[str, Any]],
    previous_snapshots: list[dict[str, Any]],
    generated_at: datetime,
) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    symbol_meta = {row.get("symbol"): row for row in symbols}
    candles_by_key = {(row.get("symbol"), row.get("timeframe")): row for row in latest_candles}
    alerts_by_key = latest_by(alerts, lambda row: (row.get("symbol"), row.get("timeframe"), row.get("strategy_id")))
    reviews_by_key = latest_by(reviews, lambda row: (row.get("symbol"), row.get("timeframe"), row.get("strategy_id")))
    deployments_by_key = group_by_key(
        [row for row in deployments if row.get("simulation_only") is True],
        lambda row: (row.get("symbol"), row.get("timeframe"), f"{row.get('strategy_name')}_{row.get('strategy_version')}"),
    )
    previous_by_candidate = latest_snapshot_by_candidate(previous_snapshots)

    scored = []
    for row in evidence:
        try:
            scored.append(
                score_candidate_row(
                    row,
                    generated_at=generated_at,
                    symbol_meta=symbol_meta,
                    candles_by_key=candles_by_key,
                    alerts_by_key=alerts_by_key,
                    reviews_by_key=reviews_by_key,
                    deployments_by_key=deployments_by_key,
                    previous_by_candidate=previous_by_candidate,
                )
            )
        except Exception as error:  # noqa: BLE001 - partial intelligence response is required
            errors.append({"subsystem": f"candidate:{row.get('candidate_id')}", "error": str(error)})

    rankings = assign_ranks(sorted(scored, key=research_rank_key))
    review_priorities = assign_priority_ranks(sorted(scored, key=priority_rank_key))
    return {
        "summary": summarize_rankings(rankings),
        "rankings": rankings,
        "review_priorities": review_priorities,
        "strategy_leaderboard": build_strategy_leaderboard(rankings),
        "asset_leaderboard": build_asset_leaderboard(rankings),
        "candidate_comparisons": build_candidate_comparisons(rankings),
        "portfolio_intelligence": build_portfolio_intelligence(rankings, deployments),
        "subsystem_errors": errors,
    }


def score_candidate_row(
    row: dict[str, Any],
    *,
    generated_at: datetime,
    symbol_meta: dict[str, dict[str, Any]],
    candles_by_key: dict[tuple[Any, Any], dict[str, Any]],
    alerts_by_key: dict[tuple[Any, Any, Any], dict[str, Any]],
    reviews_by_key: dict[tuple[Any, Any, Any], dict[str, Any]],
    deployments_by_key: dict[tuple[Any, Any, Any], list[dict[str, Any]]],
    previous_by_candidate: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    metrics = row.get("metrics") or {}
    market_results = row.get("market_results") or []
    primary_market = market_results[0] if market_results else {}
    symbol = primary_market.get("symbol") or first_value(row, "symbol") or "unknown"
    timeframe = primary_market.get("timeframe") or first_value(row, "timeframe") or "unknown"
    strategy = strategy_key(row)
    lookup_keys = candidate_lookup_keys(symbol, timeframe, strategy, row["candidate_id"])
    alert = first_lookup(alerts_by_key, lookup_keys)
    review = first_lookup(reviews_by_key, lookup_keys)
    deployments = first_group_lookup(deployments_by_key, lookup_keys)
    deployment = deployments[0] if deployments else None
    candle = candles_by_key.get((symbol, timeframe))
    asset_class = (symbol_meta.get(symbol) or {}).get("asset_class") or "unknown"
    freshness = classify_candle_freshness(candle.get("timestamp") if candle else None, timeframe, asset_class, generated_at)
    latest_setup = latest_setup_state(review, alert, deployment, freshness)
    deployment_health = deployment_health_state(deployment, alert)
    blocking_issues = blocking_issues_for(freshness, deployment_health, alert, review, row)
    score = calculate_composite_research_score(row, freshness, deployment_health)
    previous = previous_by_candidate.get(row["candidate_id"])
    rank_change = None
    score_change = None
    if previous:
        rank_change = int(previous.get("rank") or 0) if previous.get("rank") else None
        score_change = round(score["total_score"] - finite_metric(previous.get("research_score")), 3)
    review_priority = calculate_review_priority(score, latest_setup, freshness, deployment_health, alert, blocking_issues)
    classification = classify_score(score["total_score"])
    return {
        "candidate_id": row["candidate_id"],
        "evidence_ref": evidence_ref(row),
        "source_type": row["source_type"],
        "source_id": row["source_id"],
        "symbol": symbol,
        "asset_class": asset_class,
        "timeframe": timeframe,
        "strategy": strategy,
        "strategy_name": row["strategy_name"],
        "strategy_version": row["strategy_version"],
        "research_score": score["total_score"],
        "score": score,
        "classification": classification,
        "current_verdict": (review or alert or {}).get("verdict") or row.get("recommendation") or "No Setup",
        "review_priority_score": review_priority["score"],
        "review_priority": review_priority["label"],
        "review_priority_reason": review_priority["reason"],
        "data_freshness": freshness["classification"],
        "data_freshness_detail": freshness["detail"],
        "data_age_hours": freshness["age_hours"],
        "deployment_health": deployment_health,
        "latest_setup_state": latest_setup,
        "current_regime": (review or alert or {}).get("regime"),
        "metrics": normalize_metrics(metrics),
        "oos_score": component_raw(row, "out_of_sample"),
        "walk_forward_stability": component_raw(row, "walk_forward"),
        "stability": component_raw(row, "stability"),
        "cross_asset_consistency": component_raw(row, "cross_asset"),
        "cross_timeframe_consistency": component_raw(row, "timeframe"),
        "regime_consistency": component_raw(row, "regime"),
        "ranking_reason": ranking_reason(score, latest_setup, freshness, deployment_health),
        "blocking_issues": blocking_issues,
        "rank_change": rank_change,
        "score_change": score_change,
        "timestamp": generated_at.isoformat(),
        "links": {
            "candidate_detail": f"/candidates/{row['candidate_id']}",
            "validation_detail": f"/validation/{row['source_id']}" if row["source_type"] == "validation_run" else "/validation",
            "signal_review": "/paper#signal-review",
            "mission_control_deployment": "/mission-control",
            "paper_lab": "/paper",
        },
        "research_focus": research_focus_label(score["total_score"], blocking_issues, latest_setup),
        "research_focus_reason": focus_reason(score, blocking_issues, freshness),
        "source_evidence_refs": [evidence_ref(row)],
    }


def calculate_composite_research_score(row: dict[str, Any], freshness: dict[str, Any], deployment_health: str) -> dict[str, Any]:
    metrics = row.get("metrics") or {}
    component_inputs = {
        "performance_quality": performance_quality(metrics),
        "out_of_sample_quality": ratio_component(component_raw(row, "out_of_sample"), "out-of-sample result"),
        "stability_consistency": blended_ratio_component(
            [
                component_raw(row, "stability"),
                component_raw(row, "walk_forward"),
                component_raw(row, "timeframe"),
            ],
            "stability, walk-forward, and timeframe consistency",
        ),
        "sample_confidence": sample_confidence(metrics),
        "drawdown_quality": drawdown_quality(metrics),
        "regime_robustness": ratio_component(component_raw(row, "regime"), "regime performance"),
        "cross_asset_timeframe_proof": blended_ratio_component(
            [component_raw(row, "cross_asset"), component_raw(row, "timeframe")],
            "cross-asset and cross-timeframe proof",
        ),
        "freshness_health": freshness_health_component(freshness, deployment_health),
    }
    components = {}
    penalties: list[dict[str, Any]] = []
    total = 0.0
    for name, weight in SCORE_WEIGHTS.items():
        component = component_inputs[name]
        weighted = round(component["score"] * weight, 3)
        total += weighted
        components[name] = {**component, "weight": weight, "weighted_score": weighted}
    if deployment_health in {"Warning", "Error", "Paused"}:
        amount = 8 if deployment_health == "Error" else 4
        penalties.append({"reason": f"Deployment health is {deployment_health}.", "points": amount})
    if freshness["classification"] == "Stale":
        penalties.append({"reason": "Latest stored candle is stale.", "points": 6})
    if finite_metric(metrics.get("number_of_trades")) < 20:
        penalties.append({"reason": "Insufficient trade sample under 20 trades.", "points": 8})
    total = max(0.0, min(100.0, total - sum(item["points"] for item in penalties)))
    missing = [
        {"component": name, "state": component["state"], "detail": component["detail"]}
        for name, component in components.items()
        if component["state"] != "Available"
    ]
    return {
        "total_score": round(total, 3),
        "components": components,
        "component_weights": SCORE_WEIGHTS,
        "classification": classify_score(total),
        "explanation": score_explanation(components, penalties),
        "penalties": penalties,
        "missing_inputs": missing,
        "calculation_version": CALCULATION_VERSION,
    }


def performance_quality(metrics: dict[str, Any]) -> dict[str, Any]:
    pf = metric_value(metrics, "profit_factor")
    expectancy = metric_value(metrics, "expectancy_per_trade", "expectancy")
    if pf is None and expectancy is None:
        return component_state(0.45, "Missing", "Profit factor and expectancy are missing; neutral incomplete score applied.")
    pf_score = clamp((finite_metric(pf) - 0.8) / 1.2) if pf is not None else 0.45
    exp_score = clamp((finite_metric(expectancy) + 5) / 25) if expectancy is not None else 0.45
    state = "Available" if pf is not None and expectancy is not None else "Missing"
    return component_state((pf_score * 0.6) + (exp_score * 0.4), state, f"PF={pf}; expectancy={expectancy}.")


def sample_confidence(metrics: dict[str, Any]) -> dict[str, Any]:
    trades = metric_value(metrics, "number_of_trades", "trade_count")
    if trades is None:
        return component_state(0.35, "Missing", "Trade count is missing; incomplete evidence score applied.")
    count = finite_metric(trades)
    if count < 20:
        return component_state(max(0.05, count / 100), "Insufficient sample", f"{int(count)} trades is below the minimum confidence sample.")
    return component_state(clamp(count / 120), "Available", f"{int(count)} stored trades.")


def drawdown_quality(metrics: dict[str, Any]) -> dict[str, Any]:
    drawdown = metric_value(metrics, "max_drawdown", "drawdown")
    if drawdown is None:
        return component_state(0.45, "Missing", "Max drawdown is missing; neutral incomplete score applied.")
    value = finite_metric(drawdown)
    return component_state(clamp(1 - (value / 0.25)), "Available", f"Max drawdown={value}.")


def ratio_component(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return component_state(0.45, "Missing", f"{label} is missing; neutral incomplete score applied.")
    return component_state(clamp(finite_metric(value)), "Available", f"{label}={finite_metric(value):.3f}.")


def blended_ratio_component(values: list[Any], label: str) -> dict[str, Any]:
    available = [finite_metric(value) for value in values if value is not None]
    if not available:
        return component_state(0.45, "Missing", f"{label} inputs are missing; neutral incomplete score applied.")
    state = "Available" if len(available) == len(values) else "Missing"
    return component_state(clamp(sum(available) / len(available)), state, f"{label} average={sum(available) / len(available):.3f}.")


def freshness_health_component(freshness: dict[str, Any], deployment_health: str) -> dict[str, Any]:
    if freshness["classification"] == "Stale":
        return component_state(0.1, "Stale", freshness["detail"])
    freshness_score = 1.0 if freshness["classification"] == "Healthy" else 0.65
    health_score = {"Healthy": 1.0, "None": 0.75, "Paused": 0.55, "Warning": 0.45, "Error": 0.1}.get(deployment_health, 0.65)
    return component_state((freshness_score + health_score) / 2, "Available", f"Freshness={freshness['classification']}; deployment health={deployment_health}.")


def component_state(score: float, state: str, detail: str) -> dict[str, Any]:
    return {"score": round(clamp(score), 4), "state": state, "detail": detail}


def component_raw(row: dict[str, Any], family: str) -> Any:
    metrics = row.get("metrics") or {}
    candidates = {
        "out_of_sample": ["out_of_sample_score", "oos_score", "oos_success_rate"],
        "walk_forward": ["walk_forward_stability", "walk_forward_score", "walk_forward_pass_rate"],
        "stability": ["stability_score", "stability"],
        "cross_asset": ["cross_asset_consistency"],
        "timeframe": ["timeframe_consistency", "cross_timeframe_consistency"],
        "regime": ["regime_consistency", "regime_score"],
    }[family]
    for source in (metrics, row):
        for key in candidates:
            if source.get(key) is not None:
                return source.get(key)
    if family == "regime":
        return regime_consistency_from_market_results(row.get("market_results") or [])
    if family == "cross_asset":
        return consistency_from_market_results(row.get("market_results") or [], "symbol")
    if family == "timeframe":
        return consistency_from_market_results(row.get("market_results") or [], "timeframe")
    if family == "stability":
        return consistency_from_market_results(row.get("market_results") or [], "symbol_timeframe")
    return None


def regime_consistency_from_market_results(market_results: list[dict[str, Any]]) -> float | None:
    groups = [group for market in market_results for group in market.get("by_regime", [])]
    if not groups:
        return None
    return sum(1 for group in groups if profitable_metrics(group.get("metrics") or {})) / len(groups)


def consistency_from_market_results(market_results: list[dict[str, Any]], key: str) -> float | None:
    if not market_results:
        return None
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for market in market_results:
        group_key = f"{market.get('symbol')}/{market.get('timeframe')}" if key == "symbol_timeframe" else str(market.get(key) or "unknown")
        grouped[group_key].append(market.get("metrics") or {})
    if not grouped:
        return None
    passing = sum(1 for rows in grouped.values() if any(profitable_metrics(metrics) for metrics in rows))
    return passing / len(grouped)


def profitable_metrics(metrics: dict[str, Any]) -> bool:
    return finite_metric(metrics.get("profit_factor")) >= 1 and finite_metric(metrics.get("expectancy_per_trade", metrics.get("expectancy"))) > 0


def classify_score(score: float) -> str:
    if score >= 85:
        return "High-quality research evidence"
    if score >= 70:
        return "Strong research candidate"
    if score >= 55:
        return "Promising but incomplete"
    if score >= 40:
        return "Weak or mixed evidence"
    return "Reject or insufficient evidence"


def calculate_review_priority(score: dict[str, Any], setup: str, freshness: dict[str, Any], deployment_health: str, alert: dict[str, Any] | None, blocking_issues: list[str]) -> dict[str, Any]:
    value = score["total_score"]
    reason = "Research quality drives baseline priority."
    if setup in SETUP_STATUSES:
        value += 20
        reason = "Current setup state requires human review."
    if alert and alert.get("severity") == "critical":
        value += 12
        reason = "Critical stored alert requires review."
    if freshness["classification"] == "Stale":
        value -= 35
        reason = "Stale data blocks immediate review priority."
    if deployment_health in {"Error", "Warning"}:
        value -= 15
        reason = "Deployment health reduces immediate priority."
    if blocking_issues:
        value -= min(20, len(blocking_issues) * 5)
    value = clamp(value / 100) * 100
    if value >= 75:
        label = "Review first"
    elif value >= 55:
        label = "Continue monitoring"
    elif blocking_issues:
        label = "Blocked by stale data" if any("stale" in issue.lower() for issue in blocking_issues) else "Needs more evidence"
    elif value >= 35:
        label = "Needs more evidence"
    else:
        label = "Reject for now"
    return {"score": round(value, 3), "label": label, "reason": reason}


def assign_ranks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for rank, row in enumerate(rows, start=1):
        previous_rank = row.get("rank_change")
        row["global_rank"] = rank
        row["rank_change"] = (previous_rank - rank) if previous_rank else None
    for field, rank_name in (("symbol", "asset_rank"), ("strategy", "strategy_rank")):
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row[field])].append(row)
        for group in grouped.values():
            for rank, row in enumerate(sorted(group, key=research_rank_key), start=1):
                row[rank_name] = rank
    return rows


def assign_priority_ranks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priorities = []
    for rank, row in enumerate(rows, start=1):
        priorities.append(
            {
                "priority_rank": rank,
                "candidate_id": row["candidate_id"],
                "symbol": row["symbol"],
                "timeframe": row["timeframe"],
                "strategy": row["strategy"],
                "research_score": row["research_score"],
                "classification": row["classification"],
                "review_priority": row["review_priority"],
                "review_priority_score": row["review_priority_score"],
                "reason": row["review_priority_reason"],
                "blocking_issues": row["blocking_issues"],
                "timestamp": row["timestamp"],
            }
        )
    return priorities


def build_strategy_leaderboard(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped = group_by_key(rows, lambda row: row["strategy"])
    result = []
    for strategy, items in grouped.items():
        scores = [row["research_score"] for row in items]
        metrics = [row["metrics"] for row in items]
        passing = [row for row in items if row["research_score"] >= 55]
        assets = group_by_key(items, lambda row: row["symbol"])
        result.append(
            {
                "strategy": strategy,
                "tested_candidates": len(items),
                "passing_research_thresholds": len(passing),
                "average_composite_score": average(scores),
                "median_score": median(scores),
                "average_profit_factor": average(metric["profit_factor"] for metric in metrics),
                "average_expectancy": average(metric["expectancy"] for metric in metrics),
                "average_drawdown": average(metric["max_drawdown"] for metric in metrics),
                "total_trade_sample": sum(int(finite_metric(metric["trade_count"])) for metric in metrics),
                "oos_success_rate": average(row["oos_score"] for row in items),
                "regime_consistency": average(row["regime_consistency"] for row in items),
                "best_performing_asset": best_group_label(assets),
                "weakest_asset": worst_group_label(assets),
                "active_deployments": sum(1 for row in items if row["deployment_health"] not in {"None", "Paused"}),
                "confidence_penalty": confidence_penalty(items),
            }
        )
    return sorted(result, key=lambda row: (row["average_composite_score"] - row["confidence_penalty"], row["tested_candidates"], row["strategy"]), reverse=True)


def build_asset_leaderboard(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped = group_by_key(rows, lambda row: row["symbol"])
    result = []
    for symbol, items in grouped.items():
        result.append(
            {
                "symbol": symbol,
                "asset_class": items[0]["asset_class"],
                "strategies_tested": len({row["strategy"] for row in items}),
                "strongest_strategy": sorted(items, key=research_rank_key)[0]["strategy"],
                "average_research_score": average(row["research_score"] for row in items),
                "highest_research_score": max(row["research_score"] for row in items),
                "validation_pass_rate": sum(1 for row in items if row["classification"] in {"High-quality research evidence", "Strong research candidate"}) / len(items),
                "regime_consistency": average(row["regime_consistency"] for row in items),
                "current_setup_count": sum(1 for row in items if row["latest_setup_state"] in SETUP_STATUSES),
                "deployment_count": sum(1 for row in items if row["deployment_health"] != "None"),
                "data_freshness": worst_freshness(row["data_freshness"] for row in items),
                "simulated_pnl_context": "Simulated PnL is not interpreted as proof of future profitability.",
            }
        )
    return sorted(result, key=lambda row: (row["average_research_score"], row["strategies_tested"], row["symbol"]), reverse=True)


def build_candidate_comparisons(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    top = rows[:4]
    if len(top) < 2:
        return []
    comparisons = []
    for row in top:
        comparisons.append(
            {
                "candidate_id": row["candidate_id"],
                "symbol": row["symbol"],
                "timeframe": row["timeframe"],
                "strategy": row["strategy"],
                "research_score": row["research_score"],
                "score_components": row["score"]["components"],
                "profit_factor": row["metrics"]["profit_factor"],
                "expectancy": row["metrics"]["expectancy"],
                "trade_count": row["metrics"]["trade_count"],
                "max_drawdown": row["metrics"]["max_drawdown"],
                "oos_score": row["oos_score"],
                "walk_forward_stability": row["walk_forward_stability"],
                "regime_performance": row["regime_consistency"],
                "cross_asset_consistency": row["cross_asset_consistency"],
                "cross_timeframe_consistency": row["cross_timeframe_consistency"],
                "freshness": row["data_freshness"],
                "deployment_health": row["deployment_health"],
                "setup_status": row["latest_setup_state"],
                "relative_notes": comparison_notes(row, top),
            }
        )
    return comparisons


def build_portfolio_intelligence(rows: list[dict[str, Any]], deployments: list[dict[str, Any]]) -> dict[str, Any]:
    active = [row for row in deployments if row.get("status") == "active" and row.get("simulation_only") is True]
    by_symbol = Counter(row["symbol"] for row in rows)
    by_strategy = Counter(row["strategy"] for row in rows)
    by_asset_class = Counter(row["asset_class"] for row in rows)
    deployment_strategy_counts = Counter(f"{row.get('strategy_name')}_{row.get('strategy_version')}" for row in active)
    stale_or_unhealthy = [row for row in rows if row["data_freshness"] == "Stale" or row["deployment_health"] in UNHEALTHY_DEPLOYMENT_STATES]
    warnings = []
    if deployment_strategy_counts:
        strategy, count = deployment_strategy_counts.most_common(1)[0]
        if count >= 3:
            warnings.append(f"{count} active deployments use closely related {strategy} logic.")
    if by_symbol:
        symbol, count = by_symbol.most_common(1)[0]
        if count / max(1, len(rows)) >= 0.5:
            warnings.append(f"Research coverage is concentrated in {symbol}.")
    if stale_or_unhealthy:
        warnings.append(f"{len(stale_or_unhealthy)} ranked candidate(s) have stale data or unhealthy deployment context.")
    diversification = diversification_score(by_symbol, by_strategy, by_asset_class)
    return {
        "concentration_by_asset": counted_counter(by_symbol),
        "concentration_by_strategy": counted_counter(by_strategy),
        "concentration_by_asset_class": counted_counter(by_asset_class),
        "duplicate_or_overlapping_strategy_exposure": counted_counter(deployment_strategy_counts),
        "correlated_deployments": [],
        "total_simulated_exposure": "Derived exposure remains in Paper Lab; all values are simulated.",
        "exposure_by_symbol": counted_counter(Counter(row.get("symbol") for row in active)),
        "exposure_by_strategy": counted_counter(deployment_strategy_counts),
        "conflicting_deployments": [],
        "stale_or_unhealthy_deployment_concentration": len(stale_or_unhealthy),
        "candidates_dependent_on_same_regime": counted_counter(Counter(row["current_regime"] for row in rows if row.get("current_regime"))),
        "research_diversification_score": diversification,
        "diversification_methodology": "Score averages normalized spread across ranked assets, strategies, and asset classes; it describes research coverage only, not investment risk reduction.",
        "warnings": warnings,
    }


def persist_research_ranking_snapshots(conn: Any, rankings: list[dict[str, Any]], timestamp: datetime | None = None) -> None:
    timestamp = timestamp or datetime.now(UTC)
    ensure_research_snapshot_table(conn)
    for row in rankings:
        conn.execute(
            """
            INSERT INTO research_ranking_snapshots(candidate_id, research_score, rank, classification, review_priority,
                                                   component_scores, calculation_version, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                row["candidate_id"],
                Decimal(str(row["research_score"])),
                row["global_rank"],
                row["classification"],
                row["review_priority"],
                Jsonb(row["score"]),
                CALCULATION_VERSION,
                timestamp,
            ),
        )


def ensure_research_snapshot_table(conn: Any) -> None:
    return None


def score_methodology() -> dict[str, Any]:
    return {
        "calculation_version": CALCULATION_VERSION,
        "weights": SCORE_WEIGHTS,
        "classification_bands": [
            {"min": 85, "max": 100, "label": "High-quality research evidence"},
            {"min": 70, "max": 84, "label": "Strong research candidate"},
            {"min": 55, "max": 69, "label": "Promising but incomplete"},
            {"min": 40, "max": 54, "label": "Weak or mixed evidence"},
            {"min": 0, "max": 39, "label": "Reject or insufficient evidence"},
        ],
        "missing_data_behavior": "Missing inputs receive explicit Missing, Insufficient sample, Not applicable, or Stale states and neutral incomplete component scores, never perfect or zero by default.",
        "purpose": "Ranks stored research evidence for decision review; it is not a prediction of future profit and not a trading recommendation.",
    }


def normalize_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "profit_factor": metric_value(metrics, "profit_factor"),
        "expectancy": metric_value(metrics, "expectancy_per_trade", "expectancy"),
        "trade_count": metric_value(metrics, "number_of_trades", "trade_count"),
        "max_drawdown": metric_value(metrics, "max_drawdown", "drawdown"),
    }


def metric_value(metrics: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if metrics.get(key) is not None:
            return metrics.get(key)
    return None


def first_value(row: dict[str, Any], key: str) -> Any:
    if row.get(key) is not None:
        return row.get(key)
    for market in row.get("market_results") or []:
        if market.get(key) is not None:
            return market.get(key)
    return None


def candidate_lookup_keys(symbol: str, timeframe: str, strategy: str, candidate_id: str) -> list[tuple[str, str, str]]:
    keys = [(symbol, timeframe, strategy), (symbol, timeframe, candidate_id)]
    if strategy.endswith("_v1"):
        keys.append((symbol, timeframe, strategy[:-3]))
    return keys


def first_lookup(mapping: dict[tuple[Any, Any, Any], dict[str, Any]], keys: list[tuple[str, str, str]]) -> dict[str, Any] | None:
    return next((mapping[key] for key in keys if key in mapping), None)


def first_group_lookup(mapping: dict[tuple[Any, Any, Any], list[dict[str, Any]]], keys: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
    return next((mapping[key] for key in keys if key in mapping), [])


def latest_setup_state(review: dict[str, Any] | None, alert: dict[str, Any] | None, deployment: dict[str, Any] | None, freshness: dict[str, Any]) -> str:
    if freshness["classification"] == "Stale":
        return "Stale Data Blocked"
    if review:
        return str(review.get("status") or review.get("verdict") or "No Setup")
    if alert:
        return str(alert.get("verdict") or alert.get("alert_type") or "No Setup")
    if deployment:
        return "No Setup"
    return "No current setup"


def deployment_health_state(deployment: dict[str, Any] | None, alert: dict[str, Any] | None) -> str:
    if not deployment:
        return "None"
    if deployment.get("status") == "paused":
        return "Paused"
    if alert and alert.get("severity") == "critical":
        return "Error"
    if alert and alert.get("severity") == "warning":
        return "Warning"
    if str(deployment.get("last_signal") or "").lower() == "stale_data_warning":
        return "Warning"
    if deployment.get("status") == "active":
        return "Healthy"
    return "Warning"


def blocking_issues_for(
    freshness: dict[str, Any],
    deployment_health: str,
    alert: dict[str, Any] | None,
    review: dict[str, Any] | None,
    row: dict[str, Any],
) -> list[str]:
    issues = []
    if freshness["classification"] == "Stale":
        issues.append("Stale stored market data blocks immediate review.")
    if deployment_health in {"Error", "Warning", "Paused"}:
        issues.append(f"Deployment health is {deployment_health}.")
    if alert and alert.get("alert_type") == "scheduler_error":
        issues.append("Scheduler error alert is unresolved.")
    if review and review.get("status") == "Stale Data Blocked":
        issues.append("Latest signal review is stale-data blocked.")
    if finite_metric((row.get("metrics") or {}).get("number_of_trades")) < 20:
        issues.append("Stored trade sample is insufficient.")
    return issues


def ranking_reason(score: dict[str, Any], setup: str, freshness: dict[str, Any], deployment_health: str) -> str:
    best_component = max(score["components"].items(), key=lambda item: item[1]["weighted_score"])
    weakest_component = min(score["components"].items(), key=lambda item: item[1]["weighted_score"])
    return (
        f"{score['classification']} from {CALCULATION_VERSION}. Strongest component: {best_component[0].replace('_', ' ')}. "
        f"Weakest component: {weakest_component[0].replace('_', ' ')}. Setup={setup}; freshness={freshness['classification']}; deployment={deployment_health}."
    )


def research_focus_label(score: float, blocking_issues: list[str], setup: str) -> str:
    if any("stale" in issue.lower() for issue in blocking_issues):
        return "Blocked by stale data"
    if score >= 70 and setup in SETUP_STATUSES:
        return "Review first"
    if score >= 70:
        return "Continue monitoring"
    if score >= 40:
        return "Needs more evidence"
    return "Reject for now"


def focus_reason(score: dict[str, Any], blocking_issues: list[str], freshness: dict[str, Any]) -> str:
    if blocking_issues:
        return " ".join(blocking_issues)
    return f"Composite stored-evidence score is {score['total_score']} with {freshness['classification']} data."


def score_explanation(components: dict[str, Any], penalties: list[dict[str, Any]]) -> str:
    awarded = ", ".join(f"{name.replace('_', ' ')} {value['weighted_score']}/{value['weight']}" for name, value in components.items())
    lost = "; ".join(f"{item['reason']} (-{item['points']})" for item in penalties) or "No explicit penalties."
    return f"Awarded: {awarded}. Penalties: {lost}"


def summarize_rankings(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [row["research_score"] for row in rows]
    high = sum(1 for row in rows if row["classification"] == "High-quality research evidence")
    strong = sum(1 for row in rows if row["classification"] == "Strong research candidate")
    incomplete = sum(1 for row in rows if row["classification"] == "Promising but incomplete")
    weak = sum(1 for row in rows if row["classification"] in {"Weak or mixed evidence", "Reject or insufficient evidence"})
    return {
        "candidates_ranked": len(rows),
        "high_quality_evidence_count": high,
        "strong_candidate_count": strong,
        "incomplete_evidence_count": incomplete,
        "rejected_or_weak_count": weak,
        "active_setup_count": sum(1 for row in rows if row["latest_setup_state"] in SETUP_STATUSES),
        "stale_candidate_count": sum(1 for row in rows if row["data_freshness"] == "Stale"),
        "average_research_score": average(scores),
        "top_ranked_asset": rows[0]["symbol"] if rows else None,
        "top_ranked_strategy": rows[0]["strategy"] if rows else None,
    }


def latest_snapshot_by_candidate(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {}
    for row in rows:
        candidate_id = row.get("candidate_id")
        if candidate_id and candidate_id not in result:
            result[candidate_id] = row
    return result


def latest_by(rows: list[dict[str, Any]], key_fn) -> dict[Any, dict[str, Any]]:
    result = {}
    for row in sorted(rows, key=lambda item: str(item.get("created_at") or ""), reverse=True):
        result.setdefault(key_fn(row), row)
    return result


def group_by_key(rows: list[dict[str, Any]], key_fn) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[key_fn(row)].append(row)
    return grouped


def research_rank_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (-row["research_score"], row["candidate_id"], row["symbol"], row["timeframe"], row["strategy"])


def priority_rank_key(row: dict[str, Any]) -> tuple[Any, ...]:
    freshness_penalty = 1 if row["data_freshness"] == "Stale" else 0
    return (-row["review_priority_score"], freshness_penalty, row["candidate_id"], row["symbol"], row["timeframe"])


def confidence_penalty(items: list[dict[str, Any]]) -> float:
    total_trades = sum(finite_metric(row["metrics"].get("trade_count")) for row in items)
    if len(items) == 1:
        return 12.0
    if total_trades < 50:
        return 8.0
    return 0.0


def best_group_label(groups: dict[Any, list[dict[str, Any]]]) -> str | None:
    if not groups:
        return None
    return str(max(groups.items(), key=lambda item: average(row["research_score"] for row in item[1]))[0])


def worst_group_label(groups: dict[Any, list[dict[str, Any]]]) -> str | None:
    if not groups:
        return None
    return str(min(groups.items(), key=lambda item: average(row["research_score"] for row in item[1]))[0])


def worst_freshness(values: Any) -> str:
    ordered = {"Stale": 3, "Warning": 2, "Healthy": 1}
    return max(values, key=lambda value: ordered.get(value, 0), default="unknown")


def comparison_notes(row: dict[str, Any], peers: list[dict[str, Any]]) -> list[str]:
    notes = []
    if row["research_score"] == max(peer["research_score"] for peer in peers):
        notes.append("Stronger stored evidence")
    if finite_metric(row["metrics"].get("trade_count")) == max(finite_metric(peer["metrics"].get("trade_count")) for peer in peers):
        notes.append("Larger sample")
    if finite_metric(row["metrics"].get("max_drawdown")) == min(finite_metric(peer["metrics"].get("max_drawdown")) for peer in peers):
        notes.append("Lower historical drawdown")
    if row["oos_score"] is None:
        notes.append("Less complete evidence")
    elif finite_metric(row["oos_score"]) == max(finite_metric(peer["oos_score"]) for peer in peers):
        notes.append("More stable OOS behavior")
    return notes or ["Comparable stored evidence"]


def counted_counter(counter: Counter) -> list[dict[str, Any]]:
    return [{"name": str(name), "count": count} for name, count in counter.most_common() if name]


def diversification_score(*counters: Counter) -> dict[str, Any]:
    scores = []
    for counter in counters:
        total = sum(counter.values())
        if total <= 1 or len(counter) <= 1:
            scores.append(0.0)
            continue
        largest = counter.most_common(1)[0][1]
        scores.append(1 - ((largest - 1) / (total - 1)))
    value = round((sum(scores) / len(scores)) * 100, 2) if scores else 0.0
    return {"score": value, "state": "Available" if scores else "Missing"}


def median(values: Any) -> float:
    parsed = sorted(float(value) for value in values if value is not None)
    if not parsed:
        return 0.0
    middle = len(parsed) // 2
    if len(parsed) % 2:
        return round(parsed[middle], 3)
    return round((parsed[middle - 1] + parsed[middle]) / 2, 3)


def clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def normalize_candidate_evidence(
    source_type: str,
    source_id: int,
    hypothesis_id: int | None,
    created_at: Any,
    candidate: dict[str, Any],
    fallback_recommendation: str | None,
) -> dict[str, Any]:
    blocks = candidate.get("blocks") or {}
    params = candidate.get("parameters") or {}
    metrics = candidate.get("metrics") or {}
    market_results = candidate.get("market_results") or []
    failure_analysis = candidate.get("failure_analysis") or {}
    evidence_rules = candidate.get("evidence_rules") or {}
    return {
        "source_type": source_type,
        "source_id": source_id,
        "hypothesis_id": hypothesis_id,
        "created_at": created_at,
        "candidate_id": candidate.get("candidate_id", f"{source_type}_{source_id}"),
        "strategy_name": candidate.get("strategy_name", "unknown"),
        "strategy_version": candidate.get("strategy_version", "unknown"),
        "blocks": blocks,
        "parameters": params,
        "metrics": metrics,
        "market_results": market_results,
        "failure_reasons": list(failure_analysis.get("why_failed", [])),
        "loss_regimes": list(failure_analysis.get("loss_regimes", [])),
        "loss_volatility_regimes": list(failure_analysis.get("loss_volatility_regimes", [])),
        "edge_conditions": list(candidate.get("edge_conditions", [])),
        "evidence_rules": evidence_rules,
        "recommendation": candidate.get("recommendation") or fallback_recommendation or "Unknown",
        "score": finite_metric(candidate.get("validation_score", candidate.get("alpha_score"))),
    }


def build_knowledge_engine(hypotheses: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    rejected_hypotheses = [row for row in hypotheses if row.get("status") == "rejected"]
    repeated_strategy_failures = repeated_failures(evidence, lambda row: strategy_key(row))
    indicator_stats = analyze_indicators(evidence)
    parameter_stats = analyze_parameters(evidence)
    return {
        "repeatedly_failed_hypotheses": [
            {
                "hypothesis_id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "evidence_refs": [f"hypothesis:{row['id']}"],
            }
            for row in rejected_hypotheses
        ],
        "repeatedly_failed_strategies": repeated_strategy_failures,
        "low_value_indicators": [row for row in indicator_stats if row["reject_rate"] >= 0.75 and row["sample_size"] >= 2],
        "stronger_indicators": [row for row in indicator_stats if row["average_score"] > 0 and row["sample_size"] >= 2],
        "underperforming_parameter_ranges": [row for row in parameter_stats if row["reject_rate"] >= 0.75 and row["sample_size"] >= 2],
        "parameter_ranges_for_research": [row for row in parameter_stats if row["average_score"] > 0 and row["sample_size"] >= 2],
    }


def build_meta_analysis(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "most_common_failure_reasons": counted_failure_reasons(evidence),
        "most_common_rejection_rules": counted_rejection_rules(evidence),
        "strongest_indicator_combinations": rank_indicator_combinations(evidence, reverse=True),
        "weakest_indicator_combinations": rank_indicator_combinations(evidence, reverse=False),
        "strategy_family_performance": grouped_performance(evidence, lambda row: row["strategy_name"], "strategy_family"),
        "regime_specific_performance": analyze_market_group(evidence, "by_regime", "market_regime"),
        "asset_specific_performance": analyze_asset_timeframe(evidence, "symbol"),
        "timeframe_specific_performance": analyze_asset_timeframe(evidence, "timeframe"),
    }


def counted_failure_reasons(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    refs: dict[str, list[str]] = defaultdict(list)
    for row in evidence:
        reasons = row["failure_reasons"] or inferred_failure_reasons(row)
        for reason in reasons:
            counter[reason] += 1
            refs[reason].append(evidence_ref(row))
    return counted_rows(counter, refs)


def counted_rejection_rules(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    refs: dict[str, list[str]] = defaultdict(list)
    for row in evidence:
        for rule, passed in row["evidence_rules"].items():
            if passed is False:
                counter[rule] += 1
                refs[rule].append(evidence_ref(row))
    return counted_rows(counter, refs)


def inferred_failure_reasons(row: dict[str, Any]) -> list[str]:
    reasons = []
    rules = row["evidence_rules"]
    if rules.get("min_trades") is False:
        reasons.append("Minimum trade count failed.")
    if rules.get("profit_factor") is False:
        reasons.append("Profit factor evidence rule failed.")
    if rules.get("stability") is False:
        reasons.append("Stability evidence rule failed.")
    if rules.get("confidence_interval") is False:
        reasons.append("Confidence interval evidence rule failed.")
    return reasons or ["No explicit failure reason recorded."]


def counted_rows(counter: Counter[str], refs: dict[str, list[str]]) -> list[dict[str, Any]]:
    return [
        {"value": value, "count": count, "evidence_refs": refs[value]}
        for value, count in counter.most_common()
    ]


def analyze_indicators(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evidence:
        for indicator in indicator_names(row):
            grouped[indicator].append(row)
    return ranked_group_rows(grouped, "indicator")


def analyze_parameters(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evidence:
        for name, value in row["parameters"].items():
            if name in {"fee_rate", "slippage_rate", "initial_equity", "walk_forward_train_ratio"}:
                continue
            grouped[f"{name}={value}"].append(row)
    return ranked_group_rows(grouped, "parameter_range")


def ranked_group_rows(grouped: dict[str, list[dict[str, Any]]], label: str) -> list[dict[str, Any]]:
    rows = []
    for value, items in grouped.items():
        rows.append(
            {
                label: value,
                "sample_size": len(items),
                "average_score": average(row["score"] for row in items),
                "average_profit_factor": average(finite_metric(row["metrics"].get("profit_factor")) for row in items),
                "reject_rate": sum(1 for row in items if row["recommendation"] == "Reject") / len(items),
                "evidence_refs": [evidence_ref(row) for row in items],
            }
        )
    return sorted(rows, key=lambda row: (row["average_score"], -row["reject_rate"]), reverse=True)


def rank_indicator_combinations(evidence: list[dict[str, Any]], reverse: bool) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evidence:
        grouped[indicator_combo(row)].append(row)
    rows = ranked_group_rows(grouped, "indicator_combination")
    return sorted(rows, key=lambda row: row["average_score"], reverse=reverse)[:10]


def grouped_performance(evidence: list[dict[str, Any]], key_fn, label: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evidence:
        grouped[str(key_fn(row))].append(row)
    rows = []
    for key, items in grouped.items():
        rows.append(
            {
                label: key,
                "sample_size": len(items),
                "average_score": average(row["score"] for row in items),
                "average_profit_factor": average(finite_metric(row["metrics"].get("profit_factor")) for row in items),
                "average_expectancy": average(finite_metric(row["metrics"].get("expectancy_per_trade")) for row in items),
                "reject_rate": sum(1 for row in items if row["recommendation"] == "Reject") / len(items),
                "evidence_refs": [evidence_ref(row) for row in items],
            }
        )
    return sorted(rows, key=lambda row: row["average_score"], reverse=True)


def analyze_market_group(evidence: list[dict[str, Any]], result_key: str, label: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    refs: dict[str, list[str]] = defaultdict(list)
    for row in evidence:
        for market_result in row["market_results"]:
            for group in market_result.get(result_key, []):
                name = str(group.get("regime", group.get("year", "unknown")))
                grouped[name].append(group["metrics"])
                refs[name].append(evidence_ref(row))
    rows = []
    for key, metrics_rows in grouped.items():
        rows.append(
            {
                label: key,
                "sample_size": len(metrics_rows),
                "average_profit_factor": average(finite_metric(metrics.get("profit_factor")) for metrics in metrics_rows),
                "average_expectancy": average(finite_metric(metrics.get("expectancy_per_trade")) for metrics in metrics_rows),
                "average_trade_count": average(finite_metric(metrics.get("number_of_trades")) for metrics in metrics_rows),
                "evidence_refs": refs[key],
            }
        )
    return sorted(rows, key=lambda row: row["average_expectancy"], reverse=True)


def analyze_asset_timeframe(evidence: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    refs: dict[str, list[str]] = defaultdict(list)
    for row in evidence:
        for market_result in row["market_results"]:
            value = str(market_result.get(key, "unknown"))
            grouped[value].append(market_result.get("metrics", {}))
            refs[value].append(evidence_ref(row))
    rows = []
    for value, metrics_rows in grouped.items():
        rows.append(
            {
                key: value,
                "sample_size": len(metrics_rows),
                "average_profit_factor": average(finite_metric(metrics.get("profit_factor")) for metrics in metrics_rows),
                "average_expectancy": average(finite_metric(metrics.get("expectancy_per_trade")) for metrics in metrics_rows),
                "average_trade_count": average(finite_metric(metrics.get("number_of_trades")) for metrics in metrics_rows),
                "evidence_refs": refs[value],
            }
        )
    return sorted(rows, key=lambda row: row["average_expectancy"], reverse=True)


def generate_research_recommendations(evidence: list[dict[str, Any]], meta: dict[str, Any]) -> list[dict[str, Any]]:
    recommendations = []
    for row in meta["regime_specific_performance"]:
        if row["sample_size"] >= 2 and row["average_expectancy"] < 0:
            recommendations.append(
                recommendation_row(
                    title=f"Test stricter filters around {row['market_regime']}",
                    finding=f"{row['market_regime']} has repeated negative average expectancy across prior research.",
                    recommendation=f"Design a falsification test that avoids or adds confirmation inside {row['market_regime']}.",
                    evidence_refs=row["evidence_refs"],
                )
            )
    for row in meta["most_common_rejection_rules"][:3]:
        if row["count"] >= 2:
            recommendations.append(
                recommendation_row(
                    title=f"Investigate recurring {row['value']} rejection",
                    finding=f"The {row['value']} evidence rule failed in {row['count']} candidate records.",
                    recommendation=f"Create the next hypothesis around reducing {row['value']} failures before changing strategy families.",
                    evidence_refs=row["evidence_refs"],
                )
            )
    for row in meta["strongest_indicator_combinations"][:3]:
        if row["sample_size"] >= 2 and row["average_score"] > 0:
            recommendations.append(
                recommendation_row(
                    title=f"Further investigate {row['indicator_combination']}",
                    finding=f"This indicator combination has stronger relative score than other tested combinations.",
                    recommendation="Retest this combination across broader assets and regimes before treating it as alpha.",
                    evidence_refs=row["evidence_refs"],
                )
            )
    return dedupe_recommendations(recommendations)


def recommendation_row(title: str, finding: str, recommendation: str, evidence_refs: list[str]) -> dict[str, Any]:
    return {
        "title": title,
        "finding": finding,
        "recommendation": recommendation,
        "evidence_refs": sorted(set(evidence_refs)),
        "confidence": confidence_from_refs(evidence_refs),
    }


def dedupe_recommendations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    deduped = []
    for row in rows:
        if row["title"] in seen:
            continue
        seen.add(row["title"])
        deduped.append(row)
    return deduped


def build_knowledge_graph(
    hypotheses: list[dict[str, Any]],
    experiments: list[dict[str, Any]],
    validation_runs: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    recommendations: list[dict[str, Any]],
) -> dict[str, Any]:
    nodes = []
    edges = []
    for hypothesis in hypotheses:
        nodes.append(node(f"hypothesis:{hypothesis['id']}", "hypothesis", hypothesis["title"], {"status": hypothesis["status"]}))
    for experiment in experiments:
        nodes.append(node(f"experiment:{experiment['id']}", "experiment", experiment.get("name") or f"Experiment {experiment['id']}", {"recommendation": experiment.get("recommendation")}))
        if experiment.get("hypothesis_id"):
            edges.append(edge(f"hypothesis:{experiment['hypothesis_id']}", f"experiment:{experiment['id']}", "tested_by"))
    for run in validation_runs:
        nodes.append(node(f"validation_run:{run['id']}", "validation_run", f"Validation run {run['id']}", {"candidate_count": run.get("candidate_count")}))
    for row in evidence:
        candidate_node = f"candidate:{row['candidate_id']}"
        nodes.append(node(candidate_node, "strategy", strategy_key(row), {"recommendation": row["recommendation"]}))
        edges.append(edge(evidence_ref(row), candidate_node, "produced"))
        for indicator in indicator_names(row):
            indicator_node = f"indicator:{indicator}"
            nodes.append(node(indicator_node, "indicator", indicator, {}))
            edges.append(edge(candidate_node, indicator_node, "uses"))
        for market_result in row["market_results"]:
            asset_node = f"asset:{market_result.get('symbol', 'unknown')}"
            timeframe_node = f"timeframe:{market_result.get('timeframe', 'unknown')}"
            nodes.append(node(asset_node, "asset", market_result.get("symbol", "unknown"), {}))
            nodes.append(node(timeframe_node, "timeframe", market_result.get("timeframe", "unknown"), {}))
            edges.append(edge(candidate_node, asset_node, "tested_on"))
            edges.append(edge(candidate_node, timeframe_node, "tested_on"))
            for group in market_result.get("by_regime", []):
                regime_node = f"regime:{group.get('regime', 'unknown')}"
                nodes.append(node(regime_node, "market_regime", group.get("regime", "unknown"), {}))
                edges.append(edge(candidate_node, regime_node, "evaluated_in"))
    for index, recommendation in enumerate(recommendations, start=1):
        recommendation_node = f"recommendation:{index}"
        nodes.append(node(recommendation_node, "recommendation", recommendation["title"], {"confidence": recommendation["confidence"]}))
        for ref in recommendation["evidence_refs"]:
            edges.append(edge(ref, recommendation_node, "supports"))
    return {"nodes": unique_nodes(nodes), "edges": unique_edges(edges)}


def build_research_timeline(
    hypotheses: list[dict[str, Any]],
    experiments: list[dict[str, Any]],
    journal_entries: list[dict[str, Any]],
    validation_runs: list[dict[str, Any]],
    recommendations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    events = []
    for row in hypotheses:
        events.append(timeline_event(row.get("created_at"), "hypothesis", f"Hypothesis created: {row['title']}", [f"hypothesis:{row['id']}"]))
    for row in experiments:
        experiment_name = row.get("name") or f"Experiment {row['id']}"
        events.append(timeline_event(row.get("created_at"), "experiment", f"Experiment run: {experiment_name} -> {row.get('recommendation')}", [f"experiment:{row['id']}"]))
    for row in validation_runs:
        events.append(timeline_event(row.get("created_at"), "validation", f"Alpha validation run {row['id']}", [f"validation_run:{row['id']}"]))
    for row in journal_entries:
        events.append(timeline_event(row.get("created_at"), row["entry_type"], row["conclusion"], compact_refs(row)))
    for index, recommendation in enumerate(recommendations, start=1):
        events.append(timeline_event(None, "recommendation", recommendation["recommendation"], [f"recommendation:{index}", *recommendation["evidence_refs"]]))
    return sorted(events, key=lambda row: row["timestamp"] or "")


def timeline_event(timestamp: Any, event_type: str, summary: str, evidence_refs: list[str]) -> dict[str, Any]:
    return {
        "timestamp": serialize_timestamp(timestamp),
        "event_type": event_type,
        "summary": summary,
        "evidence_refs": evidence_refs,
    }


def build_archive(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "evidence_ref": evidence_ref(row),
            "candidate_id": row["candidate_id"],
            "strategy": strategy_key(row),
            "indicators": indicator_names(row),
            "assets": sorted({market.get("symbol", "unknown") for market in row["market_results"]}),
            "timeframes": sorted({market.get("timeframe", "unknown") for market in row["market_results"]}),
            "market_regimes": sorted({group.get("regime", "unknown") for market in row["market_results"] for group in market.get("by_regime", [])}),
            "recommendation": row["recommendation"],
            "failure_reasons": row["failure_reasons"] or inferred_failure_reasons(row),
            "validation_status": row["recommendation"],
            "metrics": row["metrics"],
        }
        for row in evidence
    ]


def filter_archive(rows: list[dict[str, Any]], filters: dict[str, str | None]) -> list[dict[str, Any]]:
    filtered = []
    for row in rows:
        if filters.get("strategy") and filters["strategy"] not in row["strategy"]:
            continue
        if filters.get("indicator") and filters["indicator"] not in row["indicators"]:
            continue
        if filters.get("asset") and filters["asset"] not in row["assets"]:
            continue
        if filters.get("timeframe") and filters["timeframe"] not in row["timeframes"]:
            continue
        if filters.get("market_regime") and filters["market_regime"] not in row["market_regimes"]:
            continue
        if filters.get("recommendation") and filters["recommendation"] != row["recommendation"]:
            continue
        if filters.get("failure_reason") and not any(filters["failure_reason"] in reason for reason in row["failure_reasons"]):
            continue
        if filters.get("validation_status") and filters["validation_status"] != row["validation_status"]:
            continue
        filtered.append(row)
    return filtered


def build_research_conclusions(evidence: list[dict[str, Any]], meta: dict[str, Any], recommendations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conclusions = []
    evidence_by_ref = {evidence_ref(row): row for row in evidence}
    for row in meta["most_common_failure_reasons"][:5]:
        supporting = [evidence_by_ref[ref] for ref in row["evidence_refs"] if ref in evidence_by_ref]
        conclusions.append(
            {
                "conclusion": row["value"],
                "confidence": confidence_from_evidence(supporting),
                "supporting_evidence_count": row["count"],
                "evidence_refs": row["evidence_refs"],
            }
        )
    for recommendation in recommendations:
        supporting = [evidence_by_ref[ref] for ref in recommendation["evidence_refs"] if ref in evidence_by_ref]
        conclusions.append(
            {
                "conclusion": recommendation["finding"],
                "confidence": confidence_from_evidence(supporting),
                "supporting_evidence_count": len(recommendation["evidence_refs"]),
                "evidence_refs": recommendation["evidence_refs"],
            }
        )
    return conclusions


def confidence_from_refs(evidence_refs: list[str]) -> str:
    unique = set(evidence_refs)
    if len(unique) >= 10:
        return "high"
    if len(unique) >= 4:
        return "medium"
    return "low"


def confidence_from_evidence(evidence: list[dict[str, Any]]) -> str:
    if not evidence:
        return "low"
    refs = {evidence_ref(row) for row in evidence}
    assets = {
        market.get("symbol")
        for row in evidence
        for market in row["market_results"]
        if market.get("symbol")
    }
    timeframes = {
        market.get("timeframe")
        for row in evidence
        for market in row["market_results"]
        if market.get("timeframe")
    }
    regimes = {
        group.get("regime")
        for row in evidence
        for market in row["market_results"]
        for group in market.get("by_regime", [])
        if group.get("regime")
    }
    years = {
        group.get("year")
        for row in evidence
        for market in row["market_results"]
        for group in market.get("by_year", [])
        if group.get("year")
    }
    support_score = 0
    support_score += 1 if len(refs) >= 2 else 0
    support_score += 1 if len(refs) >= 5 else 0
    support_score += 1 if len(assets) >= 2 else 0
    support_score += 1 if len(timeframes) >= 2 else 0
    support_score += 1 if len(regimes) >= 2 else 0
    support_score += 1 if len(years) >= 2 else 0
    if support_score >= 5:
        return "high"
    if support_score >= 3:
        return "medium"
    return "low"


def strategy_key(row: dict[str, Any]) -> str:
    return f"{row['strategy_name']}_{row['strategy_version']}"


def indicator_names(row: dict[str, Any]) -> list[str]:
    blocks = row["blocks"]
    names = []
    for key in ("trend_filter", "momentum", "volatility", "volume", "price_action"):
        value = blocks.get(key)
        if value and value != "none":
            names.append(str(value))
    return sorted(set(names))


def indicator_combo(row: dict[str, Any]) -> str:
    names = indicator_names(row)
    return "+".join(names) if names else "none"


def repeated_failures(evidence: list[dict[str, Any]], key_fn) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evidence:
        if row["recommendation"] == "Reject":
            grouped[str(key_fn(row))].append(row)
    return [
        {"value": key, "count": len(rows), "evidence_refs": [evidence_ref(row) for row in rows]}
        for key, rows in sorted(grouped.items(), key=lambda item: len(item[1]), reverse=True)
        if len(rows) >= 2
    ]


def average(values: Any) -> float:
    parsed = []
    for value in values:
        if value is None:
            continue
        try:
            parsed.append(float(value))
        except (TypeError, ValueError):
            continue
    return sum(parsed) / len(parsed) if parsed else 0.0


def evidence_ref(row: dict[str, Any]) -> str:
    return f"{row['source_type']}:{row['source_id']}"


def node(node_id: str, node_type: str, label: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {"id": node_id, "type": node_type, "label": label, "properties": properties}


def edge(source: str, target: str, relationship: str) -> dict[str, str]:
    return {"source": source, "target": target, "relationship": relationship}


def unique_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {}
    for item in nodes:
        by_id[item["id"]] = item
    return list(by_id.values())


def unique_edges(edges: list[dict[str, str]]) -> list[dict[str, str]]:
    seen = set()
    rows = []
    for item in edges:
        key = (item["source"], item["target"], item["relationship"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(item)
    return rows


def compact_refs(row: dict[str, Any]) -> list[str]:
    refs = []
    if row.get("hypothesis_id"):
        refs.append(f"hypothesis:{row['hypothesis_id']}")
    if row.get("experiment_id"):
        refs.append(f"experiment:{row['experiment_id']}")
    refs.append(f"journal:{row['id']}")
    return refs


def serialize_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def build_markdown_report(meta: dict[str, Any], recommendations: list[dict[str, Any]], conclusions: list[dict[str, Any]]) -> str:
    lines = [
        "# Research Intelligence Report",
        "",
        "## Evidence Summary",
        f"Common failure reasons: {len(meta['most_common_failure_reasons'])}",
        f"Research recommendations: {len(recommendations)}",
        "",
        "## Conclusions",
    ]
    for conclusion in conclusions[:10]:
        lines.append(f"- [{conclusion['confidence']}] {conclusion['conclusion']} ({conclusion['supporting_evidence_count']} refs)")
    lines.extend(["", "## Recommendations"])
    for recommendation in recommendations[:10]:
        lines.append(f"- {recommendation['title']}: {recommendation['recommendation']}")
    return "\n".join(lines)
