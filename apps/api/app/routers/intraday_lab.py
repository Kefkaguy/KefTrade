from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
import psycopg

from app.db import get_connection
from app.services.labs.intraday.overview import intraday_lab_overview
from app.services.labs.intraday.phase_analysis import phase_12_4_report

router = APIRouter(tags=["intraday-lab"])


@router.get("/research/intraday/overview")
def get_intraday_lab_overview(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return intraday_lab_overview(conn)


@router.get("/research/intraday/phase-12-4")
def get_phase_12_4_analysis(
    campaign_id: int = Query(..., description="The Phase 12.4 trade-evidence campaign id to analyze (not Campaign 47 itself, which has no trade-level rows)."),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return phase_12_4_report(conn, campaign_id)


@router.post("/research/intraday/campaigns")
def create_intraday_campaign_endpoint(
    family_ids: list[str] = Query(..., description="One or more Intraday Lab family architecture ids to launch together."),
    name: str | None = Query(None),
    asset_limit: int = Query(10, ge=1, le=100),
    timeframes: list[str] | None = Query(None),
    max_candidates_per_family: int = Query(8, ge=1, le=64),
    campaign_label: str | None = Query(None, description="Optional label distinguishing this run from an earlier campaign over the same families/assets/timeframes (e.g. a versioned re-run)."),
    hypothesis_version_id: int | None = Query(None, description="Optional research_hypothesis_versions.id linking this campaign to a documented hypothesis."),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    from app.services.labs.intraday.families.registry import create_intraday_campaign

    try:
        return create_intraday_campaign(
            conn,
            family_ids=family_ids,
            name=name,
            asset_limit=asset_limit,
            timeframes=timeframes,
            max_candidates_per_family=max_candidates_per_family,
            campaign_label=campaign_label,
            hypothesis_version_id=hypothesis_version_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/research/intraday/campaigns/low-timeframe-expansion")
def create_low_timeframe_expansion_campaign_endpoint(
    name: str | None = Query(None),
    parent_limit: int = Query(12, ge=1, le=200),
    variants_per_parent: int = Query(8, ge=1, le=64),
    asset_limit: int = Query(8, ge=1, le=50),
    timeframes: list[str] | None = Query(None, description="Defaults to 30m. Pass 15m explicitly for the separate 15m lane."),
    preferred_family: str | None = Query("Momentum"),
    auto_start: bool = Query(True, description="Start durable simulation workers after the campaign is queued."),
    workers: int = Query(4, ge=1, le=8),
    jobs_per_worker: int = Query(25, ge=1, le=100),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    from app.services.labs.intraday.low_timeframe_expansion import create_low_timeframe_expansion_campaign
    from app.services.research_campaigns import run_parallel_campaign_batch

    try:
        result = create_low_timeframe_expansion_campaign(
            conn,
            name=name,
            parent_limit=parent_limit,
            variants_per_parent=variants_per_parent,
            asset_limit=asset_limit,
            timeframes=timeframes,
            preferred_family=preferred_family,
        )
        campaign_status = str((result.get("campaign") or {}).get("status") or "")
        if auto_start and campaign_status in {"queued", "running", "failed"}:
            result["execution"] = run_parallel_campaign_batch(
                conn,
                campaign_id=int(result["campaign_id"]),
                workers=workers,
                jobs_per_worker=jobs_per_worker,
            )
        elif auto_start:
            result["execution"] = {
                "skipped": True,
                "reason": f"Campaign is {campaign_status or 'unknown'} and cannot be started.",
            }
        return result
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/research/intraday/analytics/{campaign_id}")
def get_campaign_analytics(campaign_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    """Phase 13.5: campaign analytics with explicit evidence tiers. Every
    aggregate is computed from stored rows and carries its sample size; no
    causal feature importance is claimed."""
    from app.services.labs.intraday.strategy_analytics import campaign_analytics

    return campaign_analytics(conn, campaign_id)


@router.get("/research/intraday/evidence-report/{campaign_id}/{candidate_id}")
def get_candidate_evidence_report(
    campaign_id: int,
    candidate_id: str,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    """Phase 13.9: a candidate's full evidence report, built only from stored
    database rows and the family's declared hypothesis."""
    from app.services.labs.intraday.strategy_analytics import candidate_evidence_report

    report = candidate_evidence_report(conn, campaign_id, candidate_id)
    if "error" in report:
        raise HTTPException(status_code=404, detail=report["error"])
    return report


@router.get("/research/intraday/generator-plan")
def get_generator_plan(
    architectures: list[str] = Query(..., description="Family architectures to generate for."),
    total_candidates: int = Query(40, ge=1, le=500),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    """Phase 13.6: preview what the evidence-guided generator would produce,
    including why each candidate was chosen. Read-only -- creates nothing."""
    from app.services.labs.intraday.evidence_guided_generator import generate_evidence_guided_candidates

    try:
        plan = generate_evidence_guided_candidates(
            conn, architectures=architectures, total_candidates=total_candidates
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {key: value for key, value in plan.items() if key != "candidates"} | {
        "candidate_count": len(plan["candidates"])
    }


@router.get("/research/strategy-dna")
def list_strategy_dna_endpoint(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    """Phase 13.1: latest DNA row per family, plus the behavioral-similarity
    matrix. Behavioral similarity is deliberately separate from parameter
    similarity (`candidate_parameter_distance`) -- two families can share a
    parameter shape without behaving alike, and vice versa."""
    from app.services.strategy_dna import DNA_SCHEMA_VERSION, dna_similarity_matrix, list_strategy_dna

    records = list_strategy_dna(conn)
    return {
        "dna_schema_version": DNA_SCHEMA_VERSION,
        "families": records,
        "behavioral_similarity": dna_similarity_matrix(conn) if records else [],
    }


@router.get("/research/strategy-dna/{family_architecture}")
def get_strategy_dna_endpoint(family_architecture: str, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    from app.services.strategy_dna import get_strategy_dna

    record = get_strategy_dna(conn, family_architecture)
    if not record:
        raise HTTPException(status_code=404, detail=f"No Strategy DNA recorded for {family_architecture!r}")
    return record


@router.post("/research/strategy-dna/backfill")
def backfill_strategy_dna_endpoint(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    """Append-only and idempotent: writes a DNA row for any registered family
    that lacks one at the current schema version. Never updates or deletes an
    existing row, and never touches campaign/job/candidate evidence."""
    from app.services.strategy_dna import backfill_strategy_dna

    return backfill_strategy_dna(conn)


@router.post("/research/intraday/specialists")
def create_specialist_thread_endpoint(
    payload: dict[str, Any] = Body(...),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    """Freeze a narrow-but-real finding (e.g. AMD 30m long Session Momentum
    from Phase 12.4) as a specialist research thread. Never promotes,
    deploys, or launches any campaign -- see
    docs/2026-07-24-phase12-5-architecture-proposal.md section 5."""
    from app.services.labs.intraday.specialist import create_specialist_thread

    try:
        return create_specialist_thread(
            conn,
            thread_key=payload["thread_key"],
            title=payload["title"],
            origin_candidate_id=payload["origin_candidate_id"],
            frozen_parameters=payload["frozen_parameters"],
            scope_timeframe=payload["scope_timeframe"],
            scope_direction=payload["scope_direction"],
            origin_campaign_id=payload.get("origin_campaign_id"),
            scope_symbols=payload.get("scope_symbols"),
            hypothesis_version_id=payload.get("hypothesis_version_id"),
            strategy_version=payload.get("strategy_version"),
            strategy_architecture=payload.get("strategy_architecture"),
            dna_fingerprint=payload.get("dna_fingerprint"),
            dataset_snapshot_id=payload.get("dataset_snapshot_id"),
        )
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/research/intraday/specialists/{thread_key}")
def get_specialist_thread_endpoint(thread_key: str, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    from app.services.labs.intraday.specialist import get_specialist_thread, list_specialist_investigations

    thread = get_specialist_thread(conn, thread_key)
    if not thread:
        raise HTTPException(status_code=404, detail=f"No specialist thread with thread_key {thread_key!r}")
    return {"thread": thread, "investigations": list_specialist_investigations(conn, thread_key)}


@router.patch("/research/intraday/specialists/{thread_key}/status")
def update_specialist_thread_status_endpoint(
    thread_key: str,
    status: str = Query(...),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    from app.services.labs.intraday.specialist import update_specialist_thread_status

    try:
        return update_specialist_thread_status(conn, thread_key=thread_key, status=status)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/research/intraday/specialists/{thread_key}/investigations")
def record_specialist_investigation_endpoint(
    thread_key: str,
    payload: dict[str, Any] = Body(...),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    from app.services.labs.intraday.specialist import record_specialist_investigation

    try:
        return record_specialist_investigation(
            conn,
            thread_key=thread_key,
            investigation_type=payload["investigation_type"],
            findings=payload.get("findings") or {},
            conclusion=payload.get("conclusion"),
            dataset_id=payload.get("dataset_id"),
            campaign_id=payload.get("campaign_id"),
            question=payload.get("question"),
            evidence_tier=payload.get("evidence_tier"),
        )
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/research/intraday/campaigns/{campaign_id}/pooled-evidence")
def compute_pooled_evidence_endpoint(
    campaign_id: int,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    """Pool every symbol's trades per canonical candidate in this campaign
    and re-evaluate the unchanged elite gate over the combined evidence.
    Additive: never touches elite_research_candidates or the per-symbol
    gate. See app/services/labs/intraday/pooled_evidence.py."""
    from app.services.labs.intraday.pooled_evidence import compute_pooled_candidate_evidence

    evidence = compute_pooled_candidate_evidence(conn, campaign_id=campaign_id)
    return {"campaign_id": campaign_id, "pooled_candidates": len(evidence), "evidence": evidence}


@router.get("/research/intraday/campaigns/{campaign_id}/pooled-evidence")
def list_pooled_evidence_endpoint(
    campaign_id: int,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    from app.services.labs.intraday.pooled_evidence import list_pooled_evidence

    evidence = list_pooled_evidence(conn, campaign_id=campaign_id)
    return {"campaign_id": campaign_id, "pooled_candidates": len(evidence), "evidence": evidence}


@router.get("/research/intraday/campaigns/{campaign_id}/family-ranking")
def get_campaign_family_ranking(
    campaign_id: int,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    """Rank this campaign's families by how much promise their evidence shows.

    This is what a broad screen should be judged on. Ranking never promotes
    and never writes; see app/services/labs/intraday/funnel.py."""
    from app.services.labs.intraday.funnel import campaign_funnel_report

    return campaign_funnel_report(conn, campaign_id)


@router.post("/research/intraday/campaigns/{campaign_id}/focused-expansion")
def create_focused_expansion_endpoint(
    campaign_id: int,
    max_families: int = Query(3, ge=1, le=10, description="How many top-ranked families to expand."),
    candidates_per_family: int = Query(24, ge=1, le=128, description="Depth of each family's own deterministic parameter grid."),
    asset_limit: int = Query(10, ge=1, le=100, description="Full universe by default -- breadth is the point of the expansion."),
    timeframes: list[str] | None = Query(None),
    name: str | None = Query(None),
    hypothesis_version_id: int | None = Query(None),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    """Launch a focused multi-asset expansion over the top-ranked families of
    a completed broad screen -- the funnel step that gives a canonical
    candidate enough breadth for the unchanged elite gate to be reachable."""
    from app.services.labs.intraday.funnel import create_focused_expansion_campaign

    try:
        return create_focused_expansion_campaign(
            conn,
            source_campaign_id=campaign_id,
            max_families=max_families,
            candidates_per_family=candidates_per_family,
            asset_limit=asset_limit,
            timeframes=timeframes,
            name=name,
            hypothesis_version_id=hypothesis_version_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/research/intraday/campaigns/{campaign_id}/holdout-confirmation")
def compute_holdout_confirmation_endpoint(
    campaign_id: int,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    """Re-run the unchanged elite gate over each pooled candidate's untouched
    validation split. Strictly harder than pooling alone: a candidate with no
    holdout sample is never confirmed."""
    from app.services.labs.intraday.pooled_evidence import compute_holdout_confirmation

    evaluations = compute_holdout_confirmation(conn, campaign_id=campaign_id)
    return {
        "campaign_id": campaign_id,
        "evaluated": len(evaluations),
        "confirmed": sum(1 for row in evaluations if row["confirmed"]),
        "evaluations": evaluations,
    }


@router.get("/research/intraday/campaigns/{campaign_id}/holdout-confirmation")
def list_holdout_confirmation_endpoint(
    campaign_id: int,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    from app.services.labs.intraday.pooled_evidence import list_holdout_confirmations

    evidence = list_holdout_confirmations(conn, campaign_id=campaign_id)
    return {"campaign_id": campaign_id, "confirmed_candidates": len(evidence), "evidence": evidence}
