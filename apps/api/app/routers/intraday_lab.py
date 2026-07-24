from typing import Any

from fastapi import APIRouter, Depends
import psycopg

from app.db import get_connection
from app.services.labs.intraday.overview import intraday_lab_overview

router = APIRouter(tags=["intraday-lab"])


@router.get("/research/intraday/overview")
def get_intraday_lab_overview(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return intraday_lab_overview(conn)
