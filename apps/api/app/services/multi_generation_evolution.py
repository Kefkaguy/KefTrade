from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from hashlib import sha256
import math
from statistics import median
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.services.research_architecture import (
    SAFETY_STATEMENT as ARCHITECTURE_SAFETY_STATEMENT,
    ensure_research_architecture_tables,
    jsonable,
    stable_hash,
    validation_thresholds,
    verify_dataset_snapshot,
)
from app.services.research_campaigns import (
    DEFAULT_SCHEDULING_CONFIG,
    ensure_campaign_tables,
    queue_campaign_jobs,
    strategy_family_for_candidate,
    update_campaign_counts,
)
from app.services.research_learning import ensure_research_learning_tables
from app.services.strategy_discovery import (
    DiscoveryCandidate,
    candidate_execution_key,
    canonical_candidate_key,
)
from app.services.strategy_research import finite_metric, profit_factor_passes


PHASE_4_VERSION = "multi_generation_evolution_v1"
DEFAULT_MAX_PARENTS = 3
DEFAULT_CHILDREN_PER_PARENT = 4
MAX_PARENT_SHARE = 0.40
MIN_PARENT_TRADES = 30


def create_multi_generation_evolution_campaign(
    conn: psycopg.Connection,
    *,
    dataset_id: int = 1,
    validation_dataset_id: int | None = None,
    max_parents: int = DEFAULT_MAX_PARENTS,
    children_per_parent: int = DEFAULT_CHILDREN_PER_PARENT,
    name: str | None = None,
) -> dict[str, Any]:
    """Create a bounded Phase 4 development campaign from eligible specialists.

    Independent validation is enforced by classification: if validation_dataset_id
    is absent or equal to the development dataset, no descendant can be called an
    independently confirmed improvement.
    """

    ensure_campaign_tables(conn)
    ensure_research_architecture_tables(conn)
    ensure_research_learning_tables(conn)
    development_integrity = verify_dataset_snapshot(conn, dataset_id)
    if not development_integrity["passed"]:
        raise ValueError(f"development dataset {dataset_id} failed integrity verification")
    validation_integrity = None
    independent_validation_available = False
    if validation_dataset_id is not None:
        validation_integrity = verify_dataset_snapshot(conn, validation_dataset_id)
        independent_validation_available = bool(validation_integrity["passed"]) and validation_dataset_id != dataset_id

    parents = eligible_evolution_parents(conn, dataset_id=dataset_id)
    selected = select_diverse_parents(parents, max_parents=max_parents)
    if not selected:
        raise ValueError("Phase 4 requires at least one promoted asset specialist with complete dataset, hypothesis, and gate evidence")
    blueprint = build_evolution_blueprint(
        selected,
        dataset_id=dataset_id,
        validation_dataset_id=validation_dataset_id,
        independent_validation_available=independent_validation_available,
        children_per_parent=children_per_parent,
    )
    campaign_key = "phase4_" + stable_hash(
        {
            "version": PHASE_4_VERSION,
            "dataset_id": dataset_id,
            "validation_dataset_id": validation_dataset_id,
            "lineage": [(row["parent_candidate_id"], row["mutated_parameter"], row["new_value"]) for row in blueprint["lineage"]],
        }
    )[:24]
    row = conn.execute(
        """
        INSERT INTO research_campaigns(
            campaign_key, name, universe_key, status, requested_candidates, controls, scheduling_config,
            safety_statement, dataset_id, dataset_mode, generator_version, threshold_version,
            experiment_generation, immutable_config, simulation_only
        ) VALUES (%s, %s, %s, 'queued', %s, %s, %s, %s, %s, 'reproducibility', %s, %s, %s, %s, TRUE)
        ON CONFLICT(campaign_key) DO UPDATE SET updated_at = NOW()
        RETURNING *
        """,
        (
            campaign_key,
            name or "Phase 4 Multi-Generation Evolution development campaign",
            "phase4_evolution",
            len(blueprint["children"]),
            Jsonb(jsonable(blueprint["controls"])),
            Jsonb({**DEFAULT_SCHEDULING_CONFIG, "batch_size": 1, "daily_experiment_budget": len(blueprint["children"]), "max_generated_candidates": len(blueprint["children"])}),
            ARCHITECTURE_SAFETY_STATEMENT,
            dataset_id,
            PHASE_4_VERSION,
            "strong_research_gates:v1",
            max((child.generation for child in blueprint["children"]), default=1),
            Jsonb(jsonable({"phase4": blueprint["manifest"], "scope": blueprint["targeting"], "validation_policy": validation_thresholds()})),
        ),
    ).fetchone()
    campaign_id = int(row["id"])
    created = queue_campaign_jobs(conn, campaign_id, blueprint["children"], blueprint["targeting"]["assets"], blueprint["targeting"]["timeframes"])
    for child in blueprint["children"]:
        parent = blueprint["parent_by_child"][child.candidate_id]
        conn.execute(
            """
            UPDATE research_campaign_jobs
            SET dataset_id = %s,
                hypothesis_version_id = %s,
                parent_candidate_id = %s,
                generation_channel = %s
            WHERE campaign_id = %s AND candidate_id = %s
            """,
            (
                dataset_id,
                child.parameters.get("phase4_hypothesis_version_id"),
                parent["candidate_id"],
                child.parameters.get("phase4_mutation_channel"),
                campaign_id,
                child.candidate_id,
            ),
        )
    persist_evolution_history(conn, campaign_id, blueprint)
    update_campaign_counts(conn, campaign_id)
    conn.commit()
    return {
        "campaign": jsonable(dict(row)),
        "jobs_created": created,
        "parents": blueprint["parents"],
        "lineage": blueprint["lineage"],
        "diversity": blueprint["diversity"],
        "targeting": blueprint["targeting"],
        "controls": blueprint["controls"],
        "development_dataset_integrity": development_integrity,
        "validation_dataset_integrity": validation_integrity,
        "independent_validation_available": independent_validation_available,
        "classification_policy": "Promising descendant - unconfirmed" if not independent_validation_available else "Independent validation required before improvement classification",
        "phase4_version": PHASE_4_VERSION,
        "simulation_only": True,
    }


def eligible_evolution_parents(conn: psycopg.Connection, *, dataset_id: int) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT DISTINCT ON (j.candidate_id)
                   j.*, c.dataset_id AS campaign_dataset_id, c.universe_key, c.immutable_config,
                   h.hypothesis_key, h.status AS hypothesis_status
            FROM research_campaign_jobs j
            JOIN research_campaigns c ON c.id = j.campaign_id
            LEFT JOIN research_hypothesis_versions h ON h.id = j.hypothesis_version_id
            WHERE j.status = 'promoted'
              AND j.dataset_id = %s
              AND j.hypothesis_version_id IS NOT NULL
              AND j.candidate IS NOT NULL
              AND j.simulation_only = TRUE
              AND EXISTS (
                  SELECT 1
                  FROM research_candidate_stage_evidence s
                  WHERE s.candidate_id = j.candidate_id
                    AND s.campaign_id = j.campaign_id
                    AND s.candidate_level = 'asset_specialist'
                    AND s.promoted = TRUE
                    AND s.simulation_only = TRUE
              )
            ORDER BY j.candidate_id, j.validation_score DESC, j.completed_at DESC NULLS LAST
            """,
            (dataset_id,),
        ).fetchall()
    ]
    eligible = []
    for row in rows:
        assessment = parent_eligibility(row)
        if assessment["eligible"]:
            eligible.append({**row, "eligibility": assessment})
    return sorted(
        eligible,
        key=lambda row: (
            finite_metric(row.get("validation_score")),
            finite_metric(((row.get("result") or {}).get("metrics") or {}).get("profit_factor")),
            finite_metric(((row.get("result") or {}).get("metrics") or {}).get("expectancy_per_trade")),
            str(row.get("candidate_id")),
        ),
        reverse=True,
    )


def parent_eligibility(row: dict[str, Any]) -> dict[str, Any]:
    metrics = dict((row.get("result") or {}).get("metrics") or {})
    paper = dict((row.get("result") or {}).get("paper_readiness") or {})
    checks = {
        "promoted_asset_specialist": row.get("status") == "promoted",
        "complete_dataset_lineage": row.get("dataset_id") is not None,
        "complete_hypothesis_lineage": row.get("hypothesis_version_id") is not None,
        "candidate_payload_present": bool(row.get("candidate")),
        "minimum_trade_count": finite_metric(metrics.get("number_of_trades")) >= MIN_PARENT_TRADES,
        "profit_factor_gate": profit_factor_passes(metrics, 1.2),
        "positive_expectancy": finite_metric(metrics.get("expectancy_per_trade")) > 0,
        "drawdown_gate": finite_metric(metrics.get("max_drawdown")) <= 0.12,
        "walk_forward_evidence": bool((metrics.get("walk_forward") or {}).get("enabled")),
        "paper_readiness": bool(paper.get("paper_ready")),
        "no_operational_failure": not row.get("latest_error") and row.get("failure_classification") is None,
    }
    return {
        "eligible": all(checks.values()),
        "checks": checks,
        "metrics": {
            "profit_factor": finite_metric(metrics.get("profit_factor")),
            "expectancy_per_trade": finite_metric(metrics.get("expectancy_per_trade")),
            "max_drawdown": finite_metric(metrics.get("max_drawdown")),
            "number_of_trades": finite_metric(metrics.get("number_of_trades")),
            "validation_score": finite_metric(row.get("validation_score")),
        },
    }


def select_diverse_parents(parents: list[dict[str, Any]], *, max_parents: int) -> list[dict[str, Any]]:
    selected = []
    families = Counter()
    symbols = Counter()
    for row in parents:
        family = str(row.get("strategy_family") or "")
        symbol = str(row.get("symbol") or "")
        if families[family] >= max(1, math.ceil(max_parents / 2)):
            continue
        if symbols[symbol] >= max(1, math.ceil(max_parents / 2)):
            continue
        selected.append(row)
        families[family] += 1
        symbols[symbol] += 1
        if len(selected) >= max_parents:
            break
    if len(selected) < max_parents:
        selected_ids = {row["candidate_id"] for row in selected}
        for row in parents:
            if row["candidate_id"] in selected_ids:
                continue
            selected.append(row)
            selected_ids.add(row["candidate_id"])
            if len(selected) >= max_parents:
                break
    return selected


def build_evolution_blueprint(
    parents: list[dict[str, Any]],
    *,
    dataset_id: int,
    validation_dataset_id: int | None,
    independent_validation_available: bool,
    children_per_parent: int,
) -> dict[str, Any]:
    children: list[DiscoveryCandidate] = []
    lineage = []
    seen_exec: set[str] = set()
    parent_by_child: dict[str, dict[str, Any]] = {}
    for parent_row in parents:
        parent = discovery_from_payload(parent_row["candidate"])
        parent_exec = candidate_execution_key(parent)
        seen_exec.add(parent_exec)
        for mutation in parent_mutations(parent, parent_row)[:children_per_parent]:
            child = phase4_child(parent, parent_row, mutation, dataset_id=dataset_id, validation_dataset_id=validation_dataset_id)
            execution_key = candidate_execution_key(child)
            if execution_key in seen_exec:
                continue
            if parent_share_would_exceed(children, child, len(parents), children_per_parent):
                continue
            seen_exec.add(execution_key)
            children.append(child)
            parent_by_child[child.candidate_id] = parent_row
            lineage.append(lineage_row(child, parent, parent_row, mutation, execution_key))
    parent_symbols = sorted({str(row["symbol"]) for row in parents})
    timeframes = sorted({str(row["timeframe"]) for row in parents})
    diversity = diversity_report(children, lineage)
    controls = {
        "phase4_version": PHASE_4_VERSION,
        "parent_eligibility": "promoted asset specialists only; no near-pass parents",
        "max_children_per_parent": children_per_parent,
        "max_parent_share": MAX_PARENT_SHARE,
        "execution_key_deduplication": True,
        "lineage_immutable": True,
        "validation_thresholds_changed": False,
        "independent_validation_available": independent_validation_available,
        "independent_validation_required_for_improvement": True,
        "classification_without_independent_validation": "Promising descendant - unconfirmed",
    }
    manifest = {
        "development_dataset_id": dataset_id,
        "validation_dataset_id": validation_dataset_id,
        "parent_candidate_ids": [row["candidate_id"] for row in parents],
        "child_candidate_ids": [child.candidate_id for child in children],
        "phase4_version": PHASE_4_VERSION,
    }
    return {
        "parents": [parent_summary(row) for row in parents],
        "children": children,
        "parent_by_child": parent_by_child,
        "lineage": lineage,
        "diversity": diversity,
        "targeting": {"type": "asset", "ref": "phase4_evolution", "assets": parent_symbols, "timeframes": timeframes},
        "controls": controls,
        "manifest": manifest,
    }


def parent_mutations(parent: DiscoveryCandidate, parent_row: dict[str, Any]) -> list[dict[str, Any]]:
    params = parent.parameters
    candidates: list[dict[str, Any]] = []
    grids = {
        "risk_reward": local_values(params.get("risk_reward"), [1.2, 1.4, 1.5, 1.6, 1.8, 2.0, 2.2, 2.5]),
        "atr_multiplier": local_values(params.get("atr_multiplier"), [1.25, 1.4, 1.5, 1.75, 2.0, 2.25, 2.5]),
        "max_holding_bars": local_values(params.get("max_holding_bars"), [8, 12, 18, 24, 30]),
        "volume_change_min": local_values(params.get("volume_change_min"), [-0.1, 0.0, 0.05, 0.1, 0.15, 0.2]),
        "rsi_min": local_values(params.get("rsi_min"), [45, 50, 53, 55, 57, 60]),
        "returns_5_min": local_values(params.get("returns_5_min"), [0.005, 0.008, 0.01, 0.012, 0.015]),
        "entry_distance_to_ema20_max": local_values(params.get("entry_distance_to_ema20_max"), [0.025, 0.03, 0.035, 0.04, 0.045, 0.05]),
    }
    for parameter, values in grids.items():
        for value in values[:2]:
            current = params.get(parameter)
            candidates.append(
                {
                    "parameter": parameter,
                    "old_value": current,
                    "new_value": value,
                    "channel": "nearby",
                    "expected_improvement": expected_improvement(parameter),
                    "falsification_criterion": falsification_criterion(parameter),
                }
            )
    candidates.append(
        {
            "parameter": "entry_cooldown_bars",
            "old_value": params.get("entry_cooldown_bars"),
            "new_value": 3,
            "channel": "exploration",
            "expected_improvement": "Reduce clustered low-quality repeated entries without changing validation thresholds.",
            "falsification_criterion": "Reject if trade count falls below the gate or expectancy does not improve.",
        }
    )
    return sorted(candidates, key=lambda row: (row["channel"] != "nearby", row["parameter"], str(row["new_value"])))


def phase4_child(
    parent: DiscoveryCandidate,
    parent_row: dict[str, Any],
    mutation: dict[str, Any],
    *,
    dataset_id: int,
    validation_dataset_id: int | None,
) -> DiscoveryCandidate:
    root_ancestor_id = parent.parameters.get("phase4_root_ancestor_id") or parent.parent_candidate_id or parent.candidate_id
    params = {
        **parent.parameters,
        mutation["parameter"]: mutation["new_value"],
        "phase4_version": PHASE_4_VERSION,
        "phase4_parent_candidate_id": parent.candidate_id,
        "phase4_root_ancestor_id": root_ancestor_id,
        "phase4_generation": int(parent.generation) + 1,
        "phase4_mutation_channel": mutation["channel"],
        "phase4_mutated_parameter": mutation["parameter"],
        "phase4_old_value": mutation["old_value"],
        "phase4_new_value": mutation["new_value"],
        "phase4_hypothesis_version_id": parent_row.get("hypothesis_version_id"),
        "phase4_dataset_id": dataset_id,
        "phase4_validation_dataset_id": validation_dataset_id,
        "phase4_expected_improvement": mutation["expected_improvement"],
        "phase4_falsification_criterion": mutation["falsification_criterion"],
        "phase4_independent_validation_required": True,
    }
    canonical = canonical_candidate_key(parent.blocks, params, parent.candidate_id)
    return DiscoveryCandidate(
        candidate_id=f"sd_{sha256(canonical.encode()).hexdigest()[:14]}",
        family_id=parent.family_id,
        parent_candidate_id=parent.candidate_id,
        generation=parent.generation + 1,
        blocks=dict(parent.blocks),
        parameters=params,
        complexity=parent.complexity + 1,
        canonical_key=canonical,
    )


def lineage_row(
    child: DiscoveryCandidate,
    parent: DiscoveryCandidate,
    parent_row: dict[str, Any],
    mutation: dict[str, Any],
    execution_key: str,
) -> dict[str, Any]:
    return {
        "candidate_id": child.candidate_id,
        "parent_candidate_id": parent.candidate_id,
        "root_ancestor_id": child.parameters["phase4_root_ancestor_id"],
        "generation": child.generation,
        "mutation_channel": mutation["channel"],
        "mutated_parameter": mutation["parameter"],
        "old_value": mutation["old_value"],
        "new_value": mutation["new_value"],
        "hypothesis_id": parent_row.get("hypothesis_version_id"),
        "dataset_id": parent_row.get("dataset_id"),
        "strategy_family": strategy_family_for_candidate(child),
        "expected_improvement": mutation["expected_improvement"],
        "falsification_criterion": mutation["falsification_criterion"],
        "parent_evidence_ref": f"research_campaign_job:{parent_row.get('id')}",
        "execution_key_hash": stable_hash(execution_key)[:24],
        "classification": "Promising descendant - unconfirmed",
    }


def persist_evolution_history(conn: psycopg.Connection, campaign_id: int, blueprint: dict[str, Any]) -> None:
    for row in blueprint["lineage"]:
        conn.execute(
            """
            INSERT INTO research_evolution_history(
                candidate_id, parent_candidate_id, campaign_id, mutation, reason,
                supporting_evidence, expected_improvement, confidence_score, calculation_version, simulation_only
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            """,
            (
                row["candidate_id"],
                row["parent_candidate_id"],
                campaign_id,
                Jsonb(jsonable(row)),
                "Phase 4 controlled parent-child mutation; independent validation required before any improvement claim.",
                Jsonb([row["parent_evidence_ref"], f"research_hypothesis:{row['hypothesis_id']}"]),
                row["expected_improvement"],
                0.0,
                PHASE_4_VERSION,
            ),
        )


def analyze_evolution_campaign(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    campaign = conn.execute("SELECT * FROM research_campaigns WHERE id = %s", (campaign_id,)).fetchone()
    if not campaign:
        raise ValueError(f"campaign {campaign_id} was not found")
    jobs = [dict(row) for row in conn.execute("SELECT * FROM research_campaign_jobs WHERE campaign_id = %s ORDER BY id", (campaign_id,)).fetchall()]
    lineage_rows = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM research_evolution_history WHERE campaign_id = %s AND calculation_version = %s ORDER BY id",
            (campaign_id, PHASE_4_VERSION),
        ).fetchall()
    ]
    parent_ids = sorted({str(row.get("parent_candidate_id")) for row in jobs if row.get("parent_candidate_id")})
    parent_jobs = []
    if parent_ids:
        parent_jobs = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM research_campaign_jobs
                WHERE candidate_id = ANY(%s::text[])
                  AND status = 'promoted'
                  AND simulation_only = TRUE
                ORDER BY candidate_id, completed_at DESC NULLS LAST
                """,
                (parent_ids,),
            ).fetchall()
        ]
    comparisons = compare_parent_child(parent_jobs, jobs)
    independent = bool(((campaign.get("immutable_config") or {}).get("phase4") or {}).get("validation_dataset_id")) and (
        ((campaign.get("immutable_config") or {}).get("phase4") or {}).get("validation_dataset_id") != campaign.get("dataset_id")
    )
    return {
        "campaign": jsonable(dict(campaign)),
        "jobs": [jsonable(row) for row in jobs],
        "lineage": [jsonable(row) for row in lineage_rows],
        "parent_eligibility": summarize_parent_eligibility(parent_jobs),
        "parent_child_comparison": comparisons,
        "diversity": diversity_from_jobs(jobs, lineage_rows),
        "independent_validation_available": independent,
        "confirmed_improvements": [] if not independent else [row for row in comparisons if row.get("independently_confirmed")],
        "classification_policy": "Promising descendant - unconfirmed" if not independent else "Independent holdout required for confirmed improvement.",
        "compute": compute_summary(jobs),
        "simulation_only": True,
    }


def summarize_parent_eligibility(parent_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries = []
    seen = set()
    for row in parent_jobs:
        candidate_id = str(row.get("candidate_id"))
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        candidate = dict(row.get("candidate") or {})
        summaries.append(
            {
                "candidate_id": candidate_id,
                "campaign_id": row.get("campaign_id"),
                "symbol": row.get("symbol"),
                "timeframe": row.get("timeframe"),
                "dataset_id": row.get("dataset_id"),
                "hypothesis_version_id": row.get("hypothesis_version_id"),
                "strategy_family": strategy_family_for_candidate(candidate),
                "assessment": parent_eligibility(row),
            }
        )
    return sorted(summaries, key=lambda item: str(item.get("candidate_id")))


def compare_parent_child(parent_jobs: list[dict[str, Any]], child_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parent_by_id = {str(row["candidate_id"]): row for row in parent_jobs}
    rows = []
    for child in child_jobs:
        parent_id = str(child.get("parent_candidate_id") or "")
        parent = parent_by_id.get(parent_id)
        child_metrics = dict((child.get("result") or {}).get("metrics") or {})
        parent_metrics = dict((parent.get("result") or {}).get("metrics") or {}) if parent else {}
        rows.append(
            {
                "child_candidate_id": child.get("candidate_id"),
                "parent_candidate_id": parent_id,
                "status": child.get("status"),
                "same_dataset_development_only": True,
                "classification": "Promising descendant - unconfirmed",
                "profit_factor_delta": metric_delta(child_metrics, parent_metrics, "profit_factor"),
                "expectancy_delta": metric_delta(child_metrics, parent_metrics, "expectancy_per_trade"),
                "drawdown_delta": metric_delta(child_metrics, parent_metrics, "max_drawdown"),
                "trade_count_delta": metric_delta(child_metrics, parent_metrics, "number_of_trades"),
                "reason": "No independent frozen validation dataset is available, so same-dataset differences cannot confirm improvement.",
            }
        )
    return rows


def diversity_report(children: list[DiscoveryCandidate], lineage: list[dict[str, Any]]) -> dict[str, Any]:
    parent_counts = Counter(row["parent_candidate_id"] for row in lineage)
    family_counts = Counter(row["strategy_family"] for row in lineage)
    mutation_counts = Counter(row["mutated_parameter"] for row in lineage)
    execution_keys = {candidate_execution_key(child) for child in children}
    return {
        "children": len(children),
        "unique_execution_keys": len(execution_keys),
        "parent_concentration": concentration(parent_counts),
        "family_mix": dict(family_counts),
        "mutation_parameter_mix": dict(mutation_counts),
        "parameter_entropy": entropy(mutation_counts),
        "lineage_entropy": entropy(parent_counts),
        "duplicate_execution_keys": len(children) - len(execution_keys),
        "diversity_collapsed": len(children) > 0 and (concentration(parent_counts) > MAX_PARENT_SHARE or len(execution_keys) < len(children)),
    }


def diversity_from_jobs(jobs: list[dict[str, Any]], lineage_rows: list[dict[str, Any]]) -> dict[str, Any]:
    lineage = [dict(row.get("mutation") or {}) for row in lineage_rows]
    by_candidate: dict[str, DiscoveryCandidate] = {}
    for row in jobs:
        if row.get("candidate"):
            by_candidate.setdefault(str(row["candidate_id"]), discovery_from_payload(row["candidate"]))
    return diversity_report(list(by_candidate.values()), lineage)


def compute_summary(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    runtimes = [finite_metric(row.get("execution_runtime_ms")) for row in jobs if row.get("execution_runtime_ms") is not None]
    statuses = Counter(str(row.get("status")) for row in jobs)
    return {
        "jobs": len(jobs),
        "status_counts": dict(statuses),
        "runtime_ms": int(sum(runtimes)) if runtimes else 0,
        "median_runtime_ms": median(runtimes) if runtimes else None,
        "operational_failures": int(statuses.get("failed", 0) + statuses.get("blocked_data", 0)),
    }


def parent_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": row.get("candidate_id"),
        "campaign_id": row.get("campaign_id"),
        "job_id": row.get("id"),
        "symbol": row.get("symbol"),
        "timeframe": row.get("timeframe"),
        "dataset_id": row.get("dataset_id"),
        "hypothesis_version_id": row.get("hypothesis_version_id"),
        "strategy_family": row.get("strategy_family"),
        "eligibility": row.get("eligibility"),
    }


def discovery_from_payload(payload: dict[str, Any]) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        candidate_id=str(payload["candidate_id"]),
        family_id=str(payload.get("family_id") or ""),
        parent_candidate_id=payload.get("parent_candidate_id"),
        generation=int(payload.get("generation") or 1),
        blocks=dict(payload.get("blocks") or {}),
        parameters=dict(payload.get("parameters") or {}),
        complexity=int(payload.get("complexity") or 1),
        canonical_key=str(payload.get("canonical_key") or canonical_candidate_key(dict(payload.get("blocks") or {}), dict(payload.get("parameters") or {}), payload.get("parent_candidate_id"))),
    )


def parent_share_would_exceed(children: list[DiscoveryCandidate], child: DiscoveryCandidate, parent_count: int, children_per_parent: int) -> bool:
    expected_total = max(1, parent_count * children_per_parent)
    counts = Counter(row.parent_candidate_id for row in children)
    counts[child.parent_candidate_id] += 1
    return counts[child.parent_candidate_id] / expected_total > MAX_PARENT_SHARE


def local_values(current: Any, grid: list[float | int]) -> list[float | int]:
    if current is None:
        return []
    current_value = finite_metric(current)
    return [value for value in sorted(grid, key=lambda value: (abs(float(value) - current_value), float(value))) if finite_metric(value) != current_value]


def expected_improvement(parameter: str) -> str:
    return {
        "risk_reward": "Test whether nearby payoff geometry improves PF without reducing trade count below the gate.",
        "atr_multiplier": "Test whether nearby stop width improves drawdown without destroying expectancy.",
        "max_holding_bars": "Test whether holding duration improves realized continuation without increasing drawdown.",
        "volume_change_min": "Test whether participation filtering improves quality without starving trades.",
        "rsi_min": "Test whether momentum threshold changes improve entry quality without overfitting.",
        "returns_5_min": "Test whether return trigger strength improves continuation quality.",
        "entry_distance_to_ema20_max": "Test whether pullback depth tolerance improves sample size without losing edge.",
    }.get(parameter, "Test a bounded executable mutation around a promoted parent.")


def falsification_criterion(parameter: str) -> str:
    return {
        "risk_reward": "Falsified if PF, expectancy, drawdown, or trade count deteriorates versus the parent on independent evidence.",
        "atr_multiplier": "Falsified if drawdown or expectancy deteriorates versus the parent on independent evidence.",
        "max_holding_bars": "Falsified if holding change reduces PF or positive expectancy on independent evidence.",
        "volume_change_min": "Falsified if volume filtering starves trades or admits weak expectancy.",
        "rsi_min": "Falsified if the threshold change reduces PF, expectancy, or stability.",
        "returns_5_min": "Falsified if continuation trigger remains sparse or economically weak.",
        "entry_distance_to_ema20_max": "Falsified if depth mutation loses positive expectancy or trade-count survival.",
    }.get(parameter, "Falsified if unchanged validation gates do not improve on independent frozen evidence.")


def metric_delta(child: dict[str, Any], parent: dict[str, Any], key: str) -> float | None:
    if key not in child or key not in parent:
        return None
    return round(finite_metric(child.get(key)) - finite_metric(parent.get(key)), 6)


def concentration(counts: Counter[Any]) -> float:
    total = sum(counts.values())
    return round(max(counts.values()) / total, 6) if total else 0.0


def entropy(counts: Counter[Any]) -> float:
    total = sum(counts.values())
    if total <= 0:
        return 0.0
    return round(-sum((count / total) * math.log2(count / total) for count in counts.values() if count), 6)


def stable_key(*parts: Any) -> str:
    return sha256("|".join(str(part) for part in parts).encode()).hexdigest()[:24]
