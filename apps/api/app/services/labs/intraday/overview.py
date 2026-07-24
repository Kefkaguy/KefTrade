"""Read-only reporting for the Intraday Research Lab UI (Phase 12).

Every number here is queried live from `research_campaign_jobs`/`research_campaigns`
-- nothing about trade counts, profit factors, or rejection reasons is
hardcoded. Only the strategy roster itself (which families exist, and their
lifecycle status/reason) is a static, code-owned fact, since that reflects
what has actually been implemented in this codebase, not something derivable
from a database query.

Generalized across every Intraday Lab family (Opening-Range Breakout, VWAP
Reversion, and any future one): each roster entry with real code gets its
own campaigns/jobs/trades totals, timeframe breakdown, and sample rejected
jobs, all computed by one set of architecture-parameterized queries -- no
per-family SQL duplication.
"""

from __future__ import annotations

from typing import Any

import psycopg

from app.services.labs.intraday.campaign import (
    OPENING_RANGE_BREAKOUT_ARCHITECTURE,
    SUPPORTED_ORB_TIMEFRAMES,
    SUPPORTED_VWAP_REVERSION_TIMEFRAMES,
    VWAP_REVERSION_ARCHITECTURE,
)

# Categorical failure codes only -- excludes the numeric-detail messages
# (e.g. "Profit factor 0.55 must be >= 1.25.") that `finalize_research_campaign`
# also appends to `failure_reasons`, so this reads as a rejection-reason
# histogram rather than a wall of distinct numbers.
_CATEGORICAL_FAILURE_CODES = (
    "weak_profit_factor",
    "poor_expectancy",
    "insufficient_trades",
    "high_drawdown",
    "fails_in_unknown",
    "frequency_too_low",
)

# The first ORB Campaign 44 run used walk_forward_train_ratio=1.0, a defect
# that silently produced zero trades on every job (see
# docs/2026-07-23-phase12-step2b-orb-pilot.md). Those 80 jobs are kept in the
# database (archive, don't erase) but must not be blended into the reported
# pilot numbers alongside the corrected rerun (ratio=0.7) -- doing so dilutes
# real profit-factor/expectancy figures with zero-trade noise. Every
# Intraday Lab family since has used 0.7 from the start, but this filter is
# kept general (not ORB-specific) as a standing safeguard against the same
# class of defect recurring silently in a future family.
_CORRECTED_RUN_FILTER = "(candidate->'parameters'->>'walk_forward_train_ratio')::float = 0.7"

INTRADAY_STRATEGY_ROSTER: list[dict[str, Any]] = [
    {
        "id": OPENING_RANGE_BREAKOUT_ARCHITECTURE,
        "name": "Opening Range Breakout",
        "version": "v1",
        "status": "archived",
        "reason": "No measurable edge after costs",
        "summary": (
            "Session-close exits (76% of trades) averaged ~0% gross return before costs. "
            "Transaction costs consumed 37% of average gross price movement per trade and "
            "flipped 19.5% of gross winners into net losses. No subgroup showed repeatable "
            "positive evidence across enough symbols and periods."
        ),
        "supported_timeframes": SUPPORTED_ORB_TIMEFRAMES,
    },
    {
        "id": VWAP_REVERSION_ARCHITECTURE,
        "name": "VWAP Reversion",
        "version": "v1",
        "status": "archived",
        "reason": "No measurable edge after costs",
        "summary": (
            "1,542 trades across 80 jobs, avg profit factor 0.46. Gross P&L -13,354 before costs, "
            "-33,684 after -- fees/slippage added 20,330 in additional loss. Session-close exits "
            "(61% of trades) dominate, the same forced-flat mechanism ORB showed weak realized "
            "continuation with. 0 promotions through the unmodified elite gate."
        ),
        "supported_timeframes": SUPPORTED_VWAP_REVERSION_TIMEFRAMES,
    },
    {
        "id": "gap_fill",
        "name": "Gap Fill",
        "version": None,
        "status": "planned",
        "reason": None,
        "summary": None,
        "supported_timeframes": (),
    },
    {
        "id": "session_momentum",
        "name": "Session Momentum",
        "version": None,
        "status": "planned",
        "reason": None,
        "summary": None,
        "supported_timeframes": (),
    },
]


def _architecture_job_totals(conn: psycopg.Connection, architecture: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"""
        SELECT
            count(DISTINCT campaign_id) AS campaigns,
            count(*) AS jobs,
            coalesce(sum((result->'metrics'->>'number_of_trades')::int), 0) AS trades,
            count(*) FILTER (WHERE status = 'promoted') AS promoted
        FROM research_campaign_jobs
        WHERE candidate->'parameters'->>'strategy_architecture' = %s
          AND status <> 'queued'
          AND {_CORRECTED_RUN_FILTER}
        """,
        (architecture,),
    ).fetchone()
    if not row or not row["jobs"]:
        return None
    return dict(row)


def _architecture_latest_campaign(conn: psycopg.Connection, architecture: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"""
        SELECT c.id, c.name, c.status
        FROM research_campaigns c
        WHERE c.id IN (
            SELECT DISTINCT campaign_id
            FROM research_campaign_jobs
            WHERE candidate->'parameters'->>'strategy_architecture' = %s
              AND {_CORRECTED_RUN_FILTER}
        )
        ORDER BY c.id DESC
        LIMIT 1
        """,
        (architecture,),
    ).fetchone()
    return dict(row) if row else None


def _architecture_timeframe_breakdown(conn: psycopg.Connection, architecture: str, supported_timeframes: tuple[str, ...]) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT
            timeframe,
            count(*) AS jobs,
            coalesce(sum((result->'metrics'->>'number_of_trades')::int), 0) AS trades,
            avg((result->'metrics'->>'profit_factor')::float) AS avg_profit_factor,
            avg((result->'metrics'->>'expectancy_per_trade')::float) AS avg_expectancy
        FROM research_campaign_jobs
        WHERE candidate->'parameters'->>'strategy_architecture' = %s
          AND status <> 'queued'
          AND {_CORRECTED_RUN_FILTER}
        GROUP BY timeframe
        ORDER BY timeframe
        """,
        (architecture,),
    ).fetchall()
    by_timeframe = {row["timeframe"]: dict(row) for row in rows}

    reason_rows = conn.execute(
        f"""
        SELECT timeframe, reason, count(*) AS occurrences
        FROM (
            SELECT timeframe, jsonb_array_elements_text(failure_reasons) AS reason
            FROM research_campaign_jobs
            WHERE candidate->'parameters'->>'strategy_architecture' = %s
              AND status <> 'queued'
              AND {_CORRECTED_RUN_FILTER}
        ) exploded
        WHERE reason = ANY(%s)
        GROUP BY timeframe, reason
        ORDER BY timeframe, occurrences DESC
        """,
        (architecture, list(_CATEGORICAL_FAILURE_CODES)),
    ).fetchall()
    reasons_by_timeframe: dict[str, list[dict[str, Any]]] = {}
    for row in reason_rows:
        reasons_by_timeframe.setdefault(row["timeframe"], []).append(
            {"reason": row["reason"], "occurrences": row["occurrences"]}
        )

    result = []
    for timeframe in supported_timeframes:
        totals = by_timeframe.get(timeframe)
        result.append(
            {
                "timeframe": timeframe,
                "jobs": totals["jobs"] if totals else 0,
                "trades": totals["trades"] if totals else 0,
                "avg_profit_factor": round(totals["avg_profit_factor"], 3) if totals and totals["avg_profit_factor"] is not None else None,
                "avg_expectancy": round(totals["avg_expectancy"], 2) if totals and totals["avg_expectancy"] is not None else None,
                "primary_rejection_reasons": reasons_by_timeframe.get(timeframe, [])[:3],
                "status": "archived" if totals else "not_started",
            }
        )
    return result


def _architecture_sample_jobs(conn: psycopg.Connection, architecture: str, *, limit: int = 12) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT
            symbol,
            timeframe,
            candidate->'parameters'->>'direction' AS direction,
            candidate->'parameters' AS parameters,
            status,
            validation_score,
            (result->'metrics'->>'number_of_trades')::int AS trades,
            (result->'metrics'->>'profit_factor')::float AS profit_factor,
            (result->'metrics'->>'expectancy_per_trade')::float AS expectancy,
            failure_reasons
        FROM research_campaign_jobs
        WHERE candidate->'parameters'->>'strategy_architecture' = %s
          AND status <> 'queued'
          AND {_CORRECTED_RUN_FILTER}
        ORDER BY symbol, timeframe, direction
        LIMIT %s
        """,
        (architecture, limit),
    ).fetchall()
    samples = []
    for row in rows:
        entry = dict(row)
        parameters = entry.pop("parameters") or {}
        # The one family-specific display field (ORB's breakout buffer, VWAP's
        # deviation threshold) -- everything else in this row is generic.
        entry["variant_parameter"] = parameters.get("breakout_buffer_atr") or parameters.get("entry_deviation_threshold")
        samples.append(entry)
    return samples


def _architecture_detail(conn: psycopg.Connection, architecture: str, supported_timeframes: tuple[str, ...]) -> dict[str, Any] | None:
    totals = _architecture_job_totals(conn, architecture)
    if not totals:
        return None
    latest_campaign = _architecture_latest_campaign(conn, architecture)
    return {
        "campaigns": totals["campaigns"],
        "jobs": totals["jobs"],
        "trades": totals["trades"],
        "promoted": totals["promoted"],
        "pilot": (
            {
                "campaign_id": latest_campaign["id"],
                "name": latest_campaign["name"],
                "status": latest_campaign["status"],
                "jobs": totals["jobs"],
                "trades": totals["trades"],
                "promoted": totals["promoted"],
                "outcome": "archived_negative_result" if totals["promoted"] == 0 else "under_review",
            }
            if latest_campaign
            else None
        ),
        "timeframe_breakdown": _architecture_timeframe_breakdown(conn, architecture, supported_timeframes),
        "sample_jobs": _architecture_sample_jobs(conn, architecture),
    }


def intraday_lab_overview(conn: psycopg.Connection) -> dict[str, Any]:
    all_timeframes: set[str] = set()
    strategies = []
    for entry in INTRADAY_STRATEGY_ROSTER:
        row = dict(entry)
        supported_timeframes = tuple(row.pop("supported_timeframes", ()))
        all_timeframes.update(supported_timeframes)
        if row["status"] != "planned" and supported_timeframes:
            detail = _architecture_detail(conn, row["id"], supported_timeframes)
            if detail:
                row.update(detail)
        strategies.append(row)

    return {
        "infrastructure_status": "complete",
        "timeframes_supported": sorted(all_timeframes),
        "strategies": strategies,
        "forward_validation_note": "Intraday research available. No validated intraday strategy currently approved for forward validation.",
    }
