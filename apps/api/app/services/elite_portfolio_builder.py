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

# Bounds for the exact feasibility verifier (see `verify_feasibility`). Pools at
# or below this size get a deterministic exhaustive answer; larger pools skip
# exact verification rather than risk an unbounded search, and the response
# says so explicitly instead of silently trusting the bounded greedy result.
EXACT_VERIFICATION_POOL_LIMIT = 40
EXACT_VERIFICATION_NODE_BUDGET = 250_000
EXACT_VERIFICATION_TIME_BUDGET_SECONDS = 3.0

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
    "exact_verification_pool_limit": EXACT_VERIFICATION_POOL_LIMIT,
    "exact_verification_node_budget": EXACT_VERIFICATION_NODE_BUDGET,
    "exact_verification_time_budget_seconds": EXACT_VERIFICATION_TIME_BUDGET_SECONDS,
}

# Every hard rule the constructor and verifier enforce. Surfaced through
# `options()` so the UI shows the complete rule set instead of a partial one
# (in particular, symbol-family uniqueness must not stay hidden behind the
# separate per-symbol/per-family caps -- it is a stricter, independent rule).
HARD_RULES: list[dict[str, Any]] = [
    {
        "id": "SYMBOL_FAMILY_DUPLICATE",
        "label": "One member per symbol-family pair",
        "description": "At most one candidate may occupy a given (symbol, family) pair, even when the per-symbol and per-family caps would otherwise allow two.",
    },
    {
        "id": "PARAMETER_SIMILARITY",
        "label": "Maximum parameter similarity 0.90",
        "description": "Two candidates whose strategy parameters are more than 90% similar can never appear in the same portfolio.",
    },
    {
        "id": "SIGNAL_CORRELATION_LIMIT",
        "label": "Signal-correlation conflict rule",
        "description": "Candidates whose signal-exposure series correlate above the configured signal-correlation limit are a hard conflict.",
    },
    {
        "id": "STRATEGY_RETURN_CORRELATION_LIMIT",
        "label": "Strategy-return-correlation rule",
        "description": "Candidates whose strategy-return series correlate above the configured strategy-return-correlation limit are a hard conflict.",
    },
    {
        "id": "TIMEFRAME_50_PERCENT_CAP",
        "label": "Exact timeframe balance (2 x count <= total)",
        "description": "No single timeframe may exceed half the portfolio. With exactly two timeframes selected this forces an exact 50/50 split, which is only reachable at even portfolio sizes.",
    },
]

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


def candidate_key(candidate: dict[str, Any]) -> str:
    """Identify a deployable strategy-market variant immutably."""
    return str(candidate.get("candidate_key") or candidate["candidate_id"])


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
        "candidate_key": candidate_key(candidate),
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
    frozen_candidates = sorted((immutable_candidate(row) for row in candidates), key=lambda row: row["candidate_key"])
    return {
        "solver_version": SOLVER_VERSION,
        "configuration": normalized_configuration(configuration),
        "candidate_evidence": frozen_candidates,
        "eligibility_decisions": sorted(eligibility, key=lambda row: row["candidate_key"]),
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
    for candidate in sorted(candidates, key=candidate_key):
        reasons = eligibility_reasons(candidate, configuration)
        decisions.append({
            "candidate_key": candidate_key(candidate),
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


def parameter_similarity_breakdown(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Explain, key by key, how similar two candidates' strategy parameters are.

    Only the `parameters` dict itself is compared -- metadata such as symbol,
    timeframe, or family_id never enters this calculation, so two timeframe
    variants of the same family are only flagged as near-duplicates when they
    were actually assigned near-identical parameter values, not merely for
    sharing a family. A parameter missing on one side is treated as fully
    dissimilar (key_similarity 0.0) rather than silently ignored, so a partial
    parameter set can't understate how different two candidates are.
    """
    keys = sorted(set(left).union(right))
    per_parameter: list[dict[str, Any]] = []
    similarities: list[float] = []
    for key in keys:
        missing = key not in left or key not in right
        a, b = left.get(key), right.get(key)
        if not missing and isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool):
            scale = max(abs(float(a)), abs(float(b)), 1.0)
            normalized_difference = abs(float(a) - float(b)) / scale
            key_similarity = max(0.0, 1.0 - normalized_difference)
            comparable = True
        else:
            normalized_difference = None
            comparable = not missing
            key_similarity = 1.0 if (not missing and a == b) else 0.0
        per_parameter.append({
            "parameter": key,
            "left_value": a,
            "right_value": b,
            "comparable": comparable,
            "missing_on_one_side": missing,
            "normalized_difference": normalized_difference,
            "key_similarity": round(key_similarity, 6),
        })
        similarities.append(key_similarity)
    overall = sum(similarities) / len(similarities) if similarities else 1.0
    return {"overall_similarity": round(overall, 6), "compared_parameter_count": len(keys), "per_parameter": per_parameter}


def parameter_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    return parameter_similarity_breakdown(left, right)["overall_similarity"]


def parameter_similarity_reason(breakdown: dict[str, Any], threshold: float) -> str:
    parts = []
    for row in breakdown["per_parameter"]:
        if row["missing_on_one_side"]:
            parts.append(f"{row['parameter']}=missing on one side")
        elif row["normalized_difference"] is not None:
            parts.append(f"{row['parameter']} normalized diff={row['normalized_difference']:.4f}")
        else:
            parts.append(f"{row['parameter']}={'match' if row['key_similarity'] == 1.0 else 'mismatch'}")
    detail = "; ".join(parts) if parts else "no comparable parameters"
    return (
        f"Averaged similarity {breakdown['overall_similarity']:.4f} across "
        f"{breakdown['compared_parameter_count']} compared parameter(s) ({detail}) "
        f"exceeded the {threshold:.2f} maximum-similarity threshold."
    )


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
    ordered = sorted(candidates, key=candidate_key)
    for index, left in enumerate(ordered):
        for right in ordered[index + 1:]:
            left_id, right_id = candidate_key(left), candidate_key(right)
            pair_reasons: list[tuple[str, dict[str, Any]]] = []
            if str(left.get("symbol")) == str(right.get("symbol")) and str(left.get("family_id")) == str(right.get("family_id")):
                pair_reasons.append(("SYMBOL_FAMILY_DUPLICATE", {
                    "symbol": str(left.get("symbol")),
                    "family_id": str(left.get("family_id")),
                    "reason": (
                        f"Both candidates trade {left.get('symbol')} within family {left.get('family_id')}; "
                        "at most one member per symbol-family pair is allowed, regardless of the separate "
                        "per-symbol and per-family caps."
                    ),
                }))
            similarity_threshold = float(constraints["maximum_parameter_similarity"])
            breakdown = parameter_similarity_breakdown(left.get("parameters") or {}, right.get("parameters") or {})
            if breakdown["overall_similarity"] > similarity_threshold:
                pair_reasons.append(("PARAMETER_SIMILARITY", {
                    "coefficient": breakdown["overall_similarity"],
                    "threshold": similarity_threshold,
                    "compared_parameter_count": breakdown["compared_parameter_count"],
                    "compared_parameters": breakdown["per_parameter"],
                    "reason": parameter_similarity_reason(breakdown, similarity_threshold),
                }))
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
            candidate_key(row),
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
    ids = [candidate_key(row) for row in selected]
    if any(frozenset((left, right)) in conflicts for index, left in enumerate(ids) for right in ids[index + 1:]):
        reasons.append("PAIRWISE_HARD_CONFLICT")
    return reasons


def _can_add(selected: list[dict[str, Any]], candidate: dict[str, Any], target: int, constraints: dict[str, Any], conflicts: set[frozenset[str]]) -> bool:
    candidate_id = candidate_key(candidate)
    if any(frozenset((candidate_id, candidate_key(row))) in conflicts for row in selected):
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
        tuple(sorted(candidate_key(row) for row in selected)),
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
                operations.append({"operation": "select", "target_size": target, "seed": seed and candidate_key(seed)})
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
                        operations.append({"operation": "swap", "removed": candidate_key(current), "added": candidate_key(replacement)})
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
        "selected": [candidate_key(row) for row in candidate_order(best, objective)],
        "maximum_feasible_size": len(best),
        "constraint_relaxations": [],
        "constraint_relaxation_count": 0,
        "candidate_order": [candidate_key(row) for row in ordered],
        "iterations": iterations,
        "operations": operations,
        "swap_count": swaps,
        "termination_reason": termination,
        "objective_hierarchy": list(OBJECTIVE_HIERARCHY),
        "optimization_duration_ms": duration_ms,
        "candidates_examined": len(ordered),
        "peak_memory_mb": _peak_memory_mb(),
    }


def _conflict_adjacency(pool: list[dict[str, Any]], conflicts: list[dict[str, Any]]) -> list[int]:
    """Bitmask adjacency over `pool` positions for every hard pairwise conflict."""
    index_of = {candidate_key(row): position for position, row in enumerate(pool)}
    adjacency = [0] * len(pool)
    for row in conflicts:
        if not row.get("hard_conflict", True):
            continue
        left_index = index_of.get(row["left_candidate_id"])
        right_index = index_of.get(row["right_candidate_id"])
        if left_index is None or right_index is None or left_index == right_index:
            continue
        adjacency[left_index] |= 1 << right_index
        adjacency[right_index] |= 1 << left_index
    return adjacency


def _search_feasible_subset(
    ordered: list[dict[str, Any]],
    adjacency: list[int],
    target: int,
    constraints: dict[str, Any],
    deadline: float,
    node_budget: int,
) -> tuple[list[int] | None, int, bool]:
    """Deterministic exhaustive search for one feasible subset of exactly `target` members.

    Branch-and-bound over inclusion/exclusion of each candidate in `ordered`
    (already objective-ranked, so a found witness tends to prefer higher
    quality members without this being an optimality guarantee). Pruned on:
    remaining-candidate count, per-symbol/per-family caps, and the running
    timeframe-balance bound -- all monotonic, so pruning never discards a
    feasible branch. Diversity minimums (assets/families/timeframes) can only
    be confirmed once a full-size subset is assembled. Bounded by a node and
    wall-clock budget so a pathological instance fails loud (`truncated`)
    instead of hanging or lying about infeasibility.
    """
    n = len(ordered)
    nodes = 0
    truncated = False
    minimum_unique_assets = int(constraints["minimum_unique_assets"])
    minimum_families = int(constraints["minimum_families"])
    minimum_timeframes = int(constraints["minimum_timeframes"])
    maximum_per_symbol = int(constraints["maximum_per_symbol"])
    maximum_per_family = int(constraints["maximum_per_family"])

    def recurse(index: int, mask: int, selected: list[int], symbol_counts: dict[str, int], family_counts: dict[str, int], timeframe_counts: dict[str, int]) -> list[int] | None:
        nonlocal nodes, truncated
        if truncated:
            return None
        nodes += 1
        if nodes > node_budget or time.perf_counter() > deadline:
            truncated = True
            return None
        if len(selected) == target:
            symbols = {str(ordered[i].get("symbol")) for i in selected}
            families = {str(ordered[i].get("family_id")) for i in selected}
            timeframes = {str(ordered[i].get("timeframe")) for i in selected}
            if len(symbols) >= minimum_unique_assets and len(families) >= minimum_families and len(timeframes) >= minimum_timeframes:
                return list(selected)
            return None
        if index >= n or len(selected) + (n - index) < target:
            return None
        candidate = ordered[index]
        if not (adjacency[index] & mask):
            symbol = str(candidate.get("symbol"))
            family = str(candidate.get("family_id"))
            timeframe = str(candidate.get("timeframe"))
            if (
                symbol_counts.get(symbol, 0) < maximum_per_symbol
                and family_counts.get(family, 0) < maximum_per_family
                and 2 * (timeframe_counts.get(timeframe, 0) + 1) <= target
            ):
                symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
                family_counts[family] = family_counts.get(family, 0) + 1
                timeframe_counts[timeframe] = timeframe_counts.get(timeframe, 0) + 1
                selected.append(index)
                result = recurse(index + 1, mask | (1 << index), selected, symbol_counts, family_counts, timeframe_counts)
                selected.pop()
                timeframe_counts[timeframe] -= 1
                family_counts[family] -= 1
                symbol_counts[symbol] -= 1
                if result is not None:
                    return result
        return recurse(index + 1, mask, selected, symbol_counts, family_counts, timeframe_counts)

    witness = recurse(0, 0, [], {}, {}, {})
    return witness, nodes, truncated


def verify_feasibility(pool: list[dict[str, Any]], conflicts: list[dict[str, Any]], configuration: dict[str, Any]) -> dict[str, Any]:
    """Exact, deterministic answer to "does a feasible portfolio exist" for `pool`.

    Unlike `construct_portfolio` (a bounded greedy heuristic that can miss a
    feasible combination), this exhaustively searches subset sizes from the
    configured maximum down to the minimum and returns the first (largest)
    size with a witness -- or a verified "no feasible size" result once every
    size in range has been exhausted. Pools above `exact_verification_pool_limit`
    are not searched (2^n is intractable); the response says so explicitly via
    `ran=False` rather than pretending a verified answer exists.
    """
    started = time.perf_counter()
    config = normalized_configuration(configuration)
    constraints = config["constraints"]
    pool_size = len(pool)
    limit = int(constraints["exact_verification_pool_limit"])
    if pool_size == 0 or pool_size > limit:
        return {
            "ran": False,
            "verified": False,
            "feasible": None,
            "maximum_feasible_size": None,
            "witness": None,
            "pool_size": pool_size,
            "verification_limit": limit,
            "nodes_explored": 0,
            "duration_ms": 0.0,
            "termination_reason": "empty_pool" if pool_size == 0 else "pool_exceeds_verification_limit",
        }
    node_budget = int(constraints["exact_verification_node_budget"])
    deadline = started + float(constraints["exact_verification_time_budget_seconds"])
    ordered = candidate_order(pool, config["objective"])
    adjacency = _conflict_adjacency(ordered, conflicts)
    maximum_target = min(int(constraints["maximum_portfolio_size"]), pool_size)
    minimum_target = int(constraints["minimum_portfolio_size"])
    if config["custom_size"] is not None:
        maximum_target = minimum_target = min(maximum_target, int(config["custom_size"]))
    total_nodes = 0
    for target in range(maximum_target, minimum_target - 1, -1):
        witness_indices, nodes, truncated = _search_feasible_subset(ordered, adjacency, target, constraints, deadline, max(0, node_budget - total_nodes))
        total_nodes += nodes
        if truncated:
            return {
                "ran": True,
                "verified": False,
                "feasible": None,
                "maximum_feasible_size": None,
                "witness": None,
                "pool_size": pool_size,
                "verification_limit": limit,
                "nodes_explored": total_nodes,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "termination_reason": "search_budget_exceeded",
            }
        if witness_indices is not None:
            return {
                "ran": True,
                "verified": True,
                "feasible": True,
                "maximum_feasible_size": target,
                "witness": [candidate_key(ordered[i]) for i in witness_indices],
                "pool_size": pool_size,
                "verification_limit": limit,
                "nodes_explored": total_nodes,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "termination_reason": "exact_search_found_maximum_feasible_size",
            }
    return {
        "ran": True,
        "verified": True,
        "feasible": False,
        "maximum_feasible_size": 0,
        "witness": None,
        "pool_size": pool_size,
        "verification_limit": limit,
        "nodes_explored": total_nodes,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "termination_reason": "exact_search_proved_no_feasible_portfolio",
    }


def maximum_independent_set(pool: list[dict[str, Any]], conflicts: list[dict[str, Any]], configuration: dict[str, Any]) -> dict[str, Any]:
    """Exact largest conflict-free subset, ignoring every other constraint.

    Used only for the feasibility report: it isolates how much of the
    infeasibility is attributable to pairwise conflicts (parameter similarity,
    correlation, symbol-family duplication) versus the separate diversity and
    timeframe-balance rules.
    """
    config = normalized_configuration(configuration)
    constraints = config["constraints"]
    limit = int(constraints["exact_verification_pool_limit"])
    if not pool or len(pool) > limit:
        return {"size": None, "witness": None, "verified": False}
    ordered = candidate_order(pool, config["objective"])
    adjacency = _conflict_adjacency(ordered, conflicts)
    n = len(ordered)
    node_budget = int(constraints["exact_verification_node_budget"])
    deadline = time.perf_counter() + float(constraints["exact_verification_time_budget_seconds"])
    nodes = 0
    truncated = False
    best: list[int] = []

    def recurse(index: int, mask: int, selected: list[int]) -> None:
        nonlocal nodes, truncated, best
        if truncated:
            return
        nodes += 1
        if nodes > node_budget or time.perf_counter() > deadline:
            truncated = True
            return
        if len(selected) > len(best):
            best = list(selected)
        if index >= n or len(selected) + (n - index) <= len(best):
            return
        if not (adjacency[index] & mask):
            selected.append(index)
            recurse(index + 1, mask | (1 << index), selected)
            selected.pop()
        recurse(index + 1, mask, selected)

    recurse(0, 0, [])
    if truncated:
        return {"size": None, "witness": None, "verified": False}
    return {"size": len(best), "witness": [candidate_key(ordered[i]) for i in best], "verified": True}


def construct_portfolio_verified(candidates: list[dict[str, Any]], conflicts: list[dict[str, Any]], configuration: dict[str, Any]) -> dict[str, Any]:
    """Run the bounded greedy constructor, then only trust an "infeasible" verdict once the exact verifier confirms it.

    If the greedy constructor comes back infeasible, this runs `verify_feasibility`
    on the same pool. Three outcomes:
      - verifier finds a feasible portfolio the greedy heuristic missed:
        promote the verifier's witness, flag `heuristic_miss=True`.
      - verifier exhaustively proves no feasible portfolio exists: keep the
        infeasible result, flag `verified_infeasible=True`.
      - verifier can't run or can't finish within budget (pool too large /
        search truncated): keep the greedy result as-is, unverified -- the
        response makes this explicit rather than silently upgrading a guess
        to a "proved" claim.
    """
    result = construct_portfolio(candidates, conflicts, configuration)
    verification = {
        "ran": False,
        "verified": False,
        "feasible": None,
        "maximum_feasible_size": None,
        "witness": None,
        "pool_size": len(candidates),
        "verification_limit": None,
        "nodes_explored": 0,
        "duration_ms": 0.0,
        "termination_reason": "not_required_greedy_found_a_portfolio",
    }
    heuristic_miss = False
    verified_infeasible = False
    if result["status"] == "infeasible":
        verification = verify_feasibility(candidates, conflicts, configuration)
        if verification["ran"] and verification["verified"]:
            if verification["feasible"]:
                heuristic_miss = True
                config = normalized_configuration(configuration)
                witness_keys = set(verification["witness"] or [])
                witness_rows = candidate_order([row for row in candidates if candidate_key(row) in witness_keys], config["objective"])
                result = {
                    **result,
                    "status": "review_ready",
                    "selected": [candidate_key(row) for row in witness_rows],
                    "maximum_feasible_size": len(witness_rows),
                    "termination_reason": "exact_verifier_recovered_heuristic_miss",
                }
            else:
                verified_infeasible = True
                result = {**result, "termination_reason": "exact_search_proved_no_feasible_portfolio"}
    result["heuristic_miss"] = heuristic_miss
    result["verified_infeasible"] = verified_infeasible
    result["verification"] = verification
    return result


def binding_constraints(eligibility: list[dict[str, Any]], conflicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for decision in eligibility:
        counts.update(decision.get("reasons") or [])
    counts.update(row["conflict_type"] for row in conflicts)
    return [{"constraint": key, "excluded_candidates_or_pairs": value} for key, value in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def feasibility_report(pool: list[dict[str, Any]], conflicts: list[dict[str, Any]], verification: dict[str, Any], configuration: dict[str, Any]) -> dict[str, Any]:
    """Full accounting of why a pool is or isn't feasible, for audit and UI display."""
    config = normalized_configuration(configuration)
    constraints = config["constraints"]
    pool_size = len(pool)
    conflict_type_counts = Counter(row["conflict_type"] for row in conflicts)
    unique_edges = {frozenset((row["left_candidate_id"], row["right_candidate_id"])) for row in conflicts}
    degree: Counter[str] = Counter()
    for edge in unique_edges:
        left, right = tuple(edge)
        degree[left] += 1
        degree[right] += 1
    symbols = sorted({str(row.get("symbol")) for row in pool})
    families = sorted({str(row.get("family_id")) for row in pool})
    timeframes = sorted({str(row.get("timeframe")) for row in pool})
    independent_set = maximum_independent_set(pool, conflicts, configuration)
    return {
        "pool_size": pool_size,
        "total_possible_pairs": pool_size * (pool_size - 1) // 2,
        "conflict_count_by_type": dict(sorted(conflict_type_counts.items())),
        "unique_conflict_edges": len(unique_edges),
        "candidate_conflict_degree": dict(sorted(degree.items(), key=lambda item: (-item[1], item[0]))),
        "available_symbols": symbols,
        "available_families": families,
        "available_timeframes": timeframes,
        "symbol_count": len(symbols),
        "family_count": len(families),
        "timeframe_count": len(timeframes),
        "maximum_independent_set_size": independent_set["size"],
        "maximum_independent_set_witness": independent_set["witness"],
        "maximum_independent_set_verified": independent_set["verified"],
        "maximum_feasible_size_after_all_constraints": verification.get("maximum_feasible_size"),
        "minimum_unique_assets_independently_achievable": len(symbols) >= int(constraints["minimum_unique_assets"]),
        "minimum_families_independently_achievable": len(families) >= int(constraints["minimum_families"]),
        "exact_timeframe_balance_achievable": bool(verification.get("feasible")) if verification.get("verified") else None,
        "greedy_missed_a_valid_solution": bool(verification.get("verified")) and bool(verification.get("feasible")),
        "verification_ran": bool(verification.get("ran")),
        "verification_verified": bool(verification.get("verified")),
    }


def portfolio_analytics(selected: list[dict[str, Any]], correlations: list[dict[str, Any]]) -> dict[str, Any]:
    ids = {candidate_key(row) for row in selected}
    coefficients = [abs(float(row["coefficient"])) for row in correlations if row.get("coefficient") is not None and row["left_candidate_id"] in ids and row["right_candidate_id"] in ids and row["correlation_type"] == "strategy_return"]
    expectancies = [float(row.get("expectancy") or 0) for row in selected]
    profit_factors = [float(row.get("profit_factor") or 0) for row in selected]
    drawdowns = [float(row.get("max_drawdown") or 0) for row in selected]
    contributions = [{"candidate_key": candidate_key(row), "candidate_id": row["candidate_id"], "expectancy": float(row.get("expectancy") or 0), "quality_score": candidate_quality(row, "balanced")} for row in selected]
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
    result = construct_portfolio_verified(conflict_pool, conflicts, config)
    result["candidates_examined"] = len(eligible)
    result["construction_pool_count"] = len(conflict_pool)
    result["construction_pool_candidate_ids"] = [candidate_key(row) for row in conflict_pool]
    selected_ids = set(result["selected"])
    selected = [row for row in eligible if candidate_key(row) in selected_ids]
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
        "feasibility_report": feasibility_report(conflict_pool, conflicts, result["verification"], config),
        "hard_rules": deepcopy(HARD_RULES),
        "analytics": portfolio_analytics(selected, correlations),
        "selection_explanations": [{"candidate_id": candidate_id, "reason": "Selected by deterministic objective hierarchy."} for candidate_id in result["selected"]],
        "rejection_explanations": [{"candidate_key": row["candidate_key"], "candidate_id": row["candidate_id"], "reasons": row["reasons"]} for row in eligibility if not row["eligible"]],
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
