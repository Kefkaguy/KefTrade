from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from hashlib import sha256
import json
from statistics import median
from typing import Any, Iterable

import psycopg

from app.services.research_campaigns import CAMPAIGN_VERSION, ensure_campaign_tables
from app.services.strategy_research import finite_metric


TERMINAL_STATUSES = {"completed", "rejected", "promoted", "failed", "canceled"}
TESTED_STATUSES = {"completed", "rejected", "promoted"}
FORWARD_STATES = {"active_forward_validation", "collecting_forward_evidence"}
REQUIRED_MARKET_REGIMES = ("bull_trend", "sideways", "bear_trend")
ETF_SYMBOLS = {"SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "TLT", "GLD", "VNQ"}


def research_command_center(
    conn: psycopg.Connection,
    *,
    campaign_id: int | None = None,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    campaigns = [dict(row) for row in conn.execute(
        """
        SELECT id, campaign_key, name, universe_key, status, requested_candidates,
               created_at, started_at, completed_at, updated_at
        FROM research_campaigns
        WHERE simulation_only = TRUE
        ORDER BY created_at DESC, id DESC
        """
    ).fetchall()]
    if not campaigns:
        return empty_command_center(filters or {})

    selected_id = campaign_id or int(campaigns[0]["id"])
    campaign = next((row for row in campaigns if int(row["id"]) == selected_id), None)
    if not campaign:
        raise ValueError("research campaign not found")

    jobs = [dict(row) for row in conn.execute(
        """
        SELECT id, campaign_id, candidate_id, family_id, strategy_family, symbol, timeframe,
               status, candidate, result, validation_score, consistency_score, failure_reasons,
               attempts, failure_classification, created_at, started_at, completed_at, updated_at
        FROM research_campaign_jobs
        WHERE campaign_id = %s AND simulation_only = TRUE
        ORDER BY id ASC
        """,
        (selected_id,),
    ).fetchall()]
    elite = [dict(row) for row in conn.execute(
        """
        SELECT candidate_id, family_id, research_score, profit_factor, expectancy, max_drawdown,
               trade_count, stability, forward_validation_state, created_at
        FROM elite_research_candidates
        WHERE campaign_id = %s AND simulation_only = TRUE
        """,
        (selected_id,),
    ).fetchall()]
    deployments = [dict(row) for row in conn.execute(
        """
        SELECT id, campaign_id, candidate_id, status, lifecycle_state, forward_validation_started_at,
               created_at
        FROM strategy_deployments
        WHERE campaign_id = %s AND candidate_id IS NOT NULL AND simulation_only = TRUE
        """,
        (selected_id,),
    ).fetchall()]
    universe = conn.execute(
        "SELECT metadata FROM research_universes WHERE universe_key = %s AND simulation_only = TRUE",
        (campaign["universe_key"],),
    ).fetchone()
    historical = historical_research(conn)
    payload = analyze_campaign(
        campaign,
        jobs,
        elite=elite,
        deployments=deployments,
        filters=filters or {},
        default_asset_class=str((universe or {}).get("metadata", {}).get("asset_class") or "equity"),
    )
    payload["campaigns"] = campaigns
    payload["historical_research"] = historical
    payload["source"] = {
        "authoritative_tables": [
            "research_campaigns",
            "research_campaign_jobs",
            "elite_research_candidates",
            "strategy_deployments",
        ],
        "candidate_grain": "one candidate_id within the selected campaign",
        "validation_run_grain": "one research_campaign_jobs row per candidate, asset, and timeframe",
        "historical_tables": ["alpha_validation_runs", "strategy_experiments"],
        "refreshed_at": datetime.now(UTC),
    }
    payload["simulation_only"] = True
    return payload


def analyze_campaign(
    campaign: dict[str, Any],
    jobs: list[dict[str, Any]],
    *,
    elite: list[dict[str, Any]] | None = None,
    deployments: list[dict[str, Any]] | None = None,
    filters: dict[str, Any] | None = None,
    default_asset_class: str = "equity",
) -> dict[str, Any]:
    elite = elite or []
    deployments = deployments or []
    filters = normalized_filters(filters or {})
    prepared = [prepare_job(row, default_asset_class) for row in jobs]
    options = filter_options(prepared)
    scoped = [row for row in prepared if matches_filters(row, filters)]
    scoped_ids = {row["candidate_id"] for row in scoped}
    scoped_elite = [row for row in elite if row.get("candidate_id") in scoped_ids]
    scoped_deployments = [row for row in deployments if row.get("candidate_id") in scoped_ids]

    rejection = rejection_analysis(scoped)
    families = dimension_intelligence(scoped, "strategy_family")
    assets = dimension_intelligence(scoped, "symbol")
    timeframes = dimension_intelligence(scoped, "timeframe")
    duplicates = duplicate_analysis(scoped)
    near_pass = near_pass_candidates(scoped)
    funnel = candidate_funnel(scoped, scoped_elite, scoped_deployments)
    recommendations = research_recommendations(
        campaign, scoped, rejection, families, assets, timeframes, duplicates
    )
    proposal = next_campaign_proposal(
        campaign, scoped, families, assets, timeframes, duplicates, recommendations
    )
    return {
        "campaign": campaign,
        "filters": filters,
        "filter_options": options,
        "overview": overview_counts(scoped, scoped_elite, scoped_deployments, funnel),
        "candidate_funnel": funnel,
        "rejection_analysis": rejection,
        "near_pass_candidates": near_pass,
        "strategy_intelligence": {
            "rows": families,
            "highlights": dimension_highlights(families, include_inactive=True),
        },
        "asset_intelligence": {
            "rows": assets,
            "highlights": dimension_highlights(assets),
        },
        "timeframe_intelligence": {
            "rows": timeframes,
            "highlights": dimension_highlights(timeframes),
        },
        "regime_analysis": regime_analysis(scoped),
        "duplicate_analysis": duplicates,
        "experiment_history": grouped_experiment_history(scoped),
        "recommendations": recommendations,
        "next_campaign_proposal": proposal,
        "terminology": terminology(),
        "reconciliation": funnel_reconciliation(scoped, funnel),
        "metric_definitions": {
            "candidate_quality_score": "0-100 diagnostic score combining stored pass rate, profit factor, expectancy, trade count, and drawdown. It is not a promotion gate.",
            "near_pass": "At most two failed stored gates and mean normalized threshold distance no greater than 25%.",
            "exact_duplicate": "Distinct candidate IDs with the same stored canonical strategy definition.",
            "near_duplicate": "Distinct canonical candidates with the same strategy block structure and differing parameter values.",
        },
    }


def prepare_job(row: dict[str, Any], default_asset_class: str) -> dict[str, Any]:
    item = dict(row)
    candidate = dict(item.get("candidate") or {})
    result = dict(item.get("result") or {})
    item["candidate"] = candidate
    item["result"] = result
    item["metrics"] = dict(result.get("metrics") or {})
    item["parameters"] = dict(result.get("parameters") or candidate.get("parameters") or {})
    item["blocks"] = dict(result.get("blocks") or candidate.get("blocks") or {})
    item["strategy_family"] = item.get("strategy_family") or infer_family(item["blocks"])
    item["asset_class"] = asset_class(str(item.get("symbol") or ""), default_asset_class)
    item["failed_gates"] = failed_gates(item)
    item["regimes"] = sorted({
        str(regime.get("regime"))
        for bucket in ("by_market_regime", "by_volatility_regime")
        for regime in ((result.get("regime_analysis") or {}).get(bucket) or [])
        if regime.get("regime")
    })
    return item


def normalized_filters(filters: dict[str, Any]) -> dict[str, str]:
    return {
        key: str(filters.get(key) or "").strip()
        for key in ("asset", "asset_class", "timeframe", "strategy_family", "candidate_state", "validation_rule", "regime", "date_from", "date_to")
    }


def matches_filters(row: dict[str, Any], filters: dict[str, str]) -> bool:
    direct = {
        "asset": str(row.get("symbol") or ""),
        "asset_class": str(row.get("asset_class") or ""),
        "timeframe": str(row.get("timeframe") or ""),
        "strategy_family": str(row.get("strategy_family") or ""),
        "candidate_state": candidate_run_state(row),
    }
    if any(filters[key] and filters[key] != value for key, value in direct.items()):
        return False
    if filters["validation_rule"] and filters["validation_rule"] not in row["failed_gates"]:
        return False
    if filters["regime"] and filters["regime"] not in row["regimes"]:
        return False
    created = str(row.get("created_at") or "")[:10]
    if filters["date_from"] and created < filters["date_from"]:
        return False
    if filters["date_to"] and created > filters["date_to"]:
        return False
    return True


def filter_options(jobs: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        "assets": unique(jobs, "symbol"),
        "asset_classes": unique(jobs, "asset_class"),
        "timeframes": unique(jobs, "timeframe"),
        "strategy_families": unique(jobs, "strategy_family"),
        "candidate_states": sorted({candidate_run_state(row) for row in jobs}),
        "validation_rules": sorted({gate for row in jobs for gate in row["failed_gates"]}),
        "regimes": sorted({regime for row in jobs for regime in row["regimes"]}),
    }


def overview_counts(
    jobs: list[dict[str, Any]],
    elite: list[dict[str, Any]],
    deployments: list[dict[str, Any]],
    funnel: list[dict[str, Any]],
) -> dict[str, int]:
    stages = {row["key"]: int(row["count"]) for row in funnel}
    return {
        "campaign_jobs": len(jobs),
        "candidates_generated": stages.get("generated", 0),
        "candidates_tested": stages.get("tested", 0),
        "candidates_rejected": stages.get("rejected", 0),
        "candidates_completed": completed_candidate_count(jobs),
        "needs_more_evidence": stages.get("needs_more_evidence", 0),
        "research_candidates": stages.get("research_candidate", 0),
        "elite_candidates": len({row.get("candidate_id") for row in elite}),
        "candidate_linked_deployments": len(deployments),
    }


def candidate_funnel(
    jobs: list[dict[str, Any]],
    elite: list[dict[str, Any]],
    deployments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped = group_candidates(jobs)
    generated = len(grouped)
    tested = sum(any(row["status"] in TESTED_STATUSES for row in rows) for rows in grouped.values())
    completed = {candidate_id for candidate_id, rows in grouped.items() if rows and all(row["status"] in TERMINAL_STATUSES for row in rows)}
    rejected = sum(candidate_id in completed and not any(row["status"] == "promoted" for row in rows) for candidate_id, rows in grouped.items())
    needs_more = sum(
        any(row["status"] in TESTED_STATUSES for row in rows) and candidate_id not in completed
        for candidate_id, rows in grouped.items()
    )
    research = sum(any(row["status"] == "promoted" for row in rows) for rows in grouped.values())
    elite_ids = {str(row.get("candidate_id")) for row in elite}
    deployed_ids = {str(row.get("candidate_id")) for row in deployments}
    forward_ids = {
        str(row.get("candidate_id"))
        for row in deployments
        if str(row.get("lifecycle_state") or "") in FORWARD_STATES
        and str(row.get("status") or "") == "active"
    }
    stages = [
        ("generated", "Generated", generated),
        ("tested", "Tested", tested),
        ("rejected", "Rejected", rejected),
        ("needs_more_evidence", "Needs More Evidence", needs_more),
        ("research_candidate", "Research Candidate", research),
        ("elite_candidate", "Elite Candidate", len(elite_ids)),
        ("paper_deployed", "Paper Deployed", len(deployed_ids)),
        ("forward_active", "Forward Active", len(forward_ids)),
    ]
    result = []
    counts = {key: count for key, _label, count in stages}
    conversion_basis = {
        "tested": "generated",
        "rejected": "tested",
        "needs_more_evidence": "tested",
        "research_candidate": "tested",
        "elite_candidate": "research_candidate",
        "paper_deployed": "elite_candidate",
        "forward_active": "paper_deployed",
    }
    for key, label, count in stages:
        basis = conversion_basis.get(key)
        denominator = counts.get(basis, 0) if basis else 0
        result.append({
            "key": key,
            "label": label,
            "count": int(count),
            "conversion_from_previous": round(count / denominator, 4) if denominator else None,
            "conversion_basis": basis,
            "rate_from_generated": round(count / generated, 4) if generated else 0,
        })
    return result


def rejection_analysis(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    rejected = [row for row in jobs if row["status"] == "rejected"]
    gate_counter = Counter(gate for row in rejected for gate in row["failed_gates"])
    rejected_candidates = len({row["candidate_id"] for row in rejected})
    validation_rules = []
    for gate, count in gate_counter.most_common():
        candidate_count = len({row["candidate_id"] for row in rejected if gate in row["failed_gates"]})
        validation_rules.append({
            "name": gate,
            "count": count,
            "rate": ratio(count, len(rejected)),
            "candidate_count": candidate_count,
            "candidate_rate": ratio(candidate_count, rejected_candidates),
        })
    denominator = len(rejected)
    return {
        "rejected_validation_runs": denominator,
        "rejected_candidates_observed": rejected_candidates,
        "validation_rules": validation_rules,
        "strategy_families": rejection_dimension(rejected, "strategy_family"),
        "assets": rejection_dimension(rejected, "symbol"),
        "timeframes": rejection_dimension(rejected, "timeframe"),
        "market_regimes": rejection_regimes(rejected),
        "parameter_ranges": parameter_failure_regions(rejected),
        "metric_ranges": metric_failure_ranges(rejected),
        "dominant_reasons": validation_rules[:5],
    }


def failed_gates(row: dict[str, Any]) -> list[str]:
    result = row.get("result") or {}
    readiness = result.get("paper_readiness") or {}
    gates = [canonical_gate(str(check.get("name") or "")) for check in readiness.get("checks") or [] if not check.get("passed")]
    for reason in list(row.get("failure_reasons") or result.get("failure_reasons") or []):
        canonical = canonical_gate(str(reason))
        if canonical:
            gates.append(canonical)
    return sorted(set(filter(None, gates)))


def canonical_gate(value: str) -> str:
    text = value.lower().replace("-", "_").replace(" ", "_")
    if "trade" in text and ("count" in text or "insufficient" in text):
        return "minimum_trade_count"
    if "profit_factor" in text or "profitfactor" in text:
        return "profit_factor"
    if "expectancy" in text:
        return "positive_expectancy"
    if "drawdown" in text:
        return "maximum_drawdown"
    if "confidence" in text or "bootstrap" in text:
        return "confidence_interval"
    if "stability" in text or "regime" in text:
        return "stability"
    if "walk_forward" in text or "out_of_sample" in text or "oos" in text:
        return "walk_forward_oos"
    return text if text in {"validation_error", "strategy_error", "data_unavailable", "stale_data"} else ""


def near_pass_candidates(jobs: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    candidates = []
    for candidate_id, rows in group_candidates(jobs).items():
        evaluated = [near_pass_run(row) for row in rows if row["status"] in TESTED_STATUSES]
        evaluated = [row for row in evaluated if row]
        if not evaluated:
            continue
        best = min(evaluated, key=lambda row: (row["mean_distance"], len(row["failed_gates"]), -row["validation_score"]))
        if len(best["failed_gates"]) > 2 or best["mean_distance"] > 0.25:
            continue
        best["candidate_id"] = candidate_id
        best["validation_runs"] = len(evaluated)
        candidates.append(best)
    return sorted(candidates, key=lambda row: (row["mean_distance"], len(row["failed_gates"]), -row["validation_score"]))[:limit]


def near_pass_run(row: dict[str, Any]) -> dict[str, Any] | None:
    result = row["result"]
    checks = list((result.get("paper_readiness") or {}).get("checks") or [])
    thresholds = dict((result.get("paper_readiness") or {}).get("thresholds") or {})
    if not checks:
        return None
    metrics = row["metrics"]
    gate_rows = []
    for check in checks:
        name = canonical_gate(str(check.get("name") or ""))
        actual, threshold, comparator = gate_values(name, metrics, thresholds, result)
        distance = gate_distance(name, actual, threshold, bool(check.get("passed")))
        gate_rows.append({
            "name": name,
            "passed": bool(check.get("passed")),
            "actual": actual,
            "threshold": threshold,
            "comparator": comparator,
            "distance": distance,
        })
    failed = [gate for gate in gate_rows if not gate["passed"]]
    if not failed:
        return None
    strongest = max(gate_rows, key=gate_strength)
    weakest = max(failed, key=lambda gate: gate["distance"])
    mean_distance = sum(gate["distance"] for gate in failed) / len(failed)
    justified = mean_distance <= 0.15 and len(failed) <= 2
    return {
        "asset": row["symbol"],
        "asset_class": row["asset_class"],
        "timeframe": row["timeframe"],
        "strategy_family": row["strategy_family"],
        "failed_gates": failed,
        "gate_evidence": gate_rows,
        "strongest_metric": strongest,
        "weakest_metric": weakest,
        "mean_distance": round(mean_distance, 4),
        "validation_score": round(finite_metric(row.get("validation_score")), 4),
        "recommendation": "Run a bounded follow-up test" if justified else "Do not allocate additional testing yet",
        "further_testing_justified": justified,
        "evidence_label": "near-pass evidence" if justified else "close on thresholds; evidence remains insufficient",
    }


def gate_values(name: str, metrics: dict[str, Any], thresholds: dict[str, Any], result: dict[str, Any]) -> tuple[Any, Any, str]:
    mapping = {
        "profit_factor": (metrics.get("profit_factor"), thresholds.get("profit_factor"), ">="),
        "positive_expectancy": (metrics.get("expectancy_per_trade"), thresholds.get("expectancy_per_trade", 0), ">"),
        "maximum_drawdown": (metrics.get("max_drawdown"), thresholds.get("max_drawdown"), "<="),
        "minimum_trade_count": (metrics.get("number_of_trades"), thresholds.get("number_of_trades"), ">="),
        "walk_forward_oos": (bool(result.get("walk_forward_metrics", {}).get("enabled")), True, "="),
        "stability": (None, "stored regime stability rule", "pass"),
        "confidence_interval": (None, "stored confidence interval rule", "pass"),
    }
    return mapping.get(name, (None, None, "pass"))


def gate_distance(name: str, actual: Any, threshold: Any, passed: bool) -> float:
    if passed:
        return 0.0
    if actual is None or not isinstance(threshold, (int, float)):
        return 1.0
    value = finite_metric(actual)
    target = finite_metric(threshold)
    if name in {"profit_factor", "minimum_trade_count"}:
        return round(max(0.0, target - value) / max(abs(target), 1.0), 4)
    if name == "maximum_drawdown":
        return round(max(0.0, value - target) / max(abs(target), 0.01), 4)
    if name == "positive_expectancy":
        return round(abs(min(value, 0.0)) / (abs(min(value, 0.0)) + 1.0), 4)
    return 1.0


def gate_strength(gate: dict[str, Any]) -> float:
    return 2.0 if gate["passed"] else 1.0 - min(1.0, finite_metric(gate["distance"]))


def dimension_intelligence(jobs: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in jobs:
        grouped[str(row.get(field) or "unknown")].append(row)
    result = []
    for name, rows in grouped.items():
        terminal = [row for row in rows if row["status"] in TESTED_STATUSES]
        metrics = [row["metrics"] for row in terminal]
        promoted = [row for row in terminal if row["status"] == "promoted"]
        rejected = [row for row in terminal if row["status"] == "rejected"]
        stability = gate_pass_rate(terminal, "stability")
        confidence = gate_pass_rate(terminal, "confidence_interval")
        failures = Counter(gate for row in rejected for gate in row["failed_gates"])
        row = {
            "name": name,
            "candidates_tested": len({item["candidate_id"] for item in terminal}),
            "validation_runs": len(terminal),
            "rejection_rate": ratio(len(rejected), len(terminal)),
            "pass_rate": ratio(len(promoted), len(terminal)),
            "average_profit_factor": average_metric(metrics, "profit_factor"),
            "average_expectancy": average_metric(metrics, "expectancy_per_trade"),
            "median_trade_count": median_metric(metrics, "number_of_trades"),
            "median_drawdown": median_metric(metrics, "max_drawdown"),
            "average_trade_count": average_metric(metrics, "number_of_trades"),
            "stability_pass_rate": stability,
            "confidence_interval_pass_rate": confidence,
            "dominant_failure_reason": failures.most_common(1)[0][0] if failures else None,
            "candidate_quality_score": candidate_quality_score(terminal),
            "inactive": not terminal,
        }
        if field == "strategy_family":
            row["best_asset"] = best_dimension(terminal, "symbol")
            row["best_timeframe"] = best_dimension(terminal, "timeframe")
        elif field in {"symbol", "timeframe"}:
            row["dominant_regime"] = best_regime(terminal)
            row["failure_concentration"] = ratio(failures.most_common(1)[0][1], len(rejected)) if failures and rejected else 0
            row["deprioritize"] = len(terminal) >= 3 and row["pass_rate"] == 0 and row["candidate_quality_score"] < 40
        result.append(row)
    return sorted(result, key=lambda row: (row["candidate_quality_score"], row["validation_runs"], row["name"]), reverse=True)


def dimension_highlights(rows: list[dict[str, Any]], include_inactive: bool = False) -> dict[str, str | None]:
    active = [row for row in rows if row["validation_runs"]]
    passing = [row for row in active if row["pass_rate"] > 0]
    return {
        "most_promising": max(passing, key=lambda row: (row["candidate_quality_score"], row["validation_runs"]))["name"] if passing else None,
        "highest_observed_quality": active[0]["name"] if active else None,
        "most_rejected": max(active, key=lambda row: (row["rejection_rate"], row["validation_runs"]))["name"] if active else None,
        "most_inactive": next((row["name"] for row in rows if row["inactive"]), None) if include_inactive else None,
        "most_unstable": min(
            (row for row in active if row["stability_pass_rate"] is not None),
            key=lambda row: row["stability_pass_rate"],
            default=None,
        )["name"] if any(row["stability_pass_rate"] is not None for row in active) else None,
    }


def regime_analysis(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for row in jobs:
        if row["status"] not in TESTED_STATUSES:
            continue
        for regime in ((row["result"].get("regime_analysis") or {}).get("by_market_regime") or []):
            grouped[str(regime.get("regime") or "unknown")].append((row, dict(regime.get("metrics") or {})))
    result = []
    for name in sorted(set(grouped) | set(REQUIRED_MARKET_REGIMES)):
        pairs = grouped.get(name, [])
        rows = [pair[0] for pair in pairs]
        metrics = [pair[1] for pair in pairs]
        failures = Counter(gate for row in rows if row["status"] == "rejected" for gate in row["failed_gates"])
        result.append({
            "regime": name,
            "trades": int(sum(finite_metric(metric.get("number_of_trades")) for metric in metrics)),
            "profit_factor": weighted_metric(metrics, "profit_factor"),
            "expectancy": weighted_metric(metrics, "expectancy_per_trade"),
            "drawdown": weighted_metric(metrics, "max_drawdown"),
            "win_rate": weighted_metric(metrics, "win_rate"),
            "candidate_pass_rate": ratio(sum(row["status"] == "promoted" for row in rows), len(rows)),
            "dominant_failure_reason": failures.most_common(1)[0][0] if failures else None,
            "evidence_available": bool(pairs),
        })
    return sorted(result, key=lambda row: (row["candidate_pass_rate"], row["expectancy"], row["trades"]), reverse=True)


def duplicate_analysis(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = {candidate_id: rows[0] for candidate_id, rows in group_candidates(jobs).items()}
    canonical_groups: dict[str, list[str]] = defaultdict(list)
    block_groups: dict[str, list[str]] = defaultdict(list)
    lineage_groups: dict[str, list[str]] = defaultdict(list)
    outcome_groups: dict[str, list[str]] = defaultdict(list)
    for candidate_id, row in candidates.items():
        canonical = str(row["candidate"].get("canonical_key") or canonical_json({"blocks": row["blocks"], "parameters": row["parameters"]}))
        canonical_groups[sha(canonical)].append(candidate_id)
        block_groups[sha(canonical_json(row["blocks"]))].append(candidate_id)
        parent = str(row["candidate"].get("parent_candidate_id") or row["result"].get("parent_candidate_id") or "")
        if parent:
            lineage_groups[parent].append(candidate_id)
        candidate_rows = group_candidates(jobs)[candidate_id]
        signature = canonical_json(sorted({outcome_signature(item) for item in candidate_rows if item["status"] in TESTED_STATUSES}))
        if signature != "[]":
            outcome_groups[sha(signature)].append(candidate_id)
    exact_groups = [ids for ids in canonical_groups.values() if len(ids) > 1]
    near_groups = [ids for ids in block_groups.values() if len(ids) > 1 and len({candidate_canonical(candidates[item]) for item in ids}) > 1]
    duplicate_outcomes = [ids for ids in outcome_groups.values() if len(ids) > 1]
    duplicate_lineage = [ids for ids in lineage_groups.values() if len(ids) > 1]
    redundant = [
        {"structure": sha(canonical_json(candidates[ids[0]]["blocks"]))[:12], "candidate_count": len(ids), "candidate_ids": ids[:8]}
        for ids in near_groups if len(ids) >= 3
    ]
    return {
        "unique_candidates": len(candidates) - sum(len(ids) - 1 for ids in exact_groups),
        "candidate_ids": len(candidates),
        "exact_duplicates": sum(len(ids) - 1 for ids in exact_groups),
        "near_duplicates": sum(len(ids) - 1 for ids in near_groups),
        "duplicate_validation_outcomes": sum(len(ids) - 1 for ids in duplicate_outcomes),
        "duplicate_lineage": sum(len(ids) - 1 for ids in duplicate_lineage),
        "redundant_parameter_regions": redundant,
        "exact_duplicate_groups": exact_groups[:10],
        "near_duplicate_groups": near_groups[:10],
    }


def grouped_experiment_history(jobs: list[dict[str, Any]], limit: int = 100) -> list[dict[str, Any]]:
    result = []
    for candidate_id, rows in group_candidates(jobs).items():
        terminal = [row for row in rows if row["status"] in TESTED_STATUSES]
        best = max(terminal or rows, key=lambda row: finite_metric(row.get("validation_score")))
        failures = sorted({gate for row in rows for gate in row["failed_gates"]})
        statuses = Counter(str(row["status"]) for row in rows)
        result.append({
            "experiment_id": f"campaign-{best.get('campaign_id')}-candidate-{candidate_id}",
            "candidate_id": candidate_id,
            "strategy_family": best["strategy_family"],
            "assets": sorted({row["symbol"] for row in rows}),
            "timeframes": sorted({row["timeframe"] for row in rows}),
            "parameter_version": sha(candidate_canonical(best))[:12],
            "result": candidate_result_label(statuses),
            "validation_metrics": best["metrics"],
            "failure_reasons": failures,
            "validation_runs": len(rows),
            "distinct_validation_runs": len({int(row["id"]) for row in rows}),
            "created_at": min((row.get("created_at") for row in rows if row.get("created_at")), default=None),
        })
    return sorted(result, key=lambda row: str(row.get("created_at") or ""), reverse=True)[:limit]


def research_recommendations(
    campaign: dict[str, Any],
    jobs: list[dict[str, Any]],
    rejection: dict[str, Any],
    families: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    timeframes: list[dict[str, Any]],
    duplicates: dict[str, Any],
) -> list[dict[str, Any]]:
    recommendations = []
    dominant = rejection["dominant_reasons"][0] if rejection["dominant_reasons"] else None
    actions = {
        "minimum_trade_count": "Reduce entry filters or test longer windows where trade count repeatedly fails.",
        "profit_factor": "Exclude parameter ranges that repeatedly fail the stored profit-factor gate.",
        "positive_expectancy": "Deprioritize structures with non-positive net expectancy after costs.",
        "stability": "Constrain the family to regimes where stability evidence is positive.",
        "confidence_interval": "Increase independent samples before reconsidering uncertain candidates.",
        "maximum_drawdown": "Reduce risk concentration in parameter regions exceeding drawdown limits.",
    }
    if dominant:
        recommendations.append(recommendation(
            actions.get(dominant["name"], f"Investigate the dominant {dominant['name']} rejection gate."),
            "rejection_analysis.validation_rules",
            dominant["candidate_count"],
            dominant["candidate_rate"],
            "Reduce validation jobs spent on configurations that fail the same gate.",
            f"A bounded follow-up cohort must reduce {dominant['name']} failures without reducing another required gate.",
            campaign,
        ))
    weak_family = next(
        (row for row in reversed(families) if row["validation_runs"] >= 3 and row["pass_rate"] == 0),
        next((row for row in reversed(families) if row["validation_runs"] >= 3), None),
    )
    if weak_family:
        recommendations.append(recommendation(
            f"Deprioritize {weak_family['name']} until a new falsifiable hypothesis addresses {weak_family['dominant_failure_reason'] or 'its low quality score'}.",
            "strategy_intelligence.rows",
            weak_family["candidates_tested"],
            weak_family["rejection_rate"],
            "Shift campaign capacity toward better observed families.",
            f"Retain the family only if a holdout cohort exceeds quality score {weak_family['candidate_quality_score']:.1f} and passes an existing gate.",
            campaign,
        ))
    weak_asset = next((row for row in reversed(assets) if row["deprioritize"]), None)
    if weak_asset:
        recommendations.append(recommendation(
            f"Deprioritize {weak_asset['name']} in the next campaign proposal.",
            "asset_intelligence.rows",
            weak_asset["candidates_tested"],
            weak_asset["rejection_rate"],
            "Reduce repeated low-quality asset validations.",
            "Restore the asset only if a targeted holdout improves pass rate above zero without changing thresholds.",
            campaign,
        ))
    weak_timeframe = next((row for row in reversed(timeframes) if row["deprioritize"]), None)
    if weak_timeframe:
        recommendations.append(recommendation(
            f"Deprioritize the {weak_timeframe['name']} timeframe for weak structures.",
            "timeframe_intelligence.rows",
            weak_timeframe["candidates_tested"],
            weak_timeframe["rejection_rate"],
            "Avoid repeating low-information timeframe tests.",
            "A targeted comparison must improve candidate quality while preserving trade count and drawdown gates.",
            campaign,
        ))
    duplicate_count = int(duplicates["exact_duplicates"]) + int(duplicates["near_duplicates"])
    if duplicate_count:
        recommendations.append(recommendation(
            "Exclude exact canonical duplicates and review structurally redundant parameter regions before queue generation.",
            "duplicate_analysis",
            duplicate_count,
            ratio(duplicate_count, max(1, duplicates["candidate_ids"])),
            "Reserve jobs for distinct hypotheses and market validations.",
            "The next generated cohort must produce fewer duplicate structures at equal or better gate pass rate.",
            campaign,
        ))
    return recommendations


def recommendation(
    text: str,
    source: str,
    count: int,
    confidence: float,
    benefit: str,
    falsification: str,
    campaign: dict[str, Any],
) -> dict[str, Any]:
    return {
        "recommendation": text,
        "evidence_source": source,
        "candidate_count": int(count),
        "confidence": round(min(1.0, max(0.0, confidence)), 4),
        "expected_benefit": benefit,
        "falsification_test": falsification,
        "campaign_version": str(campaign.get("campaign_key") or CAMPAIGN_VERSION),
    }


def next_campaign_proposal(
    campaign: dict[str, Any],
    jobs: list[dict[str, Any]],
    families: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    timeframes: list[dict[str, Any]],
    duplicates: dict[str, Any],
    recommendations: list[dict[str, Any]],
) -> dict[str, Any]:
    highest_quality_family = families[0]["name"] if families else None
    retain_family = [
        row["name"]
        for row in families
        if row["validation_runs"] and (row["pass_rate"] > 0 or row["name"] == highest_quality_family)
    ]
    deprioritize_family = [
        row["name"]
        for row in families
        if row["validation_runs"] and row["name"] not in retain_family
    ]
    retain_assets = [row["name"] for row in assets if not row["deprioritize"]]
    deprioritize_assets = [row["name"] for row in assets if row["deprioritize"]]
    retain_timeframes = [row["name"] for row in timeframes if not row["deprioritize"]]
    deprioritize_timeframes = [row["name"] for row in timeframes if row["deprioritize"]]
    unique_candidates = len({row["candidate_id"] for row in jobs})
    duplicate_work = int(duplicates["exact_duplicates"]) + int(duplicates["near_duplicates"])
    return {
        "proposal_version": f"phase_9_6_candidate_quality_v2_campaign_{campaign.get('id')}",
        "status": "review_required",
        "launch_authorized": False,
        "strategy_families_to_retain": retain_family,
        "strategy_families_to_deprioritize": deprioritize_family,
        "assets_to_retain": retain_assets,
        "assets_to_deprioritize": deprioritize_assets,
        "timeframes_to_retain": retain_timeframes,
        "timeframes_to_deprioritize": deprioritize_timeframes,
        "parameter_regions_to_exclude": duplicates["redundant_parameter_regions"],
        "new_hypothesis_tests": [row["falsification_test"] for row in recommendations],
        "candidate_count": unique_candidates,
        "expected_duplicate_work_reduction": ratio(duplicate_work, max(1, unique_candidates)),
        "source_campaign_version": str(campaign.get("campaign_key") or CAMPAIGN_VERSION),
        "validation_thresholds_changed": False,
    }


def historical_research(conn: psycopg.Connection) -> dict[str, Any]:
    validation_total = int(conn.execute("SELECT COUNT(*) AS count FROM alpha_validation_runs").fetchone()["count"])
    experiment_total = int(conn.execute("SELECT COUNT(*) AS count FROM strategy_experiments").fetchone()["count"])
    validation_runs = conn.execute(
        "SELECT id, candidate_count, summary, created_at FROM alpha_validation_runs ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    experiments = conn.execute(
        """
        SELECT id, name, strategy_name, strategy_version, recommendation, result, created_at
        FROM strategy_experiments ORDER BY created_at DESC LIMIT 20
        """
    ).fetchall()
    return {
        "separated_from_campaign_evidence": True,
        "alpha_validation_run_count": validation_total,
        "strategy_experiment_count": experiment_total,
        "alpha_validation_runs": [dict(row) for row in validation_runs],
        "strategy_experiments": [dict(row) for row in experiments],
    }


def empty_command_center(filters: dict[str, Any]) -> dict[str, Any]:
    return {
        "campaign": None,
        "campaigns": [],
        "filters": normalized_filters(filters),
        "overview": {},
        "candidate_funnel": [],
        "rejection_analysis": {},
        "near_pass_candidates": [],
        "strategy_intelligence": {"rows": [], "highlights": {}},
        "asset_intelligence": {"rows": [], "highlights": {}},
        "timeframe_intelligence": {"rows": [], "highlights": {}},
        "regime_analysis": [],
        "duplicate_analysis": {},
        "experiment_history": [],
        "recommendations": [],
        "next_campaign_proposal": None,
        "historical_research": {"separated_from_campaign_evidence": True, "alpha_validation_runs": [], "strategy_experiments": []},
        "simulation_only": True,
    }


def terminology() -> dict[str, str]:
    return {
        "campaign_job": "One queued or executed candidate, asset, and timeframe validation unit.",
        "generated_strategy": "A deterministic strategy definition produced by the campaign generator.",
        "candidate": "One unique candidate_id within a campaign.",
        "validation_run": "One completed campaign job evaluated against stored evidence gates.",
        "rejected_candidate": "A completed candidate with no passing validation run.",
        "completed_job": "A terminal execution outcome; completion alone does not mean validation passed.",
        "research_candidate": "A candidate with at least one validation run that passed all single-market gates.",
        "elite_candidate": "A persisted candidate that passed cross-market validation gates.",
        "paper_deployment": "A simulation-only deployment linked to a campaign candidate.",
        "forward_evidence": "Post-deployment candidate-linked paper evidence eligible under the forward-evidence rule.",
        "validation_passed": "All required evidence gates passed for the stated validation scope.",
    }


def funnel_reconciliation(jobs: list[dict[str, Any]], funnel: list[dict[str, Any]]) -> dict[str, Any]:
    stages = {row["key"]: row["count"] for row in funnel}
    grouped = group_candidates(jobs)
    completed = completed_candidate_count(jobs)
    tested_incomplete = sum(any(row["status"] in TESTED_STATUSES for row in rows) and not all(row["status"] in TERMINAL_STATUSES for row in rows) for rows in grouped.values())
    return {
        "candidate_ids": len(grouped),
        "generated_equals_unique_candidate_ids": stages.get("generated", 0) == len(grouped),
        "tested_equals_completed_plus_needs_more_evidence": stages.get("tested", 0) == completed + tested_incomplete,
        "paper_deployments_do_not_exceed_elite_candidates": stages.get("paper_deployed", 0) <= stages.get("elite_candidate", 0),
    }


def parameter_failure_regions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str]] = Counter()
    for row in rows:
        for key, value in row["parameters"].items():
            if isinstance(value, (str, int, float, bool)) and key not in {"initial_equity", "fee_rate", "slippage_rate"}:
                counts[(str(key), str(value))] += 1
    return [
        {"parameter": key, "value": value, "rejected_runs": count}
        for (key, value), count in counts.most_common(20)
    ]


def metric_failure_ranges(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs = {
        "trade_count": ("number_of_trades", [0, 5, 15, 30, 60]),
        "profit_factor": ("profit_factor", [0, 0.5, 1.0, 1.25, 2.0]),
        "expectancy": ("expectancy_per_trade", [-100, -25, 0, 25, 100]),
        "drawdown": ("max_drawdown", [0, 0.05, 0.1, 0.2, 0.5]),
        "stability": ("_stability", [0, 0.25, 0.5, 0.75, 1.0]),
    }
    result = []
    for label, (key, bounds) in specs.items():
        values = [finite_metric(row.get("consistency_score")) if key == "_stability" else finite_metric(row["metrics"].get(key)) for row in rows]
        for start, end in zip(bounds, bounds[1:]):
            count = sum(start <= value < end for value in values)
            if count:
                result.append({"metric": label, "range": f"{start:g} to <{end:g}", "rejected_runs": count})
    confidence = sum("confidence_interval" in row["failed_gates"] for row in rows)
    if confidence:
        result.append({"metric": "confidence_interval", "range": "stored gate failed", "rejected_runs": confidence})
    return result


def rejection_dimension(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    counts = Counter(str(row.get(field) or "unknown") for row in rows)
    return counter_rows(counts, len(rows))


def rejection_regimes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(regime for row in rows for regime in row["regimes"])
    return counter_rows(counts, len(rows))


def counter_rows(counter: Counter[str], denominator: int) -> list[dict[str, Any]]:
    return [
        {"name": name, "count": count, "rate": ratio(count, denominator)}
        for name, count in counter.most_common()
    ]


def candidate_quality_score(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    metrics = [row["metrics"] for row in rows]
    pass_component = ratio(sum(row["status"] == "promoted" for row in rows), len(rows))
    profit_factor = min(1.0, average_metric(metrics, "profit_factor") / 1.25)
    expectancy = 1.0 if average_metric(metrics, "expectancy_per_trade") > 0 else 0.0
    trades = min(1.0, average_metric(metrics, "number_of_trades") / 30)
    drawdown = max(0.0, 1.0 - average_metric(metrics, "max_drawdown") / 0.2)
    return round(100 * (0.35 * pass_component + 0.2 * profit_factor + 0.15 * expectancy + 0.15 * trades + 0.15 * drawdown), 2)


def gate_pass_rate(rows: list[dict[str, Any]], gate_name: str) -> float | None:
    observed = []
    for row in rows:
        checks = (row["result"].get("paper_readiness") or {}).get("checks") or []
        for check in checks:
            if canonical_gate(str(check.get("name") or "")) == gate_name:
                observed.append(bool(check.get("passed")))
    return ratio(sum(observed), len(observed)) if observed else None


def best_dimension(rows: list[dict[str, Any]], field: str) -> str | None:
    if not rows:
        return None
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(field) or "unknown")].append(row)
    return max(grouped, key=lambda key: (candidate_quality_score(grouped[key]), len(grouped[key])))


def best_regime(rows: list[dict[str, Any]]) -> str | None:
    counts = Counter(regime for row in rows for regime in row["regimes"])
    return counts.most_common(1)[0][0] if counts else None


def average_metric(metrics: Iterable[dict[str, Any]], key: str) -> float:
    values = [finite_metric(row.get(key)) for row in metrics if row.get(key) is not None]
    return round(sum(values) / len(values), 6) if values else 0.0


def median_metric(metrics: Iterable[dict[str, Any]], key: str) -> float:
    values = [finite_metric(row.get(key)) for row in metrics if row.get(key) is not None]
    return round(float(median(values)), 6) if values else 0.0


def weighted_metric(metrics: list[dict[str, Any]], key: str) -> float:
    pairs = [
        (finite_metric(row.get(key)), max(1.0, finite_metric(row.get("number_of_trades"))))
        for row in metrics if row.get(key) is not None
    ]
    weight = sum(item[1] for item in pairs)
    return round(sum(value * item_weight for value, item_weight in pairs) / weight, 6) if weight else 0.0


def group_candidates(jobs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in jobs:
        grouped[str(row["candidate_id"])].append(row)
    return grouped


def completed_candidate_count(jobs: list[dict[str, Any]]) -> int:
    return sum(rows and all(row["status"] in TERMINAL_STATUSES for row in rows) for rows in group_candidates(jobs).values())


def infer_family(blocks: dict[str, Any]) -> str:
    entry = str(blocks.get("entry") or "")
    trend = str(blocks.get("trend") or "")
    momentum = str(blocks.get("momentum") or "")
    if "mean_reversion" in entry:
        return "Mean Reversion"
    if "breakout" in entry or "opening_range" in entry:
        return "Breakout"
    if "pullback" in entry:
        return "Pullback"
    if "trend" in entry or "ema" in trend or "supertrend" in trend:
        return "Trend Following"
    if momentum:
        return "Momentum"
    return "Other"


def asset_class(symbol: str, default: str) -> str:
    if symbol.endswith("USDT"):
        return "crypto"
    if symbol in ETF_SYMBOLS:
        return "etf"
    return "us_equity" if default in {"equity", "us_equity"} else default


def candidate_run_state(row: dict[str, Any]) -> str:
    return {
        "promoted": "validation_passed",
        "rejected": "rejected",
        "completed": "completed",
    }.get(str(row.get("status") or ""), str(row.get("status") or "unknown"))


def candidate_result_label(statuses: Counter[str]) -> str:
    if statuses.get("promoted"):
        return "validation_passed"
    if statuses.get("rejected"):
        return "rejected"
    if statuses.get("running") or statuses.get("queued") or statuses.get("retrying"):
        return "in_progress"
    return statuses.most_common(1)[0][0] if statuses else "unknown"


def outcome_signature(row: dict[str, Any]) -> str:
    metrics = row["metrics"]
    return canonical_json({
        "status": row["status"],
        "failures": row["failed_gates"],
        "profit_factor": round(finite_metric(metrics.get("profit_factor")), 3),
        "expectancy": round(finite_metric(metrics.get("expectancy_per_trade")), 2),
        "trades": int(finite_metric(metrics.get("number_of_trades"))),
        "drawdown": round(finite_metric(metrics.get("max_drawdown")), 3),
    })


def candidate_canonical(row: dict[str, Any]) -> str:
    return str(row["candidate"].get("canonical_key") or canonical_json({"blocks": row["blocks"], "parameters": row["parameters"]}))


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def ratio(numerator: int | float, denominator: int | float) -> float:
    return round(float(numerator) / float(denominator), 4) if denominator else 0.0


def unique(rows: list[dict[str, Any]], field: str) -> list[str]:
    return sorted({str(row.get(field)) for row in rows if row.get(field) is not None})
