from dataclasses import dataclass
from decimal import Decimal
from itertools import product
from typing import Any

from app.services.backtester import calculate_metrics, run_backtest
from app.services.strategy import StrategyDefinition, get_strategy_definition, get_strategy_library


RANK_METRICS = (
    "profit_factor",
    "expectancy_per_trade",
    "max_drawdown",
    "sharpe_ratio",
    "win_rate",
    "number_of_trades",
    "average_win",
    "average_loss",
    "longest_losing_streak",
    "average_holding_time_hours",
)


@dataclass(frozen=True)
class StrategyCandidate:
    strategy: StrategyDefinition
    parameters: dict[str, Any]


DEFAULT_SWEEP = {
    "ema_fast": [10, 20],
    "ema_slow": [50],
    "rsi_min": [35, 40],
    "rsi_max": [60, 65],
    "risk_reward": [1.5, 2.0, 2.5],
    "swing_lookback": [3, 5, 8],
}


def build_parameter_sweep(base_params: dict[str, Any], sweep: dict[str, list[Any]] | None = None) -> list[dict[str, Any]]:
    sweep = sweep or DEFAULT_SWEEP
    sweep_keys = list(sweep.keys())
    variants: list[dict[str, Any]] = []
    for values in product(*(sweep[key] for key in sweep_keys)):
        params = dict(base_params)
        params.update(dict(zip(sweep_keys, values)))
        if "ema_fast" in params and "ema_slow" in params and int(params["ema_fast"]) >= int(params["ema_slow"]):
            continue
        if "rsi_min" in params and "rsi_max" in params and float(params["rsi_min"]) >= float(params["rsi_max"]):
            continue
        variants.append(params)
    return variants


def run_strategy_research(
    candles: list[dict[str, Any]],
    features: list[dict[str, Any]],
    strategy_name: str | None = None,
    strategy_version: str = "v1",
    base_params: dict[str, Any] | None = None,
    sweep: dict[str, list[Any]] | None = None,
) -> dict[str, Any]:
    candidates = build_candidates(strategy_name, strategy_version, base_params, sweep)
    context_by_time = build_context_by_time(candles, features)

    runs = []
    for index, candidate in enumerate(candidates, start=1):
        result = run_backtest(candles, features, candidate.parameters, candidate.strategy.decide)
        metrics = scorecard_from_result(result)
        run_id = f"{candidate.strategy.name}_{candidate.strategy.version}_{index:03d}"
        by_year = compare_by_year(result["trades"])
        by_volatility_regime = compare_by_regime(result["trades"], context_by_time, "volatility_regime")
        by_market_regime = compare_by_regime(result["trades"], context_by_time, "market_regime")
        recommendation = recommend_strategy(metrics)
        run = {
            "run_id": run_id,
            "strategy_name": candidate.strategy.name,
            "strategy_version": candidate.strategy.version,
            "description": candidate.strategy.description,
            "parameters": candidate.parameters,
            "entry_rules": candidate.strategy.entry_rules,
            "exit_rules": candidate.strategy.exit_rules,
            "supported_market_regimes": candidate.strategy.supported_market_regimes,
            "metrics": metrics,
            "equity_curve_summary": result.get("equity_curve_summary", {}),
            "trade_count": metrics["number_of_trades"],
            "by_year": by_year,
            "by_volatility_regime": by_volatility_regime,
            "by_market_regime": by_market_regime,
            "recommendation": recommendation,
            "markdown_report": "",
            "rank_score": score_metrics(metrics),
        }
        run["markdown_report"] = build_markdown_report(run)
        runs.append(run)

    ranked = sorted(runs, key=lambda row: row["rank_score"], reverse=True)
    for rank, row in enumerate(ranked, start=1):
        row["rank"] = rank

    return {
        "strategy_name": strategy_name or "strategy_library",
        "strategy_version": strategy_version,
        "run_count": len(runs),
        "rank_metrics": list(RANK_METRICS),
        "strategy_library": serialize_strategy_library(),
        "ranking_table": ranked,
        "charts": build_comparison_charts(ranked),
        "markdown_report": build_library_markdown_report(ranked),
    }


def build_candidates(
    strategy_name: str | None,
    strategy_version: str,
    base_params: dict[str, Any] | None,
    sweep: dict[str, list[Any]] | None,
) -> list[StrategyCandidate]:
    if strategy_name:
        strategy = get_strategy_definition(strategy_name, strategy_version)
        params = {**strategy.parameters, **(base_params or {})}
        parameter_sets = build_parameter_sweep(params, sweep) if sweep else [params]
        return [StrategyCandidate(strategy=strategy, parameters=variant) for variant in parameter_sets]
    return [StrategyCandidate(strategy=strategy, parameters=dict(strategy.parameters)) for strategy in get_strategy_library().values()]


def serialize_strategy_library() -> list[dict[str, Any]]:
    return [
        {
            "name": strategy.name,
            "version": strategy.version,
            "description": strategy.description,
            "parameters": strategy.parameters,
            "entry_rules": strategy.entry_rules,
            "exit_rules": strategy.exit_rules,
            "supported_market_regimes": strategy.supported_market_regimes,
        }
        for strategy in get_strategy_library().values()
    ]


def scorecard_from_result(result: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(result["metrics"])
    for metric in RANK_METRICS:
        metrics.setdefault(metric, 0)
    return metrics


def build_context_by_time(candles: list[dict[str, Any]], features: list[dict[str, Any]]) -> dict[Any, dict[str, str]]:
    if any(not isinstance(row, dict) for row in candles + features):
        return {}
    candles_by_time = {row["timestamp"]: row for row in candles}
    contexts = {}
    for feature in features:
        timestamp = feature["timestamp"]
        candle = candles_by_time.get(timestamp)
        if not candle:
            continue
        contexts[timestamp] = {
            "volatility_regime": classify_volatility_regime(feature),
            "market_regime": classify_market_regime(candle, feature),
        }
    return contexts


def classify_volatility_regime(feature: dict[str, Any]) -> str:
    volatility = feature.get("volatility_20")
    if volatility is None:
        return "unknown"
    value = Decimal(volatility)
    if value >= Decimal("0.02"):
        return "high_volatility"
    if value <= Decimal("0.01"):
        return "low_volatility"
    return "normal_volatility"


def classify_market_regime(candle: dict[str, Any], feature: dict[str, Any]) -> str:
    ema_50 = feature.get("ema_50")
    returns_5 = feature.get("returns_5")
    if ema_50 is None or returns_5 is None:
        return "unknown"
    close = Decimal(candle["close"])
    ema = Decimal(ema_50)
    momentum = Decimal(returns_5)
    if close > ema and momentum > 0:
        return "bull"
    if close < ema and momentum < 0:
        return "bear"
    return "sideways"


def compare_by_year(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for trade in trades:
        grouped.setdefault(trade["exit_time"].year, []).append(trade)
    return [{"year": year, "metrics": metrics_for_trades(rows)} for year, rows in sorted(grouped.items())]


def compare_by_regime(trades: list[dict[str, Any]], context_by_time: dict[Any, dict[str, str]], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        context = context_by_time.get(trade["entry_time"], {})
        regime = context.get(key, "unknown")
        grouped.setdefault(regime, []).append(trade)
    return [{"regime": regime, "metrics": metrics_for_trades(rows)} for regime, rows in sorted(grouped.items())]


def metrics_for_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return empty_scorecard()
    initial = Decimal("10000")
    equity = initial
    equity_curve = [equity]
    normalized_trades = []
    for trade in trades:
        pnl = Decimal(trade["pnl"])
        equity += pnl
        equity_curve.append(equity)
        normalized = dict(trade)
        normalized["pnl_pct"] = pnl / initial
        normalized_trades.append(normalized)
    return calculate_metrics(initial, equity, normalized_trades, equity_curve)


def empty_scorecard() -> dict[str, Any]:
    return {
        "initial_equity": 10000.0,
        "final_equity": 10000.0,
        "total_return": 0.0,
        "win_rate": 0.0,
        "average_win": 0.0,
        "average_loss": 0.0,
        "profit_factor": None,
        "max_drawdown": 0.0,
        "sharpe_ratio": None,
        "number_of_trades": 0,
        "expectancy_per_trade": 0.0,
        "longest_losing_streak": 0,
        "average_holding_time_hours": 0.0,
    }


def score_metrics(metrics: dict[str, Any]) -> float:
    profit_factor = finite_metric(metrics.get("profit_factor"))
    expectancy = finite_metric(metrics.get("expectancy_per_trade"))
    max_drawdown = finite_metric(metrics.get("max_drawdown"))
    sharpe = finite_metric(metrics.get("sharpe_ratio"))
    win_rate = finite_metric(metrics.get("win_rate"))
    trade_count = finite_metric(metrics.get("number_of_trades"))
    losing_streak = finite_metric(metrics.get("longest_losing_streak"))
    low_sample_penalty = max(0.0, 20.0 - trade_count) * 3.0

    return (
        profit_factor * 3.0
        + expectancy * 0.05
        - max_drawdown * 3.0
        + sharpe
        + win_rate
        + min(trade_count, 100.0) * 0.01
        - losing_streak * 0.05
        - low_sample_penalty
    )


def finite_metric(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return 0.0
    return parsed


def recommend_strategy(metrics: dict[str, Any]) -> str:
    profit_factor = finite_metric(metrics.get("profit_factor"))
    expectancy = finite_metric(metrics.get("expectancy_per_trade"))
    max_drawdown = finite_metric(metrics.get("max_drawdown"))
    trade_count = int(finite_metric(metrics.get("number_of_trades")))
    if profit_factor >= 1.25 and expectancy > 0 and max_drawdown <= 0.2 and trade_count >= 30:
        return "Candidate for Paper Trading"
    if profit_factor >= 0.9 and trade_count >= 20:
        return "Needs More Research"
    return "Reject"


def build_comparison_charts(ranked: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    top = ranked[:10]
    return {
        "profit_factor": chart_rows(top, "profit_factor"),
        "expectancy": chart_rows(top, "expectancy_per_trade"),
        "drawdown": chart_rows(top, "max_drawdown"),
        "trade_count": chart_rows(top, "number_of_trades"),
        "win_rate": chart_rows(top, "win_rate"),
    }


def chart_rows(rows: list[dict[str, Any]], metric: str) -> list[dict[str, Any]]:
    return [
        {
            "run_id": row["run_id"],
            "rank": row["rank"],
            "strategy_name": row["strategy_name"],
            "value": row["metrics"].get(metric),
        }
        for row in rows
    ]


def build_markdown_report(run: dict[str, Any]) -> str:
    metrics = run["metrics"]
    strengths = infer_strengths(metrics)
    weaknesses = infer_weaknesses(metrics)
    failures = infer_failure_analysis(metrics, run["by_market_regime"], run["by_volatility_regime"])
    return "\n".join(
        [
            f"# {run['strategy_name']}_{run['strategy_version']} Research Report",
            "",
            "## Executive Summary",
            f"{run['description']} Recommendation: {run['recommendation']}.",
            "",
            "## Metrics",
            f"- Profit Factor: {format_metric(metrics.get('profit_factor'))}",
            f"- Expectancy: {format_metric(metrics.get('expectancy_per_trade'))}",
            f"- Max Drawdown: {format_metric(metrics.get('max_drawdown'))}",
            f"- Sharpe Ratio: {format_metric(metrics.get('sharpe_ratio'))}",
            f"- Win Rate: {format_metric(metrics.get('win_rate'))}",
            f"- Trade Count: {metrics.get('number_of_trades')}",
            f"- Average Win: {format_metric(metrics.get('average_win'))}",
            f"- Average Loss: {format_metric(metrics.get('average_loss'))}",
            f"- Longest Losing Streak: {metrics.get('longest_losing_streak')}",
            f"- Average Holding Time Hours: {format_metric(metrics.get('average_holding_time_hours'))}",
            "",
            "## Equity Curve Summary",
            f"- Points: {run['equity_curve_summary'].get('points')}",
            f"- Start: {format_metric(run['equity_curve_summary'].get('start'))}",
            f"- End: {format_metric(run['equity_curve_summary'].get('end'))}",
            f"- High: {format_metric(run['equity_curve_summary'].get('high'))}",
            f"- Low: {format_metric(run['equity_curve_summary'].get('low'))}",
            "",
            "## Strengths",
            bullet_list(strengths),
            "",
            "## Weaknesses",
            bullet_list(weaknesses),
            "",
            "## Failure Analysis",
            bullet_list(failures),
            "",
            "## Recommendation",
            run["recommendation"],
        ]
    )


def build_library_markdown_report(ranked: list[dict[str, Any]]) -> str:
    if not ranked:
        return "# Strategy Research Report\n\nNo strategy runs were generated."
    top = ranked[0]
    lines = [
        "# Strategy Research Report",
        "",
        "## Executive Summary",
        f"Compared {len(ranked)} deterministic strategy runs. Top-ranked run is {top['run_id']} with recommendation: {top['recommendation']}.",
        "",
        "## Metrics",
    ]
    for row in ranked:
        metrics = row["metrics"]
        lines.append(
            f"- {row['rank']}. {row['run_id']}: PF {format_metric(metrics.get('profit_factor'))}, "
            f"Expectancy {format_metric(metrics.get('expectancy_per_trade'))}, "
            f"Max DD {format_metric(metrics.get('max_drawdown'))}, Trades {metrics.get('number_of_trades')}"
        )
    lines.extend(
        [
            "",
            "## Equity Curve Summary",
            f"Best run ended at {format_metric(top['equity_curve_summary'].get('end'))} from {format_metric(top['equity_curve_summary'].get('start'))}.",
            "",
            "## Strengths",
            bullet_list(infer_strengths(top["metrics"])),
            "",
            "## Weaknesses",
            bullet_list(infer_weaknesses(top["metrics"])),
            "",
            "## Failure Analysis",
            bullet_list(infer_failure_analysis(top["metrics"], top["by_market_regime"], top["by_volatility_regime"])),
            "",
            "## Recommendation",
            top["recommendation"],
        ]
    )
    return "\n".join(lines)


def infer_strengths(metrics: dict[str, Any]) -> list[str]:
    strengths = []
    if finite_metric(metrics.get("profit_factor")) >= 1:
        strengths.append("Gross profits exceed gross losses.")
    if finite_metric(metrics.get("max_drawdown")) <= 0.1:
        strengths.append("Drawdown stayed below 10%.")
    if finite_metric(metrics.get("number_of_trades")) >= 30:
        strengths.append("Trade count is large enough for initial research review.")
    return strengths or ["No durable strengths were detected in this deterministic run."]


def infer_weaknesses(metrics: dict[str, Any]) -> list[str]:
    weaknesses = []
    if finite_metric(metrics.get("profit_factor")) < 1:
        weaknesses.append("Profit factor is below 1.0.")
    if finite_metric(metrics.get("expectancy_per_trade")) <= 0:
        weaknesses.append("Expectancy per trade is not positive.")
    if finite_metric(metrics.get("number_of_trades")) < 30:
        weaknesses.append("Trade count is low for robust statistical confidence.")
    if finite_metric(metrics.get("longest_losing_streak")) >= 5:
        weaknesses.append("Longest losing streak may be operationally difficult.")
    return weaknesses or ["No major weakness was detected by the deterministic scorecard."]


def infer_failure_analysis(
    metrics: dict[str, Any],
    by_market_regime: list[dict[str, Any]],
    by_volatility_regime: list[dict[str, Any]],
) -> list[str]:
    failures = []
    weak_market_regimes = [row["regime"] for row in by_market_regime if finite_metric(row["metrics"].get("expectancy_per_trade")) <= 0]
    weak_volatility_regimes = [row["regime"] for row in by_volatility_regime if finite_metric(row["metrics"].get("expectancy_per_trade")) <= 0]
    if weak_market_regimes:
        failures.append(f"Non-positive expectancy in market regimes: {', '.join(weak_market_regimes)}.")
    if weak_volatility_regimes:
        failures.append(f"Non-positive expectancy in volatility regimes: {', '.join(weak_volatility_regimes)}.")
    if finite_metric(metrics.get("average_loss")) >= finite_metric(metrics.get("average_win")) and finite_metric(metrics.get("average_win")) > 0:
        failures.append("Average loss is greater than or equal to average win.")
    return failures or ["No single deterministic failure cluster was isolated."]


def bullet_list(rows: list[str]) -> str:
    return "\n".join(f"- {row}" for row in rows)


def format_metric(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return str(value)
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)
