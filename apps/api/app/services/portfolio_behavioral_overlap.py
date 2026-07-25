"""Phase 13.8: behavioral overlap analysis for the Elite Portfolio Builder.

The existing builder (`elite_portfolio_builder.py`) already enforces
correlation, parameter-similarity, symbol, family, and timeframe constraints
with a deterministic solver, snapshots, and binding-constraint reporting.
This module ADDS the behavioral dimensions Phase 13 introduced -- Strategy
DNA similarity, holding-period overlap, regime overlap, signal-time overlap,
direction, and external-execution eligibility -- as an analysis layer.

**No existing constraint is weakened, relaxed, or bypassed.** Nothing here
removes a conflict or admits a candidate the solver rejected. Behavioral
overlap is reported alongside the solver's own decisions and can only ever
tighten a human's judgment, never loosen the machine's. `advisory_only=True`
is returned on every payload to make that contract explicit.

Two candidates can be uncorrelated in returns yet behaviorally identical
(same entry structure, same regime dependence, same session window) -- that
is a concentration risk return correlation alone will not show, which is
exactly why DNA similarity is measured separately from both return
correlation and parameter similarity.
"""

from __future__ import annotations

from statistics import fmean
from typing import Any

import psycopg

BEHAVIORAL_OVERLAP_VERSION = "portfolio_behavioral_overlap_v1"

# A pair at or above this DNA similarity is flagged as behaviorally
# redundant. Advisory only -- it never removes a candidate.
BEHAVIORAL_REDUNDANCY_THRESHOLD = 0.85
# A portfolio whose mean pairwise DNA similarity is at or above this is
# flagged as behaviorally concentrated overall.
PORTFOLIO_CONCENTRATION_THRESHOLD = 0.7

OVERLAP_DIMENSIONS = (
    "dna_similarity",
    "family_overlap",
    "symbol_overlap",
    "timeframe_overlap",
    "direction_overlap",
    "holding_horizon_overlap",
    "session_window_overlap",
    "regime_overlap",
    "behavior_class_overlap",
    "external_execution_eligibility",
)


def _dna_for(conn: psycopg.Connection, architecture: str | None) -> dict[str, Any] | None:
    if not architecture:
        return None
    from app.services.strategy_dna import get_strategy_dna

    record = get_strategy_dna(conn, architecture)
    return record["dna"] if record else None


def _architecture_of(candidate: dict[str, Any]) -> str | None:
    parameters = candidate.get("parameters") or {}
    return parameters.get("strategy_architecture") or candidate.get("family_id")


def _overlap(a: Any, b: Any) -> bool:
    return a is not None and b is not None and a == b


def _list_overlap(a: Any, b: Any) -> list[str]:
    if not isinstance(a, list) or not isinstance(b, list):
        return []
    return sorted(set(a) & set(b))


def pair_behavioral_overlap(
    conn: psycopg.Connection,
    left: dict[str, Any],
    right: dict[str, Any],
) -> dict[str, Any]:
    """Overlap across every Phase 13.8 dimension for one candidate pair.

    Returns `dna_similarity: None` when either side has no DNA record --
    an unmeasured pair is reported as unmeasured, never as diverse.
    """
    from app.services.strategy_dna import behavioral_similarity

    left_architecture = _architecture_of(left)
    right_architecture = _architecture_of(right)
    left_dna = _dna_for(conn, left_architecture)
    right_dna = _dna_for(conn, right_architecture)

    similarity = None
    if left_dna and right_dna:
        similarity = behavioral_similarity(left_dna, right_dna)

    left_parameters = left.get("parameters") or {}
    right_parameters = right.get("parameters") or {}

    overlaps = {
        "dna_similarity": similarity,
        "family_overlap": _overlap(left_architecture, right_architecture),
        "symbol_overlap": _overlap(left.get("symbol"), right.get("symbol")),
        "timeframe_overlap": _overlap(left.get("timeframe"), right.get("timeframe")),
        "direction_overlap": _overlap(
            left_parameters.get("direction") or left.get("strategy_direction"),
            right_parameters.get("direction") or right.get("strategy_direction"),
        ),
        "holding_horizon_overlap": _overlap(
            (left_dna or {}).get("holding_horizon_class"), (right_dna or {}).get("holding_horizon_class")
        ),
        "session_window_overlap": _overlap(
            (left_dna or {}).get("session_dependency"), (right_dna or {}).get("session_dependency")
        ),
        "regime_overlap": _list_overlap((left_dna or {}).get("required_regime"), (right_dna or {}).get("required_regime")),
        "behavior_class_overlap": _overlap(
            (left_dna or {}).get("behavior_class"), (right_dna or {}).get("behavior_class")
        ),
        "external_execution_eligibility": {
            "left": (left_dna or {}).get("execution_capability", "unknown"),
            "right": (right_dna or {}).get("execution_capability", "unknown"),
            "both_externally_eligible": (
                (left_dna or {}).get("execution_capability") == "external_paper_long_only"
                and (right_dna or {}).get("execution_capability") == "external_paper_long_only"
            ),
        },
    }

    redundant = similarity is not None and similarity >= BEHAVIORAL_REDUNDANCY_THRESHOLD
    notes = []
    if redundant:
        notes.append(
            f"DNA similarity {similarity:.2f} at or above the {BEHAVIORAL_REDUNDANCY_THRESHOLD} redundancy "
            "threshold: these behave alike even if their returns are not correlated."
        )
    if similarity is None:
        notes.append(
            "DNA similarity unmeasured (one or both candidates have no Strategy DNA record); "
            "treat as unknown, not as diverse."
        )
    if overlaps["session_window_overlap"] and overlaps["behavior_class_overlap"]:
        notes.append("Same session window and same behavior class: likely to fire on the same conditions.")

    return {
        "left": left.get("candidate_id"),
        "right": right.get("candidate_id"),
        "overlaps": overlaps,
        "behaviorally_redundant": redundant,
        "notes": notes,
    }


def portfolio_behavioral_diversity(
    conn: psycopg.Connection,
    members: list[dict[str, Any]],
) -> dict[str, Any]:
    """Behavioral diversity assessment for a proposed portfolio.

    Advisory: it never alters membership. Its purpose is to answer "is this
    portfolio actually diverse, or merely uncorrelated?".
    """
    pairs = []
    for index, left in enumerate(members):
        for right in members[index + 1 :]:
            pairs.append(pair_behavioral_overlap(conn, left, right))

    measured = [pair["overlaps"]["dna_similarity"] for pair in pairs if pair["overlaps"]["dna_similarity"] is not None]
    mean_similarity = round(fmean(measured), 4) if measured else None
    redundant_pairs = [pair for pair in pairs if pair["behaviorally_redundant"]]

    architectures = [_architecture_of(member) for member in members]
    distinct_architectures = len({a for a in architectures if a})

    behavior_classes = set()
    session_windows = set()
    for member in members:
        dna = _dna_for(conn, _architecture_of(member))
        if dna:
            behavior_classes.add(dna.get("behavior_class"))
            session_windows.add(dna.get("session_dependency"))

    concentrated = mean_similarity is not None and mean_similarity >= PORTFOLIO_CONCENTRATION_THRESHOLD

    assessment = "unmeasured"
    if mean_similarity is not None:
        if concentrated:
            assessment = "behaviorally_concentrated"
        elif mean_similarity < 0.5:
            assessment = "behaviorally_diverse"
        else:
            assessment = "moderately_diverse"

    return {
        "behavioral_overlap_version": BEHAVIORAL_OVERLAP_VERSION,
        "advisory_only": True,
        "member_count": len(members),
        "distinct_architectures": distinct_architectures,
        "distinct_behavior_classes": sorted(c for c in behavior_classes if c),
        "distinct_session_windows": sorted(w for w in session_windows if w),
        "mean_pairwise_dna_similarity": mean_similarity,
        "measured_pairs": len(measured),
        "unmeasured_pairs": len(pairs) - len(measured),
        "behaviorally_redundant_pairs": redundant_pairs,
        "assessment": assessment,
        "dimensions_evaluated": list(OVERLAP_DIMENSIONS),
        "constraint_note": (
            "Advisory analysis only. No portfolio constraint is weakened, relaxed, or bypassed by "
            "this module; it cannot admit a candidate the solver rejected."
        ),
    }


def explain_infeasibility(
    feasibility: dict[str, Any],
    binding: list[dict[str, Any]],
    *,
    target_size: int,
) -> dict[str, Any]:
    """Turn the solver's own feasibility output into an explicit account of
    what blocked the portfolio and which relaxations a HUMAN could consider.

    Suggested relaxations are surfaced for review; nothing here applies one.
    """
    achieved = int(feasibility.get("maximum_feasible_size") or 0)
    infeasible = achieved < target_size

    blocking = sorted(binding, key=lambda row: -int(row.get("excluded_candidates_or_pairs") or 0))
    primary = blocking[0]["constraint"] if blocking else None

    alternatives: list[dict[str, Any]] = []
    if infeasible:
        if achieved > 0:
            alternatives.append(
                {
                    "profile": "smaller_portfolio",
                    "description": f"A portfolio of {achieved} members is feasible under the unchanged constraints.",
                    "requires_constraint_change": False,
                }
            )
        for row in blocking[:3]:
            alternatives.append(
                {
                    "profile": f"review_{row['constraint'].lower()}",
                    "description": (
                        f"{row['constraint']} excluded {row['excluded_candidates_or_pairs']} candidates or pairs. "
                        "A human may review whether that threshold is right for this research question."
                    ),
                    "requires_constraint_change": True,
                }
            )
        alternatives.append(
            {
                "profile": "widen_candidate_pool",
                "description": "Add more elite candidates from other families so the solver has more diverse material.",
                "requires_constraint_change": False,
            }
        )

    return {
        "feasible": not infeasible,
        "target_size": target_size,
        "maximum_feasible_size": achieved,
        "primary_blocking_constraint": primary,
        "blocking_constraints": blocking,
        "alternative_profiles": alternatives,
        "constraint_note": (
            "No constraint was changed to produce this report. Any option marked "
            "requires_constraint_change is a suggestion for human review, not an applied change."
        ),
    }
