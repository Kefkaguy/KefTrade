from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from app.services.strategy_research import finite_metric


def build_research_intelligence(
    hypotheses: list[dict[str, Any]],
    experiments: list[dict[str, Any]],
    journal_entries: list[dict[str, Any]],
    validation_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    evidence = collect_evidence(experiments, validation_runs)
    meta = build_meta_analysis(evidence)
    recommendations = generate_research_recommendations(evidence, meta)
    graph = build_knowledge_graph(hypotheses, experiments, validation_runs, evidence, recommendations)
    timeline = build_research_timeline(hypotheses, experiments, journal_entries, validation_runs, recommendations)
    conclusions = build_research_conclusions(evidence, meta, recommendations)
    return {
        "summary": {
            "hypothesis_count": len(hypotheses),
            "experiment_count": len(experiments),
            "validation_run_count": len(validation_runs),
            "evidence_item_count": len(evidence),
            "recommendation_count": len(recommendations),
        },
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
        nodes.append(node(f"experiment:{experiment['id']}", "experiment", experiment["name"], {"recommendation": experiment["recommendation"]}))
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
        events.append(timeline_event(row.get("created_at"), "experiment", f"Experiment run: {row['name']} -> {row['recommendation']}", [f"experiment:{row['id']}"]))
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
    parsed = [float(value) for value in values]
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
