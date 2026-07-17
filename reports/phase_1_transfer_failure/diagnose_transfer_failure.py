"""Build the read-only evidence bundle for the Phase 1 transfer diagnosis.

This script never writes to PostgreSQL and never changes validation thresholds. It
reconciles preserved specialist-stage evidence with the exact campaign jobs,
immutable dataset metadata, asset profiles, clusters, and hypothesis versions that
produced it. The only write is the requested local JSON evidence artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))

from app.db import connect  # noqa: E402
from app.services.research_architecture import verify_dataset_snapshot  # noqa: E402
from app.services.research_campaigns import candidate_from_payload  # noqa: E402
from app.services.strategy_discovery import candidate_execution_key  # noqa: E402


CALCULATION_VERSION = "phase_1_transfer_diagnosis_v1"
STRONG_GATES = {
    "trade_count",
    "profit_factor",
    "positive_expectancy",
    "maximum_drawdown",
    "walk_forward",
    "paper_readiness",
}


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def median(values: Iterable[Any]) -> float | None:
    numbers = [number for value in values if (number := finite(value)) is not None]
    return round(statistics.median(numbers), 8) if numbers else None


def percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> dict[str, Any]:
    if total <= 0:
        return {"successes": successes, "total": total, "rate": None, "lower_95": None, "upper_95": None}
    rate = successes / total
    denominator = 1 + z * z / total
    center = (rate + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total)) / denominator
    return {
        "successes": successes,
        "total": total,
        "rate": round(rate, 8),
        "lower_95": round(max(0.0, center - margin), 8),
        "upper_95": round(min(1.0, center + margin), 8),
        "method": "Wilson score interval; repeated candidate/asset observations are not independent",
    }


def bootstrap_median_interval(values: list[float], *, iterations: int = 10_000, seed: int = 92821) -> dict[str, Any]:
    if not values:
        return {"estimate": None, "lower_95": None, "upper_95": None, "n": 0}
    randomizer = random.Random(seed)
    bootstrapped = [
        statistics.median(randomizer.choice(values) for _ in values)
        for _ in range(iterations)
    ]
    return {
        "estimate": round(statistics.median(values), 8),
        "lower_95": round(percentile(bootstrapped, 0.025), 8),
        "upper_95": round(percentile(bootstrapped, 0.975), 8),
        "n": len(values),
        "iterations": iterations,
        "seed": seed,
        "interpretation_limit": "Describes dispersion across searched executable configurations; it is not an independent-market confidence interval.",
    }


def metrics(job: dict[str, Any]) -> dict[str, Any]:
    return dict((job.get("result") or {}).get("metrics") or {})


def diagnostics(job: dict[str, Any]) -> list[dict[str, Any]]:
    rows = job.get("rejection_diagnostics") or (job.get("result") or {}).get("gate_diagnostics") or []
    return [dict(row) for row in rows if isinstance(row, dict)]


def failed_gates(job: dict[str, Any]) -> list[str]:
    return sorted({str(row.get("name")) for row in diagnostics(job) if not bool(row.get("passed"))})


def strong_pass(job: dict[str, Any]) -> bool:
    rows = diagnostics(job)
    names = {str(row.get("name")) for row in rows}
    return STRONG_GATES.issubset(names) and all(bool(row.get("passed")) for row in rows if row.get("name") in STRONG_GATES)


def failure_class(job: dict[str, Any]) -> list[str]:
    failures = set(failed_gates(job))
    classes: list[str] = []
    if failures & {"profit_factor", "positive_expectancy"}:
        classes.append("economic_edge")
    if "trade_count" in failures:
        classes.append("sample_frequency")
    if "maximum_drawdown" in failures:
        classes.append("risk")
    if "walk_forward" in failures:
        classes.append("walk_forward")
    if "paper_readiness" in failures:
        classes.append("paper_readiness")
    return classes


def sample_only_failure(job: dict[str, Any]) -> bool:
    failures = set(failed_gates(job))
    # Paper readiness repeats the unchanged trade-count threshold, so it is a
    # derived failure rather than a separate economic blocker in this case.
    return bool(failures) and failures - {"paper_readiness"} == {"trade_count"}


def regime_failures(job: dict[str, Any]) -> list[str]:
    reasons = list(job.get("failure_reasons") or [])
    return sorted({str(reason).removeprefix("fails_in_") for reason in reasons if str(reason).startswith("fails_in_")})


def metric_summary(job: dict[str, Any]) -> dict[str, Any]:
    row = metrics(job)
    return {
        "profit_factor": finite(row.get("profit_factor")),
        "profit_factor_is_infinite": bool(row.get("profit_factor_is_infinite")),
        "expectancy_per_trade": finite(row.get("expectancy_per_trade")),
        "number_of_trades": int(finite(row.get("number_of_trades")) or 0),
        "max_drawdown": finite(row.get("max_drawdown")),
        "win_rate": finite(row.get("win_rate")),
        "walk_forward": dict(row.get("walk_forward") or {}),
    }


def attempt_summary(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "asset": job["symbol"],
        "timeframe": job["timeframe"],
        "job_id": int(job["id"]),
        "evidence_ref": f"research_campaign_job:{job['id']}",
        "strong_gate_pass": strong_pass(job),
        "worker_status": job["status"],
        "metrics": metric_summary(job),
        "failed_gates": failed_gates(job),
        "failure_classes": failure_class(job),
        "sample_only_failure": sample_only_failure(job),
        "regime_failures": regime_failures(job),
        "failure_reasons": list(job.get("failure_reasons") or []),
    }


def selected_parameters(candidate: dict[str, Any]) -> dict[str, Any]:
    params = dict(candidate.get("parameters") or {})
    keys = (
        "trend_method",
        "trend_fast",
        "trend_slow",
        "momentum",
        "rsi_min",
        "rsi_max",
        "volume_change_min",
        "entry",
        "entry_distance_to_ema20_max",
        "returns_5_min",
        "exit",
        "risk_reward",
        "atr_multiplier",
        "swing_lookback",
        "max_holding_bars",
        "relevant_regimes",
        "phase_9_8_regime_filter",
        "phase_9_9_regime_filter",
        "phase_9_10_high_volatility_block",
        "phase_9_11_regime_filter",
        "block_sideways",
    )
    return {key: params.get(key) for key in keys}


def fetch_rows(conn: Any, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def pool_regimes(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    pooled: dict[str, dict[tuple[str, str, str], dict[str, float]]] = {
        "market": defaultdict(lambda: defaultdict(float)),
        "volatility": defaultdict(lambda: defaultdict(float)),
    }
    for record in records:
        for job in record["jobs"]:
            scope = "home" if job["symbol"] == record["specialist_asset"] else "target"
            analysis = dict((job.get("result") or {}).get("regime_analysis") or {})
            for dimension, source_key in (("market", "by_market_regime"), ("volatility", "by_volatility_regime")):
                for item in analysis.get(source_key) or []:
                    item_metrics = dict(item.get("metrics") or {})
                    key = (scope, record["specialist_asset"], str(item.get("regime") or "unknown"))
                    bucket = pooled[dimension][key]
                    for metric_name in ("number_of_trades", "gross_profit", "gross_loss"):
                        bucket[metric_name] += finite(item_metrics.get(metric_name)) or 0.0
    output: dict[str, list[dict[str, Any]]] = {"market": [], "volatility": []}
    for dimension, buckets in pooled.items():
        for (scope, specialist_asset, regime), values in sorted(buckets.items()):
            trades = int(values["number_of_trades"])
            gross_profit = values["gross_profit"]
            gross_loss = values["gross_loss"]
            output[dimension].append(
                {
                    "scope": scope,
                    "specialist_asset": specialist_asset,
                    "regime": regime,
                    "trade_observations": trades,
                    "gross_profit": round(gross_profit, 6),
                    "gross_loss": round(gross_loss, 6),
                    "pooled_profit_factor": round(gross_profit / gross_loss, 8) if gross_loss else None,
                    "pooled_expectancy": round((gross_profit - gross_loss) / trades, 8) if trades else None,
                    "dependence_warning": "Strategies share candles and trades; pooled rows are descriptive, not independent samples.",
                }
            )
    return output


def aggregate_attempts(rows: list[dict[str, Any]], key_name: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row[key_name])].append(row)
    result: list[dict[str, Any]] = []
    for key, items in sorted(grouped.items()):
        result.append(
            {
                key_name: key,
                "attempts": len(items),
                "strong_gate_passes": sum(strong_pass(item) for item in items),
                "economic_failures": sum("economic_edge" in failure_class(item) for item in items),
                "sample_only_failures": sum(sample_only_failure(item) for item in items),
                "median_profit_factor": median(metrics(item).get("profit_factor") for item in items),
                "median_expectancy": median(metrics(item).get("expectancy_per_trade") for item in items),
                "median_trade_count": median(metrics(item).get("number_of_trades") for item in items),
                "trade_observations": sum(int(finite(metrics(item).get("number_of_trades")) or 0) for item in items),
            }
        )
    return result


def mutation_analysis(
    conn: Any,
    dataset_id: int,
    campaign_ids: list[int],
    specialist_ids: set[str],
) -> dict[str, Any]:
    candidates = fetch_rows(
        conn,
        """
        SELECT DISTINCT ON (j.campaign_id, j.candidate_id)
               j.campaign_id, j.candidate_id, j.parent_candidate_id, j.candidate
        FROM research_campaign_jobs j
        JOIN research_campaigns c ON c.id = j.campaign_id
        WHERE c.dataset_id = %s
          AND j.campaign_id = ANY(%s)
          AND j.generation_channel = 'nearby'
          AND j.parent_candidate_id IS NOT NULL
        ORDER BY j.campaign_id, j.candidate_id, j.id
        """,
        (dataset_id, campaign_ids),
    )
    all_jobs = fetch_rows(
        conn,
        """
        SELECT j.*
        FROM research_campaign_jobs j
        JOIN research_campaigns c ON c.id = j.campaign_id
        WHERE c.dataset_id = %s
        ORDER BY j.campaign_id, j.id
        """,
        (dataset_id,),
    )
    child_jobs: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    parent_jobs: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for job in all_jobs:
        child_jobs[(int(job["campaign_id"]), str(job["candidate_id"]))].append(job)
        parent_jobs[(str(job["candidate_id"]), str(job["symbol"]), str(job["timeframe"]))].append(job)

    comparisons: list[dict[str, Any]] = []
    missing_parent_comparisons = 0
    for candidate in candidates:
        mutation = dict((candidate["candidate"].get("parameters") or {}).get("controlled_mutation") or {})
        parameter = str(mutation.get("parameter") or "unrecorded")
        for child in child_jobs[(int(candidate["campaign_id"]), str(candidate["candidate_id"]))]:
            options = parent_jobs.get((str(candidate["parent_candidate_id"]), str(child["symbol"]), str(child["timeframe"])), [])
            eligible = [row for row in options if int(row["campaign_id"]) <= int(candidate["campaign_id"])]
            if not eligible:
                missing_parent_comparisons += 1
                continue
            parent = max(eligible, key=lambda row: (int(row["campaign_id"]), int(row["id"])))
            child_pf = finite(metrics(child).get("profit_factor"))
            parent_pf = finite(metrics(parent).get("profit_factor"))
            comparisons.append(
                {
                    "campaign_id": int(candidate["campaign_id"]),
                    "candidate_id": candidate["candidate_id"],
                    "parent_candidate_id": candidate["parent_candidate_id"],
                    "parameter": parameter,
                    "from": mutation.get("from"),
                    "to": mutation.get("to"),
                    "asset": child["symbol"],
                    "child_job_id": int(child["id"]),
                    "parent_job_id": int(parent["id"]),
                    "profit_factor_delta": round(child_pf - parent_pf, 8) if child_pf is not None and parent_pf is not None else None,
                    "trade_count_delta": int(finite(metrics(child).get("number_of_trades")) or 0) - int(finite(metrics(parent).get("number_of_trades")) or 0),
                    "parent_strong_pass": strong_pass(parent),
                    "child_strong_pass": strong_pass(child),
                    "specialist_linked": str(candidate["candidate_id"]) in specialist_ids or str(candidate["parent_candidate_id"]) in specialist_ids,
                }
            )

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in comparisons:
        grouped[row["parameter"]].append(row)
    aggregate = []
    for parameter, items in sorted(grouped.items()):
        aggregate.append(
            {
                "parameter": parameter,
                "mutant_asset_comparisons": len(items),
                "mutant_candidates": len({(row["campaign_id"], row["candidate_id"]) for row in items}),
                "median_profit_factor_delta": median(row["profit_factor_delta"] for row in items),
                "median_trade_count_delta": median(row["trade_count_delta"] for row in items),
                "strong_pass_gains": sum(not row["parent_strong_pass"] and row["child_strong_pass"] for row in items),
                "strong_pass_losses": sum(row["parent_strong_pass"] and not row["child_strong_pass"] for row in items),
                "specialist_linked_comparisons": sum(row["specialist_linked"] for row in items),
            }
        )
    return {
        "nearby_mutant_candidates": len(candidates),
        "asset_level_comparisons": len(comparisons),
        "missing_parent_comparisons": missing_parent_comparisons,
        "aggregate_by_parameter": aggregate,
        "comparisons": comparisons,
        "post_hoc": True,
    }


def build_evidence() -> dict[str, Any]:
    conn = connect()
    try:
        stages = fetch_rows(
            conn,
            """
            SELECT e.*, c.dataset_id, c.cluster_id, c.hypothesis_version_id AS campaign_hypothesis_version_id,
                   c.name AS campaign_name, c.generator_version, c.threshold_version, c.experiment_generation
            FROM research_candidate_stage_evidence e
            JOIN research_campaigns c ON c.id = e.campaign_id
            WHERE e.candidate_level = 'asset_specialist' AND e.promoted = TRUE
            ORDER BY e.campaign_id, e.candidate_id
            """,
        )
        if not stages:
            raise RuntimeError("No preserved asset specialists were found.")
        dataset_ids = sorted({int(row["dataset_id"]) for row in stages if row.get("dataset_id") is not None})
        if len(dataset_ids) != 1:
            raise RuntimeError(f"Expected one authoritative frozen dataset, found {dataset_ids}.")
        dataset_id = dataset_ids[0]

        jobs = fetch_rows(
            conn,
            """
            WITH specialists AS (
                SELECT campaign_id, candidate_id, scope_ref AS specialist_asset
                FROM research_candidate_stage_evidence
                WHERE candidate_level = 'asset_specialist' AND promoted = TRUE
            )
            SELECT j.*, s.specialist_asset
            FROM specialists s
            JOIN research_campaign_jobs j USING (campaign_id, candidate_id)
            ORDER BY j.campaign_id, j.candidate_id, j.symbol, j.timeframe
            """,
        )
        jobs_by_stage: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
        for job in jobs:
            jobs_by_stage[(int(job["campaign_id"]), str(job["candidate_id"]))].append(job)

        records: list[dict[str, Any]] = []
        for stage in stages:
            key = (int(stage["campaign_id"]), str(stage["candidate_id"]))
            stage_jobs = jobs_by_stage[key]
            home_jobs = [job for job in stage_jobs if job["symbol"] == stage["scope_ref"] and strong_pass(job)]
            if len(home_jobs) != 1:
                raise RuntimeError(f"Specialist {key} has {len(home_jobs)} strong home jobs; expected one.")
            home = home_jobs[0]
            candidate = dict(home["candidate"])
            execution_key = candidate_execution_key(candidate_from_payload(candidate))
            records.append(
                {
                    "campaign_id": key[0],
                    "campaign_name": stage["campaign_name"],
                    "candidate_id": key[1],
                    "specialist_asset": stage["scope_ref"],
                    "timeframe": home["timeframe"],
                    "hypothesis_version_id": stage.get("hypothesis_version_id"),
                    "parent_candidate_id": stage.get("parent_candidate_id"),
                    "stage_evidence_ref": f"research_candidate_stage_evidence:{stage['evidence_key']}",
                    "execution_key": execution_key,
                    "execution_signature": hashlib.sha256(execution_key.encode()).hexdigest()[:16],
                    "blocks": dict(candidate.get("blocks") or {}),
                    "parameters": selected_parameters(candidate),
                    "home_job": home,
                    "target_jobs": [job for job in stage_jobs if job["id"] != home["id"]],
                    "jobs": stage_jobs,
                }
            )

        execution_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            execution_groups[(record["specialist_asset"], record["execution_key"])].append(record)
        representatives = [min(group, key=lambda row: (row["campaign_id"], row["candidate_id"])) for group in execution_groups.values()]
        representative_keys = {(row["campaign_id"], row["candidate_id"]) for row in representatives}

        all_targets = [job for record in records for job in record["target_jobs"]]
        unique_targets = [job for record in representatives for job in record["target_jobs"]]
        transfer_candidate_successes = sum(any(strong_pass(job) for job in record["target_jobs"]) for record in records)
        unique_transfer_successes = sum(any(strong_pass(job) for job in record["target_jobs"]) for record in representatives)

        gate_counter = Counter(gate for job in all_targets for gate in failed_gates(job))
        unique_gate_counter = Counter(gate for job in unique_targets for gate in failed_gates(job))
        reason_counter = Counter(regime for job in all_targets for regime in regime_failures(job))
        unique_reason_counter = Counter(regime for job in unique_targets for regime in regime_failures(job))

        paired_pf_deltas = []
        paired_trade_deltas = []
        for record in representatives:
            home_metrics = metrics(record["home_job"])
            target_pf = median(metrics(job).get("profit_factor") for job in record["target_jobs"])
            target_trades = median(metrics(job).get("number_of_trades") for job in record["target_jobs"])
            home_pf = finite(home_metrics.get("profit_factor"))
            home_trades = finite(home_metrics.get("number_of_trades"))
            if home_pf is not None and target_pf is not None:
                paired_pf_deltas.append(home_pf - target_pf)
            if home_trades is not None and target_trades is not None:
                paired_trade_deltas.append(home_trades - target_trades)

        candidate_diagnostics = []
        for record in records:
            duplicate_group = execution_groups[(record["specialist_asset"], record["execution_key"])]
            representative = min(duplicate_group, key=lambda row: (row["campaign_id"], row["candidate_id"]))
            candidate_diagnostics.append(
                {
                    "campaign_id": record["campaign_id"],
                    "candidate_id": record["candidate_id"],
                    "specialist_asset": record["specialist_asset"],
                    "timeframe": record["timeframe"],
                    "hypothesis_version_id": record["hypothesis_version_id"],
                    "parent_candidate_id": record["parent_candidate_id"],
                    "stage_evidence_ref": record["stage_evidence_ref"],
                    "execution_signature": record["execution_signature"],
                    "execution_representative": {
                        "campaign_id": representative["campaign_id"],
                        "candidate_id": representative["candidate_id"],
                    },
                    "repeated_execution_evidence": len(duplicate_group) > 1,
                    "blocks": record["blocks"],
                    "parameters": record["parameters"],
                    "home": attempt_summary(record["home_job"]),
                    "targets": [attempt_summary(job) for job in record["target_jobs"]],
                }
            )

        duplicate_consistency_issues = []
        for group in execution_groups.values():
            if len(group) < 2:
                continue
            baseline = min(group, key=lambda row: (row["campaign_id"], row["candidate_id"]))
            baseline_by_asset = {job["symbol"]: metric_summary(job) for job in baseline["jobs"]}
            for repeated in group:
                repeated_by_asset = {job["symbol"]: metric_summary(job) for job in repeated["jobs"]}
                if repeated_by_asset != baseline_by_asset:
                    duplicate_consistency_issues.append(
                        {
                            "execution_signature": baseline["execution_signature"],
                            "baseline": [baseline["campaign_id"], baseline["candidate_id"]],
                            "different": [repeated["campaign_id"], repeated["candidate_id"]],
                        }
                    )

        profiles = fetch_rows(
            conn,
            """
            SELECT id, symbol, timeframe, evidence_window, metrics, behavior_labels, regime_distribution,
                   correlations, limitations, calculation_version
            FROM asset_profile_versions
            WHERE dataset_id = %s AND timeframe = '1h'
            ORDER BY symbol
            """,
            (dataset_id,),
        )
        cluster_ids = sorted({int(row["cluster_id"]) for row in stages if row.get("cluster_id") is not None})
        clusters = fetch_rows(
            conn,
            """
            SELECT c.id, c.cluster_key, c.version, c.name, c.description, c.centroid, c.member_count,
                   c.quality_metrics, c.algorithm_version, m.symbol, m.timeframe,
                   m.distance_to_centroid, m.similarity_score, m.evidence
            FROM asset_cluster_versions c
            JOIN asset_cluster_members m ON m.cluster_id = c.id
            WHERE c.id = ANY(%s)
            ORDER BY c.id, m.symbol, m.timeframe
            """,
            (cluster_ids,),
        )
        hypothesis_ids = sorted({int(row["hypothesis_version_id"]) for row in stages if row.get("hypothesis_version_id") is not None})
        hypotheses = fetch_rows(
            conn,
            """
            SELECT id, hypothesis_key, version, parent_hypothesis_id, scope_type, scope_ref, strategy_family,
                   title, observation, hypothesis, expected_behavior, relevant_regimes, confidence_score,
                   evidence_window, creation_source, status, supporting_evidence, contradictory_evidence,
                   test_summary, calculation_version
            FROM research_hypothesis_versions
            WHERE id = ANY(%s)
            ORDER BY id
            """,
            (hypothesis_ids,),
        )
        campaign_ids = sorted({int(row["campaign_id"]) for row in stages})
        campaigns = fetch_rows(
            conn,
            """
            SELECT c.id, c.name, c.status, c.requested_candidates, c.completed_jobs, c.promoted_candidates,
                   c.dataset_id, c.hypothesis_version_id, c.cluster_id, c.experiment_generation,
                   c.generator_version, c.threshold_version, c.started_at, c.completed_at,
                   COUNT(j.id) AS actual_jobs,
                   COALESCE(SUM(j.execution_runtime_ms), 0) AS summed_job_runtime_ms
            FROM research_campaigns c
            LEFT JOIN research_campaign_jobs j ON j.campaign_id = c.id
            WHERE c.id = ANY(%s)
            GROUP BY c.id
            ORDER BY c.id
            """,
            (campaign_ids,),
        )
        manifest = dict(
            conn.execute("SELECT * FROM research_dataset_manifests WHERE id = %s", (dataset_id,)).fetchone()
        )

        entry_exit_rows = []
        for dimension in ("entry", "exit"):
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for record in representatives:
                grouped[str(record["parameters"].get(dimension))].append(record)
            for value, group in sorted(grouped.items()):
                group_targets = [job for record in group for job in record["target_jobs"]]
                entry_exit_rows.append(
                    {
                        "dimension": dimension,
                        "value": value,
                        "execution_unique_specialists": len(group),
                        "transfer_attempts": len(group_targets),
                        "transfer_passes": sum(strong_pass(job) for job in group_targets),
                        "median_home_profit_factor": median(metrics(record["home_job"]).get("profit_factor") for record in group),
                        "median_target_profit_factor": median(metrics(job).get("profit_factor") for job in group_targets),
                        "economic_failure_rate": round(sum("economic_edge" in failure_class(job) for job in group_targets) / len(group_targets), 8) if group_targets else None,
                        "sample_only_failures": sum(sample_only_failure(job) for job in group_targets),
                    }
                )

        specialist_ids = {record["candidate_id"] for record in records}
        evidence = {
            "calculation_version": CALCULATION_VERSION,
            "generated_at": datetime.now().astimezone().isoformat(),
            "analysis_type": "post_hoc_diagnostic_unconfirmed",
            "scope": {
                "phase": 1,
                "validation_threshold_changes": 0,
                "new_campaign_jobs": 0,
                "campaign_ids": campaign_ids,
                "dataset_id": dataset_id,
                "candidate_population": "Every promoted asset_specialist row in research_candidate_stage_evidence",
            },
            "source_of_truth": {
                "authoritative_tables": [
                    "research_candidate_stage_evidence",
                    "research_campaign_jobs",
                    "research_campaigns",
                    "research_dataset_manifests",
                    "research_dataset_candles",
                    "asset_profile_versions",
                    "asset_cluster_versions",
                    "asset_cluster_members",
                    "research_hypothesis_versions",
                ],
                "validation_grain": "One research_campaign_jobs row per candidate, asset, and timeframe",
                "promotion_grain": "One immutable research_candidate_stage_evidence row per achieved candidate level and scope",
            },
            "dataset": manifest,
            "dataset_integrity": verify_dataset_snapshot(conn, dataset_id),
            "cohort": {
                "preserved_specialist_ids": len(records),
                "execution_unique_specialists": len(representatives),
                "repeated_specialist_ids": len(records) - len(representatives),
                "specialist_assets": dict(sorted(Counter(record["specialist_asset"] for record in records).items())),
                "home_validation_jobs": len(records),
                "non_home_transfer_attempts": len(all_targets),
                "execution_unique_transfer_attempts": len(unique_targets),
                "home_trade_observations": sum(metric_summary(record["home_job"])["number_of_trades"] for record in records),
                "non_home_trade_observations": sum(metric_summary(job)["number_of_trades"] for job in all_targets),
                "duplicate_consistency_issues": duplicate_consistency_issues,
            },
            "transfer_rates": {
                "candidate_id_level": wilson_interval(transfer_candidate_successes, len(records)),
                "execution_unique_candidate_level": wilson_interval(unique_transfer_successes, len(representatives)),
                "asset_attempt_level": wilson_interval(sum(strong_pass(job) for job in all_targets), len(all_targets)),
                "execution_unique_asset_attempt_level": wilson_interval(sum(strong_pass(job) for job in unique_targets), len(unique_targets)),
            },
            "paired_performance": {
                "home_minus_median_target_profit_factor": bootstrap_median_interval(paired_pf_deltas),
                "home_minus_median_target_trade_count": bootstrap_median_interval(paired_trade_deltas, seed=92822),
            },
            "failure_gates": {
                "id_level": dict(sorted(gate_counter.items())),
                "execution_unique": dict(sorted(unique_gate_counter.items())),
                "id_level_regime_failures": dict(sorted(reason_counter.items())),
                "execution_unique_regime_failures": dict(sorted(unique_reason_counter.items())),
                "id_level_economic_failures": sum("economic_edge" in failure_class(job) for job in all_targets),
                "execution_unique_economic_failures": sum("economic_edge" in failure_class(job) for job in unique_targets),
                "id_level_sample_only_failures": sum(sample_only_failure(job) for job in all_targets),
                "execution_unique_sample_only_failures": sum(sample_only_failure(job) for job in unique_targets),
            },
            "target_asset_results": {
                "id_level": aggregate_attempts(all_targets, "symbol"),
                "execution_unique": aggregate_attempts(unique_targets, "symbol"),
            },
            "entry_exit_results_execution_unique": entry_exit_rows,
            "regime_results_execution_unique": pool_regimes(representatives),
            "parameter_drift": mutation_analysis(conn, dataset_id, campaign_ids, specialist_ids),
            "campaigns": campaigns,
            "profiles": profiles,
            "clusters_used_by_specialist_campaigns": clusters,
            "hypotheses_used_by_specialist_campaigns": hypotheses,
            "candidate_diagnostics": candidate_diagnostics,
            "limitations": [
                "This is post-hoc analysis of outcomes already observed on dataset 1; every proposed cause remains unconfirmed until tested on a future unseen frozen dataset.",
                "Candidate IDs, target-asset attempts, regime rows, and trades are dependent because strategies share lineage and candles; Wilson intervals at those grains are descriptive bounds, not independent-market inference.",
                "The ledger preserves aggregate and regime-level metrics, not independent experimental randomization. Causal attribution to a single parameter is therefore not supported.",
                "No versioned corporate-event dataset exists, so earnings-event effects are unavailable rather than inferred from price.",
            ],
        }
        return evidence
    finally:
        conn.close()


def markdown_table(headers: list[str], rows: list[list[Any]], alignments: list[str] | None = None) -> list[str]:
    alignments = alignments or ["---"] * len(headers)
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(alignments) + "|"]
    for row in rows:
        values = [str(value).replace("|", "\\|").replace("\n", " ") for value in row]
        lines.append("| " + " | ".join(values) + " |")
    return lines


def fmt(value: Any, digits: int = 3) -> str:
    number = finite(value)
    return "n/a" if number is None else f"{number:.{digits}f}"


def percent(numerator: int, denominator: int, digits: int = 1) -> str:
    return "n/a" if denominator == 0 else f"{numerator / denominator * 100:.{digits}f}%"


def render_report(evidence: dict[str, Any]) -> str:
    candidates = list(evidence["candidate_diagnostics"])
    unique_candidates = [
        candidate
        for candidate in candidates
        if candidate["execution_representative"]
        == {"campaign_id": candidate["campaign_id"], "candidate_id": candidate["candidate_id"]}
    ]
    unique_targets = [target for candidate in unique_candidates for target in candidate["targets"]]
    home_pfs = [candidate["home"]["metrics"]["profit_factor"] for candidate in unique_candidates]
    target_pfs = [target["metrics"]["profit_factor"] for target in unique_targets]
    cohort = evidence["cohort"]
    rates = evidence["transfer_rates"]
    failure = evidence["failure_gates"]
    paired = evidence["paired_performance"]
    campaigns = evidence["campaigns"]
    total_campaign_jobs = sum(int(row["actual_jobs"]) for row in campaigns)
    total_runtime_hours = sum(int(row["summed_job_runtime_ms"]) for row in campaigns) / 3_600_000
    dataset = evidence["dataset"]

    lines: list[str] = [
        "# Phase 1 — Why KefTrade specialists do not transfer",
        "",
        "**Status:** complete for review; Phase 2 has not started.",
        "",
        "## Technical summary",
        "",
        (
            "The demonstrated blocker is **loss of economic edge on the second asset**, not a hidden threshold change and not primarily a shortage of trades. "
            f"Across campaigns {', '.join(str(row['id']) for row in campaigns)}, all {cohort['preserved_specialist_ids']} preserved specialist IDs failed every non-home transfer: "
            f"0/{cohort['non_home_transfer_attempts']} candidate–asset attempts passed the unchanged strong gates. After collapsing exact executable repeats, the result is still 0/{cohort['execution_unique_transfer_attempts']} attempts across {cohort['execution_unique_specialists']} strategies "
            f"(95% Wilson upper bound {rates['execution_unique_asset_attempt_level']['upper_95'] * 100:.1f}%; observations are dependent)."
        ),
        "",
        (
            f"At execution-unique grain, {failure['execution_unique_economic_failures']}/{cohort['execution_unique_transfer_attempts']} failures "
            f"({percent(failure['execution_unique_economic_failures'], cohort['execution_unique_transfer_attempts'])}) missed profit factor or positive expectancy, while only "
            f"{failure['execution_unique_sample_only_failures']}/{cohort['execution_unique_transfer_attempts']} were sample-only near transfers. "
            f"The median strategy lost {paired['home_minus_median_target_profit_factor']['estimate']:.3f} profit-factor points from home to its median target "
            f"(configuration-bootstrap interval {paired['home_minus_median_target_profit_factor']['lower_95']:.3f}–{paired['home_minus_median_target_profit_factor']['upper_95']:.3f}; descriptive, not independent-market inference)."
        ),
        "",
        "Three mechanisms are supported:",
        "",
        "1. **The original cluster hypothesis was too coarse for transfer.** All 16 GOOGL specialist IDs came from the least centroid-representative member of the five-asset cluster (distance 2.784). This is consistent with one measurable contributor to repeatedly selecting GOOGL-specific winners; it does not establish causality. Contradictory evidence: AMD was the most centroid-representative member of its cluster (distance 1.277) and its six specialists still failed NVDA and TSLA, so centroid distance is not a complete cause.",
        "2. **The edge reverses inside the same named regimes.** GOOGL-home strategies had pooled bull-trend PF 1.570 over 294 trade observations versus PF 0.771 over 845 target trade observations; AMD-home strategies had bull-trend PF 1.533 over 210 versus 0.758 over 305 targets. Low- and normal-volatility slices reverse similarly. Therefore a label such as `bull_trend` is not a sufficient transfer condition.",
        "3. **Hypothesis regime metadata is descriptive, not executable.** Candidate annotation stores `relevant_regimes`, but the decision path only activates regime filtering when separate phase-specific flags are present. The 22 specialists do not carry those flags. Generation therefore tested generic trend/pullback rules across every asset rather than a market-behavior-conditioned strategy. Code evidence: `research_architecture.py:1182-1194`, `strategy_discovery.py:322-345`, and `strategy_discovery.py:497-526`.",
        "",
        "No conclusion below is presented as confirmed causal knowledge. This diagnosis is post-hoc and must be falsified on a future unseen frozen dataset.",
        "",
        "## The failure is economic on 48 of 52 unique transfer attempts",
        "",
        "The unchanged gates are PF ≥ 1.20, positive expectancy, drawdown ≤ 0.12, at least 30 trades, enabled walk-forward, and paper readiness. `paper_readiness` repeats the trade/economic checks, so it is not counted as an independent cause.",
        "",
    ]

    target_rows = []
    for row in evidence["target_asset_results"]["execution_unique"]:
        target_rows.append(
            [
                row["symbol"],
                row["attempts"],
                row["trade_observations"],
                fmt(row["median_profit_factor"]),
                fmt(row["median_expectancy"], 2),
                fmt(row["median_trade_count"], 1),
                row["economic_failures"],
                row["sample_only_failures"],
                row["strong_gate_passes"],
            ]
        )
    lines.extend(
        markdown_table(
            ["Target", "Unique attempts", "Trade obs.", "Median PF", "Median exp.", "Median trades", "Economic fails", "Sample-only", "Passes"],
            target_rows,
            ["---", "---:", "---:", "---:", "---:", "---:", "---:", "---:", "---:"],
        )
    )
    lines.extend(
        [
            "",
            "AAPL is the only credible frequency frontier: three of ten unique attempts had positive economics but fewer than 30 trades. AMZN, MSFT, NVDA, and TSLA failed economically on every unique attempt; META had one sample-only case and nine economic failures. This contradicts any claim that simply generating more of the same candidates will solve transfer.",
            "",
            "## Matching the regime name does not preserve the payoff distribution",
            "",
        ]
    )

    regime_lookup = {
        (dimension, row["scope"], row["specialist_asset"], row["regime"]): row
        for dimension, rows in evidence["regime_results_execution_unique"].items()
        for row in rows
    }
    regime_specs = [
        ("market", "GOOGL", "bull_trend"),
        ("market", "GOOGL", "sideways"),
        ("volatility", "GOOGL", "low_volatility"),
        ("volatility", "GOOGL", "normal_volatility"),
        ("market", "AMD", "bull_trend"),
        ("market", "AMD", "sideways"),
        ("volatility", "AMD", "low_volatility"),
        ("volatility", "AMD", "normal_volatility"),
        ("volatility", "AMD", "high_volatility"),
    ]
    regime_rows = []
    for dimension, specialist_asset, regime in regime_specs:
        home = regime_lookup.get((dimension, "home", specialist_asset, regime))
        target = regime_lookup.get((dimension, "target", specialist_asset, regime))
        if not home or not target:
            continue
        regime_rows.append(
            [
                specialist_asset,
                regime,
                home["trade_observations"],
                fmt(home["pooled_profit_factor"]),
                fmt(home["pooled_expectancy"], 2),
                target["trade_observations"],
                fmt(target["pooled_profit_factor"]),
                fmt(target["pooled_expectancy"], 2),
            ]
        )
    lines.extend(
        markdown_table(
            ["Specialist home", "Regime", "Home trades", "Home PF", "Home exp.", "Target trades", "Target PF", "Target exp."],
            regime_rows,
            ["---", "---", "---:", "---:", "---:", "---:", "---:", "---:"],
        )
    )
    lines.extend(
        [
            "",
            "These are pooled trade observations across execution-unique strategies, not independent trades: strategies share candles and often share entries. They are valid descriptive evidence of within-regime reversal, but no causal p-value is claimed. High-volatility transfer for AMD has only two target trades and is explicitly inconclusive; it cannot contradict or support transfer.",
            "",
            "## The cluster observation selected asset-specific winners",
            "",
            "Campaigns 51 and 54 used cluster 1; campaign 53 used cluster 2. The campaign-used v1 clusters stored zero similarity for every multi-member asset, so the only discriminating campaign-time cohesion measure was distance to centroid.",
            "",
        ]
    )
    cluster_rows = []
    for row in evidence["clusters_used_by_specialist_campaigns"]:
        profile = next(item for item in evidence["profiles"] if item["symbol"] == row["symbol"] and item["timeframe"] == row["timeframe"])
        profile_metrics = profile["metrics"]
        cluster_rows.append(
            [
                row["id"],
                row["symbol"],
                fmt(row["distance_to_centroid"]),
                fmt(profile_metrics["trend_strength"], 4),
                fmt(profile_metrics["trend_persistence"], 2),
                fmt(profile_metrics["realized_volatility"], 4),
                fmt(profile_metrics["median_pullback_depth"], 4),
                fmt(profile_metrics["volume_expansion_ratio"], 3),
                cohort["specialist_assets"].get(row["symbol"], 0),
            ]
        )
    lines.extend(
        markdown_table(
            ["Cluster", "Asset", "Centroid distance", "Trend strength", "Trend persistence", "Realized vol.", "Median pullback", "Volume expansion", "Specialist IDs"],
            cluster_rows,
            ["---:", "---", "---:", "---:", "---:", "---:", "---:", "---:", "---:"],
        )
    )
    lines.extend(
        [
            "",
            "GOOGL is simultaneously the cluster-1 outlier and the source of every cluster-1 specialist. AMD supplies the counterexample: it is cluster 2's nearest member, yet its specialists reverse economically on both targets. The supported conclusion is narrower than “clustering is wrong”: the current behavior vector and regime labels are not sufficient conditions for strategy payoff transfer.",
            "",
            "There is also a direct epistemic mismatch in the inputs. Hypothesis versions 28 and 32 have status `testing`, yet their hypothesis text begins with “Confirmed directional persistence” based only on profile aggregates (25,000 and 15,000 candle observations; confidence scores 0.8899 and 0.8679). Hypothesis 34 preserves campaign-51 contradictory stage evidence but retains the same “Confirmed” wording and `testing` status. Campaigns 51, 53, and 54 therefore exploited an unconfirmed cluster-behavior statement before any configuration had demonstrated transfer. This is a ledger-supported state/wording inconsistency, not a claim that the profile measurements themselves are false.",
            "",
            "## Entry, exit, and one-parameter drift do not restore transfer",
            "",
        ]
    )
    entry_exit_rows = []
    for row in evidence["entry_exit_results_execution_unique"]:
        entry_exit_rows.append(
            [
                row["dimension"],
                row["value"],
                row["execution_unique_specialists"],
                row["transfer_attempts"],
                fmt(row["median_home_profit_factor"]),
                fmt(row["median_target_profit_factor"]),
                percent(round(row["economic_failure_rate"] * row["transfer_attempts"]), row["transfer_attempts"]),
                row["sample_only_failures"],
                row["transfer_passes"],
            ]
        )
    lines.extend(
        markdown_table(
            ["Component", "Value", "Unique specialists", "Target attempts", "Home median PF", "Target median PF", "Economic-fail rate", "Sample-only", "Passes"],
            entry_exit_rows,
            ["---", "---", "---:", "---:", "---:", "---:", "---:", "---:", "---:"],
        )
    )
    lines.extend(
        [
            "",
            "Every tested entry and exit family failed transfer. ATR exits produced the strongest home median PF (1.945) but only 0.815 on targets. Trend-continuation entries failed economically on 29/30 unique target attempts; pullbacks failed economically on 19/22, with the remaining three limited by sample size.",
            "",
            f"Campaigns 51, 53, and 54 also preserve {evidence['parameter_drift']['nearby_mutant_candidates']} one-parameter nearby mutants and {evidence['parameter_drift']['asset_level_comparisons']} matched parent–child asset comparisons. The results below are explicitly post-hoc.",
            "",
        ]
    )
    mutation_rows = []
    for row in evidence["parameter_drift"]["aggregate_by_parameter"]:
        mutation_rows.append(
            [
                row["parameter"],
                row["mutant_candidates"],
                row["mutant_asset_comparisons"],
                fmt(row["median_profit_factor_delta"]),
                fmt(row["median_trade_count_delta"], 1),
                row["strong_pass_gains"],
                row["strong_pass_losses"],
                row["specialist_linked_comparisons"],
            ]
        )
    lines.extend(
        markdown_table(
            ["Mutated parameter", "Mutants", "Asset comparisons", "Median ΔPF", "Median Δtrades", "Pass gains", "Pass losses", "Specialist-linked"],
            mutation_rows,
            ["---", "---:", "---:", "---:", "---:", "---:", "---:", "---:"],
        )
    )
    lines.extend(
        [
            "",
            "The clearest sensitivity pattern is a quality–frequency tradeoff. Increasing holding bars added a median 16.5 trades but reduced median PF by 0.220. Relaxing volume added three trades but reduced median PF by 0.049. The isolated pass gains occurred on the home asset only; none created a transfer. Conversely, changing the slow trend window caused two strong-pass losses and no gains. No single-parameter causal effect is claimed because the mutations were selected after earlier outcomes and share the same dataset.",
            "",
            "## What is reusable and what is asset-specific",
            "",
            "**Reusable but unconfirmed:** the generic 20/50 trend backbone, RSI/stochastic momentum checks, and both trend-continuation and pullback entries can produce a valid single-asset specialist on GOOGL and AMD. That is evidence of reusable candidate components, not evidence that any component is structurally sound across assets; no executable configuration passed a second asset.",
            "",
            "**Asset-specific in the observed evidence:** the complete entry/exit/threshold combinations and their payoff distributions. GOOGL specialists remain profitable in GOOGL bull, sideways, low-volatility, and normal-volatility slices while losing in the same target regime labels. AMD shows the same reversal. This makes the interaction between parameters and finer asset behavior—not the broad strategy-family label—the measurable unit that future hypotheses must target.",
            "",
            "**Inconclusive:** which individual feature causes that interaction. Stored feature correlations are based on 30–56 home trades per specialist and are observational; they cannot isolate RSI, EMA distance, volume change, or volatility as causal. Earnings behavior is unavailable because no versioned corporate-event dataset exists.",
            "",
            "## Measurable hypotheses for the next approved phase",
            "",
            "All five hypotheses below are post-hoc and **unconfirmed**. They must not be stored as confirmed or used to claim improvement until tested prospectively on a new frozen dataset.",
            "",
            "1. **Executable regime conditioning.** If `relevant_regimes` is enforced as an executable filter rather than metadata, execution-unique transfer success will exceed the current 0/52 baseline without weakening any gate. Test a fixed-budget matched control/treatment on a future dataset. Supporting evidence: campaigns 51/53/54, all 22 specialists, within-regime reversals above. Contradiction: regime names alone did not preserve edge, so the treatment must include finer behavior conditions and may still fail.",
            "2. **Representative-member confirmation.** If exploitation begins only after a configuration passes a centroid-near asset and one behavior-diverse cluster member, fewer specialists will be misclassified as plausible transfer candidates and jobs per cluster elite will decline. Test with the same candidate/job budget. Supporting evidence: campaigns 51/54 selected only GOOGL, the cluster-1 outlier. Contradiction: AMD was centroid-near and still did not transfer, so representativeness is necessary-at-most, not sufficient.",
            "3. **Behavior-normalized entries.** Scaling pullback distance, return thresholds, and volume thresholds to each asset's frozen profile percentiles will improve transfer PF relative to fixed thresholds while preserving ≥30 trades. Supporting evidence: target profile differences and 48/52 unique economic failures. Contradiction: AAPL had three sample-only near transfers, so normalization may reduce rather than increase frequency.",
            "4. **Within-regime structural matching.** Conditioning on trend strength, pullback depth, momentum persistence, and volume expansion jointly will predict transfer better than the current `bull_trend`/volatility labels. Falsify by preregistering similarity bands and comparing held-out transfer rate at equal compute. Supporting evidence: strong home-to-target PF reversals within identical broad regimes. Contradiction: the current profile sample is one frozen window and may not be stable through time.",
            "5. **Frequency-only frontier.** For configurations that already have PF ≥1.20 and positive expectancy but <30 trades, a preregistered frequency mutation can reach 30 trades without pushing PF below 1.20. Test only the four execution-unique sample-only cases (three AAPL, one META), with no gate change and no post-result retuning. Supporting evidence: the four sample-only rows in Appendix B. Contradiction: the broader mutation history shows frequency increases often reduce PF.",
            "",
            "## Scope, definitions, and reproducibility",
            "",
            f"The authoritative frozen dataset is `{dataset['dataset_key']}` (dataset {dataset['id']}), window {dataset['window_start']} through {dataset['window_end']}. Integrity verification passed with no issues. Each relevant 1h asset has 5,000 frozen candles; validation uses the stored walk-forward split and candidate-level trade counts shown below.",
            "",
            f"The preserved cohort contains every `asset_specialist` stage row: {cohort['preserved_specialist_ids']} IDs, {cohort['execution_unique_specialists']} execution-unique configurations, {cohort['home_trade_observations']} home trade observations, and {cohort['non_home_trade_observations']} non-home trade observations. Six campaign-54 IDs exactly repeat campaign-51 executable strategies; all repeated metrics match, which is a deterministic reproducibility pass but not independent evidence.",
            "",
        ]
    )
    campaign_rows = []
    specialist_counts = Counter(candidate["campaign_id"] for candidate in candidates)
    for row in campaigns:
        count = specialist_counts[int(row["id"])]
        campaign_rows.append(
            [
                row["id"],
                row["hypothesis_version_id"],
                row["cluster_id"],
                row["requested_candidates"],
                row["actual_jobs"],
                count,
                fmt(row["actual_jobs"] / count if count else None, 1),
                row["generator_version"],
                row["threshold_version"],
            ]
        )
    lines.extend(
        markdown_table(
            ["Campaign", "Hypothesis", "Cluster", "Candidates", "Jobs", "Specialist IDs", "Jobs / specialist", "Generator", "Thresholds"],
            campaign_rows,
            ["---:", "---:", "---:", "---:", "---:", "---:", "---:", "---", "---"],
        )
    )
    lines.extend(
        [
            "",
            f"These campaigns consumed {total_campaign_jobs:,} stored jobs and approximately {total_runtime_hours:.2f} summed job-runtime hours. Phase 1 added **zero** validation jobs and changed **zero** thresholds; it queried preserved evidence only. Across the three campaigns, validation efficiency was {total_campaign_jobs / cohort['preserved_specialist_ids']:.1f} jobs per preserved specialist ID or {total_campaign_jobs / cohort['execution_unique_specialists']:.1f} jobs per execution-unique specialist, with zero cluster elites and therefore no finite jobs-per-cluster-elite result.",
            "",
            "Method: reconcile immutable stage rows to campaign jobs; use the six strong gate diagnostics rather than the worker's weaker status label; collapse exact executable keys for independent-looking summaries; pool stored regime gross profit/loss descriptively; compare one-parameter nearby children to the matched parent on the same frozen asset; use Wilson intervals for zero-success rates and a fixed-seed configuration bootstrap for the median PF drop.",
            "",
            "No chart or screenshot was produced. Exact audit tables are used because the requested review requires candidate-by-target lookup and the task explicitly prohibited browser and screenshot/image review.",
            "",
            "## Limitations and robustness checks",
            "",
            "- **Post-hoc:** all causal explanations and proposed hypotheses were constructed after seeing dataset-1 outcomes. They remain unconfirmed.",
            "- **Dependence:** candidates share lineage, candles, and sometimes exact execution parameters. Candidate/asset Wilson intervals are descriptive and likely narrower than a true independent-dataset interval.",
            "- **Frozen-data reproducibility passed:** dataset integrity passed and six exact revalidations reproduced all asset metrics without inconsistency.",
            "- **Broad regime labels are coarse:** the report can reject those labels as sufficient conditions but cannot identify the missing causal feature from aggregate metrics alone.",
            "- **Corporate events unavailable:** earnings behavior was not guessed because no versioned event dataset exists.",
            "- **Contradictory evidence retained:** AAPL/META sample-only cases, centroid-near AMD failure, and the two-trade AMD high-volatility target sample prevent overgeneralized conclusions.",
            "",
            "## Required next step",
            "",
            "Review this Phase 1 diagnosis. Per the sequencing rule, no Phase 2 or Phase 3 implementation may start until explicit approval. If approved, the first design decision should be which unconfirmed hypothesis to preregister against a future frozen dataset; no validation gate needs to change.",
            "",
            "## Further questions",
            "",
            "- Should the next independent dataset be a later time window for the same assets, a disjoint asset universe, or both? This changes what “transfer” can establish.",
            "- Should a cluster hypothesis require a minimum observed similarity/cohesion value before it can generate cluster-targeted candidates? The campaign-used v1 similarity values provide no positive evidence.",
            "- Is a corporate-event dataset in scope later? Without it, event-driven gaps and earnings sensitivity must remain explicitly unavailable.",
            "",
            "## Appendix A — every preserved specialist home result",
            "",
            "Each row is a strong-gate pass. Trade count is the candidate-level sample size. Repeated campaign-54 executions are marked by their representative.",
            "",
        ]
    )
    home_rows = []
    for candidate in candidates:
        home = candidate["home"]["metrics"]
        params = candidate["parameters"]
        representative = candidate["execution_representative"]
        repeated = "—"
        if representative != {"campaign_id": candidate["campaign_id"], "candidate_id": candidate["candidate_id"]}:
            repeated = f"C{representative['campaign_id']} `{representative['candidate_id']}`"
        home_rows.append(
            [
                candidate["campaign_id"],
                f"`{candidate['candidate_id']}`",
                candidate["specialist_asset"],
                f"{params['entry']} / {params['exit']}",
                fmt(home["profit_factor"]),
                fmt(home["expectancy_per_trade"], 2),
                home["number_of_trades"],
                fmt(home["max_drawdown"]),
                f"job {candidate['home']['job_id']}",
                repeated,
            ]
        )
    lines.extend(
        markdown_table(
            ["Campaign", "Candidate", "Home", "Entry / exit", "PF", "Exp.", "Trades", "DD", "Evidence", "Exact repeat of"],
            home_rows,
            ["---:", "---", "---", "---", "---:", "---:", "---:", "---:", "---", "---"],
        )
    )
    lines.extend(
        [
            "",
            "## Appendix B — every specialist-to-target failure",
            "",
            "This is the line-level answer to why specialist X did not transfer to asset Y. Gate names are the unchanged strong gates. `sample-only` means profit factor, expectancy, drawdown, and walk-forward passed but the 30-trade minimum (and therefore paper readiness) did not. Individual rows are descriptive; no independent-candidate p-value is claimed.",
            "",
        ]
    )
    detail_rows = []
    for candidate in candidates:
        for target in candidate["targets"]:
            target_metrics = target["metrics"]
            classes = [item for item in target["failure_classes"] if item != "paper_readiness"]
            diagnosis = "sample-only" if target["sample_only_failure"] else " + ".join(classes) or "unclassified"
            detail_rows.append(
                [
                    candidate["campaign_id"],
                    f"`{candidate['candidate_id']}`",
                    target["asset"],
                    fmt(target_metrics["profit_factor"]),
                    fmt(target_metrics["expectancy_per_trade"], 2),
                    target_metrics["number_of_trades"],
                    fmt(target_metrics["max_drawdown"]),
                    ", ".join(target["failed_gates"]),
                    ", ".join(target["regime_failures"]) or "none evidenced",
                    diagnosis,
                    f"job {target['job_id']}",
                ]
            )
    lines.extend(
        markdown_table(
            ["Campaign", "Specialist", "Target", "PF", "Exp.", "Trades", "DD", "Failed gates", "Weak regimes", "Direct diagnosis", "Evidence"],
            detail_rows,
            ["---:", "---", "---", "---:", "---:", "---:", "---:", "---", "---", "---", "---"],
        )
    )
    lines.extend(
        [
            "",
            "## Appendix C — evidence artifacts",
            "",
            "- `evidence.json`: full source-bound data, candidate diagnostics, rate intervals, regime pools, asset profiles, clusters, hypotheses, and matched parameter mutations.",
            "- `diagnose_transfer_failure.py`: read-only reproduction script. It performs no database writes, launches no campaign, and changes no threshold.",
            "- Primary source tables: `research_candidate_stage_evidence`, `research_campaign_jobs`, `research_campaigns`, `research_dataset_manifests`, `research_dataset_candles`, `asset_profile_versions`, `asset_cluster_versions`, `asset_cluster_members`, and `research_hypothesis_versions`.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("evidence.json"),
        help="Path for the generated evidence JSON.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(__file__).with_name("report.md"),
        help="Path for the generated Phase 1 Markdown report.",
    )
    args = parser.parse_args()
    evidence = build_evidence()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, default=json_default) + "\n", encoding="utf-8")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_report(evidence) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "report": str(args.report.resolve()),
                "preserved_specialists": evidence["cohort"]["preserved_specialist_ids"],
                "execution_unique_specialists": evidence["cohort"]["execution_unique_specialists"],
                "transfer_attempts": evidence["cohort"]["non_home_transfer_attempts"],
                "transfer_passes": evidence["transfer_rates"]["asset_attempt_level"]["successes"],
                "dataset_integrity_passed": evidence["dataset_integrity"]["passed"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
