from dataclasses import dataclass
from typing import Any

from app.services.alpha_discovery import AlphaCandidate, generate_alpha_candidates
from app.services.alpha_validation import DEFAULT_VALIDATION_THRESHOLDS, ValidationDataset, validate_candidate
from app.services.strategy_research import finite_metric


DEFAULT_EXPERIMENT_THRESHOLDS = {
    **DEFAULT_VALIDATION_THRESHOLDS,
    "min_condition_trades": 10,
}


@dataclass(frozen=True)
class ResearchHypothesis:
    title: str
    hypothesis: str
    tags: list[str]


def run_research_experiment(
    hypothesis: ResearchHypothesis,
    datasets: list[ValidationDataset],
    max_candidates: int = 25,
    thresholds: dict[str, Any] | None = None,
    monte_carlo_runs: int = 50,
    bootstrap_runs: int = 50,
) -> dict[str, Any]:
    thresholds = {**DEFAULT_EXPERIMENT_THRESHOLDS, **(thresholds or {})}
    candidates = generate_alpha_candidates(max_candidates=max_candidates)
    rows = []
    for candidate in candidates:
        validation = validate_candidate(candidate, datasets, monte_carlo_runs, bootstrap_runs, thresholds)
        trades = collect_candidate_trades(candidate, validation, datasets)
        failure_analysis = explain_failure(validation, trades)
        edge_conditions = discover_edge_conditions(validation, trades, thresholds)
        evolution = suggest_strategy_evolution(candidate, validation, failure_analysis, edge_conditions)
        rows.append(
            {
                **validation,
                "experiment_dimensions": describe_experiment_dimensions(candidate),
                "failure_analysis": failure_analysis,
                "edge_conditions": edge_conditions,
                "strategy_evolution": evolution,
            }
        )
    ranked = sorted(rows, key=lambda row: row["validation_score"], reverse=True)
    for rank, row in enumerate(ranked, start=1):
        row["rank"] = rank
        row["research_report"] = build_research_report(hypothesis, row)
    summary = summarize_experiment(hypothesis, ranked, datasets, thresholds)
    return {
        "hypothesis": {
            "title": hypothesis.title,
            "hypothesis": hypothesis.hypothesis,
            "tags": hypothesis.tags,
        },
        "candidate_count": len(ranked),
        "datasets": [{"symbol": dataset.symbol, "timeframe": dataset.timeframe, "candles": len(dataset.candles)} for dataset in datasets],
        "thresholds": thresholds,
        "summary": summary,
        "leaderboard": ranked,
        "journal_entry": build_journal_entry(hypothesis, summary, ranked),
        "markdown_report": build_experiment_markdown(summary, ranked[:5]),
    }


def collect_candidate_trades(candidate: AlphaCandidate, validation: dict[str, Any], datasets: list[ValidationDataset]) -> list[dict[str, Any]]:
    # Trade-level data is intentionally not persisted by alpha validation.
    # Research lab derives condition-level edge discovery from aggregate
    # validation output unless a future caller passes richer trade logs.
    return []


def describe_experiment_dimensions(candidate: AlphaCandidate) -> dict[str, Any]:
    return {
        "indicator_combination": {
            "trend": candidate.blocks["trend_filter"],
            "momentum": candidate.blocks["momentum"],
            "volatility": candidate.blocks["volatility"],
            "volume": candidate.blocks["volume"],
        },
        "stop_loss_model": "swing_low_with_atr_proxy",
        "exit_model": "fixed_risk_reward_target_or_stop",
        "holding_period": "until_stop_target_or_end_of_data",
        "parameters": {
            "trend_fast": candidate.parameters["trend_fast"],
            "trend_slow": candidate.parameters["trend_slow"],
            "risk_reward": candidate.parameters["risk_reward"],
            "atr_multiplier": candidate.parameters["atr_multiplier"],
        },
    }


def explain_failure(validation: dict[str, Any], trades: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    metrics = validation["metrics"]
    gates = validation["evidence_rules"]
    reasons = []
    if not gates["min_trades"]:
        reasons.append("Trade frequency was insufficient for statistical review.")
    if not gates["profit_factor"]:
        reasons.append("Profit factor did not exceed the required evidence threshold.")
    if not gates["stability"]:
        reasons.append("Performance was not stable across years, assets, regimes, or volatility buckets.")
    if not gates["confidence_interval"]:
        reasons.append("Bootstrap confidence interval was too wide for a reliable edge claim.")

    loss_regimes = find_negative_condition_groups(validation, "by_regime")
    loss_volatility = find_negative_condition_groups(validation, "by_volatility")
    average_win = finite_metric(metrics.get("average_win"))
    average_loss = finite_metric(metrics.get("average_loss"))
    exit_quality = "Average loss exceeded average win." if average_loss >= average_win and average_win > 0 else "Exit payoff distribution was not the primary detected failure."
    stop_quality = "Stops may be too tight or entries too late." if finite_metric(metrics.get("win_rate")) < 0.45 and average_loss > 0 else "Stop behavior did not stand out from aggregate scorecard metrics."
    entry_quality = "Entries appear late or poorly filtered when positive trend filters still produced non-positive expectancy." if loss_regimes else "No isolated late-entry cluster was detected from available aggregates."

    return {
        "why_failed": reasons or ["Evidence rules passed; failure analysis is focused on improvement risk."],
        "loss_regimes": loss_regimes,
        "loss_volatility_regimes": loss_volatility,
        "entry_timing": entry_quality,
        "exit_quality": exit_quality,
        "stop_quality": stop_quality,
        "trade_frequency": "Sufficient" if gates["min_trades"] else "Insufficient",
    }


def find_negative_condition_groups(validation: dict[str, Any], key: str) -> list[dict[str, Any]]:
    groups = []
    for market_result in validation.get("market_results", []):
        for row in market_result.get(key, []):
            metrics = row["metrics"]
            if finite_metric(metrics.get("number_of_trades")) <= 0:
                continue
            if finite_metric(metrics.get("expectancy_per_trade")) <= 0 or finite_metric(metrics.get("profit_factor")) < 1:
                groups.append(
                    {
                        "symbol": market_result["symbol"],
                        "timeframe": market_result["timeframe"],
                        "condition": row["regime"],
                        "profit_factor": metrics.get("profit_factor"),
                        "expectancy": metrics.get("expectancy_per_trade"),
                        "trade_count": metrics.get("number_of_trades"),
                    }
                )
    return groups


def discover_edge_conditions(validation: dict[str, Any], trades: list[dict[str, Any]] | None, thresholds: dict[str, Any]) -> list[dict[str, Any]]:
    conditions = []
    for market_result in validation.get("market_results", []):
        conditions.extend(condition_rows(market_result, "year", "by_year", thresholds))
        conditions.extend(condition_rows(market_result, "market_regime", "by_regime", thresholds))
        conditions.extend(condition_rows(market_result, "volatility_regime", "by_volatility", thresholds))
    return sorted(conditions, key=lambda row: row["profit_factor"], reverse=True)


def condition_rows(market_result: dict[str, Any], condition_type: str, key: str, thresholds: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for group in market_result.get(key, []):
        metrics = group["metrics"]
        trade_count = int(finite_metric(metrics.get("number_of_trades")))
        profit_factor = finite_metric(metrics.get("profit_factor"))
        expectancy = finite_metric(metrics.get("expectancy_per_trade"))
        if (
            trade_count >= int(thresholds["min_condition_trades"])
            and profit_factor >= float(thresholds["min_profit_factor"])
            and expectancy > 0
        ):
            rows.append(
                {
                    "symbol": market_result["symbol"],
                    "timeframe": market_result["timeframe"],
                    "condition_type": condition_type,
                    "condition": group.get("year", group.get("regime", "unknown")),
                    "profit_factor": profit_factor,
                    "expectancy": expectancy,
                    "trade_count": trade_count,
                    "claim": "Research pocket only; not a validated edge until full evidence rules pass.",
                }
            )
    return rows


def suggest_strategy_evolution(
    candidate: AlphaCandidate,
    validation: dict[str, Any],
    failure_analysis: dict[str, Any],
    edge_conditions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    suggestions = []
    base_params = candidate.parameters
    next_version = "v2"
    if failure_analysis["trade_frequency"] == "Insufficient":
        params = {**base_params, "rsi_min": max(20, int(base_params.get("rsi_min", 35)) - 5)}
        suggestions.append(evolution_row(candidate, next_version, params, "Relax momentum threshold to increase sample size for evaluation."))
    if failure_analysis["loss_volatility_regimes"]:
        params = {**base_params, "volatility_block": "volatility", "volatility_min": max(float(base_params.get("volatility_min", 0.01)), 0.015)}
        suggestions.append(evolution_row(candidate, next_version, params, "Filter out weaker volatility states that produced non-positive expectancy."))
    if validation["metrics"].get("average_loss") and finite_metric(validation["metrics"].get("average_loss")) >= finite_metric(validation["metrics"].get("average_win")):
        params = {**base_params, "risk_reward": min(float(base_params["risk_reward"]) + 0.5, 4.0)}
        suggestions.append(evolution_row(candidate, next_version, params, "Increase reward multiple because average loss was not compensated by average win."))
    if edge_conditions:
        condition = edge_conditions[0]
        suggestions.append(
            evolution_row(
                candidate,
                "v3",
                dict(base_params),
                f"Restrict research to {condition['condition_type']}={condition['condition']} for falsification; do not claim edge yet.",
            )
        )
    return suggestions or [evolution_row(candidate, next_version, dict(base_params), "No deterministic improvement path isolated; reject or test a different hypothesis.")]


def evolution_row(candidate: AlphaCandidate, version: str, parameters: dict[str, Any], rationale: str) -> dict[str, Any]:
    return {
        "from_strategy": f"{candidate.name}_{candidate.version}",
        "to_strategy": f"{candidate.name}_{version}",
        "parameters": parameters,
        "rationale": rationale,
    }


def summarize_experiment(
    hypothesis: ResearchHypothesis,
    ranked: list[dict[str, Any]],
    datasets: list[ValidationDataset],
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    recommendations: dict[str, int] = {}
    for row in ranked:
        recommendations[row["recommendation"]] = recommendations.get(row["recommendation"], 0) + 1
    best = ranked[0] if ranked else None
    return {
        "hypothesis": hypothesis.hypothesis,
        "best_candidate": best["candidate_id"] if best else None,
        "best_recommendation": best["recommendation"] if best else None,
        "recommendations": recommendations,
        "validated_edge": bool(best and best["recommendation"] == "Validated Alpha"),
        "edge_condition_count": sum(len(row["edge_conditions"]) for row in ranked),
        "dataset_count": len(datasets),
        "thresholds": thresholds,
    }


def build_journal_entry(hypothesis: ResearchHypothesis, summary: dict[str, Any], ranked: list[dict[str, Any]]) -> dict[str, Any]:
    best = ranked[0] if ranked else {}
    conclusion = (
        "Validated alpha candidate found."
        if summary["validated_edge"]
        else "No statistically valid edge found under current evidence rules."
    )
    next_actions = []
    if best.get("edge_conditions"):
        next_actions.append("Falsify the strongest research pockets with stricter cross-asset and cross-regime validation.")
    if best.get("strategy_evolution"):
        next_actions.append("Test the proposed deterministic strategy evolution as a new hypothesis.")
    if not next_actions:
        next_actions.append("Reject this hypothesis or gather broader datasets before retesting.")
    return {
        "entry_type": "experiment_run",
        "hypothesis": hypothesis.hypothesis,
        "results": summary,
        "conclusion": conclusion,
        "next_actions": next_actions,
    }


def build_research_report(hypothesis: ResearchHypothesis, row: dict[str, Any]) -> str:
    metrics = row["metrics"]
    return "\n".join(
        [
            f"# {row['candidate_id']} Research Report",
            "",
            "## Hypothesis",
            hypothesis.hypothesis,
            "",
            "## Evidence",
            f"Recommendation: {row['recommendation']}",
            f"Profit Factor: {metrics.get('profit_factor')}",
            f"Expectancy: {metrics.get('expectancy_per_trade')}",
            f"Trade Count: {metrics.get('number_of_trades')}",
            f"Stability: {row['stability']['stability_score']}",
            "",
            "## Failure Analysis",
            bullet_list(row["failure_analysis"]["why_failed"]),
            "",
            "## Edge Conditions",
            bullet_list([f"{item['condition_type']}={item['condition']} PF={item['profit_factor']:.2f}" for item in row["edge_conditions"]] or ["No condition-specific research pocket passed thresholds."]),
            "",
            "## Strategy Evolution",
            bullet_list([item["rationale"] for item in row["strategy_evolution"]]),
        ]
    )


def build_experiment_markdown(summary: dict[str, Any], top_rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Alpha Research Experiment",
        "",
        "## Executive Summary",
        f"Hypothesis: {summary['hypothesis']}",
        f"Best candidate: {summary['best_candidate']} ({summary['best_recommendation']}).",
        f"Validated edge: {summary['validated_edge']}",
        "",
        "## Top Candidates",
    ]
    for row in top_rows:
        metrics = row["metrics"]
        lines.append(
            f"- {row['candidate_id']}: {row['recommendation']}; PF={metrics.get('profit_factor')}; "
            f"Expectancy={metrics.get('expectancy_per_trade')}; Trades={metrics.get('number_of_trades')}"
        )
    lines.extend(["", "## Conclusion", "No edge is claimed unless all evidence rules pass."])
    return "\n".join(lines)


def bullet_list(rows: list[str]) -> str:
    return "\n".join(f"- {row}" for row in rows)
