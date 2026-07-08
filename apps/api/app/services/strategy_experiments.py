from dataclasses import dataclass
from typing import Any

from app.services.strategy import get_strategy_library
from app.services.strategy_research import run_strategy_research


@dataclass(frozen=True)
class StrategyExperiment:
    id: str
    strategy: str
    title: str
    hypothesis: str
    variables: list[str]
    sweep: dict[str, list[Any]]
    rationale: str


def get_strategy_experiments() -> list[StrategyExperiment]:
    return [
        StrategyExperiment(
            id="trend_pullback_rsi_ema_exit_sweep",
            strategy="trend_pullback",
            title="Trend Pullback RSI, EMA, and Exit Geometry Sweep",
            hypothesis="Trend pullback losses may come from entering shallow pullbacks with a low hit rate and a stop/target geometry that does not offset frequent stop-outs.",
            variables=["rsi_min", "rsi_max", "ema_fast", "ema_slow", "entry_distance_to_ema20_max", "swing_lookback", "risk_reward"],
            sweep={
                "rsi_min": [35, 40, 45],
                "rsi_max": [55, 60, 65],
                "ema_fast": [10, 20, 30],
                "ema_slow": [50, 100],
                "entry_distance_to_ema20_max": [0.01, 0.015, 0.025],
                "swing_lookback": [5, 8, 13],
                "risk_reward": [1.5, 2.0, 2.5],
            },
            rationale="Tests whether better pullback depth, slower trend filters, wider stops, or different reward multiples improve expectancy without changing validation standards.",
        ),
        StrategyExperiment(
            id="breakout_lookback_volume_exit_sweep",
            strategy="breakout",
            title="Breakout Lookback, Volume, and Exit Sweep",
            hypothesis="Breakouts may be buying exhaustion moves; lookback length, volume confirmation, and payoff target need falsification.",
            variables=["breakout_lookback", "volume_change_min", "swing_lookback", "risk_reward"],
            sweep={
                "breakout_lookback": [12, 20, 34, 55],
                "volume_change_min": [0.0, 0.05, 0.15, 0.3],
                "swing_lookback": [5, 10, 20],
                "risk_reward": [1.2, 1.5, 2.0, 2.5],
            },
            rationale="Separates false breakouts from confirmed participation and tests whether shorter targets are more realistic than fixed 2R exits.",
        ),
        StrategyExperiment(
            id="mean_reversion_activation_sweep",
            strategy="mean_reversion",
            title="Mean Reversion Activation Sweep",
            hypothesis="Mean reversion currently fires too rarely or not at all because the oversold and EMA stretch gates are too restrictive for the tested market.",
            variables=["rsi_oversold", "distance_from_ema_20_min", "swing_lookback", "risk_reward"],
            sweep={
                "rsi_oversold": [30, 35, 40, 45],
                "distance_from_ema_20_min": [-0.015, -0.025, -0.04, -0.06],
                "swing_lookback": [5, 10, 20],
                "risk_reward": [1.0, 1.25, 1.5, 2.0],
            },
            rationale="Tests whether the strategy is too selective and whether mean-reversion exits need smaller profit targets than trend systems.",
        ),
        StrategyExperiment(
            id="momentum_trend_return_sweep",
            strategy="momentum",
            title="Momentum Return, Trend, and Stop Sweep",
            hypothesis="Momentum entries may be late; return thresholds and trend filters should be tested for earlier confirmation or fewer exhaustion entries.",
            variables=["returns_5_min", "ema_slow", "swing_lookback", "risk_reward"],
            sweep={
                "returns_5_min": [0.005, 0.01, 0.02, 0.035],
                "ema_slow": [50, 100],
                "swing_lookback": [5, 8, 13, 21],
                "risk_reward": [1.2, 1.5, 2.0, 2.5],
            },
            rationale="Tests whether smaller return triggers reduce late entries and whether wider swing stops reduce noise exits.",
        ),
        StrategyExperiment(
            id="volatility_breakout_filter_sweep",
            strategy="volatility_breakout",
            title="Volatility Breakout Filter Sweep",
            hypothesis="Volatility breakouts may be entering high-volatility noise without enough directional confirmation.",
            variables=["breakout_lookback", "volatility_20_min", "volume_change_min", "risk_reward"],
            sweep={
                "breakout_lookback": [8, 12, 20, 34],
                "volatility_20_min": [0.01, 0.015, 0.02, 0.03],
                "volume_change_min": [0.0, 0.1, 0.25, 0.5],
                "risk_reward": [1.2, 1.5, 2.0, 2.5],
            },
            rationale="Tests whether volatility and volume filters are selecting tradable expansion or simply expensive noise.",
        ),
        StrategyExperiment(
            id="trend_200ema_momentum_exit_sweep",
            strategy="trend_following_200ema",
            title="200 EMA Trend Momentum and Exit Sweep",
            hypothesis="The 200 EMA strategy has very low sample size and poor hit rate; momentum threshold and stop/target geometry need falsification.",
            variables=["returns_5_min", "swing_lookback", "risk_reward"],
            sweep={
                "returns_5_min": [0.0, 0.005, 0.01, 0.02],
                "swing_lookback": [10, 20, 34],
                "risk_reward": [1.5, 2.0, 2.5, 3.0],
            },
            rationale="Tests whether broad trend activation is too selective and whether larger trend targets compensate for low hit rate.",
        ),
    ]


def list_strategy_experiments(strategy: str | None = None) -> list[dict[str, Any]]:
    experiments = get_strategy_experiments()
    if strategy:
        experiments = [experiment for experiment in experiments if experiment.strategy == strategy]
    return [serialize_experiment(experiment) for experiment in experiments]


def run_strategy_experiment(
    candles: list[dict[str, Any]],
    features: list[dict[str, Any]],
    regimes: list[dict[str, Any]] | None,
    experiment_id: str,
    max_runs: int = 120,
) -> dict[str, Any]:
    experiment = get_experiment(experiment_id)
    strategy = get_strategy_library()[f"{experiment.strategy}_v1"]
    limited_sweep = limit_sweep(experiment.sweep, max_runs=max_runs)
    report = run_strategy_research(
        candles=candles,
        features=features,
        regimes=regimes,
        strategy_name=experiment.strategy,
        strategy_version="v1",
        base_params=strategy.parameters,
        sweep=limited_sweep,
    )
    return {
        "experiment": serialize_experiment(experiment),
        "max_runs": max_runs,
        "effective_sweep": limited_sweep,
        **report,
        "research_takeaways": build_experiment_takeaways(report),
        "experiment_report": build_experiment_report(experiment, report),
    }


def get_experiment(experiment_id: str) -> StrategyExperiment:
    for experiment in get_strategy_experiments():
        if experiment.id == experiment_id:
            return experiment
    supported = ", ".join(experiment.id for experiment in get_strategy_experiments())
    raise ValueError(f"Unsupported strategy experiment '{experiment_id}'. Supported experiments: {supported}")


def serialize_experiment(experiment: StrategyExperiment) -> dict[str, Any]:
    return {
        "id": experiment.id,
        "strategy": experiment.strategy,
        "title": experiment.title,
        "hypothesis": experiment.hypothesis,
        "variables": experiment.variables,
        "sweep": experiment.sweep,
        "rationale": experiment.rationale,
    }


def limit_sweep(sweep: dict[str, list[Any]], max_runs: int) -> dict[str, list[Any]]:
    limited: dict[str, list[Any]] = {}
    combinations = 1
    for key, values in sweep.items():
        kept = []
        for value in values:
            if combinations * max(1, len(kept) + 1) > max_runs and kept:
                break
            kept.append(value)
        limited[key] = kept or values[:1]
        combinations *= len(limited[key])
    return limited


def build_experiment_takeaways(report: dict[str, Any]) -> list[str]:
    rows = report.get("ranking_table", [])
    if not rows:
        return ["No experiment runs were generated."]
    best = rows[0]
    metrics = best["metrics"]
    takeaways = [
        f"Best run {best['run_id']} had profit factor {metrics.get('profit_factor')} and {metrics.get('number_of_trades')} trades.",
    ]
    if metrics.get("profit_factor") is not None and float(metrics["profit_factor"]) < 1:
        takeaways.append("All tested variants remain below break-even profit factor; reject or test a different hypothesis before validation.")
    if int(float(metrics.get("number_of_trades") or 0)) < 30:
        takeaways.append("Best variant is still too selective for robust research confidence.")
    if float(metrics.get("expectancy_per_trade") or 0) <= 0:
        takeaways.append("Best variant still has non-positive expectancy after fees and slippage.")
    return takeaways


def build_experiment_report(experiment: StrategyExperiment, report: dict[str, Any]) -> str:
    rows = report.get("ranking_table", [])
    if not rows:
        return f"# {experiment.title}\n\nNo experiment runs were generated."
    best = rows[0]
    baseline = rows[-1]
    best_metrics = best["metrics"]
    baseline_metrics = baseline["metrics"]
    changed = ", ".join(
        f"{key}={best['parameters'].get(key)}"
        for key in experiment.variables
        if key in best.get("parameters", {})
    )
    pf_delta = finite_float(best_metrics.get("profit_factor")) - finite_float(baseline_metrics.get("profit_factor"))
    trade_delta = int(finite_float(best_metrics.get("number_of_trades"))) - int(finite_float(baseline_metrics.get("number_of_trades")))
    drawdown_delta = finite_float(best_metrics.get("max_drawdown")) - finite_float(baseline_metrics.get("max_drawdown"))
    robust = (
        finite_float(best_metrics.get("profit_factor")) >= 1.0
        and finite_float(best_metrics.get("number_of_trades")) >= 30
        and finite_float(best_metrics.get("expectancy_per_trade")) > 0
    )
    return "\n".join(
        [
            f"# {experiment.title}",
            "",
            "## What Changed",
            changed or "No sweep parameters were changed.",
            "",
            "## Why Performance Changed",
            f"Best run changed profit factor by {pf_delta:.3f}, trade count by {trade_delta}, and drawdown by {drawdown_delta:.3f} versus the lowest-ranked bounded run.",
            "",
            "## Robustness",
            "Results appear robust enough for broader research." if robust else "Results do not appear robust enough yet; additional evidence is required.",
            "",
            "## Next Step",
            "Send to cross-asset and out-of-sample research before alpha validation." if robust else "Continue research or reject this hypothesis; do not claim an edge.",
        ]
    )


def finite_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else 0.0
