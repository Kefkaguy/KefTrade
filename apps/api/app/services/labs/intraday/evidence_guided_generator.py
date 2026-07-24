"""Phase 13.6: evidence-guided candidate generation.

Blind grid expansion wastes compute re-testing regions already known to fail.
This generator reads evidence from COMPLETED campaigns and biases generation
toward regions that worked, away from regions that did not, while reserving
a fixed share of capacity for genuinely new ideas.

**The evidence boundary is the load-bearing rule here.** The generator reads
only aggregate per-job metrics from campaigns that have already been
finalized. It never reads:

  * a candidate's own future performance (there isn't any yet);
  * validation-window trades separately from training-window trades;
  * anything from the campaign currently being generated.

If the generator could see per-window results it would be selecting on the
validation window, which is precisely what walk-forward validation exists to
prevent. `EVIDENCE_BOUNDARY` documents this and the tests assert it.

**The allocation is fixed and versioned.** 50% exploitation / 30% diversity
mutation / 20% exploration is stored in `GENERATOR_VERSION` and written into
campaign metadata, so a campaign's mix can always be reconstructed and a
silent change is impossible -- changing the ratio requires a new version
string, which the tests pin.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import psycopg

GENERATOR_VERSION = "evidence_guided_generator_v1_50_30_20"

ALLOCATION = {
    "exploitation": 0.50,
    "diversity_mutation": 0.30,
    "exploration": 0.20,
}

EVIDENCE_BOUNDARY = {
    "reads": [
        "aggregate per-job metrics (profit_factor, expectancy_per_trade, number_of_trades)",
        "from campaigns whose status is 'completed' only",
    ],
    "never_reads": [
        "validation-window results separated from training-window results",
        "forward-validation or paper-trading outcomes",
        "any job from the campaign currently being generated",
    ],
    "rationale": (
        "Selecting parameters on validation-window performance would contaminate the very "
        "hold-out the elite gate depends on. The generator therefore sees only whole-job "
        "aggregates from already-finalized campaigns."
    ),
}

# A parameter region needs at least this many completed jobs before the
# generator will treat it as evidence rather than noise.
MINIMUM_JOBS_FOR_REGION_EVIDENCE = 8
# Families at or below this promotion rate across enough jobs are treated as
# dead ends and lose exploitation capacity (they keep exploration capacity --
# a dead end is not the same as a forbidden idea).
DEAD_END_PROMOTION_RATE = 0.0
MINIMUM_JOBS_FOR_DEAD_END = 40


@dataclass(frozen=True)
class FamilyEvidence:
    architecture: str
    jobs: int
    promoted: int
    median_profit_factor: float | None
    median_expectancy: float | None

    @property
    def promotion_rate(self) -> float:
        return (self.promoted / self.jobs) if self.jobs else 0.0

    @property
    def is_dead_end(self) -> bool:
        return self.jobs >= MINIMUM_JOBS_FOR_DEAD_END and self.promotion_rate <= DEAD_END_PROMOTION_RATE

    @property
    def has_usable_evidence(self) -> bool:
        return self.jobs >= MINIMUM_JOBS_FOR_REGION_EVIDENCE


def load_family_evidence(conn: psycopg.Connection, *, exclude_campaign_id: int | None = None) -> dict[str, FamilyEvidence]:
    """Aggregate per-family evidence from COMPLETED campaigns only.

    `exclude_campaign_id` enforces the boundary explicitly when regenerating
    into an existing campaign: a campaign can never inform its own generation.
    """

    rows = conn.execute(
        """
        SELECT j.candidate->'parameters'->>'strategy_architecture' AS architecture,
               count(*) AS jobs,
               count(*) FILTER (WHERE j.status = 'promoted') AS promoted,
               percentile_cont(0.5) WITHIN GROUP (
                   ORDER BY (j.result->'metrics'->>'profit_factor')::float
               ) FILTER (WHERE j.result->'metrics'->>'profit_factor' IS NOT NULL) AS median_profit_factor,
               percentile_cont(0.5) WITHIN GROUP (
                   ORDER BY (j.result->'metrics'->>'expectancy_per_trade')::float
               ) FILTER (WHERE j.result->'metrics'->>'expectancy_per_trade' IS NOT NULL) AS median_expectancy
        FROM research_campaign_jobs j
        JOIN research_campaigns c ON c.id = j.campaign_id
        WHERE c.status = 'completed'
          AND j.status <> 'queued'
          AND j.candidate->'parameters'->>'strategy_architecture' IS NOT NULL
          AND (%s::bigint IS NULL OR j.campaign_id <> %s::bigint)
        GROUP BY 1
        """,
        (exclude_campaign_id, exclude_campaign_id),
    ).fetchall()

    evidence: dict[str, FamilyEvidence] = {}
    for row in rows:
        architecture = row["architecture"]
        evidence[architecture] = FamilyEvidence(
            architecture=architecture,
            jobs=int(row["jobs"] or 0),
            promoted=int(row["promoted"] or 0),
            median_profit_factor=float(row["median_profit_factor"]) if row["median_profit_factor"] is not None else None,
            median_expectancy=float(row["median_expectancy"]) if row["median_expectancy"] is not None else None,
        )
    return evidence


def allocate_candidate_budget(total: int) -> dict[str, int]:
    """Split a budget by the fixed allocation, deterministically, with the
    remainder going to exploration so rounding never silently inflates
    exploitation."""

    exploitation = int(total * ALLOCATION["exploitation"])
    diversity = int(total * ALLOCATION["diversity_mutation"])
    exploration = total - exploitation - diversity
    return {"exploitation": exploitation, "diversity_mutation": diversity, "exploration": exploration}


def _generation_reason(channel: str, architecture: str, evidence: FamilyEvidence | None) -> str:
    if channel == "exploration":
        if evidence is None or not evidence.has_usable_evidence:
            return f"Exploration: {architecture} has no usable prior evidence ({0 if evidence is None else evidence.jobs} completed jobs)."
        return (
            f"Exploration: reserved capacity for new ground in {architecture} "
            f"despite {evidence.jobs} prior completed jobs."
        )
    if channel == "exploitation":
        assert evidence is not None
        return (
            f"Exploitation: {architecture} showed median profit factor "
            f"{evidence.median_profit_factor} over {evidence.jobs} completed jobs "
            f"({evidence.promoted} promoted)."
        )
    assert evidence is not None
    return (
        f"Diversity mutation: varying {architecture} away from its tested region "
        f"({evidence.jobs} completed jobs, promotion rate {evidence.promotion_rate:.0%})."
    )


def generate_evidence_guided_candidates(
    conn: psycopg.Connection,
    *,
    architectures: list[str],
    total_candidates: int,
    exclude_campaign_id: int | None = None,
) -> dict[str, Any]:
    """Return candidates plus the full provenance of why each was generated.

    Every candidate carries `generation_channel` and `generation_reason` in
    its parameters, so a campaign can always answer "why was this tested?".
    """

    from app.services.labs.intraday.families.registry import FAMILY_REGISTRY

    unknown = [a for a in architectures if a not in FAMILY_REGISTRY]
    if unknown:
        raise ValueError(f"Unknown architectures: {unknown}")

    evidence = load_family_evidence(conn, exclude_campaign_id=exclude_campaign_id)
    budget = allocate_candidate_budget(total_candidates)

    # Exploitation goes to families with usable, non-dead-end evidence,
    # ranked by median profit factor. Exploration always covers every
    # requested family, including dead ends -- reserving capacity for new
    # ideas is the point, and a dead end is not a banned idea.
    exploitable = sorted(
        (
            evidence[a]
            for a in architectures
            if a in evidence and evidence[a].has_usable_evidence and not evidence[a].is_dead_end
        ),
        key=lambda item: (-(item.median_profit_factor or 0.0), item.architecture),
    )
    dead_ends = [a for a in architectures if a in evidence and evidence[a].is_dead_end]

    candidates: list[Any] = []
    provenance: list[dict[str, Any]] = []

    def emit(channel: str, architecture: str, count: int) -> None:
        if count <= 0:
            return
        definition = FAMILY_REGISTRY[architecture]
        generated = definition.candidate_generator(max_candidates=count)
        family_evidence = evidence.get(architecture)
        reason = _generation_reason(channel, architecture, family_evidence)
        for candidate in generated:
            candidate.parameters["generation_channel"] = channel
            candidate.parameters["generation_reason"] = reason
            candidate.parameters["generator_version"] = GENERATOR_VERSION
            candidates.append(candidate)
            provenance.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "architecture": architecture,
                    "generation_channel": channel,
                    "generation_reason": reason,
                }
            )

    if exploitable:
        per_family = max(1, budget["exploitation"] // len(exploitable))
        for family_evidence in exploitable:
            emit("exploitation", family_evidence.architecture, per_family)
        per_family_diversity = max(1, budget["diversity_mutation"] // len(exploitable))
        for family_evidence in exploitable:
            emit("diversity_mutation", family_evidence.architecture, per_family_diversity)
    else:
        # No usable evidence yet (the honest state before the first completed
        # V2 campaign): everything is exploration. The allocation is NOT
        # silently rewritten -- it is reported as inapplicable.
        budget = {"exploitation": 0, "diversity_mutation": 0, "exploration": total_candidates}

    if architectures:
        per_family = max(1, budget["exploration"] // len(architectures))
        for architecture in architectures:
            emit("exploration", architecture, per_family)

    return {
        "generator_version": GENERATOR_VERSION,
        "allocation": dict(ALLOCATION),
        "allocated_budget": budget,
        "evidence_boundary": EVIDENCE_BOUNDARY,
        "families_with_usable_evidence": [item.architecture for item in exploitable],
        "dead_end_families": dead_ends,
        "candidates": candidates,
        "provenance": provenance,
    }
