from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import math
import re
from statistics import median
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.services.research_architecture import (
    HYPOTHESIS_VERSION,
    ensure_research_architecture_tables,
    jsonable,
    stable_hash,
)
from app.services.research_learning import parameter_buckets
from app.services.strategy_discovery import DiscoveryCandidate, candidate_execution_key
from app.services.strategy_families import PHASE_2_FAMILY_NAMES, strategy_family_spec
from app.services.strategy_research import finite_metric


EDGE_DISCOVERY_VERSION = "edge_discovery_engine_v1"
MIN_FAMILY_JOBS = 10
MIN_COMPUTED_RESULTS = 4
MIN_UNIQUE_EXECUTIONS = 3
MAX_GENERATED_HYPOTHESES = 12
POST_HOC_LABEL = "Post-hoc and unconfirmed."


def run_edge_discovery(
    conn: psycopg.Connection,
    *,
    dataset_id: int | None = None,
    max_hypotheses: int = MAX_GENERATED_HYPOTHESES,
) -> dict[str, Any]:
    """Convert preserved research outcomes into standard hypothesis versions.

    This intentionally appends new hypothesis rows. It does not update old
    hypothesis records, campaign evidence, thresholds, candidates, or jobs.
    """

    ensure_research_architecture_tables(conn)
    history = fetch_research_history(conn, dataset_id=dataset_id)
    discovery = build_edge_discovery_hypotheses(history, max_hypotheses=max_hypotheses)
    stored = store_edge_hypotheses(conn, discovery["hypotheses"])
    result = {
        **discovery,
        "stored_hypotheses": stored,
        "stored_hypothesis_ids": [row["id"] for row in stored],
        "storage": {
            "table": "research_hypothesis_versions",
            "format": "existing KefTrade hypothesis structure",
            "immutable_history_rewritten": False,
        },
    }
    conn.commit()
    return jsonable(result)


def fetch_research_history(conn: psycopg.Connection, *, dataset_id: int | None = None) -> dict[str, Any]:
    dataset_filter = "AND c.dataset_id = %s" if dataset_id is not None else "AND c.dataset_id IS NOT NULL"
    params: tuple[Any, ...] = (dataset_id,) if dataset_id is not None else ()
    campaigns = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT c.*, h.hypothesis_key, h.version AS hypothesis_version_number,
                   h.status AS hypothesis_status, h.hypothesis AS hypothesis_text,
                   h.test_summary AS hypothesis_test_summary
            FROM research_campaigns c
            LEFT JOIN research_hypothesis_versions h ON h.id = c.hypothesis_version_id
            WHERE c.simulation_only = TRUE
              {dataset_filter}
            ORDER BY c.id
            """,
            params,
        ).fetchall()
    ]
    campaign_ids = [int(row["id"]) for row in campaigns]
    jobs: list[dict[str, Any]] = []
    stages: list[dict[str, Any]] = []
    if campaign_ids:
        jobs = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM research_campaign_jobs
                WHERE campaign_id = ANY(%s::bigint[])
                  AND simulation_only = TRUE
                ORDER BY campaign_id, candidate_id, symbol, timeframe, id
                """,
                (campaign_ids,),
            ).fetchall()
        ]
        stages = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM research_candidate_stage_evidence
                WHERE campaign_id = ANY(%s::bigint[])
                  AND simulation_only = TRUE
                ORDER BY campaign_id, candidate_id, candidate_level, id
                """,
                (campaign_ids,),
            ).fetchall()
        ]
    profile_dataset_id = dataset_id or next((row.get("dataset_id") for row in reversed(campaigns) if row.get("dataset_id")), None)
    profiles = []
    clusters = []
    if profile_dataset_id is not None:
        profiles = [dict(row) for row in conn.execute("SELECT * FROM asset_profile_versions WHERE dataset_id = %s ORDER BY timeframe, symbol", (profile_dataset_id,)).fetchall()]
        clusters = [dict(row) for row in conn.execute("SELECT * FROM asset_cluster_versions WHERE dataset_id = %s ORDER BY id", (profile_dataset_id,)).fetchall()]
    old_hypotheses = [
        dict(row)
        for row in conn.execute(
            """
            SELECT *
            FROM research_hypothesis_versions
            WHERE simulation_only = TRUE
            ORDER BY id
            """
        ).fetchall()
    ]
    return {
        "dataset_id": profile_dataset_id,
        "campaigns": campaigns,
        "jobs": jobs,
        "stages": stages,
        "profiles": profiles,
        "clusters": clusters,
        "hypotheses": old_hypotheses,
    }


def build_edge_discovery_hypotheses(history: dict[str, Any], *, max_hypotheses: int = MAX_GENERATED_HYPOTHESES) -> dict[str, Any]:
    campaigns = [dict(row) for row in history.get("campaigns") or []]
    jobs = [normalize_job_evidence(row) for row in history.get("jobs") or []]
    jobs = [row for row in jobs if row["strategy_family"] not in {"", "unknown"}]
    lifecycle = [
        derive_lifecycle_interpretation(row)
        for row in history.get("hypotheses") or []
        if row.get("creation_source") != EDGE_DISCOVERY_VERSION
    ]
    candidates = []
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in jobs:
        grouped[row["strategy_family"]].append(row)

    inconclusive = []
    for family in sorted(grouped):
        rows = grouped[family]
        audit = family_audit(family, rows)
        if not audit["sample_control_passed"]:
            inconclusive.append(
                {
                    "family": family,
                    "reason": "Inconclusive - insufficient evidence.",
                    "controls": audit,
                }
            )
            continue
        candidates.extend(edge_hypotheses_for_family(family, rows, campaigns, history))

    candidates = dedupe_hypotheses(candidates)
    candidates = sorted(
        candidates,
        key=lambda row: (
            finite_metric(row["confidence_score"]),
            len(row["supporting_evidence"]),
            row["hypothesis_key"],
        ),
        reverse=True,
    )[:max_hypotheses]
    # Lifecycle integrity is not a market edge ranking problem. If historical
    # wording/status inconsistencies exist, store the derived interpretation
    # even when market-edge hypotheses fill the requested evidence budget.
    candidates = dedupe_hypotheses(candidates + lifecycle_hypotheses(lifecycle, history))
    return {
        "edge_discovery_version": EDGE_DISCOVERY_VERSION,
        "dataset_id": history.get("dataset_id"),
        "campaign_ids_analyzed": sorted({int(row["campaign_id"]) for row in jobs if row.get("campaign_id") is not None}),
        "jobs_analyzed": len(jobs),
        "unique_execution_keys": len({row["execution_key"] for row in jobs}),
        "families_analyzed": sorted(grouped),
        "hypotheses": candidates,
        "inconclusive_findings": inconclusive,
        "lifecycle_interpretations": lifecycle,
        "controls": {
            "minimum_family_jobs": MIN_FAMILY_JOBS,
            "minimum_computed_results": MIN_COMPUTED_RESULTS,
            "minimum_unique_executions": MIN_UNIQUE_EXECUTIONS,
            "winner_and_loser_comparison": True,
            "executable_strategy_deduplication": True,
            "candidate_family_separation": True,
            "source_target_comparison": True,
            "multiple_comparison_awareness": "effect sizes are descriptive; all generated hypotheses remain post-hoc until independent frozen validation",
            "validation_thresholds_changed": False,
            "candidate_volume_increased": False,
        },
        "simulation_only": True,
    }


def normalize_job_evidence(job: dict[str, Any]) -> dict[str, Any]:
    candidate = dict(job.get("candidate") or {})
    params = dict(candidate.get("parameters") or {})
    result = dict(job.get("result") or {})
    metrics = dict(result.get("metrics") or {})
    candidate_id = str(job.get("candidate_id") or candidate.get("candidate_id") or "")
    execution_key = execution_key_for_job(candidate)
    diagnostics = list(job.get("rejection_diagnostics") or [])
    failed_gates = [str(row.get("name")) for row in diagnostics if row and not row.get("passed")]
    if not failed_gates and str(job.get("status")) in {"rejected", "failed", "blocked_data"}:
        failed_gates = infer_failed_gates(metrics, result)
    return {
        "job_id": job.get("id"),
        "campaign_id": job.get("campaign_id"),
        "dataset_id": job.get("dataset_id"),
        "hypothesis_version_id": job.get("hypothesis_version_id"),
        "candidate_id": candidate_id,
        "execution_key": execution_key,
        "symbol": str(job.get("symbol") or result.get("symbol") or ""),
        "timeframe": str(job.get("timeframe") or result.get("timeframe") or ""),
        "strategy_family": str(job.get("strategy_family") or params.get("phase2_strategy_family") or params.get("hypothesis_strategy_family") or "unknown"),
        "status": str(job.get("status") or ""),
        "promoted": str(job.get("status")) == "promoted",
        "rejected": str(job.get("status")) in {"rejected", "failed", "blocked_data"},
        "computed": bool(metrics),
        "metrics": metrics,
        "result": result,
        "parameters": params,
        "parameter_buckets": parameter_buckets(params),
        "failed_gates": sorted(set(failed_gates)),
        "evidence_ref": f"research_campaign_job:{job.get('id')}",
    }


def execution_key_for_job(candidate: dict[str, Any]) -> str:
    try:
        discovery = DiscoveryCandidate(
            candidate_id=str(candidate["candidate_id"]),
            family_id=str(candidate.get("family_id") or ""),
            parent_candidate_id=candidate.get("parent_candidate_id"),
            generation=int(candidate.get("generation") or 1),
            blocks=dict(candidate.get("blocks") or {}),
            parameters=dict(candidate.get("parameters") or {}),
            complexity=int(candidate.get("complexity") or 1),
            canonical_key=str(candidate.get("canonical_key") or ""),
        )
        return candidate_execution_key(discovery)
    except (KeyError, TypeError, ValueError):
        return stable_hash(candidate.get("parameters") or candidate)


def infer_failed_gates(metrics: dict[str, Any], result: dict[str, Any]) -> list[str]:
    failed = []
    if finite_metric(metrics.get("number_of_trades")) < 30:
        failed.append("trade_count")
    if metrics.get("profit_factor") is not None and finite_metric(metrics.get("profit_factor")) < 1.2:
        failed.append("profit_factor")
    if finite_metric(metrics.get("expectancy_per_trade")) <= 0:
        failed.append("positive_expectancy")
    if finite_metric(metrics.get("max_drawdown")) > 0.12:
        failed.append("maximum_drawdown")
    if not bool((metrics.get("walk_forward") or {}).get("enabled")):
        failed.append("walk_forward")
    if not bool((result.get("paper_readiness") or {}).get("paper_ready")):
        failed.append("paper_readiness")
    return failed or ["validation"]


def family_audit(family: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    computed = [row for row in rows if row["computed"]]
    executions = {row["execution_key"] for row in rows}
    promoted = [row for row in rows if row["promoted"]]
    rejected = [row for row in rows if row["rejected"]]
    return {
        "family": family,
        "jobs": len(rows),
        "computed_results": len(computed),
        "unique_execution_keys": len(executions),
        "promoted_jobs": len(promoted),
        "rejected_jobs": len(rejected),
        "sample_control_passed": len(rows) >= MIN_FAMILY_JOBS and len(computed) >= MIN_COMPUTED_RESULTS and len(executions) >= MIN_UNIQUE_EXECUTIONS,
    }


def edge_hypotheses_for_family(
    family: str,
    rows: list[dict[str, Any]],
    campaigns: list[dict[str, Any]],
    history: dict[str, Any],
) -> list[dict[str, Any]]:
    computed = [row for row in rows if row["computed"]]
    promoted = [row for row in rows if row["promoted"]]
    rejected = [row for row in rows if row["rejected"]]
    hypotheses = []
    gate_counts = Counter(gate for row in rejected for gate in row["failed_gates"])
    pfs = [finite_metric(row["metrics"].get("profit_factor")) for row in computed if row["metrics"].get("profit_factor") is not None]
    expectancies = [finite_metric(row["metrics"].get("expectancy_per_trade")) for row in computed if row["metrics"].get("expectancy_per_trade") is not None]
    sample_fail_rate = gate_counts.get("trade_count", 0) / max(1, len(rows))
    economic_fail_rate = (gate_counts.get("profit_factor", 0) + gate_counts.get("positive_expectancy", 0)) / max(1, len(rows) * 2)
    scope = family_scope(rows, campaigns)
    if sample_fail_rate >= 0.45:
        hypotheses.append(
            make_edge_hypothesis(
                family=family,
                discovery_type="frequency_condition",
                scope=scope,
                rows=rows,
                title=f"{family} frequency-condition test on {scope['label']}",
                observation=(
                    f"{POST_HOC_LABEL} {gate_counts.get('trade_count', 0)} of {len(rows)} {family} jobs failed the trade-count gate "
                    f"under unchanged validation while {len({row['execution_key'] for row in rows})} executable keys were tested."
                ),
                hypothesis=(
                    f"On an independent frozen {scope['label']} dataset, {family} candidates that relax only the dominant frequency-sensitive "
                    "conditions identified by Edge Discovery will reach at least 30 trades per market without reducing median profit factor "
                    "below the matched family baseline."
                ),
                expected_behavior="Trade-count survival improves while profit factor, expectancy, drawdown, walk-forward, and paper-readiness gates remain unchanged.",
                supporting=[row["evidence_ref"] for row in rows if "trade_count" in row["failed_gates"]],
                contradictory=[row["evidence_ref"] for row in rows if "trade_count" not in row["failed_gates"]],
                effect={
                    "sample_failure_rate": round(sample_fail_rate, 6),
                    "computed_results": len(computed),
                    "median_profit_factor": safe_median(pfs),
                    "median_expectancy": safe_median(expectancies),
                },
                history=history,
            )
        )
    if pfs and safe_median(pfs) is not None and safe_median(pfs) < 1.0 and economic_fail_rate >= 0.35:
        hypotheses.append(
            make_edge_hypothesis(
                family=family,
                discovery_type="economic_failure_condition",
                scope=scope,
                rows=rows,
                title=f"{family} economic-filter test on {scope['label']}",
                observation=(
                    f"{POST_HOC_LABEL} Median computed profit factor was {safe_median(pfs)} and median expectancy was "
                    f"{safe_median(expectancies)} across {len(computed)} computed {family} jobs."
                ),
                hypothesis=(
                    f"On an independent frozen {scope['label']} dataset, {family} generation should reject or materially alter parameter "
                    "regions matching the observed losing buckets unless the candidate shows positive expectancy and PF >= 1.2 on at least two markets."
                ),
                expected_behavior="Economic quality improves through falsifiable parameter-region avoidance, not through larger candidate volume.",
                supporting=[row["evidence_ref"] for row in rejected if "profit_factor" in row["failed_gates"] or "positive_expectancy" in row["failed_gates"]],
                contradictory=[row["evidence_ref"] for row in rows if row["promoted"] or finite_metric(row["metrics"].get("profit_factor")) >= 1.2],
                effect={
                    "economic_failure_rate": round(economic_fail_rate, 6),
                    "computed_results": len(computed),
                    "median_profit_factor": safe_median(pfs),
                    "median_expectancy": safe_median(expectancies),
                },
                history=history,
            )
        )
    near_passes = [
        row
        for row in rejected
        if finite_metric(row["metrics"].get("number_of_trades")) >= 30
        and finite_metric(row["metrics"].get("profit_factor")) >= 1.0
        and finite_metric(row["metrics"].get("expectancy_per_trade")) > 0
    ]
    if near_passes:
        hypotheses.append(
            make_edge_hypothesis(
                family=family,
                discovery_type="near_pass_subcondition",
                scope=scope,
                rows=rows,
                title=f"{family} near-pass subcondition test on {scope['label']}",
                observation=(
                    f"{POST_HOC_LABEL} {len(near_passes)} rejected {family} jobs reached trade count with positive expectancy and PF >= 1.0, "
                    "but failed at least one unchanged readiness or threshold gate."
                ),
                hypothesis=(
                    f"On an independent frozen {scope['label']} dataset, {family} candidates matching the near-pass subconditions must improve "
                    "PF to >= 1.2 and preserve paper-readiness on at least two markets before the pattern can be considered supported."
                ),
                expected_behavior="Near-pass behavior either converts into a valid specialist/cluster result on independent evidence or is rejected as historical noise.",
                supporting=[row["evidence_ref"] for row in near_passes],
                contradictory=[row["evidence_ref"] for row in rejected if row not in near_passes],
                effect={
                    "near_pass_jobs": len(near_passes),
                    "near_pass_rate": round(len(near_passes) / max(1, len(rows)), 6),
                    "best_profit_factor": max(finite_metric(row["metrics"].get("profit_factor")) for row in near_passes),
                    "best_expectancy": max(finite_metric(row["metrics"].get("expectancy_per_trade")) for row in near_passes),
                },
                history=history,
            )
        )
    if promoted and rejected:
        hypotheses.append(
            make_edge_hypothesis(
                family=family,
                discovery_type="winner_loser_transfer_condition",
                scope=scope,
                rows=rows,
                title=f"{family} winner-loser transfer test on {scope['label']}",
                observation=(
                    f"{POST_HOC_LABEL} {len(promoted)} promoted and {len(rejected)} rejected {family} jobs coexist in preserved evidence, "
                    "so the family requires source/target separation before any transfer claim."
                ),
                hypothesis=(
                    f"On an independent frozen {scope['label']} dataset, {family} candidates should only be treated as structurally transferable "
                    "when the same executable key passes unchanged gates on at least two assets and does not fail economic gates on the target asset."
                ),
                expected_behavior="Transferability is measured by unchanged executable-key survival across assets, not by source-asset profitability alone.",
                supporting=[row["evidence_ref"] for row in promoted],
                contradictory=[row["evidence_ref"] for row in rejected],
                effect={
                    "promoted_jobs": len(promoted),
                    "rejected_jobs": len(rejected),
                    "transfer_success_rate": round(len(promoted) / max(1, len(rows)), 6),
                },
                history=history,
            )
        )
    return hypotheses


def make_edge_hypothesis(
    *,
    family: str,
    discovery_type: str,
    scope: dict[str, str],
    rows: list[dict[str, Any]],
    title: str,
    observation: str,
    hypothesis: str,
    expected_behavior: str,
    supporting: list[str],
    contradictory: list[str],
    effect: dict[str, Any],
    history: dict[str, Any],
) -> dict[str, Any]:
    campaign_ids = sorted({int(row["campaign_id"]) for row in rows if row.get("campaign_id") is not None})
    candidate_ids = sorted({str(row["candidate_id"]) for row in rows if row.get("candidate_id")})
    dataset_id = history.get("dataset_id")
    spec = strategy_family_spec(family) if family in PHASE_2_FAMILY_NAMES else None
    key = "edge_hyp_" + stable_hash(
        {
            "version": EDGE_DISCOVERY_VERSION,
            "dataset_id": dataset_id,
            "family": family,
            "type": discovery_type,
            "scope": scope["ref"],
        }
    )[:20]
    confidence = edge_confidence(rows, supporting, contradictory, effect)
    return {
        "hypothesis_key": key,
        "scope_type": scope["type"],
        "scope_ref": scope["ref"],
        "strategy_family": family,
        "title": title,
        "observation": observation,
        "hypothesis": hypothesis,
        "expected_behavior": expected_behavior,
        "relevant_regimes": list(spec.relevant_conditions) if spec else ["bull_trend", "sideways", "normal_volatility"],
        "confidence_score": confidence,
        "evidence_window": {
            "dataset_id": dataset_id,
            "campaign_ids": campaign_ids,
            "candidate_ids": candidate_ids[:100],
            "sample_size": len(rows),
            "computed_results": sum(1 for row in rows if row["computed"]),
            "unique_execution_keys": len({row["execution_key"] for row in rows}),
            "independent_confirmation_required": True,
        },
        "creation_source": EDGE_DISCOVERY_VERSION,
        "status": "proposed",
        "supporting_evidence": sorted(set(supporting)),
        "contradictory_evidence": sorted(set(contradictory)),
        "test_summary": {
            "source_dataset_id": dataset_id,
            "campaign_ids": campaign_ids,
            "symbols": sorted({row["symbol"] for row in rows if row["symbol"]}),
            "timeframes": sorted({row["timeframe"] for row in rows if row["timeframe"]}),
            "discovery_type": discovery_type,
            "effect_size": effect,
            "post_hoc": True,
            "confirmation_status": "unconfirmed",
            "independent_confirmation_required": True,
            "edge_discovery_version": EDGE_DISCOVERY_VERSION,
            "measurable_success_criteria": [
                "independent frozen dataset",
                "unchanged strong_research_gates:v1 thresholds",
                "PF >= 1.2 where profit-factor gate applies",
                "positive expectancy",
                "minimum trade count preserved",
                "walk-forward and paper-readiness survival preserved",
            ],
            "falsification_criteria": [
                "same parameter condition fails economic gates on independent evidence",
                "trade-count survival remains below gate",
                "source-asset result does not transfer to target assets",
                "contradictory evidence dominates supporting evidence",
            ],
            "controls": {
                "winner_loser_comparison": True,
                "executable_deduplication": True,
                "candidate_family_separation": True,
                "multiple_comparison_awareness": True,
                "thresholds_changed": False,
                "candidate_volume_increased": False,
            },
            "candidate_generation_contract": "standard generate_targeted_candidates; no custom glue code",
        },
        "calculation_version": HYPOTHESIS_VERSION,
    }


def family_scope(rows: list[dict[str, Any]], campaigns: list[dict[str, Any]]) -> dict[str, str]:
    campaign_by_id = {int(row["id"]): row for row in campaigns if row.get("id") is not None}
    scopes = []
    for row in rows:
        campaign = campaign_by_id.get(int(row["campaign_id"])) if row.get("campaign_id") is not None else None
        scope = dict((campaign or {}).get("immutable_config") or {}).get("scope") or dict((campaign or {}).get("controls") or {}).get("target_scope") or {}
        if scope.get("type") and scope.get("ref"):
            scopes.append((str(scope["type"]), str(scope["ref"])))
    if scopes:
        (scope_type, scope_ref), _count = Counter(scopes).most_common(1)[0]
    else:
        scope_type, scope_ref = "universal", "full_research_history"
    symbols = sorted({row["symbol"] for row in rows if row["symbol"]})
    label = "/".join(symbols[:4]) if symbols else scope_ref
    return {"type": scope_type, "ref": scope_ref, "label": label}


def edge_confidence(rows: list[dict[str, Any]], supporting: list[str], contradictory: list[str], effect: dict[str, Any]) -> float:
    sample_component = min(0.30, math.log10(max(10, len(rows))) / 10)
    support_component = min(0.20, len(set(supporting)) / max(1, len(rows)) * 0.20)
    contradiction_penalty = min(0.18, len(set(contradictory)) / max(1, len(rows)) * 0.18)
    effect_component = min(0.20, max(abs(finite_metric(value)) for value in effect.values() if isinstance(value, (int, float))) / 5) if effect else 0
    return round(max(0.10, min(0.74, 0.32 + sample_component + support_component + effect_component - contradiction_penalty)), 4)


def lifecycle_hypotheses(lifecycle: list[dict[str, Any]], history: dict[str, Any]) -> list[dict[str, Any]]:
    inconsistent = [row for row in lifecycle if row["wording_status_inconsistent"]]
    if not inconsistent:
        return []
    refs = [f"research_hypothesis:{row['hypothesis_id']}" for row in inconsistent]
    dataset_id = history.get("dataset_id")
    key = "edge_hyp_" + stable_hash({"version": EDGE_DISCOVERY_VERSION, "dataset_id": dataset_id, "type": "lifecycle_interpretation", "ids": refs})[:20]
    return [
        {
            "hypothesis_key": key,
            "scope_type": "universal",
            "scope_ref": "hypothesis_lifecycle",
            "strategy_family": "Trend Following",
            "title": "Hypothesis lifecycle wording consistency interpretation",
            "observation": f"{POST_HOC_LABEL} {len(inconsistent)} preserved hypothesis versions contain confirmed wording while their authoritative stored status is not independently supported.",
            "hypothesis": (
                "On future frozen research records, hypothesis display and campaign planning should use the derived authoritative lifecycle interpretation: "
                "confirmed wording in historical text is not confirmation unless the version has independent supported status."
            ),
            "expected_behavior": "Planning treats inconsistent historical wording as unconfirmed while preserving the original immutable evidence.",
            "relevant_regimes": [],
            "confidence_score": 0.72,
            "evidence_window": {
                "dataset_id": dataset_id,
                "sample_size": len(inconsistent),
                "independent_confirmation_required": False,
                "immutable_history_rewritten": False,
            },
            "creation_source": EDGE_DISCOVERY_VERSION,
            "status": "proposed",
            "supporting_evidence": refs,
            "contradictory_evidence": [],
            "test_summary": {
                "source_dataset_id": dataset_id,
                "discovery_type": "hypothesis_lifecycle_interpretation",
                "post_hoc": True,
                "confirmation_status": "unconfirmed",
                "authoritative_interpretation": "stored status plus independent-confirmation metadata overrides optimistic wording",
                "corrected_versions_created": True,
                "old_records_mutated": False,
                "inconsistent_hypotheses": inconsistent,
                "edge_discovery_version": EDGE_DISCOVERY_VERSION,
                "candidate_generation_contract": "standard generate_targeted_candidates; no custom glue code",
            },
            "calculation_version": HYPOTHESIS_VERSION,
        }
    ]


def derive_lifecycle_interpretation(row: dict[str, Any]) -> dict[str, Any]:
    summary = dict(row.get("test_summary") or {})
    text = " ".join(str(row.get(key) or "") for key in ("title", "observation", "hypothesis")).lower()
    claims_confirmed = bool(re.search(r"\bconfirmed\b", text)) or str(summary.get("confirmation_status") or "").lower() == "confirmed"
    status = str(row.get("status") or "")
    independently_supported = status == "supported" and not (
        bool(summary.get("post_hoc")) and str(summary.get("source_dataset_id")) == str((row.get("evidence_window") or {}).get("dataset_id"))
    )
    authoritative_status = "supported" if independently_supported else status
    confirmation_status = "confirmed" if independently_supported else "unconfirmed"
    return {
        "hypothesis_id": row.get("id"),
        "hypothesis_key": row.get("hypothesis_key"),
        "stored_status": status,
        "claims_confirmed_in_text": claims_confirmed,
        "authoritative_status": authoritative_status,
        "authoritative_confirmation_status": confirmation_status,
        "wording_status_inconsistent": bool(claims_confirmed and confirmation_status != "confirmed"),
        "immutable_history_rewritten": False,
    }


def store_edge_hypotheses(conn: psycopg.Connection, hypotheses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stored = []
    for hypothesis in hypotheses:
        existing = conn.execute(
            """
            SELECT *
            FROM research_hypothesis_versions
            WHERE hypothesis_key = %s
              AND creation_source = %s
              AND test_summary->>'source_dataset_id' = %s
            ORDER BY version DESC
            LIMIT 1
            """,
            (hypothesis["hypothesis_key"], EDGE_DISCOVERY_VERSION, str((hypothesis.get("test_summary") or {}).get("source_dataset_id"))),
        ).fetchone()
        if existing:
            stored.append(jsonable(dict(existing)))
            continue
        prior = conn.execute(
            "SELECT * FROM research_hypothesis_versions WHERE hypothesis_key = %s ORDER BY version DESC LIMIT 1",
            (hypothesis["hypothesis_key"],),
        ).fetchone()
        version_row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM research_hypothesis_versions WHERE hypothesis_key = %s",
            (hypothesis["hypothesis_key"],),
        ).fetchone()
        row = conn.execute(
            """
            INSERT INTO research_hypothesis_versions(
                hypothesis_key, version, parent_hypothesis_id, scope_type, scope_ref, strategy_family,
                title, observation, hypothesis, expected_behavior, relevant_regimes, confidence_score,
                evidence_window, creation_source, status, supporting_evidence, contradictory_evidence,
                test_summary, calculation_version, simulation_only
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            RETURNING *
            """,
            (
                hypothesis["hypothesis_key"],
                int(version_row["next_version"]),
                prior.get("id") if prior else None,
                hypothesis["scope_type"],
                hypothesis["scope_ref"],
                hypothesis["strategy_family"],
                hypothesis["title"],
                hypothesis["observation"],
                hypothesis["hypothesis"],
                hypothesis["expected_behavior"],
                Jsonb(list(hypothesis.get("relevant_regimes") or [])),
                hypothesis["confidence_score"],
                Jsonb(dict(hypothesis.get("evidence_window") or {})),
                hypothesis["creation_source"],
                hypothesis["status"],
                Jsonb(list(hypothesis.get("supporting_evidence") or [])),
                Jsonb(list(hypothesis.get("contradictory_evidence") or [])),
                Jsonb(dict(hypothesis.get("test_summary") or {})),
                hypothesis["calculation_version"],
            ),
        ).fetchone()
        stored.append(jsonable(dict(row)))
    return stored


def dedupe_hypotheses(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for row in rows:
        key = row["hypothesis_key"]
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def safe_median(values: list[float]) -> float | None:
    cleaned = [finite_metric(value) for value in values if value is not None]
    return round(median(cleaned), 6) if cleaned else None


def stable_key(*parts: Any) -> str:
    return sha256("|".join(str(part) for part in parts).encode()).hexdigest()[:24]


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value
