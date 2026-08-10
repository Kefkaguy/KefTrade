from typing import Any

from fastapi import APIRouter, Depends, HTTPException
import psycopg

from app.db import get_connection
from app.services.intraday_paper_lab import monitor

router = APIRouter(prefix="/intraday-paper-lab", tags=["intraday-paper-lab"])


@router.get("/experiments/{experiment_id}")
def get_intraday_paper_lab_experiment(
    experiment_id: int,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    try:
        return monitor(conn, experiment_id=experiment_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
