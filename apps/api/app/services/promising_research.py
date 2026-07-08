from dataclasses import dataclass
from typing import Any

import psycopg

from app.services.alpha_validation import DEFAULT_VALIDATION_THRESHOLDS
from app.services.backtester import combine_candles_features, run_backtest
from app.services.features import load_candles
from app.services.regimes import load_regimes
from app.services.strategy import get_strategy_library
from app.services.strategy_experiments import StrategyExperiment, get_strategy_experiments, limit_sweep
from app.services.strategy_research import build_parameter_sweep, finite_metric


@dataclass(frozen=True)
class ResearchDataset:
    symbol: str
    timeframe: str
    candles: list[dict[str, Any]]
    features: list[dict[str, Any]]
    regimes: list[dict[str, Any]]


def build_promising_research_candidates(
    conn: psycopg.Connection,
    max_candidates: int = 36,
    max_runs_per_experiment: int = 8,
    train_ratio: float = 0.7,
    fold_count: int = 3,
) -> dict[str, Any]:
    datasets = load_available_research_datasets(conn)
    candidate_specs = build_candidate_specs(max_candidates=max_candidates, max_runs_per_experiment=max_runs_per_experiment)
    rows = [evaluate_candidate(spec, datasets, train_ratio, fold_count) for spec in candidate_specs]
    ranked = sorted(rows, key=lambda row: row["research_score"], reverse=True)
    for rank, row in enumerate(ranked, start=1):
        row["rank"] = rank
    summary = summarize_promising_research(ranked, datasets)
    return {
        "summary": summary,
        "datasets": [{"symbol": dataset.symbol, "timeframe": dataset.timeframe, "candles": len(dataset.candles), "features": len(dataset.features)} for dataset in datasets],
        "thresholds": DEFAULT_VALIDATION_THRESHOLDS,
        "rank_metrics": [
            "research_score",
            "profit_factor",
            "expectancy_per_trade",
            "stability_score",
            "max_drawdown",
            "number_of_trades",
            "cross_asset_consistency",
            "out_of_sample_score",
        ],
        "candidates": ranked,
        "markdown_report": build_promising_research_markdown(summary, ranked[:10]),
    }


def load_available_research_datasets(conn: psycopg.Connection) -> list[ResearchDataset]:
    rows = conn.execute(
        """
        SELECT c.symbol, c.timeframe, COUNT(*) AS candle_count, COUNT(f.timestamp) AS feature_count
        FROM candles c
        LEFT JOIN features f
            ON f.symbol = c.symbol
           AND f.timeframe = c.timeframe
           AND f.timestamp = c.timestamp
        GROUP BY c.symbol, c.timeframe
        HAVING COUNT(*) >= 120 AND COUNT(f.timestamp) >= 80
        ORDER BY c.symbol, c.timeframe
        """
    ).fetchall()
    datasets = []
    for row in rows:
        symbol = row["symbol"]
        timeframe = row["timeframe"]
        candles = load_candles(conn, symbol=symbol, timeframe=timeframe)
        features = conn.execute(
            """
            SELECT *
            FROM features
            WHERE symbol = %s AND timeframe = %s
            ORDER BY timestamp ASC
            """,
            (symbol, timeframe),
        ).fetchall()
        datasets.append(
            ResearchDataset(
                symbol=symbol,
                timeframe=timeframe,
                candles=candles,
                features=list(features),
                regimes=load_regimes(conn, symbol=symbol, timeframe=timeframe),
            )
        )
    return datasets


def build_candidate_specs(max_candidates: int, max_runs_per_experiment: int) -> list[dict[str, Any]]:
    specs = []
    library = get_strategy_library()
    for experiment in get_strategy_experiments():
        strategy = library[f"{experiment.strategy}_v1"]
        sweep = limit_sweep(experiment.sweep, max_runs=max_runs_per_experiment)
        for index, params in enumerate(build_parameter_sweep(strategy.parameters, sweep), start=1):
            specs.append(
                {
                    "candidate_id": f"{experiment.id}_{index:03d}",
                    "experiment": experiment,
                    "strategy": strategy,
                    "parameters": params,
                }
            )
            if len(specs) >= max_candidates:
                return specs
    return specs


def evaluate_candidate(spec: dict[str, Any], datasets: list[ResearchDataset], train_ratio: float, fold_count: int) -> dict[str, Any]:
    experiment: StrategyExperiment = spec["experiment"]
    full_results = []
    train_test_results = []
    all_metrics = []
    for dataset in datasets:
        full = run_dataset_backtest(dataset, spec["parameters"], spec["strategy"].decide)
        train, test = run_train_test(dataset, spec["parameters"], spec["strategy"].decide, train_ratio)
        full_results.append({"symbol": dataset.symbol, "timeframe": dataset.timeframe, "metrics": full})
        train_test_results.append({"symbol": dataset.symbol, "timeframe": dataset.timeframe, "train": train, "test": test})
        all_metrics.append(full)

    aggregate = aggregate_metrics(all_metrics)
    cross_asset = cross_asset_consistency(full_results)
    timeframe = timeframe_consistency(full_results)
    oos = out_of_sample_score(train_test_results)
    stability = stability_score(full_results)
    walk_forward = walk_forward_summary(datasets, spec["parameters"], spec["strategy"].decide, fold_count)
    score = research_score(aggregate, stability, cross_asset, oos)
    worked = [f"{row['symbol']} {row['timeframe']}" for row in full_results if profitable(row["metrics"])]
    failed = [f"{row['symbol']} {row['timeframe']}" for row in full_results if not profitable(row["metrics"])]
    report = build_candidate_report(experiment, spec["parameters"], aggregate, full_results, train_test_results, walk_forward, score)
    return {
        "rank": 0,
        "candidate_id": spec["candidate_id"],
        "experiment_id": experiment.id,
        "strategy_name": experiment.strategy,
        "title": experiment.title,
        "parameters": {key: spec["parameters"].get(key) for key in experiment.variables if key in spec["parameters"]},
        "aggregate_metrics": aggregate,
        "research_score": score,
        "stability_score": stability,
        "cross_asset_consistency": cross_asset,
        "timeframe_consistency": timeframe,
        "out_of_sample_score": oos,
        "dataset_results": full_results,
        "train_test_results": train_test_results,
        "walk_forward": walk_forward,
        "assets_worked": worked,
        "assets_failed": failed,
        "validation_status": validation_status(aggregate, stability, cross_asset, oos),
        "evidence_summary": evidence_summary(aggregate, stability, cross_asset, oos, len(full_results)),
        "recommended_next_experiment": recommended_next_experiment(aggregate, stability, cross_asset, oos),
        "research_report": report,
    }


def run_dataset_backtest(dataset: ResearchDataset, params: dict[str, Any], decide: Any) -> dict[str, Any]:
    if not dataset.candles or not dataset.features:
        return empty_metrics()
    return run_backtest(dataset.candles, dataset.features, params, decide)["metrics"]


def run_train_test(dataset: ResearchDataset, params: dict[str, Any], decide: Any, train_ratio: float) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = combine_candles_features(dataset.candles, dataset.features)
    if len(rows) < 120:
        return empty_metrics(), empty_metrics()
    split = max(60, min(len(rows) - 40, int(len(rows) * train_ratio)))
    train_rows = rows[:split]
    test_rows = rows[split:]
    return (
        run_backtest([row["candle"] for row in train_rows], [row["feature"] for row in train_rows], params, decide)["metrics"],
        run_backtest([row["candle"] for row in test_rows], [row["feature"] for row in test_rows], params, decide)["metrics"],
    )


def walk_forward_summary(datasets: list[ResearchDataset], params: dict[str, Any], decide: Any, fold_count: int) -> dict[str, Any]:
    folds = []
    for dataset in datasets:
        rows = combine_candles_features(dataset.candles, dataset.features)
        if len(rows) < 180:
            continue
        window = len(rows) // (fold_count + 1)
        for fold in range(fold_count):
            train_start = fold * window
            train_end = train_start + (window * 2)
            test_end = min(len(rows), train_end + window)
            if test_end - train_end < 40:
                continue
            train_rows = rows[train_start:train_end]
            test_rows = rows[train_end:test_end]
            train_metrics = run_backtest([row["candle"] for row in train_rows], [row["feature"] for row in train_rows], params, decide)["metrics"]
            test_metrics = run_backtest([row["candle"] for row in test_rows], [row["feature"] for row in test_rows], params, decide)["metrics"]
            folds.append(
                {
                    "symbol": dataset.symbol,
                    "timeframe": dataset.timeframe,
                    "fold": fold + 1,
                    "train": compact_metrics(train_metrics),
                    "test": compact_metrics(test_metrics),
                    "passed_oos": profitable(test_metrics),
                }
            )
    passed = sum(1 for fold in folds if fold["passed_oos"])
    return {"fold_count": len(folds), "passed_oos_folds": passed, "pass_rate": passed / len(folds) if folds else 0.0, "folds": folds[:24]}


def aggregate_metrics(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    if not metrics:
        return empty_metrics()
    return {
        "profit_factor": average([finite_metric(row.get("profit_factor")) for row in metrics]),
        "expectancy_per_trade": average([finite_metric(row.get("expectancy_per_trade")) for row in metrics]),
        "max_drawdown": average([finite_metric(row.get("max_drawdown")) for row in metrics]),
        "number_of_trades": sum(int(finite_metric(row.get("number_of_trades"))) for row in metrics),
        "win_rate": average([finite_metric(row.get("win_rate")) for row in metrics]),
    }


def empty_metrics() -> dict[str, Any]:
    return {"profit_factor": None, "expectancy_per_trade": 0.0, "max_drawdown": 0.0, "number_of_trades": 0, "win_rate": 0.0}


def compact_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "profit_factor": metrics.get("profit_factor"),
        "expectancy_per_trade": metrics.get("expectancy_per_trade"),
        "max_drawdown": metrics.get("max_drawdown"),
        "number_of_trades": metrics.get("number_of_trades"),
    }


def average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def profitable(metrics: dict[str, Any]) -> bool:
    return finite_metric(metrics.get("profit_factor")) >= 1.0 and finite_metric(metrics.get("expectancy_per_trade")) > 0


def cross_asset_consistency(results: list[dict[str, Any]]) -> float:
    by_asset: dict[str, list[dict[str, Any]]] = {}
    for row in results:
        by_asset.setdefault(row["symbol"], []).append(row["metrics"])
    if not by_asset:
        return 0.0
    passing = [symbol for symbol, rows in by_asset.items() if any(profitable(row) for row in rows)]
    return len(passing) / len(by_asset)


def timeframe_consistency(results: list[dict[str, Any]]) -> float:
    by_timeframe: dict[str, list[dict[str, Any]]] = {}
    for row in results:
        by_timeframe.setdefault(row["timeframe"], []).append(row["metrics"])
    if not by_timeframe:
        return 0.0
    passing = [timeframe for timeframe, rows in by_timeframe.items() if any(profitable(row) for row in rows)]
    return len(passing) / len(by_timeframe)


def out_of_sample_score(train_test_results: list[dict[str, Any]]) -> float:
    evaluated = [row for row in train_test_results if finite_metric(row["test"].get("number_of_trades")) > 0]
    if not evaluated:
        return 0.0
    passed = [row for row in evaluated if profitable(row["test"])]
    return len(passed) / len(evaluated)


def stability_score(results: list[dict[str, Any]]) -> float:
    evaluated = [row for row in results if finite_metric(row["metrics"].get("number_of_trades")) > 0]
    if not evaluated:
        return 0.0
    passed = [row for row in evaluated if profitable(row["metrics"])]
    return len(passed) / len(evaluated)


def research_score(metrics: dict[str, Any], stability: float, cross_asset: float, oos: float) -> float:
    trade_count = min(finite_metric(metrics.get("number_of_trades")), 250.0)
    return round(
        finite_metric(metrics.get("profit_factor")) * 22
        + finite_metric(metrics.get("expectancy_per_trade")) * 0.08
        + stability * 20
        + cross_asset * 20
        + oos * 25
        + trade_count * 0.08
        - finite_metric(metrics.get("max_drawdown")) * 35,
        3,
    )


def validation_status(metrics: dict[str, Any], stability: float, cross_asset: float, oos: float) -> str:
    thresholds = DEFAULT_VALIDATION_THRESHOLDS
    if (
        finite_metric(metrics.get("number_of_trades")) >= float(thresholds["min_trades"])
        and finite_metric(metrics.get("profit_factor")) >= float(thresholds["min_profit_factor"])
        and stability >= float(thresholds["min_stability_score"])
        and cross_asset >= 0.5
        and oos >= 0.5
    ):
        return "Research candidate for alpha validation"
    if finite_metric(metrics.get("profit_factor")) >= 1.0 and oos > 0:
        return "Needs more evidence"
    return "Reject for now"


def evidence_summary(metrics: dict[str, Any], stability: float, cross_asset: float, oos: float, dataset_count: int) -> str:
    return (
        f"Evaluated across {dataset_count} datasets. Aggregate PF {finite_metric(metrics.get('profit_factor')):.2f}, "
        f"{int(finite_metric(metrics.get('number_of_trades')))} trades, stability {stability:.2f}, "
        f"cross-asset consistency {cross_asset:.2f}, out-of-sample score {oos:.2f}."
    )


def recommended_next_experiment(metrics: dict[str, Any], stability: float, cross_asset: float, oos: float) -> str:
    if finite_metric(metrics.get("number_of_trades")) < DEFAULT_VALIDATION_THRESHOLDS["min_trades"]:
        return "Increase evidence by testing broader assets/timeframes or less selective activation rules."
    if oos < 0.5:
        return "Reject in-sample-only variants and test more conservative out-of-sample filters."
    if cross_asset < 0.5:
        return "Investigate asset-specific behavior before any validation run."
    if stability < DEFAULT_VALIDATION_THRESHOLDS["min_stability_score"]:
        return "Run regime-specific activation tests to isolate unstable market conditions."
    return "Candidate can be sent to alpha validation for formal evidence gating."


def summarize_promising_research(ranked: list[dict[str, Any]], datasets: list[ResearchDataset]) -> dict[str, Any]:
    return {
        "candidate_count": len(ranked),
        "dataset_count": len(datasets),
        "top_candidate": ranked[0]["candidate_id"] if ranked else None,
        "top_score": ranked[0]["research_score"] if ranked else None,
        "validation_ready_count": sum(1 for row in ranked if row["validation_status"] == "Research candidate for alpha validation"),
    }


def build_candidate_report(
    experiment: StrategyExperiment,
    parameters: dict[str, Any],
    aggregate: dict[str, Any],
    dataset_results: list[dict[str, Any]],
    train_test_results: list[dict[str, Any]],
    walk_forward: dict[str, Any],
    score: float,
) -> str:
    worked = [f"{row['symbol']} {row['timeframe']}" for row in dataset_results if profitable(row["metrics"])]
    failed = [f"{row['symbol']} {row['timeframe']}" for row in dataset_results if not profitable(row["metrics"])]
    changed = ", ".join(f"{key}={parameters.get(key)}" for key in experiment.variables if key in parameters)
    return "\n".join(
        [
            f"# {experiment.title}",
            "",
            "## What Changed",
            changed or "No experiment parameters were changed.",
            "",
            "## Evidence Summary",
            f"Research score: {score}",
            f"Aggregate profit factor: {aggregate.get('profit_factor')}",
            f"Aggregate expectancy: {aggregate.get('expectancy_per_trade')}",
            f"Aggregate trades: {aggregate.get('number_of_trades')}",
            f"Aggregate drawdown: {aggregate.get('max_drawdown')}",
            "",
            "## Robustness",
            f"Worked: {', '.join(worked) if worked else 'None'}",
            f"Failed: {', '.join(failed) if failed else 'None'}",
            f"Walk-forward pass rate: {walk_forward.get('pass_rate')}",
            "",
            "## Conclusion",
            "Additional evidence is required unless formal validation gates pass.",
        ]
    )


def build_promising_research_markdown(summary: dict[str, Any], top_rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Promising Research Candidates",
        "",
        f"Datasets evaluated: {summary['dataset_count']}",
        f"Candidates evaluated: {summary['candidate_count']}",
        "",
        "## Top Candidates",
    ]
    for row in top_rows:
        metrics = row["aggregate_metrics"]
        lines.append(
            f"- {row['candidate_id']}: score={row['research_score']}, PF={metrics.get('profit_factor')}, "
            f"trades={metrics.get('number_of_trades')}, status={row['validation_status']}"
        )
    lines.extend(["", "No edge is claimed unless alpha validation passes."])
    return "\n".join(lines)
