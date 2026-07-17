from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
from statistics import mean, median
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.services.research_architecture import jsonable, stable_hash
from app.services.strategy_research import finite_metric


PHASE_6_REPORT_VERSION = "automated_scientific_reporting_v1"
INSUFFICIENT = "Inconclusive — insufficient evidence."


def generate_automated_scientific_report(
    conn: psycopg.Connection,
    campaign_id: int,
    *,
    analytics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bundle = load_campaign_evidence(conn, campaign_id, analytics=analytics)
    payload = build_scientific_report(bundle)
    markdown = scientific_report_markdown(payload)
    report_key = sha256(f"{PHASE_6_REPORT_VERSION}|campaign|{campaign_id}".encode()).hexdigest()
    row = conn.execute(
        """
        INSERT INTO research_campaign_reports(campaign_id, report_key, title, summary, recommendations, markdown_report, simulation_only)
        VALUES (%s, %s, %s, %s, %s, %s, TRUE)
        ON CONFLICT(report_key) DO UPDATE
        SET summary = EXCLUDED.summary,
            recommendations = EXCLUDED.recommendations,
            markdown_report = EXCLUDED.markdown_report,
            created_at = NOW()
        RETURNING *
        """,
        (
            campaign_id,
            report_key,
            f"Scientific Campaign Report: {bundle['campaign'].get('name') or campaign_id}",
            Jsonb(jsonable(payload)),
            Jsonb(jsonable(payload["next_campaign_recommendations"])),
            markdown,
        ),
    ).fetchone()
    return jsonable(dict(row))


def load_campaign_evidence(conn: psycopg.Connection, campaign_id: int, *, analytics: dict[str, Any] | None = None) -> dict[str, Any]:
    campaign_row = conn.execute("SELECT * FROM research_campaigns WHERE id = %s", (campaign_id,)).fetchone()
    if not campaign_row:
        raise ValueError(f"campaign {campaign_id} was not found")
    campaign = dict(campaign_row)
    jobs = [dict(row) for row in conn.execute("SELECT * FROM research_campaign_jobs WHERE campaign_id = %s ORDER BY id", (campaign_id,)).fetchall()]
    stage_rows = optional_rows(
        conn,
        "SELECT * FROM research_candidate_stage_evidence WHERE campaign_id = %s ORDER BY id",
        (campaign_id,),
    )
    hypothesis = None
    if campaign.get("hypothesis_version_id"):
        hypothesis_row = optional_one(conn, "SELECT * FROM research_hypothesis_versions WHERE id = %s", (campaign["hypothesis_version_id"],))
        hypothesis = dict(hypothesis_row) if hypothesis_row else None
    hypothesis_versions = optional_rows(
        conn,
        """
        SELECT *
        FROM research_hypothesis_versions
        WHERE test_summary->>'campaign_id' = %s
           OR id = %s
        ORDER BY id
        """,
        (str(campaign_id), campaign.get("hypothesis_version_id") or -1),
    )
    dataset = None
    if campaign.get("dataset_id"):
        dataset_row = optional_one(conn, "SELECT * FROM research_dataset_manifests WHERE id = %s", (campaign["dataset_id"],))
        dataset = dict(dataset_row) if dataset_row else None
    archives = optional_rows(
        conn,
        "SELECT * FROM research_campaign_archives WHERE original_campaign_id = %s ORDER BY created_at DESC",
        (campaign_id,),
    )
    evolution = optional_rows(
        conn,
        "SELECT * FROM research_evolution_history WHERE campaign_id = %s ORDER BY id",
        (campaign_id,),
    )
    learning = {}
    for table in (
        "research_failure_patterns",
        "research_success_patterns",
        "research_candidate_confidence",
        "research_evolution_history",
        "research_learning_recommendations",
    ):
        learning[table] = safe_campaign_rows(conn, table, campaign_id)
    previous_campaigns = optional_rows(
        conn,
        """
        SELECT id, name, status, dataset_id, hypothesis_version_id, promoted_candidates, rejected_candidates,
               analytics, completed_at, created_at
        FROM research_campaigns
        WHERE id <> %s
          AND status = 'completed'
          AND simulation_only = TRUE
        ORDER BY COALESCE(completed_at, created_at) DESC, id DESC
        LIMIT 20
        """,
        (campaign_id,),
    )
    return {
        "campaign": campaign,
        "jobs": jobs,
        "stage_rows": stage_rows,
        "hypothesis": hypothesis,
        "hypothesis_versions": hypothesis_versions,
        "dataset": dataset,
        "archives": archives,
        "evolution": evolution,
        "learning": learning,
        "previous_campaigns": previous_campaigns,
        "analytics": analytics or dict(campaign.get("analytics") or {}),
    }


def optional_rows(conn: psycopg.Connection, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in conn.execute(query, params).fetchall()]
    except Exception:  # noqa: BLE001 - optional evidence should not block report generation
        return []


def optional_one(conn: psycopg.Connection, query: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    try:
        row = conn.execute(query, params).fetchone()
    except Exception:  # noqa: BLE001 - optional evidence should not block report generation
        return None
    return dict(row) if row else None


def safe_campaign_rows(conn: psycopg.Connection, table: str, campaign_id: int) -> list[dict[str, Any]]:
    try:
        exists = conn.execute("SELECT to_regclass(%s) AS table_name", (table,)).fetchone()
        if not exists or not exists.get("table_name"):
            return []
        columns = [
            str(row["column_name"])
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = %s
                """,
                (table,),
            ).fetchall()
        ]
    except Exception:  # noqa: BLE001 - optional learning evidence should not block reporting
        return []
    if "campaign_id" not in columns:
        return []
    order_column = "id" if "id" in columns else "created_at" if "created_at" in columns else "campaign_id"
    return [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM {table} WHERE campaign_id = %s ORDER BY {order_column}",
            (campaign_id,),
        ).fetchall()
    ]


def build_scientific_report(bundle: dict[str, Any]) -> dict[str, Any]:
    campaign = bundle["campaign"]
    jobs = bundle["jobs"]
    dataset = bundle.get("dataset") or {}
    campaign_id = int(campaign["id"])
    dataset_ref = dataset_reference(dataset, campaign)
    status_counts = Counter(str(row.get("status") or "unknown") for row in jobs)
    candidate_ids = sorted({str(row.get("candidate_id")) for row in jobs if row.get("candidate_id")})
    promoted_jobs = [row for row in jobs if row.get("status") == "promoted"]
    rejected_jobs = [row for row in jobs if row.get("status") == "rejected"]
    stage_counts = Counter(str(row.get("candidate_level")) for row in bundle["stage_rows"])
    best_family, worst_family = strategy_family_performance(jobs)
    observation_contrib = observation_contributions(jobs)
    failure_rows = candidate_failure_analysis(jobs)
    transfer = transferability_analysis(jobs, bundle["stage_rows"])
    evolution = evolution_outcomes(bundle["evolution"], jobs)
    hypothesis = hypothesis_lifecycle(bundle)
    comparison = campaign_comparison(campaign, bundle["previous_campaigns"], jobs)
    contradictions = contradictory_evidence(bundle, failure_rows, hypothesis)
    learned = learned_findings(bundle, best_family, worst_family, transfer, hypothesis)
    recommendations = next_campaign_recommendations(bundle, learned, failure_rows, transfer, evolution, observation_contrib)
    payload = {
        "report_version": PHASE_6_REPORT_VERSION,
        "campaign_id": campaign_id,
        "campaign_name": campaign.get("name"),
        "simulation_only": True,
        "dataset": dataset_ref,
        "executive_summary": {
            "status": campaign.get("status"),
            "jobs": len(jobs),
            "candidates": len(candidate_ids),
            "promoted_jobs": int(status_counts.get("promoted", 0)),
            "rejected_jobs": int(status_counts.get("rejected", 0)),
            "candidate_levels": dict(stage_counts),
            "primary_conclusion": learned[0]["statement"] if learned else INSUFFICIENT,
            "evidence_refs": campaign_refs(campaign_id, dataset_ref, jobs[:3]),
        },
        "what_was_learned": learned,
        "what_improved": comparison,
        "what_failed": failure_rows[:25] if failure_rows else [{"statement": INSUFFICIENT, "evidence_refs": [f"research_campaign:{campaign_id}"]}],
        "hypothesis_lifecycle": hypothesis,
        "observation_contributions": observation_contrib,
        "strategy_family_performance": {"best": best_family, "worst": worst_family},
        "structural_similarity": structural_similarity(bundle),
        "candidate_validation_failures": failure_rows,
        "transferability_analysis": transfer,
        "evolution_outcomes": evolution,
        "contradictory_evidence": contradictions,
        "unresolved_questions": unresolved_questions(hypothesis, transfer, evolution, observation_contrib, failure_rows),
        "next_campaign_recommendations": recommendations,
        "reproducibility": {
            "deterministic": True,
            "report_input_hash": stable_hash(jsonable({"campaign": campaign, "jobs": jobs, "stage_rows": bundle["stage_rows"], "hypothesis_versions": bundle["hypothesis_versions"]})),
            "dataset_id": campaign.get("dataset_id"),
            "dataset_hash": dataset.get("content_hash"),
            "archive_refs": [row.get("archive_key") for row in bundle["archives"]],
            "validation_thresholds_changed": False,
        },
        "compute_budget": compute_budget(jobs, bundle["previous_campaigns"]),
    }
    return jsonable(payload)


def dataset_reference(dataset: dict[str, Any], campaign: dict[str, Any]) -> dict[str, Any]:
    if not dataset:
        return {"dataset_id": campaign.get("dataset_id"), "content_hash": None, "sample_sizes": {}, "note": INSUFFICIENT}
    return {
        "dataset_id": dataset.get("id"),
        "dataset_key": dataset.get("dataset_key"),
        "content_hash": dataset.get("content_hash"),
        "sample_sizes": dataset.get("candle_counts") or {},
        "window_start": dataset.get("window_start"),
        "window_end": dataset.get("window_end"),
    }


def strategy_family_performance(jobs: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for job in jobs:
        family = str(job.get("strategy_family") or (job.get("candidate") or {}).get("parameters", {}).get("phase2_strategy_family") or (job.get("candidate") or {}).get("parameters", {}).get("hypothesis_strategy_family") or "unknown")
        grouped[family].append(job)
    rows = []
    for family, family_jobs in grouped.items():
        promoted = sum(1 for row in family_jobs if row.get("status") == "promoted")
        metrics = [dict((row.get("result") or {}).get("metrics") or {}) for row in family_jobs if row.get("result")]
        rows.append(
            {
                "strategy_family": family,
                "jobs": len(family_jobs),
                "promoted": promoted,
                "promotion_rate": round(promoted / len(family_jobs), 6) if family_jobs else 0.0,
                "median_profit_factor": safe_median([finite_metric(row.get("profit_factor")) for row in metrics]),
                "median_expectancy": safe_median([finite_metric(row.get("expectancy_per_trade")) for row in metrics]),
                "evidence_refs": [f"research_campaign_job:{row['id']}" for row in family_jobs[:10]],
            }
        )
    if not rows:
        empty = {"statement": INSUFFICIENT, "evidence_refs": []}
        return empty, empty
    ranked = sorted(rows, key=lambda row: (row["promotion_rate"], row["median_profit_factor"], row["jobs"], row["strategy_family"]), reverse=True)
    return ranked[0], ranked[-1]


def observation_contributions(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"promoted": [], "rejected": []})
    for job in jobs:
        metrics = dict((job.get("result") or {}).get("metrics") or {})
        candidate = dict(job.get("candidate") or {})
        parameters = dict(candidate.get("parameters") or {})
        observations = metrics.get("market_structure_observations") or parameters.get("market_structure_observations") or {}
        flattened = {}
        for key, value in {**metrics, **parameters}.items():
            if str(key).endswith("_score") or str(key).endswith("_event_rate"):
                flattened[str(key)] = finite_metric(value)
        for key, value in observations.items():
            if isinstance(value, dict):
                flattened[f"{key}_score"] = finite_metric(value.get("score"))
                flattened[f"{key}_event_rate"] = finite_metric(value.get("event_rate"))
        for key, value in flattened.items():
            if key.startswith("phase"):
                continue
            status = "promoted" if job.get("status") == "promoted" else "rejected"
            buckets[key][status].append(value)
    rows = []
    for key, groups in buckets.items():
        if not groups["promoted"] and not groups["rejected"]:
            continue
        promoted_mean = mean(groups["promoted"]) if groups["promoted"] else None
        rejected_mean = mean(groups["rejected"]) if groups["rejected"] else None
        rows.append(
            {
                "observation": key,
                "promoted_mean": round(promoted_mean, 6) if promoted_mean is not None else None,
                "rejected_mean": round(rejected_mean, 6) if rejected_mean is not None else None,
                "sample_size": len(groups["promoted"]) + len(groups["rejected"]),
                "interpretation": INSUFFICIENT if promoted_mean is None or rejected_mean is None else f"Mean promoted-minus-rejected difference {promoted_mean - rejected_mean:.6f}.",
            }
        )
    return sorted(rows, key=lambda row: (row["sample_size"], abs((row["promoted_mean"] or 0) - (row["rejected_mean"] or 0))), reverse=True)[:20] or [
        {"statement": INSUFFICIENT, "reason": "No Phase 5 observation fields were present in completed job payloads.", "evidence_refs": []}
    ]


def candidate_failure_analysis(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for job in jobs:
        if job.get("status") == "promoted":
            continue
        reasons = list(job.get("failure_reasons") or [])
        diagnostics = list(job.get("rejection_diagnostics") or [])
        failed_gates = [row for row in diagnostics if row.get("passed") is False]
        if not reasons and failed_gates:
            reasons = [str(row.get("name")) for row in failed_gates]
        if not reasons:
            reasons = [INSUFFICIENT]
        rows.append(
            {
                "candidate_id": job.get("candidate_id"),
                "job_id": job.get("id"),
                "asset": job.get("symbol"),
                "timeframe": job.get("timeframe"),
                "status": job.get("status"),
                "failure_reasons": reasons,
                "failed_gates": failed_gates,
                "sample_size": finite_metric(((job.get("result") or {}).get("metrics") or {}).get("number_of_trades")),
                "evidence_refs": [f"research_campaign_job:{job.get('id')}", f"research_campaign:{job.get('campaign_id')}"],
            }
        )
    return rows


def transferability_analysis(jobs: list[dict[str, Any]], stage_rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_candidate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for job in jobs:
        by_candidate[str(job.get("candidate_id"))].append(job)
    rows = []
    for candidate_id, candidate_jobs in by_candidate.items():
        assets = sorted({str(row.get("symbol")) for row in candidate_jobs if row.get("symbol")})
        promoted_assets = sorted({str(row.get("symbol")) for row in candidate_jobs if row.get("status") == "promoted"})
        rows.append(
            {
                "candidate_id": candidate_id,
                "assets_tested": assets,
                "assets_promoted": promoted_assets,
                "asset_pass_rate": round(len(promoted_assets) / len(assets), 6) if assets else 0.0,
                "evidence_refs": [f"research_campaign_job:{row['id']}" for row in candidate_jobs],
            }
        )
    cluster_evidence = [row for row in stage_rows if row.get("candidate_level") in {"cluster_candidate", "cluster_elite", "universal_elite"}]
    return {
        "candidate_transfer": sorted(rows, key=lambda row: (row["asset_pass_rate"], len(row["assets_tested"])), reverse=True),
        "cluster_or_universal_evidence_count": len(cluster_evidence),
        "interpretation": INSUFFICIENT if not cluster_evidence else "At least one candidate produced cluster/universal stage evidence.",
        "evidence_refs": [f"research_candidate_stage_evidence:{row.get('evidence_key')}" for row in cluster_evidence[:20]],
    }


def evolution_outcomes(evolution_rows: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> dict[str, Any]:
    if not evolution_rows and not any(row.get("parent_candidate_id") for row in jobs):
        return {"statement": INSUFFICIENT, "evidence_refs": []}
    parent_counts = Counter(str(row.get("parent_candidate_id")) for row in jobs if row.get("parent_candidate_id"))
    promoted_descendants = [row for row in jobs if row.get("parent_candidate_id") and row.get("status") == "promoted"]
    return {
        "parent_count": len(parent_counts),
        "descendant_jobs": sum(parent_counts.values()),
        "promoted_descendant_jobs": len(promoted_descendants),
        "confirmed_improvements": 0,
        "classification": "Promising descendant - unconfirmed",
        "reason": "Independent future frozen validation is required before any descendant is a confirmed improvement.",
        "evidence_refs": [f"research_evolution_history:{row.get('id')}" for row in evolution_rows[:20]] + [f"research_campaign_job:{row['id']}" for row in promoted_descendants[:10]],
    }


def hypothesis_lifecycle(bundle: dict[str, Any]) -> dict[str, Any]:
    hypothesis = bundle.get("hypothesis")
    versions = bundle.get("hypothesis_versions") or []
    if not hypothesis and not versions:
        return {"statement": INSUFFICIENT, "evidence_refs": []}
    rows = []
    for row in versions:
        summary = dict(row.get("test_summary") or {})
        post_hoc = bool(summary.get("post_hoc"))
        confirmation = summary.get("confirmation_status") or ("unconfirmed" if post_hoc else None)
        rows.append(
            {
                "hypothesis_id": row.get("id"),
                "hypothesis_key": row.get("hypothesis_key"),
                "status": row.get("status"),
                "post_hoc": post_hoc,
                "confirmation_status": confirmation,
                "lifecycle_interpretation": summary.get("lifecycle_interpretation"),
                "supporting_evidence": row.get("supporting_evidence") or [],
                "contradictory_evidence": row.get("contradictory_evidence") or [],
            }
        )
    return {
        "records": rows,
        "confirmed": [row for row in rows if row["status"] == "supported" and row["confirmation_status"] != "unconfirmed"],
        "strengthened": [row for row in rows if row["status"] in {"supported", "testing"}],
        "weakened": [row for row in rows if row["status"] == "weak"],
        "rejected": [row for row in rows if row["status"] == "rejected"],
        "inconclusive": [row for row in rows if row["confirmation_status"] == "unconfirmed" or row["status"] in {"proposed", "testing"}],
        "evidence_refs": [f"research_hypothesis:{row.get('id')}" for row in versions],
    }


def campaign_comparison(campaign: dict[str, Any], previous: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> dict[str, Any]:
    current_rate = sum(1 for row in jobs if row.get("status") == "promoted") / len(jobs) if jobs else 0.0
    previous_rates = []
    for row in previous:
        analytics = dict(row.get("analytics") or {})
        tested = finite_metric(analytics.get("strategies_tested") or analytics.get("jobs") or 0)
        promoted = finite_metric(analytics.get("promoted") or row.get("promoted_candidates") or 0)
        if tested:
            previous_rates.append(promoted / tested)
    baseline = mean(previous_rates) if previous_rates else None
    if baseline is None:
        return {"statement": INSUFFICIENT, "current_promotion_rate": round(current_rate, 6), "evidence_refs": [f"research_campaign:{campaign['id']}"]}
    return {
        "current_promotion_rate": round(current_rate, 6),
        "recent_completed_campaign_baseline": round(baseline, 6),
        "delta": round(current_rate - baseline, 6),
        "interpretation": "Improved versus recent completed campaign baseline." if current_rate > baseline else "Did not improve versus recent completed campaign baseline.",
        "evidence_refs": [f"research_campaign:{campaign['id']}"] + [f"research_campaign:{row['id']}" for row in previous[:5]],
    }


def structural_similarity(bundle: dict[str, Any]) -> dict[str, Any]:
    analytics = dict(bundle.get("analytics") or {})
    architecture = dict(analytics.get("research_architecture") or {})
    scope = dict(architecture.get("scope") or {})
    if scope.get("type") == "cluster":
        return {
            "scope": scope,
            "statement": "Campaign used a measured cluster scope.",
            "evidence_refs": [f"asset_cluster:{scope.get('ref')}", f"research_dataset:{bundle['campaign'].get('dataset_id')}"],
        }
    return {"statement": INSUFFICIENT, "reason": "No measured cluster scope was available for this campaign.", "evidence_refs": [f"research_campaign:{bundle['campaign']['id']}"]}


def contradictory_evidence(bundle: dict[str, Any], failures: list[dict[str, Any]], hypothesis: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for record in hypothesis.get("records") or []:
        if record.get("contradictory_evidence"):
            rows.append(
                {
                    "statement": "Hypothesis has contradictory evidence.",
                    "hypothesis_id": record.get("hypothesis_id"),
                    "contradictory_evidence": record.get("contradictory_evidence"),
                    "evidence_refs": [f"research_hypothesis:{record.get('hypothesis_id')}"],
                }
            )
    common_failures = Counter(reason for row in failures for reason in row.get("failure_reasons", []))
    for reason, count in common_failures.most_common(5):
        rows.append({"statement": f"Repeated failure reason: {reason}", "count": count, "evidence_refs": [ref for row in failures[:10] for ref in row.get("evidence_refs", [])]})
    return rows or [{"statement": INSUFFICIENT, "evidence_refs": [f"research_campaign:{bundle['campaign']['id']}"]}]


def learned_findings(bundle: dict[str, Any], best: dict[str, Any], worst: dict[str, Any], transfer: dict[str, Any], hypothesis: dict[str, Any]) -> list[dict[str, Any]]:
    campaign_id = bundle["campaign"]["id"]
    findings = []
    if best.get("strategy_family"):
        findings.append({"statement": f"{best['strategy_family']} was the strongest observed strategy family by promotion rate.", "evidence_refs": best.get("evidence_refs", [])})
    if worst.get("strategy_family") and worst.get("strategy_family") != best.get("strategy_family"):
        findings.append({"statement": f"{worst['strategy_family']} was the weakest observed strategy family by promotion rate.", "evidence_refs": worst.get("evidence_refs", [])})
    if transfer.get("candidate_transfer"):
        top = transfer["candidate_transfer"][0]
        findings.append({"statement": f"Top transfer candidate {top['candidate_id']} passed {len(top['assets_promoted'])} of {len(top['assets_tested'])} tested assets.", "evidence_refs": top.get("evidence_refs", [])})
    if hypothesis.get("inconclusive"):
        findings.append({"statement": "At least one hypothesis remains inconclusive or unconfirmed.", "evidence_refs": hypothesis.get("evidence_refs", [])})
    return findings or [{"statement": INSUFFICIENT, "evidence_refs": [f"research_campaign:{campaign_id}"]}]


def next_campaign_recommendations(bundle: dict[str, Any], learned: list[dict[str, Any]], failures: list[dict[str, Any]], transfer: dict[str, Any], evolution: dict[str, Any], observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    recommendations = []
    if failures:
        reason = Counter(reason for row in failures for reason in row.get("failure_reasons", [])).most_common(1)[0][0]
        recommendations.append(
            {
                "priority": 1,
                "recommendation": f"Design the next campaign to directly test or reduce the dominant failure mode: {reason}.",
                "supporting_evidence": [ref for row in failures[:10] for ref in row.get("evidence_refs", [])],
            }
        )
    if transfer.get("candidate_transfer"):
        top = transfer["candidate_transfer"][0]
        recommendations.append(
            {
                "priority": 2,
                "recommendation": f"Retest candidate lineage around {top['candidate_id']} on an independent future frozen dataset before calling it transferable.",
                "supporting_evidence": top.get("evidence_refs", []),
            }
        )
    if evolution.get("descendant_jobs"):
        recommendations.append(
            {
                "priority": 3,
                "recommendation": "Forward-validate promoted descendants; keep classification as promising descendant - unconfirmed until independent evidence exists.",
                "supporting_evidence": evolution.get("evidence_refs", []),
            }
        )
    if observations and observations[0].get("observation"):
        recommendations.append(
            {
                "priority": 4,
                "recommendation": f"Use Phase 5 observation `{observations[0]['observation']}` as an explicit next-campaign stratification variable, not as a confirmation claim.",
                "supporting_evidence": [f"research_campaign:{bundle['campaign']['id']}"],
            }
        )
    return recommendations or [{"priority": 1, "recommendation": INSUFFICIENT, "supporting_evidence": [f"research_campaign:{bundle['campaign']['id']}"]}]


def unresolved_questions(hypothesis: dict[str, Any], transfer: dict[str, Any], evolution: dict[str, Any], observations: list[dict[str, Any]], failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    questions = []
    if hypothesis.get("inconclusive"):
        questions.append({"question": "Will post-hoc hypotheses survive an independent future frozen dataset?", "evidence_refs": hypothesis.get("evidence_refs", [])})
    if transfer.get("interpretation") == INSUFFICIENT:
        questions.append({"question": "Is there enough cross-asset evidence to claim structural transferability?", "evidence_refs": transfer.get("evidence_refs", [])})
    if evolution.get("confirmed_improvements") == 0:
        questions.append({"question": "Do evolved descendants improve outside same-dataset development evidence?", "evidence_refs": evolution.get("evidence_refs", [])})
    if observations and observations[0].get("statement") == INSUFFICIENT:
        questions.append({"question": "Which Phase 5 observations actually separate winners from failures?", "evidence_refs": []})
    if not failures:
        questions.append({"question": "Why did no candidate-level failure rows exist for this campaign?", "evidence_refs": []})
    return questions or [{"question": INSUFFICIENT, "evidence_refs": []}]


def compute_budget(jobs: list[dict[str, Any]], previous_campaigns: list[dict[str, Any]]) -> dict[str, Any]:
    runtimes = [finite_metric(row.get("execution_runtime_ms")) for row in jobs if row.get("execution_runtime_ms") is not None]
    current = {
        "jobs": len(jobs),
        "runtime_ms": int(sum(runtimes)) if runtimes else 0,
        "median_runtime_ms": safe_median(runtimes),
    }
    previous = []
    for campaign in previous_campaigns[:10]:
        analytics = dict(campaign.get("analytics") or {})
        runtime = analytics.get("total_runtime_ms") or (analytics.get("runtime") or {}).get("total_ms")
        jobs_count = analytics.get("strategies_tested") or analytics.get("jobs")
        if runtime is not None or jobs_count is not None:
            previous.append({"campaign_id": campaign.get("id"), "jobs": jobs_count, "runtime_ms": runtime})
    return {"current": current, "recent_completed_campaigns": previous, "comparison": INSUFFICIENT if not previous else "Comparable compute summary included for recent completed campaigns."}


def campaign_refs(campaign_id: int, dataset: dict[str, Any], jobs: list[dict[str, Any]]) -> list[str]:
    refs = [f"research_campaign:{campaign_id}"]
    if dataset.get("dataset_id"):
        refs.append(f"research_dataset:{dataset['dataset_id']}")
    refs.extend(f"research_campaign_job:{row.get('id')}" for row in jobs if row.get("id"))
    return refs


def scientific_report_markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"# Scientific Campaign Report: {payload['campaign_name']}",
        "",
        "Simulation-only research report. No broker routing, paper routing, live routing, or validation-threshold change is authorized by this report.",
        "",
        "## Executive summary",
        "",
        f"- Campaign: `{payload['campaign_id']}`",
        f"- Dataset: `{payload['dataset'].get('dataset_id')}` / `{payload['dataset'].get('content_hash')}`",
        f"- Jobs: `{payload['executive_summary']['jobs']}`",
        f"- Promoted jobs: `{payload['executive_summary']['promoted_jobs']}`",
        f"- Rejected jobs: `{payload['executive_summary']['rejected_jobs']}`",
        f"- Primary conclusion: {payload['executive_summary']['primary_conclusion']}",
        "",
        "## What was learned",
        "",
    ]
    lines.extend(f"- {row['statement']} Evidence: `{row.get('evidence_refs', [])}`" for row in payload["what_was_learned"])
    lines.extend(["", "## What improved", "", json_dump(payload["what_improved"])])
    lines.extend(["", "## What failed and why", ""])
    lines.extend(f"- Candidate `{row.get('candidate_id')}`: {row.get('failure_reasons')} Evidence: `{row.get('evidence_refs')}`" for row in payload["what_failed"][:20])
    lines.extend(["", "## Hypothesis lifecycle", "", json_dump(payload["hypothesis_lifecycle"])])
    lines.extend(["", "## Observation contributions", "", json_dump(payload["observation_contributions"])])
    lines.extend(["", "## Strategy family performance", "", json_dump(payload["strategy_family_performance"])])
    lines.extend(["", "## Structural similarity and transferability", "", json_dump({"structural_similarity": payload["structural_similarity"], "transferability": payload["transferability_analysis"]})])
    lines.extend(["", "## Evolution outcomes", "", json_dump(payload["evolution_outcomes"])])
    lines.extend(["", "## Contradictory evidence and unresolved questions", "", json_dump({"contradictory_evidence": payload["contradictory_evidence"], "unresolved_questions": payload["unresolved_questions"]})])
    lines.extend(["", "## Prioritized next campaign recommendations", ""])
    lines.extend(f"{row['priority']}. {row['recommendation']} Evidence: `{row.get('supporting_evidence', [])}`" for row in payload["next_campaign_recommendations"])
    lines.extend(["", "## Reproducibility and compute", "", json_dump({"reproducibility": payload["reproducibility"], "compute_budget": payload["compute_budget"]})])
    return "\n".join(lines)


def json_dump(value: Any) -> str:
    import json

    return "```json\n" + json.dumps(jsonable(value), indent=2, sort_keys=True) + "\n```"


def safe_median(values: list[float]) -> float:
    cleaned = [finite_metric(value) for value in values if value is not None]
    return round(median(cleaned), 6) if cleaned else 0.0
