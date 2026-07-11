from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from itertools import product
from math import sqrt
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

PAPER_READY_THRESHOLDS = {
    "profit_factor": 1.25,
    "expectancy_per_trade": 0.0,
    "max_drawdown": 0.2,
    "number_of_trades": 30,
}


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
    regimes: list[dict[str, Any]] | None = None,
    strategy_name: str | None = None,
    strategy_version: str = "v1",
    base_params: dict[str, Any] | None = None,
    sweep: dict[str, list[Any]] | None = None,
    filters: dict[str, str] | None = None,
) -> dict[str, Any]:
    candidates = build_candidates(strategy_name, strategy_version, base_params, sweep)
    context_by_time = build_context_by_time(candles, features, regimes)

    runs = []
    for index, candidate in enumerate(candidates, start=1):
        result = run_backtest(candles, features, candidate.parameters, candidate.strategy.decide)
        metrics = scorecard_from_result(result)
        run_id = f"{candidate.strategy.name}_{candidate.strategy.version}_{index:03d}"
        by_year = compare_by_year(result["trades"])
        by_volatility_regime = compare_by_regime(result["trades"], context_by_time, "volatility_regime")
        by_market_regime = compare_by_regime(result["trades"], context_by_time, "trend_regime")
        by_trend_strength = compare_by_regime(result["trades"], context_by_time, "trend_strength_bucket")
        enriched_trades = build_trade_explorer(result["trades"], context_by_time, filters)
        feature_correlations = calculate_feature_correlations(enriched_trades)
        dashboard = build_research_dashboard(result, enriched_trades)
        paper_readiness = paper_readiness_report(metrics, by_market_regime, by_volatility_regime)
        recommendation = recommend_strategy(metrics, paper_readiness)
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
            "by_trend_strength": by_trend_strength,
            "feature_correlations": feature_correlations,
            "trade_explorer": enriched_trades,
            "filter_options": build_filter_options(result["trades"], context_by_time),
            "dashboard": dashboard,
            "paper_readiness": paper_readiness,
            "why_not_paper_ready": paper_readiness["failed_reasons"],
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
        "dashboard": build_library_dashboard(ranked),
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


def build_context_by_time(
    candles: list[dict[str, Any]],
    features: list[dict[str, Any]],
    regimes: list[dict[str, Any]] | None = None,
) -> dict[Any, dict[str, Any]]:
    if any(not isinstance(row, dict) for row in candles + features):
        return {}
    candles_by_time = {row["timestamp"]: row for row in candles}
    regimes_by_time = {row["timestamp"]: row for row in regimes or []}
    contexts = {}
    for feature in features:
        timestamp = feature["timestamp"]
        candle = candles_by_time.get(timestamp)
        if not candle:
            continue
        stored_regime = regimes_by_time.get(timestamp)
        trend_regime = stored_regime["trend_regime"] if stored_regime else classify_market_regime(candle, feature)
        volatility_regime = stored_regime["volatility_regime"] if stored_regime else classify_volatility_regime(feature)
        trend_strength = stored_regime.get("trend_strength") if stored_regime else abs(Decimal(feature.get("distance_from_ema_50") or 0))
        contexts[timestamp] = {
            "volatility_regime": volatility_regime,
            "trend_regime": trend_regime,
            "trend_strength": trend_strength,
            "trend_strength_bucket": classify_trend_strength(trend_strength),
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
        return "bull_trend"
    if close < ema and momentum < 0:
        return "bear_trend"
    return "sideways"


def classify_trend_strength(value: Any) -> str:
    if value is None:
        return "unknown"
    strength = abs(Decimal(value))
    if strength >= Decimal("0.05"):
        return "strong"
    if strength >= Decimal("0.02"):
        return "moderate"
    return "weak"


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


def build_trade_explorer(
    trades: list[dict[str, Any]],
    context_by_time: dict[Any, dict[str, Any]],
    filters: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    rows = []
    for index, trade in enumerate(trades, start=1):
        context = context_by_time.get(trade["entry_time"], {})
        outcome = "winning" if Decimal(trade["pnl"]) > 0 else "losing"
        row = {
            "trade_number": index,
            "symbol": trade["symbol"],
            "side": trade["side"],
            "entry_time": trade["entry_time"],
            "exit_time": trade["exit_time"],
            "entry_price": trade["entry_price"],
            "exit_price": trade["exit_price"],
            "pnl": trade["pnl"],
            "pnl_pct": trade["pnl_pct"],
            "outcome": outcome,
            "entry_reason": trade.get("entry_reason", []),
            "exit_reason": trade["exit_reason"],
            "entry_chart": trade.get("entry_candle", {}),
            "exit_chart": trade.get("exit_candle", {}),
            "indicators": trade.get("indicators", {}),
            "trend_regime": context.get("trend_regime", "unknown"),
            "volatility_regime": context.get("volatility_regime", "unknown"),
            "trend_strength": context.get("trend_strength"),
            "trend_strength_bucket": context.get("trend_strength_bucket", "unknown"),
        }
        if row_matches_filters(row, filters):
            rows.append(row)
    return rows


def row_matches_filters(row: dict[str, Any], filters: dict[str, str] | None) -> bool:
    if not filters:
        return True
    for key, expected in filters.items():
        if expected and row.get(key) != expected:
            return False
    return True


def build_filter_options(trades: list[dict[str, Any]], context_by_time: dict[Any, dict[str, Any]]) -> dict[str, list[str]]:
    explorer = build_trade_explorer(trades, context_by_time)
    return {
        "trend_regime": sorted(unique_values(row["trend_regime"] for row in explorer)),
        "volatility_regime": sorted(unique_values(row["volatility_regime"] for row in explorer)),
        "trend_strength_bucket": sorted(unique_values(row["trend_strength_bucket"] for row in explorer)),
        "outcome": sorted(unique_values(row["outcome"] for row in explorer)),
    }


def unique_values(values: Iterable[str]) -> set[str]:
    return {value for value in values if value}


def calculate_feature_correlations(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = ["rsi_14", "distance_from_ema_20", "distance_from_ema_50", "macd", "volume_change", "volatility_20"]
    rows = []
    for field in fields:
        x_values = []
        y_values = []
        for trade in trades:
            value = trade.get("indicators", {}).get(field)
            if value is None:
                continue
            x_values.append(float(value))
            y_values.append(1.0 if Decimal(trade["pnl"]) > 0 else 0.0)
        rows.append({"feature": field, "correlation_to_profitable_trade": pearson(x_values, y_values), "sample_size": len(x_values)})
    return rows


def pearson(x_values: list[float], y_values: list[float]) -> float | None:
    if len(x_values) < 2 or len(y_values) < 2:
        return None
    x_mean = sum(x_values) / len(x_values)
    y_mean = sum(y_values) / len(y_values)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_values, y_values))
    x_var = sum((x - x_mean) ** 2 for x in x_values)
    y_var = sum((y - y_mean) ** 2 for y in y_values)
    denominator = sqrt(x_var * y_var)
    return numerator / denominator if denominator else None


def build_research_dashboard(result: dict[str, Any], trades: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "equity_curve": result.get("equity_curve", []),
        "drawdown_curve": result.get("drawdown_curve", []),
        "monthly_returns": calculate_monthly_returns(trades),
        "rolling_profit_factor": rolling_metric(trades, "profit_factor", window=10),
        "rolling_win_rate": rolling_metric(trades, "win_rate", window=10),
        "strategy_heatmap": [],
        "regime_heatmap": build_regime_heatmap(trades),
    }


def calculate_monthly_returns(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, Decimal] = {}
    for trade in trades:
        key = f"{trade['exit_time'].year:04d}-{trade['exit_time'].month:02d}"
        grouped[key] = grouped.get(key, Decimal("0")) + Decimal(trade["pnl_pct"])
    return [{"month": month, "return": float(value)} for month, value in sorted(grouped.items())]


def rolling_metric(trades: list[dict[str, Any]], metric: str, window: int) -> list[dict[str, Any]]:
    rows = []
    for index in range(len(trades)):
        sample = trades[max(0, index - window + 1) : index + 1]
        metrics = metrics_for_trades(sample)
        rows.append({"trade_number": index + 1, metric: metrics.get(metric)})
    return rows


def build_regime_heatmap(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for trade in trades:
        key = (trade["trend_regime"], trade["volatility_regime"])
        grouped.setdefault(key, []).append(trade)
    rows = []
    for (trend_regime, volatility_regime), grouped_trades in sorted(grouped.items()):
        metrics = metrics_for_trades(grouped_trades)
        rows.append(
            {
                "trend_regime": trend_regime,
                "volatility_regime": volatility_regime,
                "trade_count": metrics["number_of_trades"],
                "expectancy": metrics["expectancy_per_trade"],
                "profit_factor": metrics["profit_factor"],
            }
        )
    return rows


def build_library_dashboard(ranked: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "strategy_heatmap": [
            {
                "strategy": f"{row['strategy_name']}_{row['strategy_version']}",
                "profit_factor": row["metrics"].get("profit_factor"),
                "expectancy": row["metrics"].get("expectancy_per_trade"),
                "max_drawdown": row["metrics"].get("max_drawdown"),
                "trade_count": row["metrics"].get("number_of_trades"),
            }
            for row in ranked
        ],
        "regime_heatmap": [
            {
                "strategy": f"{row['strategy_name']}_{row['strategy_version']}",
                **heatmap_row,
            }
            for row in ranked
            for heatmap_row in row["dashboard"]["regime_heatmap"]
        ],
    }


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
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "profit_factor": None,
        "profit_factor_is_infinite": False,
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


def recommend_strategy(metrics: dict[str, Any], paper_readiness: dict[str, Any] | None = None) -> str:
    if paper_readiness is None:
        paper_readiness = paper_readiness_report(metrics)
    profit_factor = finite_metric(metrics.get("profit_factor"))
    expectancy = finite_metric(metrics.get("expectancy_per_trade"))
    trade_count = int(finite_metric(metrics.get("number_of_trades")))
    if paper_readiness["paper_ready"]:
        return "Candidate for Paper Trading"
    if profit_factor >= 1.05 and expectancy > 0 and trade_count >= 15:
        return "Needs More Research"
    if profit_factor >= 0.9 and trade_count >= 20:
        return "Needs More Research"
    return "Reject"


def paper_readiness_report(
    metrics: dict[str, Any],
    by_market_regime: list[dict[str, Any]] | None = None,
    by_volatility_regime: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    checks = [
        readiness_check(
            "profit_factor",
            finite_metric(metrics.get("profit_factor")) >= PAPER_READY_THRESHOLDS["profit_factor"],
            f"Profit factor {format_metric(metrics.get('profit_factor'))} must be >= {PAPER_READY_THRESHOLDS['profit_factor']}.",
        ),
        readiness_check(
            "positive_expectancy",
            finite_metric(metrics.get("expectancy_per_trade")) > PAPER_READY_THRESHOLDS["expectancy_per_trade"],
            f"Expectancy {format_metric(metrics.get('expectancy_per_trade'))} must be positive.",
        ),
        readiness_check(
            "drawdown",
            finite_metric(metrics.get("max_drawdown")) <= PAPER_READY_THRESHOLDS["max_drawdown"],
            f"Max drawdown {format_metric(metrics.get('max_drawdown'))} must be <= {PAPER_READY_THRESHOLDS['max_drawdown']}.",
        ),
        readiness_check(
            "trade_count",
            finite_metric(metrics.get("number_of_trades")) >= PAPER_READY_THRESHOLDS["number_of_trades"],
            f"Trade count {int(finite_metric(metrics.get('number_of_trades')))} must be >= {PAPER_READY_THRESHOLDS['number_of_trades']}.",
        ),
        readiness_check(
            "walk_forward_oos",
            bool((metrics.get("walk_forward") or {}).get("enabled")),
            "Walk-forward/OOS validation window must be available.",
        ),
        readiness_check(
            "regime_stability",
            regime_stability_passes(by_market_regime or [], by_volatility_regime or []),
            regime_stability_detail(by_market_regime or [], by_volatility_regime or []),
        ),
    ]
    failed_reasons = [check["detail"] for check in checks if not check["passed"]]
    return {
        "thresholds": PAPER_READY_THRESHOLDS,
        "checks": checks,
        "paper_ready": not failed_reasons,
        "failed_reasons": failed_reasons,
    }


def readiness_check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": passed, "detail": detail}


def regime_stability_passes(by_market_regime: list[dict[str, Any]], by_volatility_regime: list[dict[str, Any]]) -> bool:
    material_rows = [
        row
        for row in [*by_market_regime, *by_volatility_regime]
        if finite_metric(row.get("metrics", {}).get("number_of_trades")) >= 5
    ]
    if not material_rows:
        return True
    return all(
        finite_metric(row.get("metrics", {}).get("expectancy_per_trade")) > 0
        and finite_metric(row.get("metrics", {}).get("profit_factor")) >= 1
        for row in material_rows
    )


def regime_stability_detail(by_market_regime: list[dict[str, Any]], by_volatility_regime: list[dict[str, Any]]) -> str:
    weak_rows = [
        row["regime"]
        for row in [*by_market_regime, *by_volatility_regime]
        if finite_metric(row.get("metrics", {}).get("number_of_trades")) >= 5
        and (
            finite_metric(row.get("metrics", {}).get("expectancy_per_trade")) <= 0
            or finite_metric(row.get("metrics", {}).get("profit_factor")) < 1
        )
    ]
    if weak_rows:
        return f"Material regimes with weak expectancy/profit factor: {', '.join(sorted(set(weak_rows)))}."
    return "No material regime bucket failed profit-factor or expectancy stability checks."


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
            "## Why Not Paper-Ready",
            bullet_list(run["why_not_paper_ready"] or ["All paper-readiness gates passed."]),
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
            "## Why Not Paper-Ready",
            bullet_list(top["why_not_paper_ready"] or ["All paper-readiness gates passed."]),
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
