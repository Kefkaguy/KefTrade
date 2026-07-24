from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
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
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
