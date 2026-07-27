"""The research funnel: screen families broadly, then spend compute narrowly.

A broad multi-family screen cannot produce elites, and should never have been
expected to. Campaign 101 ran 18 families as 120 candidates over 1200 jobs
and produced 120 `generated` but only 5 `research_candidate` rows.

The reason is not that candidates lacked symbol coverage -- at 1200 jobs for
120 candidates, each canonical candidate already ran ~10 jobs. It is that a
broad screen spends its budget on *breadth of families* rather than *depth
within one*: roughly 6-7 parameter points per family, spread across 18
families, most of which had no edge to find. Each individual job then has to
clear the per-job gate on one symbol's trades alone, and a shallow parameter
probe rarely lands on a region that both trades often enough and has an edge.
The gate rejects nearly everything, and that rejection says nothing about
whether any family was worth pursuing.

The fix is not a weaker gate. It is a funnel that spends the next campaign's
compute where the screen actually found signal, and that gives promising
candidates enough evidence *before* judging them:

    1. Broad screen        -- existing multi-family campaign
    2. Rank families       -- `rank_campaign_families` (here)
    3. Focused expansion   -- `create_focused_expansion_campaign` (here)
    4. Holdout confirmation-- `pooled_evidence.compute_holdout_confirmation`
    5. Promotion           -- the UNCHANGED elite gate, untouched

Stage 3 is what makes stage 5 reachable. Re-running only the families that
showed edge *and* trade frequency, at several times the parameter depth,
across the full asset universe, searches the regions where a variant that
clears the per-job gate on its own symbol plausibly exists -- instead of
spending 15 of every 18 job-slots on families the screen already showed had
nothing. Stage 4 then credits real cross-sectional breadth (pooled evidence)
and demands it survive untouched data.

Nothing here lowers a threshold and nothing here promotes anything:
screening only decides where to spend the next campaign's compute, and
promotion remains entirely the unchanged gate's decision.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg

FUNNEL_VERSION = "evidence_funnel_v1"

# Screening floors. These answer "is this family worth another campaign's
# compute?", NEVER "is this family good enough to trade?" -- they are
# deliberately far below the elite gate, because their whole purpose is to
# admit families whose evidence is still too thin to judge. Promotion
# remains the sole responsibility of the unchanged gate in
# research_campaigns.passes_cross_validation.
SCREEN_MINIMUM_PROFIT_FACTOR = 1.0
SCREEN_MINIMUM_SYMBOLS = 2

# Ranking weights. Edge dominates, but trade frequency carries real weight
# because trade starvation -- not absent edge -- is the documented reason
# intraday candidates fail the gate (see the Phase 12.4 failure analysis).
_WEIGHT_EDGE = 0.40
_WEIGHT_PROMOTION_RATE = 0.25
_WEIGHT_FREQUENCY = 0.25
_WEIGHT_BREADTH = 0.10

# A family whose average profit factor reaches this is at the top of the
# edge term; the cap keeps one lucky thin-sample family from dominating.
_EDGE_SATURATION_PROFIT_FACTOR = 3.0

# Trades per job at which the frequency term saturates. The per-job elite
# gate wants 30+ trades, so a family already averaging that is not
# trade-starved and gains nothing more from this term.
_FREQUENCY_SATURATION_TRADES_PER_JOB = 30.0


def _screen_score(row: dict[str, Any]) -> float:
    """Rank by promise per unit of evidence. Every term is bounded to 0..1 so
    no single dimension can dominate, and the weights are explicit above."""
    profit_factor = row.get("avg_profit_factor") or 0.0
    edge = min(1.0, max(0.0, profit_factor - 1.0) / (_EDGE_SATURATION_PROFIT_FACTOR - 1.0))
    promotion_rate = min(1.0, max(0.0, float(row.get("promotion_rate") or 0.0)))
    frequency = min(1.0, max(0.0, float(row.get("trades_per_job") or 0.0)) / _FREQUENCY_SATURATION_TRADES_PER_JOB)
    breadth = min(1.0, max(0.0, float(row.get("symbols") or 0)) / max(1.0, float(SCREEN_MINIMUM_SYMBOLS)))
    return round(
        edge * _WEIGHT_EDGE
        + promotion_rate * _WEIGHT_PROMOTION_RATE
        + frequency * _WEIGHT_FREQUENCY
        + breadth * _WEIGHT_BREADTH,
        6,
    )


def _exclusion_reasons(row: dict[str, Any], definition: Any) -> list[str]:
    """Every reason a family is not worth expanding (empty = worth expanding)."""
    reasons: list[str] = []
    if definition is None:
        reasons.append("UNKNOWN_FAMILY")
    elif definition.status != "active":
        # Phase 12.4 archived the six v1 families after finding no edge; the
        # standing instruction is to stop spending compute on them.
        reasons.append("ARCHIVED_FAMILY")
    if row.get("evidence_tier") == "insufficient_sample":
        reasons.append("INSUFFICIENT_SAMPLE")
    profit_factor = row.get("avg_profit_factor")
    if profit_factor is None or profit_factor < SCREEN_MINIMUM_PROFIT_FACTOR:
        reasons.append("NO_EDGE_SIGNAL")
    expectancy = row.get("avg_expectancy")
    if expectancy is None or expectancy <= 0:
        reasons.append("NON_POSITIVE_EXPECTANCY")
    if int(row.get("symbols") or 0) < SCREEN_MINIMUM_SYMBOLS:
        reasons.append("TOO_FEW_SYMBOLS")
    return reasons


def rank_campaign_families(conn: psycopg.Connection, campaign_id: int) -> list[dict[str, Any]]:
    """Rank a broad screen's families by how much promise their evidence shows.

    This is the output a broad campaign should be judged on -- not elite
    counts. Ranking never promotes and never writes; it reads the same
    stored job rows everything else does, via the existing
    `campaign_family_analytics`, and adds an explicit `promising` verdict
    with the reasons behind it.
    """
    from app.services.labs.intraday.families.registry import FAMILY_REGISTRY
    from app.services.labs.intraday.strategy_analytics import campaign_family_analytics

    ranked: list[dict[str, Any]] = []
    for row in campaign_family_analytics(conn, campaign_id):
        architecture = row.get("architecture")
        definition = FAMILY_REGISTRY.get(architecture) if architecture else None
        reasons = _exclusion_reasons(row, definition)
        ranked.append(
            {
                **row,
                "family_name": definition.name if definition else None,
                "family_status": definition.status if definition else None,
                "screen_score": _screen_score(row),
                "promising": not reasons,
                "exclusion_reasons": reasons,
                "funnel_version": FUNNEL_VERSION,
            }
        )
    return sorted(
        ranked,
        key=lambda item: (item["promising"], item["screen_score"], item["trades"]),
        reverse=True,
    )


def campaign_funnel_report(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    """Family ranking plus the explicit statement that a broad screen is not
    expected to yield elites -- so an empty elite count is not read as
    failure when the screen actually did its job."""
    ranked = rank_campaign_families(conn, campaign_id)
    promising = [row for row in ranked if row["promising"]]
    return {
        "campaign_id": campaign_id,
        "funnel_version": FUNNEL_VERSION,
        "families_screened": len(ranked),
        "families_promising": len(promising),
        "next_step": (
            "Run a focused multi-asset expansion over the promising families."
            if promising
            else "No family cleared the screening floor; widen the screen or revise the hypotheses."
        ),
        "screening_policy": (
            "A broad screen ranks families. It is not expected to produce elites, and its elite count "
            "is not a measure of its success. Promotion thresholds are unchanged and are applied only "
            "after a focused expansion has given a candidate enough evidence to be judged."
        ),
        "families": ranked,
    }


def create_focused_expansion_campaign(
    conn: psycopg.Connection,
    *,
    source_campaign_id: int,
    max_families: int = 3,
    candidates_per_family: int = 24,
    asset_limit: int = 10,
    timeframes: list[str] | None = None,
    name: str | None = None,
    hypothesis_version_id: int | None = None,
) -> dict[str, Any]:
    """Launch a focused campaign over the top-ranked families of a broad screen.

    Concentrating the same compute on fewer families across the *full* asset
    universe is what finally gives a canonical candidate the breadth the
    unchanged elite gate has always required. Deeper parameter grids come
    from each family's own registry generator -- the identical deterministic
    generator the broad screen used, simply not truncated as early -- so no
    new candidate-generation logic is introduced and the expansion cannot
    drift from the family it claims to expand.
    """
    from app.services.labs.intraday.families.registry import create_intraday_campaign

    ranked = rank_campaign_families(conn, source_campaign_id)
    if not ranked:
        raise ValueError(f"campaign {source_campaign_id} has no completed jobs to screen")

    winners = [row for row in ranked if row["promising"]][:max_families]
    if not winners:
        summary = "; ".join(
            f"{row.get('architecture')}: {', '.join(row['exclusion_reasons'])}" for row in ranked[:8]
        )
        raise ValueError(
            f"no family in campaign {source_campaign_id} cleared the screening floor -- {summary}"
        )

    family_ids = [str(row["architecture"]) for row in winners]
    family_names = ", ".join(str(row.get("family_name") or row["architecture"]) for row in winners)
    result = create_intraday_campaign(
        conn,
        family_ids=family_ids,
        name=name or f"Focused expansion of campaign {source_campaign_id}: {family_names}",
        asset_limit=asset_limit,
        timeframes=timeframes,
        max_candidates_per_family=candidates_per_family,
        # A unique label per launch: two expansions of the same winners must
        # not collide on campaign_key and silently reuse the earlier campaign.
        campaign_label=(
            f"{FUNNEL_VERSION}_src{source_campaign_id}_{len(family_ids)}f_"
            f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}_{uuid4().hex[:8]}"
        ),
        hypothesis_version_id=hypothesis_version_id,
    )
    result["funnel_version"] = FUNNEL_VERSION
    result["source_campaign_id"] = source_campaign_id
    result["expanded_families"] = [
        {
            "architecture": row["architecture"],
            "family_name": row.get("family_name"),
            "screen_score": row["screen_score"],
            "evidence_tier": row.get("evidence_tier"),
            "avg_profit_factor": row.get("avg_profit_factor"),
            "trades_per_job": row.get("trades_per_job"),
            "promotion_rate": row.get("promotion_rate"),
        }
        for row in winners
    ]
    result["families_screened"] = len(ranked)
    result["families_rejected"] = [
        {"architecture": row["architecture"], "exclusion_reasons": row["exclusion_reasons"]}
        for row in ranked
        if not row["promising"]
    ]
    return result
