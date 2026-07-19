from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

import psycopg
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb

from app.services.features import load_candles
from app.services.regimes import load_regimes
from app.services.strategy_experiments import get_experiment, get_strategy_experiments, run_strategy_experiment
from app.services.strategy_research import finite_metric

AUTOMATION_VERSION = "research_automation_v1"
DEFAULT_TIMEFRAMES = ("1h", "4h", "1d")
MIN_CANDLES = 120
MIN_FEATURES = 80


def ensure_research_automation_tables(conn: psycopg.Connection) -> None:
    return None


def discover_research_universe(conn: psycopg.Connection, *, asset_limit: int = 100, timeframes: list[str] | None = None) -> list[dict[str, Any]]:
    wanted_timeframes = tuple(timeframes or list(DEFAULT_TIMEFRAMES))
    rows = conn.execute(
        """
        SELECT c.symbol, c.timeframe, s.asset_class, s.name, COUNT(*) AS candle_count, COUNT(f.timestamp) AS feature_count,
               MAX(c.timestamp) AS latest_candle_timestamp
        FROM candles c
        LEFT JOIN features f
          ON f.symbol = c.symbol
         AND f.timeframe = c.timeframe
         AND f.timestamp = c.timestamp
        LEFT JOIN symbols s ON s.symbol = c.symbol
        WHERE (%s IS NULL OR c.timeframe = ANY(%s))
          AND COALESCE(s.is_active, TRUE) = TRUE
        GROUP BY c.symbol, c.timeframe, s.asset_class, s.name
        HAVING COUNT(*) >= %s AND COUNT(f.timestamp) >= %s
        ORDER BY c.symbol, c.timeframe
        LIMIT %s
        """,
        (list(wanted_timeframes), list(wanted_timeframes), MIN_CANDLES, MIN_FEATURES, asset_limit * max(1, len(wanted_timeframes))),
    ).fetchall()
    return [
        {
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "asset_class": row.get("asset_class") or "unknown",
            "name": row.get("name"),
            "candle_count": int(row["candle_count"]),
            "feature_count": int(row["feature_count"]),
            "latest_candle_timestamp": row.get("latest_candle_timestamp"),
        }
        for row in rows
    ]


def queue_research_automation(
    conn: psycopg.Connection,
    *,
    asset_limit: int = 100,
    timeframes: list[str] | None = None,
    max_experiments_per_asset: int = 6,
) -> dict[str, Any]:
    ensure_research_automation_tables(conn)
    universe = discover_research_universe(conn, asset_limit=asset_limit, timeframes=timeframes)
    experiments = get_strategy_experiments()[:max_experiments_per_asset]
    created = 0
    skipped = 0
    for asset in universe:
        for experiment in experiments:
            key = automation_job_key(asset["symbol"], asset["timeframe"], experiment.id)
            row = conn.execute(
                """
                INSERT INTO research_automation_queue(job_key, symbol, timeframe, experiment_id, strategy_name, status, priority, reason, simulation_only)
                VALUES (%s, %s, %s, %s, %s, 'queued', %s, %s, TRUE)
                ON CONFLICT(job_key) DO NOTHING
                RETURNING id
                """,
                (
                    key,
                    asset["symbol"],
                    asset["timeframe"],
                    experiment.id,
                    experiment.strategy,
                    priority_for_asset(asset),
                    "Asset has sufficient stored candles/features and needs automated research coverage.",
                ),
            ).fetchone()
            created += 1 if row else 0
            skipped += 0 if row else 1
    conn.commit()
    return {
        "queued": created,
        "skipped_duplicates": skipped,
        "asset_count": len(universe),
        "experiment_count": len(experiments),
        "automation_version": AUTOMATION_VERSION,
        "simulation_only": True,
    }


def run_research_automation_batch(
    conn: psycopg.Connection,
    *,
    batch_size: int = 10,
    max_runs_per_experiment: int = 24,
) -> dict[str, Any]:
    ensure_research_automation_tables(conn)
    jobs = list(
        conn.execute(
            """
            SELECT *
            FROM research_automation_queue
            WHERE status = 'queued'
              AND simulation_only = TRUE
            ORDER BY priority ASC, created_at ASC, id ASC
            LIMIT %s
            """,
            (batch_size,),
        ).fetchall()
    )
    completed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for job in jobs:
        conn.execute("UPDATE research_automation_queue SET status = 'running', attempts = attempts + 1, updated_at = NOW() WHERE id = %s", (job["id"],))
        try:
            result = run_automation_job(conn, job, max_runs_per_experiment=max_runs_per_experiment)
            completed.append(result)
            conn.execute(
                """
                UPDATE research_automation_queue
                SET status = 'completed', completed_at = NOW(), updated_at = NOW(), latest_error = NULL
                WHERE id = %s
                """,
                (job["id"],),
            )
        except Exception as error:  # noqa: BLE001 - automation batch must continue
            failed.append({"queue_id": job["id"], "symbol": job["symbol"], "timeframe": job["timeframe"], "experiment_id": job["experiment_id"], "error": str(error)})
            conn.execute(
                """
                UPDATE research_automation_queue
                SET status = 'failed', latest_error = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (str(error), job["id"]),
            )
    conn.commit()
    return {
        "requested": batch_size,
        "selected": len(jobs),
        "completed": len(completed),
        "failed": len(failed),
        "results": completed,
        "errors": failed,
        "automation_version": AUTOMATION_VERSION,
        "simulation_only": True,
    }


def run_automation_job(conn: psycopg.Connection, job: dict[str, Any], *, max_runs_per_experiment: int) -> dict[str, Any]:
    candles = load_candles(conn, job["symbol"], job["timeframe"])
    features = list(
        conn.execute(
            """
            SELECT *
            FROM features
            WHERE symbol = %s AND timeframe = %s
            ORDER BY timestamp ASC
            """,
            (job["symbol"], job["timeframe"]),
        ).fetchall()
    )
    regimes = load_regimes(conn, symbol=job["symbol"], timeframe=job["timeframe"])
    if len(candles) < MIN_CANDLES or len(features) < MIN_FEATURES:
        raise ValueError("Stored candles/features are insufficient for automated research.")
    report = run_strategy_experiment(
        candles=candles,
        features=features,
        regimes=regimes,
        experiment_id=job["experiment_id"],
        max_runs=max_runs_per_experiment,
    )
    report["symbol"] = job["symbol"]
    report["timeframe"] = job["timeframe"]
    hypothesis = generate_failure_hypothesis(job, report)
    objective = objective_metrics(report)
    persist_automation_run(conn, job, report, hypothesis, objective)
    persist_strategy_experiment(conn, job, report, hypothesis)
    return {
        "queue_id": job["id"],
        "symbol": job["symbol"],
        "timeframe": job["timeframe"],
        "experiment_id": job["experiment_id"],
        "best_run_id": objective.get("best_run_id"),
        "best_profit_factor": objective.get("profit_factor"),
        "recommendation": objective.get("recommendation"),
        "generated_hypothesis": hypothesis,
    }


def persist_automation_run(conn: psycopg.Connection, job: dict[str, Any], report: dict[str, Any], hypothesis: dict[str, Any], objective: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO research_automation_runs(queue_id, symbol, timeframe, experiment_id, strategy_name, result,
                                             generated_hypothesis, objective_metrics, automation_version, simulation_only)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
        """,
        (
            job["id"],
            job["symbol"],
            job["timeframe"],
            job["experiment_id"],
            job["strategy_name"],
            Jsonb(jsonable_encoder(report)),
            Jsonb(jsonable_encoder(hypothesis)),
            Jsonb(jsonable_encoder(objective)),
            AUTOMATION_VERSION,
        ),
    )


def persist_strategy_experiment(conn: psycopg.Connection, job: dict[str, Any], report: dict[str, Any], hypothesis: dict[str, Any]) -> None:
    experiment = get_experiment(job["experiment_id"])
    conn.execute(
        """
        INSERT INTO strategy_experiments(hypothesis_id, name, dataset, strategy_name, strategy_version, parameters,
                                         comparison_plan, evidence_rules, result, recommendation, markdown_report)
        VALUES (NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            f"Automated {experiment.title} on {job['symbol']} {job['timeframe']}",
            Jsonb({"symbol": job["symbol"], "timeframe": job["timeframe"], "automation_version": AUTOMATION_VERSION}),
            experiment.strategy,
            "v1",
            Jsonb({"experiment_id": experiment.id, "automated": True}),
            Jsonb({"sweep": report.get("effective_sweep", {}), "max_runs": report.get("max_runs")}),
            Jsonb({"objective": objective_rule_summary(report)}),
            Jsonb(jsonable_encoder({"leaderboard": automation_leaderboard(report), "automation": report, "generated_hypothesis": hypothesis})),
            objective_metrics(report).get("recommendation", "Reject"),
            report.get("experiment_report") or report.get("markdown_report") or "",
        ),
    )


def research_automation_status(conn: psycopg.Connection) -> dict[str, Any]:
    ensure_research_automation_tables(conn)
    queue_rows = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM research_automation_queue
        WHERE simulation_only = TRUE
        GROUP BY status
        """
    ).fetchall()
    recent_runs = conn.execute(
        """
        SELECT symbol, timeframe, experiment_id, strategy_name, objective_metrics, generated_hypothesis, created_at
        FROM research_automation_runs
        WHERE simulation_only = TRUE
        ORDER BY created_at DESC
        LIMIT 20
        """
    ).fetchall()
    return {
        "queue": {row["status"]: int(row["count"]) for row in queue_rows},
        "recent_runs": list(recent_runs),
        "automation_version": AUTOMATION_VERSION,
        "simulation_only": True,
        "safety": "Research automation uses stored data only and never submits orders.",
    }


def analyze_research_automation(conn: psycopg.Connection) -> dict[str, Any]:
    ensure_research_automation_tables(conn)
    rows = list(
        conn.execute(
            """
            SELECT *
            FROM research_automation_runs
            WHERE simulation_only = TRUE
            ORDER BY created_at ASC
            """
        ).fetchall()
    )
    by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_regime: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    for row in rows:
        objective = row.get("objective_metrics") or {}
        by_strategy[row["strategy_name"]].append(objective)
        by_symbol[row["symbol"]].append(objective)
        hypothesis = row.get("generated_hypothesis") or {}
        for reason in hypothesis.get("failure_reasons", []):
            failures[reason] += 1
        regime = hypothesis.get("dominant_failure_regime")
        if regime:
            by_regime[regime] += 1
    return {
        "run_count": len(rows),
        "best_strategy_families": ranked_metric_groups(by_strategy),
        "best_assets": ranked_metric_groups(by_symbol),
        "recurring_failure_reasons": [{"reason": reason, "count": count} for reason, count in failures.most_common()],
        "weak_regimes": [{"regime": regime, "count": count} for regime, count in by_regime.most_common()],
        "parameter_improvements": parameter_improvement_summary(rows),
        "strategy_family_trends": strategy_family_trends(rows),
        "automation_version": AUTOMATION_VERSION,
        "simulation_only": True,
    }


def generate_failure_hypothesis(job: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    best = (report.get("ranking_table") or [{}])[0]
    metrics = best.get("metrics") or {}
    reasons = failure_reasons(best)
    dominant_regime = dominant_failure_regime(best)
    experiment = get_experiment(job["experiment_id"])
    if dominant_regime and "sideways" in dominant_regime:
        next_step = f"{experiment.strategy} loses primarily during sideways markets. Generate variants requiring trend confirmation before entry."
    elif "insufficient_trades" in reasons:
        next_step = f"{experiment.strategy} is too selective. Generate variants with looser activation filters while preserving risk controls."
    elif "high_drawdown" in reasons:
        next_step = f"{experiment.strategy} drawdown is high. Generate variants with tighter stops, lower risk/reward targets, or volatility filters."
    elif "weak_profit_factor" in reasons:
        next_step = f"{experiment.strategy} has weak profit factor. Search parameter ranges that improve payoff ratio and reduce false positives."
    else:
        next_step = f"Continue automated parameter search for {experiment.strategy} using stored evidence only."
    return {
        "title": f"Automated hypothesis for {job['symbol']} {job['timeframe']} {experiment.strategy}",
        "hypothesis": next_step,
        "failure_reasons": reasons,
        "dominant_failure_regime": dominant_regime,
        "source_experiment_id": job["experiment_id"],
        "source_best_run_id": best.get("run_id"),
        "source_metrics": metrics,
        "created_by": AUTOMATION_VERSION,
    }


def failure_reasons(run: dict[str, Any]) -> list[str]:
    metrics = run.get("metrics") or {}
    reasons = []
    if finite_metric(metrics.get("number_of_trades")) < 30:
        reasons.append("insufficient_trades")
    if finite_metric(metrics.get("profit_factor")) < 1.0:
        reasons.append("weak_profit_factor")
    if finite_metric(metrics.get("expectancy_per_trade")) <= 0:
        reasons.append("poor_expectancy")
    if finite_metric(metrics.get("max_drawdown")) > 0.2:
        reasons.append("high_drawdown")
    if not (metrics.get("walk_forward") or {}).get("enabled"):
        reasons.append("unstable_walk_forward")
    if dominant_failure_regime(run):
        reasons.append("regime_specific_failure")
    return reasons or ["no_major_failure_cluster"]


def dominant_failure_regime(run: dict[str, Any]) -> str | None:
    weak = []
    for row in (run.get("by_market_regime") or []) + (run.get("by_volatility_regime") or []):
        metrics = row.get("metrics") or {}
        if finite_metric(metrics.get("number_of_trades")) > 0 and finite_metric(metrics.get("expectancy_per_trade")) <= 0:
            weak.append((finite_metric(metrics.get("number_of_trades")), row.get("regime")))
    if not weak:
        return None
    return str(sorted(weak, reverse=True)[0][1])


def objective_metrics(report: dict[str, Any]) -> dict[str, Any]:
    best = (report.get("ranking_table") or [{}])[0]
    metrics = best.get("metrics") or {}
    return {
        "best_run_id": best.get("run_id"),
        "strategy_name": best.get("strategy_name"),
        "strategy_version": best.get("strategy_version"),
        "profit_factor": metrics.get("profit_factor"),
        "expectancy_per_trade": metrics.get("expectancy_per_trade"),
        "max_drawdown": metrics.get("max_drawdown"),
        "number_of_trades": metrics.get("number_of_trades"),
        "rank_score": best.get("rank_score"),
        "recommendation": best.get("recommendation", "Reject"),
        "paper_ready": (best.get("paper_readiness") or {}).get("paper_ready", False),
        "failure_reasons": failure_reasons(best),
    }


def automation_leaderboard(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for row in report.get("ranking_table", []):
        rows.append(
            {
                "candidate_id": row["run_id"],
                "strategy_name": row["strategy_name"],
                "strategy_version": row["strategy_version"],
                "parameters": row["parameters"],
                "metrics": row["metrics"],
                "market_results": [
                    {
                        "symbol": report.get("symbol"),
                        "timeframe": report.get("timeframe"),
                        "metrics": row["metrics"],
                        "by_regime": row.get("by_market_regime", []),
                        "by_volatility": row.get("by_volatility_regime", []),
                        "by_year": row.get("by_year", []),
                    }
                ],
                "evidence_rules": objective_rule_summary({"ranking_table": [row]}),
                "recommendation": row.get("recommendation", "Reject"),
                "validation_score": row.get("rank_score", 0),
            }
        )
    return rows


def objective_rule_summary(report: dict[str, Any]) -> dict[str, bool]:
    best = (report.get("ranking_table") or [{}])[0]
    metrics = best.get("metrics") or {}
    return {
        "min_trades": finite_metric(metrics.get("number_of_trades")) >= 30,
        "profit_factor": finite_metric(metrics.get("profit_factor")) >= 1.0,
        "positive_expectancy": finite_metric(metrics.get("expectancy_per_trade")) > 0,
        "drawdown": finite_metric(metrics.get("max_drawdown")) <= 0.2,
    }


def ranked_metric_groups(groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    rows = []
    for name, items in groups.items():
        rows.append(
            {
                "name": name,
                "sample_size": len(items),
                "average_profit_factor": average(item.get("profit_factor") for item in items),
                "average_expectancy": average(item.get("expectancy_per_trade") for item in items),
                "average_drawdown": average(item.get("max_drawdown") for item in items),
                "total_trades": sum(int(finite_metric(item.get("number_of_trades"))) for item in items),
                "paper_ready_count": sum(1 for item in items if item.get("paper_ready")),
            }
        )
    return sorted(rows, key=lambda row: (row["average_profit_factor"], row["average_expectancy"], row["sample_size"]), reverse=True)


def parameter_improvement_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    impacts: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        result = row.get("result") or {}
        ranked = result.get("ranking_table") or []
        if len(ranked) < 2:
            continue
        best = ranked[0]
        baseline = ranked[-1]
        delta = finite_metric((best.get("metrics") or {}).get("profit_factor")) - finite_metric((baseline.get("metrics") or {}).get("profit_factor"))
        for key, value in (best.get("parameters") or {}).items():
            if baseline.get("parameters", {}).get(key) != value:
                impacts[f"{key}={value}"].append(delta)
    return sorted(
        [{"parameter": key, "average_profit_factor_delta": average(values), "sample_size": len(values)} for key, values in impacts.items()],
        key=lambda item: (item["average_profit_factor_delta"], item["sample_size"]),
        reverse=True,
    )[:25]


def strategy_family_trends(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["strategy_name"]].append(row)
    result = []
    for strategy, items in grouped.items():
        ordered = sorted(items, key=lambda item: str(item.get("created_at")))
        first = finite_metric((ordered[0].get("objective_metrics") or {}).get("profit_factor"))
        last = finite_metric((ordered[-1].get("objective_metrics") or {}).get("profit_factor"))
        result.append({"strategy": strategy, "run_count": len(items), "profit_factor_change": round(last - first, 4)})
    return sorted(result, key=lambda row: row["profit_factor_change"], reverse=True)


def automation_job_key(symbol: str, timeframe: str, experiment_id: str) -> str:
    raw = f"{AUTOMATION_VERSION}|{symbol}|{timeframe}|{experiment_id}"
    return sha256(raw.encode("utf-8")).hexdigest()


def priority_for_asset(asset: dict[str, Any]) -> int:
    if str(asset.get("asset_class") or "").lower() in {"us_equity", "equity", "etf"}:
        return 20
    return 50


def average(values: Any) -> float:
    parsed = [finite_metric(value) for value in values if value is not None]
    return round(sum(parsed) / len(parsed), 4) if parsed else 0.0
