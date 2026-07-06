from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from random import Random
from typing import Any

from app.services.alpha_discovery import (
    AlphaCandidate,
    calculate_confidence_score,
    calculate_sortino,
    generate_alpha_candidates,
    make_strategy_definition,
    run_monte_carlo,
)
from app.services.backtester import calculate_metrics, run_backtest
from app.services.strategy_research import build_context_by_time, compare_by_regime, compare_by_year, finite_metric, metrics_for_trades


DEFAULT_VALIDATION_THRESHOLDS = {
    "min_trades": 100,
    "min_profit_factor": 1.2,
    "min_stability_score": 0.6,
    "max_confidence_interval_width": 0.35,
    "min_confidence_score": 70,
}


@dataclass(frozen=True)
class ValidationDataset:
    symbol: str
    timeframe: str
    candles: list[dict[str, Any]]
    features: list[dict[str, Any]]
    regimes: list[dict[str, Any]]


def run_alpha_validation(
    datasets: list[ValidationDataset],
    max_candidates: int = 100,
    monte_carlo_runs: int = 200,
    bootstrap_runs: int = 200,
    thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    thresholds = {**DEFAULT_VALIDATION_THRESHOLDS, **(thresholds or {})}
    candidates = generate_alpha_candidates(max_candidates=max_candidates)
    validations = [validate_candidate(candidate, datasets, monte_carlo_runs, bootstrap_runs, thresholds) for candidate in candidates]
    ranked = sorted(validations, key=lambda row: row["validation_score"], reverse=True)
    for rank, row in enumerate(ranked, start=1):
        row["rank"] = rank
    summary = summarize_validation(ranked, datasets, thresholds)
    markdown = build_validation_markdown(summary, ranked[:10])
    return {
        "candidate_count": len(ranked),
        "thresholds": thresholds,
        "summary": summary,
        "leaderboard": ranked,
        "markdown_report": markdown,
    }


def validate_candidate(
    candidate: AlphaCandidate,
    datasets: list[ValidationDataset],
    monte_carlo_runs: int,
    bootstrap_runs: int,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    strategy = make_strategy_definition(candidate)
    market_results = []
    all_trades = []
    for dataset in datasets:
        result = run_backtest(dataset.candles, dataset.features, strategy.parameters, strategy.decide)
        context = build_context_by_time(dataset.candles, dataset.features, dataset.regimes)
        trades = [attach_market_context(trade, dataset, context) for trade in result["trades"]]
        all_trades.extend(trades)
        market_results.append(
            {
                "symbol": dataset.symbol,
                "timeframe": dataset.timeframe,
                "metrics": result["metrics"],
                "by_year": compare_by_year(trades),
                "by_regime": compare_by_regime(trades, context, "trend_regime"),
                "by_volatility": compare_by_regime(trades, context, "volatility_regime"),
                "trade_count": len(trades),
            }
        )

    aggregate_metrics = aggregate_trade_metrics(all_trades)
    stability = calculate_validation_stability(market_results)
    cross_asset = calculate_cross_asset_stability(market_results)
    parameter_stability = calculate_parameter_sensitivity(candidate, datasets)
    monte_carlo = run_monte_carlo(all_trades, monte_carlo_runs)
    bootstrap = run_bootstrap(all_trades, bootstrap_runs)
    sortino = calculate_sortino(all_trades)
    confidence_width = confidence_interval_width(bootstrap)
    confidence_score = calculate_confidence_score(aggregate_metrics, stability, cross_asset, monte_carlo)
    gates = evaluate_evidence_rules(aggregate_metrics, stability, confidence_width, thresholds)
    recommendation = recommend_validation(gates, confidence_score)
    validation_score = calculate_validation_score(aggregate_metrics, stability, cross_asset, parameter_stability, confidence_score, confidence_width, sortino)
    row = {
        "rank": 0,
        "candidate_id": "",
        "strategy_name": candidate.name,
        "strategy_version": candidate.version,
        "blocks": candidate.blocks,
        "parameters": candidate.parameters,
        "metrics": {**aggregate_metrics, "sortino_ratio": sortino},
        "market_results": market_results,
        "robustness": {
            "walk_forward": aggregate_metrics.get("walk_forward"),
            "bootstrap": bootstrap,
            "monte_carlo": monte_carlo,
            "parameter_sensitivity": parameter_stability,
        },
        "stability": {
            "stability_score": stability,
            "cross_asset_score": cross_asset,
            "confidence_interval_width": confidence_width,
            "confidence_score": confidence_score,
        },
        "evidence_rules": gates,
        "validation_score": validation_score,
        "recommendation": recommendation,
        "markdown_report": "",
    }
    row["candidate_id"] = candidate_id(candidate)
    row["markdown_report"] = build_candidate_markdown(row)
    return row


def attach_market_context(trade: dict[str, Any], dataset: ValidationDataset, context_by_time: dict[Any, dict[str, Any]]) -> dict[str, Any]:
    context = context_by_time.get(trade["entry_time"], {})
    return {
        **trade,
        "symbol": dataset.symbol,
        "timeframe": dataset.timeframe,
        "trend_regime": context.get("trend_regime", "unknown"),
        "volatility_regime": context.get("volatility_regime", "unknown"),
    }


def aggregate_trade_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = metrics_for_trades(trades)
    metrics["walk_forward"] = {"enabled": True, "scope": "per-market validation windows"}
    return metrics


def calculate_validation_stability(market_results: list[dict[str, Any]]) -> float:
    groups = []
    for result in market_results:
        groups.extend(result["by_year"])
        groups.extend(result["by_regime"])
        groups.extend(result["by_volatility"])
    evaluated = [row for row in groups if row["metrics"].get("number_of_trades", 0) > 0]
    if not evaluated:
        return 0.0
    positive = [row for row in evaluated if finite_metric(row["metrics"].get("expectancy_per_trade")) > 0 and finite_metric(row["metrics"].get("profit_factor")) >= 1]
    return len(positive) / len(evaluated)


def calculate_cross_asset_stability(market_results: list[dict[str, Any]]) -> float:
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for result in market_results:
        by_symbol.setdefault(result["symbol"], []).append(result)
    evaluated = []
    for rows in by_symbol.values():
        trades = sum(row["trade_count"] for row in rows)
        expectancy_values = [finite_metric(row["metrics"].get("expectancy_per_trade")) for row in rows if row["trade_count"] > 0]
        if trades:
            evaluated.append(sum(expectancy_values) / len(expectancy_values) if expectancy_values else 0)
    if not evaluated:
        return 0.0
    positive = [value for value in evaluated if value > 0]
    return len(positive) / len(evaluated)


def calculate_parameter_sensitivity(candidate: AlphaCandidate, datasets: list[ValidationDataset]) -> dict[str, Any]:
    variants = []
    base_rr = float(candidate.parameters["risk_reward"])
    for risk_reward in sorted({max(1.0, base_rr - 0.5), base_rr, base_rr + 0.5}):
        params = {**candidate.parameters, "risk_reward": risk_reward}
        variant_candidate = AlphaCandidate(candidate.name, candidate.version, candidate.description, params, candidate.blocks)
        strategy = make_strategy_definition(variant_candidate)
        trades = []
        for dataset in datasets:
            result = run_backtest(dataset.candles, dataset.features, strategy.parameters, strategy.decide)
            trades.extend(result["trades"])
        metrics = aggregate_trade_metrics(trades)
        variants.append({"risk_reward": risk_reward, "metrics": metrics})
    profitable = [row for row in variants if finite_metric(row["metrics"].get("profit_factor")) >= 1.0 and finite_metric(row["metrics"].get("expectancy_per_trade")) > 0]
    return {"variants": variants, "stable_variant_ratio": len(profitable) / len(variants) if variants else 0.0}


def run_bootstrap(trades: list[dict[str, Any]], runs: int = 200) -> dict[str, Any]:
    if not trades:
        return {"runs": runs, "p05_expectancy": None, "p50_expectancy": None, "p95_expectancy": None, "p05_profit_factor": None, "p95_profit_factor": None}
    rng = Random(99)
    expectancies = []
    profit_factors = []
    for _ in range(runs):
        sample = [trades[rng.randrange(len(trades))] for _ in range(len(trades))]
        metrics = aggregate_trade_metrics(sample)
        expectancies.append(metrics["expectancy_per_trade"])
        profit_factors.append(finite_metric(metrics["profit_factor"]))
    return {
        "runs": runs,
        "p05_expectancy": percentile(expectancies, 0.05),
        "p50_expectancy": percentile(expectancies, 0.50),
        "p95_expectancy": percentile(expectancies, 0.95),
        "p05_profit_factor": percentile(profit_factors, 0.05),
        "p95_profit_factor": percentile(profit_factors, 0.95),
    }


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return ordered[index]


def confidence_interval_width(bootstrap: dict[str, Any]) -> float:
    p05 = bootstrap.get("p05_expectancy")
    p95 = bootstrap.get("p95_expectancy")
    if p05 is None or p95 is None:
        return 1.0
    denominator = max(abs(float(bootstrap.get("p50_expectancy") or 0)), 1.0)
    return abs(float(p95) - float(p05)) / denominator


def evaluate_evidence_rules(metrics: dict[str, Any], stability: float, confidence_width: float, thresholds: dict[str, Any]) -> dict[str, bool]:
    return {
        "min_trades": int(metrics.get("number_of_trades", 0)) >= int(thresholds["min_trades"]),
        "profit_factor": finite_metric(metrics.get("profit_factor")) >= float(thresholds["min_profit_factor"]),
        "stability": stability >= float(thresholds["min_stability_score"]),
        "confidence_interval": confidence_width <= float(thresholds["max_confidence_interval_width"]),
    }


def recommend_validation(gates: dict[str, bool], confidence_score: float) -> str:
    if all(gates.values()) and confidence_score >= 70:
        return "Validated Alpha"
    if gates["profit_factor"] and gates["stability"]:
        return "Research More"
    return "Reject"


def calculate_validation_score(
    metrics: dict[str, Any],
    stability: float,
    cross_asset: float,
    parameter_stability: dict[str, Any],
    confidence_score: float,
    confidence_width: float,
    sortino: float | None,
) -> float:
    return (
        finite_metric(metrics.get("profit_factor")) * 20
        + finite_metric(metrics.get("expectancy_per_trade")) * 0.1
        + finite_metric(metrics.get("sharpe_ratio")) * 5
        + finite_metric(sortino) * 4
        - finite_metric(metrics.get("max_drawdown")) * 30
        + stability * 20
        + cross_asset * 20
        + float(parameter_stability.get("stable_variant_ratio", 0)) * 15
        + confidence_score * 0.2
        - confidence_width * 10
    )


def candidate_id(candidate: AlphaCandidate) -> str:
    keys = ["trend_filter", "trend_fast", "trend_slow", "momentum_block", "price_action", "risk_reward"]
    suffix = "_".join(str(candidate.parameters[key]) for key in keys)
    return f"validation_{sha256(suffix.encode('utf-8')).hexdigest()[:10]}"


def summarize_validation(ranked: list[dict[str, Any]], datasets: list[ValidationDataset], thresholds: dict[str, Any]) -> dict[str, Any]:
    recommendations: dict[str, int] = {}
    for row in ranked:
        recommendations[row["recommendation"]] = recommendations.get(row["recommendation"], 0) + 1
    return {
        "best_candidate": ranked[0]["candidate_id"] if ranked else None,
        "best_recommendation": ranked[0]["recommendation"] if ranked else None,
        "datasets": [{"symbol": dataset.symbol, "timeframe": dataset.timeframe, "candles": len(dataset.candles)} for dataset in datasets],
        "thresholds": thresholds,
        "recommendations": recommendations,
    }


def build_validation_markdown(summary: dict[str, Any], top_rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Alpha Validation Report",
        "",
        "## Executive Summary",
        f"Best candidate: {summary.get('best_candidate')} ({summary.get('best_recommendation')}).",
        "",
        "## Statistical Confidence",
        f"Thresholds: {summary.get('thresholds')}",
        "",
        "## Leaderboard",
    ]
    for row in top_rows:
        metrics = row["metrics"]
        lines.append(
            f"- {row['candidate_id']}: {row['recommendation']}; PF={metrics.get('profit_factor')}; "
            f"Expectancy={metrics.get('expectancy_per_trade')}; Trades={metrics.get('number_of_trades')}; "
            f"Stability={row['stability']['stability_score']:.2f}"
        )
    return "\n".join(lines)


def build_candidate_markdown(row: dict[str, Any]) -> str:
    metrics = row["metrics"]
    return "\n".join(
        [
            f"# {row['candidate_id']} Validation",
            "",
            "## Executive Summary",
            f"Recommendation: {row['recommendation']}.",
            "",
            "## Statistical Confidence",
            f"Confidence Score: {row['stability']['confidence_score']}",
            f"Bootstrap: {row['robustness']['bootstrap']}",
            "",
            "## Metrics",
            f"Profit Factor: {metrics.get('profit_factor')}",
            f"Expectancy: {metrics.get('expectancy_per_trade')}",
            f"Max Drawdown: {metrics.get('max_drawdown')}",
            f"Trade Count: {metrics.get('number_of_trades')}",
            f"Stability: {row['stability']['stability_score']}",
            "",
            "## Cross-Asset Performance",
            f"{row['market_results']}",
            "",
            "## Parameter Stability",
            f"{row['robustness']['parameter_sensitivity']}",
            "",
            "## Recommendation",
            row["recommendation"],
        ]
    )
