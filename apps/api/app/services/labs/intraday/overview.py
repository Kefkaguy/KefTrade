"""Read-only reporting for the Intraday Research Lab UI (Phase 12).

Every number here is queried live from `research_campaign_jobs`/`research_campaigns`
-- nothing about trade counts, profit factors, or rejection reasons is
hardcoded. The strategy roster's identity/timeframes/status come from
`families.registry.FAMILY_REGISTRY` (the single source of truth every other
consumer already uses); only the archived-family narrative text
(`_ARCHIVED_SUMMARIES` below) is hand-written, since that's a research
conclusion, not something a query can produce.

Generalized across every Intraday Lab family: each roster entry gets its
own campaigns/jobs/trades totals, timeframe breakdown, and sample rejected
jobs, all computed by one set of architecture-parameterized queries -- no
per-family SQL duplication, and no per-family branching when a new family
is added to `FAMILY_REGISTRY`.
"""

from __future__ import annotations

from typing import Any

import psycopg

from app.services.labs.intraday.campaign import OPENING_RANGE_BREAKOUT_ARCHITECTURE, VWAP_REVERSION_ARCHITECTURE
from app.services.labs.intraday.families.registry import FAMILY_REGISTRY

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

# Hand-written research narrative for families whose pilots have been run
# and analyzed. A family present in FAMILY_REGISTRY but absent here (every
# Phase 12.3 family, until its own pilot/analysis is written up) simply
# shows its live query numbers with no reason/summary text yet -- not
# hidden, not fabricated.
_ARCHIVED_SUMMARIES: dict[str, dict[str, str]] = {
    OPENING_RANGE_BREAKOUT_ARCHITECTURE: {
        "reason": "No measurable edge after costs",
        "summary": (
            "Session-close exits (76% of trades) averaged ~0% gross return before costs. "
            "Transaction costs consumed 37% of average gross price movement per trade and "
            "flipped 19.5% of gross winners into net losses. No subgroup showed repeatable "
            "positive evidence across enough symbols and periods."
        ),
    },
    VWAP_REVERSION_ARCHITECTURE: {
        "reason": "No measurable edge after costs",
        "summary": (
            "1,542 trades across 80 jobs, avg profit factor 0.46. Gross P&L -13,354 before costs, "
            "-33,684 after -- fees/slippage added 20,330 in additional loss. Session-close exits "
            "(61% of trades) dominate, the same forced-flat mechanism ORB showed weak realized "
            "continuation with. 0 promotions through the unmodified elite gate."
        ),
    },
}


def _latest_campaign_id_for_architecture(conn: psycopg.Connection, architecture: str) -> int | None:
    """A family can accumulate more than one campaign over time -- e.g. Phase
    12.4 relaunched the same 6 Phase 12.3 families under a new campaign_id
    (see campaign_label) once trade-level evidence capture existed. Summing
    every campaign's jobs together would double-count the same underlying
    candidates/trades across two runs of an unchanged strategy. Every
    aggregate query below is therefore scoped to the single most recent
    campaign that has evidence for this architecture, exactly like
    _CORRECTED_RUN_FILTER already excludes one bad historical run -- this is
    the same "don't blend runs" principle, generalized to any number of
    campaigns per architecture, not specific to any one family."""

    row = conn.execute(
        f"""
        SELECT max(campaign_id) AS campaign_id
        FROM research_campaign_jobs
        WHERE candidate->'parameters'->>'strategy_architecture' = %s
          AND status <> 'queued'
          AND {_CORRECTED_RUN_FILTER}
        """,
        (architecture,),
    ).fetchone()
    return int(row["campaign_id"]) if row and row["campaign_id"] is not None else None


def _architecture_job_totals(conn: psycopg.Connection, architecture: str, campaign_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        f"""
        SELECT
            count(DISTINCT campaign_id) AS campaigns,
            count(*) AS jobs,
            coalesce(sum((result->'metrics'->>'number_of_trades')::int), 0) AS trades,
            count(*) FILTER (WHERE status = 'promoted') AS promoted
        FROM research_campaign_jobs
        WHERE candidate->'parameters'->>'strategy_architecture' = %s
          AND campaign_id = %s
          AND status <> 'queued'
          AND {_CORRECTED_RUN_FILTER}
        """,
        (architecture, campaign_id),
    ).fetchone()
    if not row or not row["jobs"]:
        return None
    return dict(row)


def _architecture_latest_campaign(conn: psycopg.Connection, architecture: str, campaign_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, name, status FROM research_campaigns WHERE id = %s",
        (campaign_id,),
    ).fetchone()
    return dict(row) if row else None


def _architecture_timeframe_breakdown(conn: psycopg.Connection, architecture: str, supported_timeframes: tuple[str, ...], campaign_id: int) -> list[dict[str, Any]]:
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
          AND campaign_id = %s
          AND status <> 'queued'
          AND {_CORRECTED_RUN_FILTER}
        GROUP BY timeframe
        ORDER BY timeframe
        """,
        (architecture, campaign_id),
    ).fetchall()
    by_timeframe = {row["timeframe"]: dict(row) for row in rows}

    reason_rows = conn.execute(
        f"""
        SELECT timeframe, reason, count(*) AS occurrences
        FROM (
            SELECT timeframe, jsonb_array_elements_text(failure_reasons) AS reason
            FROM research_campaign_jobs
            WHERE candidate->'parameters'->>'strategy_architecture' = %s
              AND campaign_id = %s
              AND status <> 'queued'
              AND {_CORRECTED_RUN_FILTER}
        ) exploded
        WHERE reason = ANY(%s)
        GROUP BY timeframe, reason
        ORDER BY timeframe, occurrences DESC
        """,
        (architecture, campaign_id, list(_CATEGORICAL_FAILURE_CODES)),
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
                "status": "has_evidence" if totals else "not_started",
            }
        )
    return result


def _architecture_sample_jobs(conn: psycopg.Connection, architecture: str, campaign_id: int, *, limit: int = 12) -> list[dict[str, Any]]:
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
          AND campaign_id = %s
          AND status <> 'queued'
          AND {_CORRECTED_RUN_FILTER}
        ORDER BY symbol, timeframe, direction
        LIMIT %s
        """,
        (architecture, campaign_id, limit),
    ).fetchall()
    samples = []
    for row in rows:
        entry = dict(row)
        parameters = entry.pop("parameters") or {}
        # The one family-specific display field (ORB's breakout buffer, VWAP's
        # deviation threshold, etc.) -- everything else in this row is generic.
        entry["variant_parameter"] = (
            parameters.get("breakout_buffer_atr")
            or parameters.get("entry_deviation_threshold")
            or parameters.get("minimum_gap_threshold")
            or parameters.get("momentum_threshold")
            or parameters.get("pullback_depth_threshold")
            or parameters.get("fade_buffer_atr")
        )
        samples.append(entry)
    return samples


def _architecture_detail(conn: psycopg.Connection, architecture: str, supported_timeframes: tuple[str, ...]) -> dict[str, Any] | None:
    campaign_id = _latest_campaign_id_for_architecture(conn, architecture)
    if campaign_id is None:
        return None
    totals = _architecture_job_totals(conn, architecture, campaign_id)
    if not totals:
        return None
    latest_campaign = _architecture_latest_campaign(conn, architecture, campaign_id)
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
        "timeframe_breakdown": _architecture_timeframe_breakdown(conn, architecture, supported_timeframes, campaign_id),
        "sample_jobs": _architecture_sample_jobs(conn, architecture, campaign_id),
    }


def intraday_lab_overview(conn: psycopg.Connection) -> dict[str, Any]:
    all_timeframes: set[str] = set()
    strategies = []
    for architecture, definition in FAMILY_REGISTRY.items():
        all_timeframes.update(definition.supported_timeframes)
        archived_meta = _ARCHIVED_SUMMARIES.get(architecture, {})
        row: dict[str, Any] = {
            "id": architecture,
            "name": definition.name,
            "version": "v1",
            "status": definition.status,
            "reason": archived_meta.get("reason"),
            "summary": archived_meta.get("summary"),
        }
        detail = _architecture_detail(conn, architecture, definition.supported_timeframes)
        if detail:
            row.update(detail)
        strategies.append(row)

    return {
        "infrastructure_status": "complete",
        "timeframes_supported": sorted(all_timeframes),
        "strategies": strategies,
        "forward_validation_note": "Intraday research available. No validated intraday strategy currently approved for forward validation.",
    }
