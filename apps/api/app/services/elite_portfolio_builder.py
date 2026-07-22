from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable

try:
    import resource
except ImportError:  # pragma: no cover - unavailable on Windows
    resource = None  # type: ignore[assignment]


SOLVER_VERSION = "elite_portfolio_constructor_v1"
MAX_PORTFOLIO_SIZE = 20

DEFAULT_THRESHOLDS: dict[str, Any] = {
    "minimum_profit_factor": 1.20,
    "minimum_expectancy": 0.0,
    "maximum_drawdown": 0.12,
    "minimum_trade_count": 60,
    "minimum_stability": 0.60,
    "minimum_assets_passed": 2,
    "minimum_timeframes_passed": 1,
    "allowed_health": ["healthy"],
}

DEFAULT_CONSTRAINTS: dict[str, Any] = {
    "maximum_portfolio_size": MAX_PORTFOLIO_SIZE,
    "minimum_portfolio_size": 5,
    "minimum_unique_assets": 5,
    "minimum_families": 4,
    "minimum_timeframes": 2,
    "maximum_per_symbol": 2,
    "maximum_per_family": 2,
    "maximum_parameter_similarity": 0.90,
    "maximum_signal_correlation": 0.90,
    "maximum_strategy_return_correlation": 0.75,
    "minimum_correlation_observations": 30,
    "timeframe_cap_numerator": 1,
    "timeframe_cap_denominator": 2,
}

OBJECTIVE_HIERARCHY = [
    "maximum_feasible_size",
    "selected_objective",
    "quality_score",
    "diversity_score",
    "minimum_average_pairwise_correlation",
    "forward_validated",
    "minimum_drawdown",
    "trade_count",
    "immutable_candidate_id",
]


@dataclass(frozen=True)
class ConstructionTelemetry:
    candidates_examined: int
    iterations: int
    swaps: int
    termination_reason: str


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def decision_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalized_configuration(configuration: dict[str, Any] | None = None) -> dict[str, Any]:
    supplied = deepcopy(configuration or {})
    constraints = {**DEFAULT_CONSTRAINTS, **dict(supplied.get("constraints") or {})}
    thresholds = {**DEFAULT_THRESHOLDS, **dict(supplied.get("thresholds") or {})}
    constraints["maximum_portfolio_size"] = min(MAX_PORTFOLIO_SIZE, max(1, int(constraints["maximum_portfolio_size"])))
    constraints["minimum_portfolio_size"] = max(1, int(constraints["minimum_portfolio_size"]))
    return {
        "solver_version": SOLVER_VERSION,
        "objective": str(supplied.get("objective") or "balanced"),
        "custom_size": int(supplied["custom_size"]) if supplied.get("custom_size") is not None else None,
        "universe": sorted({str(item).upper() for item in supplied.get("universe") or []}),
        "families": sorted({str(item) for item in supplied.get("families") or []}),
        "directions": sorted({str(item) for item in supplied.get("directions") or ["long", "short"]}),
        "timeframes": sorted({str(item) for item in supplied.get("timeframes") or []}),
        "constraints": constraints,
        "thresholds": thresholds,
        "automatic_constraint_relaxation": False,
        "objective_hierarchy": list(OBJECTIVE_HIERARCHY),
    }


def immutable_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "elite_id": candidate.get("elite_id") or candidate.get("id"),
        "candidate_id": str(candidate["candidate_id"]),
        "campaign_id": candidate.get("campaign_id"),
        "strategy_version": candidate.get("strategy_version"),
        "dataset_ids": sorted(str(item) for item in candidate.get("dataset_ids") or []),
        "data_snapshot_hash": candidate.get("data_snapshot_hash"),
        "symbol": str(candidate.get("symbol") or "").upper(),
        "timeframe": str(candidate.get("timeframe") or ""),
        "family_id": str(candidate.get("family_id") or ""),
        "strategy_direction": str(candidate.get("strategy_direction") or "long"),
        "execution_capability": str(candidate.get("execution_capability") or "external_observe"),
        "parameters": deepcopy(candidate.get("parameters") or {}),
        "metrics": {
            key: candidate.get(key)
            for key in (
                "research_score", "quality_score", "profit_factor", "expectancy", "max_drawdown",
                "trade_count", "stability", "assets_passed", "timeframes_passed", "regimes_passed",
            )
        },
        "health": candidate.get("health") or "unclassified",
        "forward_validation_state": candidate.get("forward_validation_state"),
        "forward_evidence": deepcopy(candidate.get("forward_evidence") or {}),
        "strategy_returns": dict(sorted((candidate.get("strategy_returns") or {}).items())),
        "signal_returns": dict(sorted((candidate.get("signal_returns") or {}).items())),
    }


def candidate_snapshot(candidates: Iterable[dict[str, Any]], configuration: dict[str, Any], eligibility: list[dict[str, Any]], correlations: list[dict[str, Any]]) -> dict[str, Any]:
    frozen_candidates = sorted((immutable_candidate(row) for row in candidates), key=lambda row: row["candidate_id"])
    return {
        "solver_version": SOLVER_VERSION,
        "configuration": normalized_configuration(configuration),
        "candidate_evidence": frozen_candidates,
        "eligibility_decisions": sorted(eligibility, key=lambda row: row["candidate_id"]),
        "correlations": sorted(correlations, key=lambda row: (row["left_candidate_id"], row["right_candidate_id"], row["correlation_type"])),
    }


def snapshot_with_hash(snapshot: dict[str, Any]) -> dict[str, Any]:
    frozen = deepcopy(snapshot)
    frozen["decision_hash"] = decision_hash(frozen)
    return frozen


def eligibility_reasons(candidate: dict[str, Any], configuration: dict[str, Any]) -> list[str]:
    config = normalized_configuration(configuration)
    thresholds = config["thresholds"]
    reasons: list[str] = []
    symbol = str(candidate.get("symbol") or "").upper()
    if config["universe"] and symbol not in config["universe"]:
        reasons.append("UNIVERSE_EXCLUDED")
    if config["families"] and str(candidate.get("family_id") or "") not in config["families"]:
        reasons.append("FAMILY_EXCLUDED")
    if str(candidate.get("strategy_direction") or "long") not in config["directions"]:
        reasons.append("DIRECTION_EXCLUDED")
    if config["timeframes"] and str(candidate.get("timeframe") or "") not in config["timeframes"]:
        reasons.append("TIMEFRAME_EXCLUDED")
    numeric_gates = (
        ("profit_factor", "minimum_profit_factor", lambda actual, required: actual >= required, "PROFIT_FACTOR_MINIMUM"),
        ("expectancy", "minimum_expectancy", lambda actual, required: actual > required, "EXPECTANCY_POSITIVE"),
        ("max_drawdown", "maximum_drawdown", lambda actual, required: actual <= required, "DRAWDOWN_MAXIMUM"),
        ("trade_count", "minimum_trade_count", lambda actual, required: actual >= required, "TRADE_COUNT_MINIMUM"),
        ("stability", "minimum_stability", lambda actual, required: actual >= required, "STABILITY_MINIMUM"),
        ("assets_passed", "minimum_assets_passed", lambda actual, required: actual >= required, "ASSET_BREADTH_MINIMUM"),
        ("timeframes_passed", "minimum_timeframes_passed", lambda actual, required: actual >= required, "TIMEFRAME_EVIDENCE_MINIMUM"),
    )
    for field, threshold, predicate, code in numeric_gates:
        actual = float(candidate.get(field) or 0)
        required = float(thresholds[threshold])
        if not predicate(actual, required):
            reasons.append(code)
    allowed_health = {str(item) for item in thresholds.get("allowed_health") or []}
    if allowed_health and str(candidate.get("health") or "unclassified") not in allowed_health:
        reasons.append("HEALTH_CLASSIFICATION")
    return reasons


def evaluate_eligibility(candidates: Iterable[dict[str, Any]], configuration: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    decisions = []
    eligible = []
    for candidate in sorted(candidates, key=lambda row: str(row["candidate_id"])):
        reasons = eligibility_reasons(candidate, configuration)
        decisions.append({
            "candidate_id": str(candidate["candidate_id"]),
            "eligible": not reasons,
            "reasons": reasons,
            "strategy_direction": str(candidate.get("strategy_direction") or "long"),
            "execution_capability": str(candidate.get("execution_capability") or "external_observe"),
        })
        if not reasons:
            eligible.append(candidate)
    return eligible, decisions


def pearson_correlation(left: dict[str, Any], right: dict[str, Any]) -> tuple[float | None, int, list[str]]:
    aligned = sorted(set(left).intersection(right))
    if len(aligned) < 2:
        return None, len(aligned), aligned
    x = [float(left[key]) for key in aligned]
    y = [float(right[key]) for key in aligned]
    x_mean = sum(x) / len(x)
    y_mean = sum(y) / len(y)
    numerator = sum((a - x_mean) * (b - y_mean) for a, b in zip(x, y, strict=True))
    x_size = math.sqrt(sum((a - x_mean) ** 2 for a in x))
    y_size = math.sqrt(sum((b - y_mean) ** 2 for b in y))
    if x_size == 0 or y_size == 0:
        return None, len(aligned), aligned
    return numerator / (x_size * y_size), len(aligned), aligned


def parameter_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    keys = sorted(set(left).union(right))
    if not keys:
        return 1.0
    similarities = []
    for key in keys:
        a, b = left.get(key), right.get(key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            scale = max(abs(float(a)), abs(float(b)), 1.0)
            similarities.append(max(0.0, 1.0 - abs(float(a) - float(b)) / scale))
        else:
            similarities.append(1.0 if a == b else 0.0)
    return sum(similarities) / len(similarities)


def correlation_confidence(observations: int) -> str:
    if observations < 30:
        return "insufficient"
    if observations < 60:
        return "provisional"
    return "established"


def build_conflicts(candidates: list[dict[str, Any]], configuration: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    constraints = normalized_configuration(configuration)["constraints"]
    conflicts: list[dict[str, Any]] = []
    correlations: list[dict[str, Any]] = []
    ordered = sorted(candidates, key=lambda row: str(row["candidate_id"]))
    for index, left in enumerate(ordered):
        for right in ordered[index + 1:]:
            left_id, right_id = str(left["candidate_id"]), str(right["candidate_id"])
            pair_reasons: list[tuple[str, dict[str, Any]]] = []
            if str(left.get("symbol")) == str(right.get("symbol")) and str(left.get("family_id")) == str(right.get("family_id")):
                pair_reasons.append(("SYMBOL_FAMILY_DUPLICATE", {}))
            similarity = parameter_similarity(left.get("parameters") or {}, right.get("parameters") or {})
            if similarity > float(constraints["maximum_parameter_similarity"]):
                pair_reasons.append(("PARAMETER_SIMILARITY", {"coefficient": similarity}))
            for correlation_type, field, limit in (
                ("signal", "signal_returns", float(constraints["maximum_signal_correlation"])),
                ("strategy_return", "strategy_returns", float(constraints["maximum_strategy_return_correlation"])),
            ):
                coefficient, observations, aligned = pearson_correlation(left.get(field) or {}, right.get(field) or {})
                confidence = correlation_confidence(observations)
                correlation = {
                    "left_candidate_id": left_id,
                    "right_candidate_id": right_id,
                    "correlation_type": correlation_type,
                    "coefficient": coefficient,
                    "observation_count": observations,
                    "confidence": confidence,
                    "window_start": aligned[0] if aligned else None,
                    "window_end": aligned[-1] if aligned else None,
                    "return_frequency": "strategy_evaluation",
                    "method": "pearson_aligned_strategy_returns",
                    "data_snapshot_hash": decision_hash({"left": left.get(field) or {}, "right": right.get(field) or {}}),
                }
                correlations.append(correlation)
                if observations < int(constraints["minimum_correlation_observations"]):
                    pair_reasons.append((f"{correlation_type.upper()}_CORRELATION_INSUFFICIENT", {"observation_count": observations, "confidence": confidence}))
                elif coefficient is not None and abs(coefficient) > limit:
                    pair_reasons.append((f"{correlation_type.upper()}_CORRELATION_LIMIT", {"coefficient": coefficient, "limit": limit, "confidence": confidence}))
            for conflict_type, evidence in pair_reasons:
                conflicts.append({
                    "left_candidate_id": left_id,
                    "right_candidate_id": right_id,
                    "conflict_type": conflict_type,
                    "hard_conflict": True,
                    "evidence": evidence,
                })
    return conflicts, correlations


def candidate_quality(candidate: dict[str, Any], objective: str) -> float:
    if objective == "profit_factor":
        return float(candidate.get("profit_factor") or 0)
    if objective == "expectancy":
        return float(candidate.get("expectancy") or 0)
    if objective == "minimum_drawdown":
        return -float(candidate.get("max_drawdown") or 0)
    return float(candidate.get("quality_score") or candidate.get("research_score") or 0)


def candidate_order(candidates: Iterable[dict[str, Any]], objective: str) -> list[dict[str, Any]]:
    return sorted(
        candidates,
        key=lambda row: (
            -candidate_quality(row, objective),
            -float(row.get("research_score") or 0),
            -int(str(row.get("forward_validation_state") or "") == "forward_validation_passed"),
            float(row.get("max_drawdown") or 0),
            -int(row.get("trade_count") or 0),
            str(row["candidate_id"]),
        ),
    )


def exact_timeframe_cap_holds(selected: Iterable[dict[str, Any]]) -> bool:
    rows = list(selected)
    total = len(rows)
    return total > 0 and all(2 * count <= total for count in Counter(str(row.get("timeframe")) for row in rows).values())


def portfolio_constraint_reasons(selected: list[dict[str, Any]], constraints: dict[str, Any], conflicts: set[frozenset[str]]) -> list[str]:
    reasons: list[str] = []
    if len({str(row.get("symbol")) for row in selected}) < int(constraints["minimum_unique_assets"]):
        reasons.append("MINIMUM_UNIQUE_ASSETS")
    if len({str(row.get("family_id")) for row in selected}) < int(constraints["minimum_families"]):
        reasons.append("MINIMUM_FAMILIES")
    if len({str(row.get("timeframe")) for row in selected}) < int(constraints["minimum_timeframes"]):
        reasons.append("MINIMUM_TIMEFRAMES")
    if any(count > int(constraints["maximum_per_symbol"]) for count in Counter(str(row.get("symbol")) for row in selected).values()):
        reasons.append("MAXIMUM_PER_SYMBOL")
    if any(count > int(constraints["maximum_per_family"]) for count in Counter(str(row.get("family_id")) for row in selected).values()):
        reasons.append("MAXIMUM_PER_FAMILY")
    if not exact_timeframe_cap_holds(selected):
        reasons.append("TIMEFRAME_50_PERCENT_CAP")
    ids = [str(row["candidate_id"]) for row in selected]
    if any(frozenset((left, right)) in conflicts for index, left in enumerate(ids) for right in ids[index + 1:]):
        reasons.append("PAIRWISE_HARD_CONFLICT")
    return reasons


def _can_add(selected: list[dict[str, Any]], candidate: dict[str, Any], target: int, constraints: dict[str, Any], conflicts: set[frozenset[str]]) -> bool:
    candidate_id = str(candidate["candidate_id"])
    if any(frozenset((candidate_id, str(row["candidate_id"]))) in conflicts for row in selected):
        return False
    if sum(str(row.get("symbol")) == str(candidate.get("symbol")) for row in selected) >= int(constraints["maximum_per_symbol"]):
        return False
    if sum(str(row.get("family_id")) == str(candidate.get("family_id")) for row in selected) >= int(constraints["maximum_per_family"]):
        return False
    timeframe_count = sum(str(row.get("timeframe")) == str(candidate.get("timeframe")) for row in selected) + 1
    return 2 * timeframe_count <= target


def _selection_score(selected: list[dict[str, Any]], objective: str) -> tuple[Any, ...]:
    symbols = len({str(row.get("symbol")) for row in selected})
    families = len({str(row.get("family_id")) for row in selected})
    return (
        len(selected),
        round(sum(candidate_quality(row, objective) for row in selected), 12),
        symbols + families,
        sum(str(row.get("forward_validation_state")) == "forward_validation_passed" for row in selected),
        -sum(float(row.get("max_drawdown") or 0) for row in selected),
        sum(int(row.get("trade_count") or 0) for row in selected),
        tuple(sorted(str(row["candidate_id"]) for row in selected)),
    )


def construct_portfolio(candidates: list[dict[str, Any]], conflicts: list[dict[str, Any]], configuration: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    config = normalized_configuration(configuration)
    constraints = config["constraints"]
    objective = config["objective"]
    ordered = candidate_order(candidates, objective)
    conflict_pairs = {frozenset((row["left_candidate_id"], row["right_candidate_id"])) for row in conflicts if row.get("hard_conflict", True)}
    maximum = min(int(constraints["maximum_portfolio_size"]), len(ordered))
    minimum = int(constraints["minimum_portfolio_size"])
    if config["custom_size"] is not None:
        maximum = minimum = min(maximum, int(config["custom_size"]))
    iterations = 0
    best: list[dict[str, Any]] = []
    operations: list[dict[str, Any]] = []
    # Multiple stable seeds avoid making the first high-scoring candidate an
    # irreversible choice while keeping the constructor bounded for 500 rows.
    seeds: list[dict[str, Any] | None] = [None, *ordered[: min(64, len(ordered))]]
    for target in range(maximum, minimum - 1, -1):
        for seed in seeds:
            iterations += 1
            selected: list[dict[str, Any]] = []
            if seed is not None and _can_add([], seed, target, constraints, conflict_pairs):
                selected.append(seed)
            for candidate in ordered:
                if len(selected) == target:
                    break
                if candidate in selected:
                    continue
                if _can_add(selected, candidate, target, constraints, conflict_pairs):
                    selected.append(candidate)
            if len(selected) != target or portfolio_constraint_reasons(selected, constraints, conflict_pairs):
                continue
            if not best or _selection_score(selected, objective) > _selection_score(best, objective):
                best = selected
                operations.append({"operation": "select", "target_size": target, "seed": seed and seed["candidate_id"]})
        if best:
            break
    swaps = 0
    if best:
        unselected = [row for row in ordered if row not in best]
        improved = True
        while improved and swaps < 100:
            improved = False
            for current in list(reversed(candidate_order(best, objective))):
                for replacement in unselected:
                    trial = [row for row in best if row is not current] + [replacement]
                    if portfolio_constraint_reasons(trial, constraints, conflict_pairs):
                        continue
                    if _selection_score(trial, objective) > _selection_score(best, objective):
                        best = trial
                        unselected = [row for row in ordered if row not in best]
                        swaps += 1
                        operations.append({"operation": "swap", "removed": current["candidate_id"], "added": replacement["candidate_id"]})
                        improved = True
                        break
                if improved:
                    break
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    status = "review_ready" if best else "infeasible"
    termination = "largest_feasible_portfolio_found" if best else "no_portfolio_satisfies_all_constraints"
    return {
        "status": status,
        "solver_version": SOLVER_VERSION,
        "selected": [str(row["candidate_id"]) for row in candidate_order(best, objective)],
        "maximum_feasible_size": len(best),
        "constraint_relaxations": [],
        "constraint_relaxation_count": 0,
        "candidate_order": [str(row["candidate_id"]) for row in ordered],
        "iterations": iterations,
        "operations": operations,
        "swap_count": swaps,
        "termination_reason": termination,
        "objective_hierarchy": list(OBJECTIVE_HIERARCHY),
        "optimization_duration_ms": duration_ms,
        "candidates_examined": len(ordered),
        "peak_memory_mb": _peak_memory_mb(),
    }


def binding_constraints(eligibility: list[dict[str, Any]], conflicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for decision in eligibility:
        counts.update(decision.get("reasons") or [])
    counts.update(row["conflict_type"] for row in conflicts)
    return [{"constraint": key, "excluded_candidates_or_pairs": value} for key, value in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def portfolio_analytics(selected: list[dict[str, Any]], correlations: list[dict[str, Any]]) -> dict[str, Any]:
    ids = {str(row["candidate_id"]) for row in selected}
    coefficients = [abs(float(row["coefficient"])) for row in correlations if row.get("coefficient") is not None and row["left_candidate_id"] in ids and row["right_candidate_id"] in ids and row["correlation_type"] == "strategy_return"]
    expectancies = [float(row.get("expectancy") or 0) for row in selected]
    profit_factors = [float(row.get("profit_factor") or 0) for row in selected]
    drawdowns = [float(row.get("max_drawdown") or 0) for row in selected]
    contributions = [{"candidate_id": row["candidate_id"], "expectancy": float(row.get("expectancy") or 0), "quality_score": candidate_quality(row, "balanced")} for row in selected]
    return {
        "portfolio_profit_factor": sum(profit_factors) / len(profit_factors) if profit_factors else 0,
        "portfolio_expectancy": sum(expectancies),
        "portfolio_max_drawdown_conservative": sum(drawdowns),
        "simultaneous_positions": len(selected),
        "gross_exposure_units": len(selected),
        "net_directional_exposure_units": sum(-1 if row.get("strategy_direction") == "short" else 1 for row in selected),
        "symbol_concentration": _distribution(selected, "symbol"),
        "sector_concentration": _distribution(selected, "sector"),
        "asset_class_concentration": _distribution(selected, "asset_class"),
        "direction_distribution": _distribution(selected, "strategy_direction"),
        "timeframe_distribution": _distribution(selected, "timeframe"),
        "family_distribution": _distribution(selected, "family_id"),
        "member_contribution": contributions,
        "worst_member": min(contributions, key=lambda row: row["expectancy"], default=None),
        "worst_period": None,
        "opportunity_frequency": sum(float(row.get("opportunity_frequency") or 0) for row in selected),
        "average_pairwise_correlation": sum(coefficients) / len(coefficients) if coefficients else None,
        "maximum_pairwise_correlation": max(coefficients, default=None),
        "limitations": ["Portfolio replay analytics use frozen candidate evidence; borrow availability, borrow costs, recalls, and external short execution are not modeled."],
    }


def preview(candidates: list[dict[str, Any]], configuration: dict[str, Any] | None = None) -> dict[str, Any]:
    total_started = time.perf_counter()
    config = normalized_configuration(configuration)
    eligibility_started = time.perf_counter()
    eligible, eligibility = evaluate_eligibility(candidates, config)
    eligibility_ms = round((time.perf_counter() - eligibility_started) * 1000, 3)
    # V1 is intentionally a bounded constructor, not an exact optimizer. All
    # candidates are eligibility-checked and snapshot-hashed, while expensive
    # pair evidence is materialized for a stable objective-ranked pool sized at
    # four times the maximum portfolio. This bounds 500-candidate latency and
    # preserves a deterministic audit trail of the candidates actually searched.
    conflict_pool_limit = min(len(eligible), max(80, int(config["constraints"]["maximum_portfolio_size"]) * 4))
    conflict_pool = candidate_order(eligible, config["objective"])[:conflict_pool_limit]
    conflict_started = time.perf_counter()
    conflicts, correlations = build_conflicts(conflict_pool, config)
    conflict_ms = round((time.perf_counter() - conflict_started) * 1000, 3)
    result = construct_portfolio(conflict_pool, conflicts, config)
    result["candidates_examined"] = len(eligible)
    result["construction_pool_count"] = len(conflict_pool)
    result["construction_pool_candidate_ids"] = [str(row["candidate_id"]) for row in conflict_pool]
    selected_ids = set(result["selected"])
    selected = [row for row in eligible if str(row["candidate_id"]) in selected_ids]
    snapshot = snapshot_with_hash(candidate_snapshot(candidates, config, eligibility, correlations))
    response = {
        **result,
        "configuration": config,
        "snapshot": snapshot,
        "eligible_count": len(eligible),
        "excluded_count": len(candidates) - len(eligible),
        "eligibility": eligibility,
        "conflicts": conflicts,
        "conflict_count_by_type": dict(sorted(Counter(row["conflict_type"] for row in conflicts).items())),
        "correlations": correlations,
        "binding_constraints": binding_constraints(eligibility, conflicts),
        "analytics": portfolio_analytics(selected, correlations),
        "selection_explanations": [{"candidate_id": candidate_id, "reason": "Selected by deterministic objective hierarchy."} for candidate_id in result["selected"]],
        "rejection_explanations": [{"candidate_id": row["candidate_id"], "reasons": row["reasons"]} for row in eligibility if not row["eligible"]],
    }
    response["timing"] = {
        "eligibility_ms": eligibility_ms,
        "conflicts_ms": conflict_ms,
        "solver_ms": result["optimization_duration_ms"],
        "end_to_end_ms": round((time.perf_counter() - total_started) * 1000, 3),
    }
    response["response_size_bytes"] = len(canonical_json(response).encode("utf-8"))
    return response


def _distribution(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get(field) or "unknown") for row in rows).items()))


def _peak_memory_mb() -> float | None:
    if resource is None:
        return None
    try:
        usage = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        # Linux reports KiB and macOS reports bytes. Windows may not expose it.
        return round(usage / (1024 * 1024 if usage > 10_000_000 else 1024), 3)
    except (AttributeError, ValueError):
        return None
