from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
import psycopg

from app.db import get_connection
from app.services.labs.intraday.overview import intraday_lab_overview

router = APIRouter(tags=["intraday-lab"])


@router.get("/research/intraday/overview")
def get_intraday_lab_overview(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return intraday_lab_overview(conn)


@router.post("/research/intraday/campaigns")
def create_intraday_campaign_endpoint(
    family_ids: list[str] = Query(..., description="One or more Intraday Lab family architecture ids to launch together."),
    name: str | None = Query(None),
    asset_limit: int = Query(10, ge=1, le=100),
    timeframes: list[str] | None = Query(None),
    max_candidates_per_family: int = Query(8, ge=1, le=64),
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
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
