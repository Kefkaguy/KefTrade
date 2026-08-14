"""Read-only HTTP surface for pre-strategy alpha cartography.

Declaring and measuring a grid are deliberately CLI-only. Both spend an
irreversible piece of statistical budget -- a declaration fixes a trial count,
a measurement consumes a split phase and can never be repeated for that
declaration -- and an endpoint that can be re-fired from a dashboard is the
wrong shape for either. What the API exposes is the resulting map.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
import psycopg

from app.db import get_connection
from app.services.intraday_alpha_map import (
    TRANSFORMS,
    alpha_map_report,
    feature_horizon_profile,
)
from app.services.research_architecture import jsonable

router = APIRouter(prefix="/intraday-alpha-map", tags=["intraday-alpha-map"])


@router.get("/runs")
def list_alpha_map_runs(
    limit: int = Query(25, ge=1, le=200),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT r.id, r.declaration_id, r.dataset_id, r.phase, r.observation_count,
               r.effective_trials, r.probability_of_backtest_overfitting,
               r.strategy_construction_authorized, r.survivors, r.created_at,
               d.signal_timeframe, d.grid_timeframe, d.declared_cell_count
        FROM intraday_alpha_map_runs r
        JOIN intraday_alpha_map_declarations d ON d.id = r.declaration_id
        ORDER BY r.created_at DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return jsonable({"runs": [dict(row) for row in rows]})


@router.get("/runs/{run_id}")
def get_alpha_map_run(
    run_id: int,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    try:
        return alpha_map_report(conn, run_id=run_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/features/{feature}/horizon-profile")
def get_feature_horizon_profile(
    feature: str,
    transform: str | None = Query(
        None,
        description=f"One of: {', '.join(TRANSFORMS)}. Omit to combine every transform.",
    ),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    """Where a feature's information lives, across every run ever measured.

    This is the table that replaces "the strategy lost money" as the first
    thing anyone looks at: one row per horizon, with the measured edge next to
    the cost of harvesting it.
    """
    if transform is not None and transform not in TRANSFORMS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown transform {transform!r}; expected one of {sorted(TRANSFORMS)}",
        )
    return feature_horizon_profile(conn, feature=feature, transform=transform)


@router.get("/cleared-cells")
def list_cleared_cells(
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    """Cells that currently authorize strategy construction.

    Usually empty. That is the expected state of a research programme that is
    working, not a sign that something is broken -- a cheap, early, honest
    rejection is the product this layer exists to make.
    """
    rows = conn.execute(
        """
        SELECT c.run_id, c.cell_key, c.feature, c.feature_transform, c.horizon_seconds,
               c.slice_kind, c.slice_value, c.observations, c.distinct_sessions,
               c.rank_ic, c.rank_ic_t_statistic, c.extreme_bucket_gross_bps,
               c.estimated_round_trip_cost_bps, c.required_gross_bps, c.net_bps,
               c.monotonicity, r.phase, r.effective_trials, r.created_at
        FROM intraday_alpha_map_cells c
        JOIN intraday_alpha_map_runs r ON r.id = c.run_id
        WHERE c.verdict = 'tradable_candidate'
          AND r.strategy_construction_authorized
        ORDER BY c.extreme_bucket_gross_bps DESC NULLS LAST
        """
    ).fetchall()
    return jsonable({"cleared_cells": [dict(row) for row in rows], "count": len(rows)})
