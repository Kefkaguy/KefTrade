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
        )
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
