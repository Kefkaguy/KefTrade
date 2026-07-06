from dataclasses import dataclass
from itertools import product
from typing import Any

from app.services.backtester import run_backtest


RANK_METRICS = (
    "profit_factor",
    "expectancy_per_trade",
    "max_drawdown",
    "sharpe_ratio",
    "win_rate",
    "number_of_trades",
)


@dataclass(frozen=True)
class StrategyCandidate:
    strategy_name: str
    strategy_version: str
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
        if int(params["ema_fast"]) >= int(params["ema_slow"]):
            continue
        if float(params["rsi_min"]) >= float(params["rsi_max"]):
            continue
        variants.append(params)
    return variants


def run_strategy_research(
    candles: list[dict[str, Any]],
    features: list[dict[str, Any]],
    strategy_name: str,
    strategy_version: str,
    base_params: dict[str, Any],
    sweep: dict[str, list[Any]] | None = None,
) -> dict[str, Any]:
    candidates = [
        StrategyCandidate(strategy_name=strategy_name, strategy_version=strategy_version, parameters=params)
        for params in build_parameter_sweep(base_params, sweep)
    ]
    runs = []
    for index, candidate in enumerate(candidates, start=1):
        result = run_backtest(candles, features, candidate.parameters)
        metrics = result["metrics"]
        run_id = f"{candidate.strategy_name}_{candidate.strategy_version}_{index:03d}"
        runs.append(
            {
                "run_id": run_id,
                "strategy_name": candidate.strategy_name,
                "strategy_version": candidate.strategy_version,
                "parameters": candidate.parameters,
                "metrics": metrics,
                "trade_count": metrics["number_of_trades"],
                "rank_score": score_metrics(metrics),
            }
        )

    ranked = sorted(runs, key=lambda row: row["rank_score"], reverse=True)
    for rank, row in enumerate(ranked, start=1):
        row["rank"] = rank

    return {
        "strategy_name": strategy_name,
        "strategy_version": strategy_version,
        "run_count": len(runs),
        "rank_metrics": list(RANK_METRICS),
        "ranking_table": ranked,
        "charts": build_comparison_charts(ranked),
    }


def score_metrics(metrics: dict[str, Any]) -> float:
    profit_factor = finite_metric(metrics.get("profit_factor"))
    expectancy = finite_metric(metrics.get("expectancy_per_trade"))
    max_drawdown = finite_metric(metrics.get("max_drawdown"))
    sharpe = finite_metric(metrics.get("sharpe_ratio"))
    win_rate = finite_metric(metrics.get("win_rate"))
    trade_count = finite_metric(metrics.get("number_of_trades"))

    # Deterministic composite for ordering only; the raw metrics remain the report source of truth.
    return (
        profit_factor * 3.0
        + expectancy * 0.05
        - max_drawdown * 3.0
        + sharpe
        + win_rate
        + min(trade_count, 100.0) * 0.01
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


def build_comparison_charts(ranked: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    top = ranked[:10]
    return {
        "profit_factor": chart_rows(top, "profit_factor"),
        "expectancy": chart_rows(top, "expectancy_per_trade"),
        "drawdown": chart_rows(top, "max_drawdown"),
        "trade_count": chart_rows(top, "number_of_trades"),
    }


def chart_rows(rows: list[dict[str, Any]], metric: str) -> list[dict[str, Any]]:
    return [
        {
            "run_id": row["run_id"],
            "rank": row["rank"],
            "value": row["metrics"].get(metric),
        }
        for row in rows
    ]
