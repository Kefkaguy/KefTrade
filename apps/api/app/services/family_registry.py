from __future__ import annotations

from typing import Any

import psycopg

from app.observability import log_event

FAMILY_CLASSIFICATION_VERSION = "family_audit_v1"

# Families in these classes stay active for future campaign generation.
# Everything else is archived as 'legacy' -- evidence preserved, compute stopped.
ACTIVE_CLASSIFICATIONS = {
    "Excellent",
    "Good: promising, under-promoted",
    "Too restrictive",
}

FAMILY_AUDIT_SQL = """
WITH j AS (
  SELECT COALESCE(NULLIF(result->>'family_id',''), family_id,'unknown') AS fam,
    candidate_id, status,
    (result->'metrics'->>'profit_factor')::float AS pf,
    (result->'metrics'->>'win_rate')::float AS win_rate,
    (result->'metrics'->>'max_drawdown')::float AS dd,
    (result->'metrics'->>'number_of_trades')::float AS trades,
    (result->'metrics'->>'average_holding_time_hours')::float AS hold_h
  FROM research_campaign_jobs WHERE result->'metrics' IS NOT NULL AND simulation_only = TRUE
),
e AS (
  SELECT COALESCE(NULLIF(family_id,''),'unknown') AS fam, count(*) AS elites
  FROM elite_research_candidates
  WHERE simulation_only = TRUE AND COALESCE(promotion_state, 'elite') = 'elite'
  GROUP BY 1
)
SELECT j.fam AS family_id,
  count(*) AS jobs,
  count(distinct candidate_id) AS candidates,
  count(*) FILTER (WHERE status='promoted') AS promoted_jobs,
  COALESCE(max(e.elites),0) AS elites,
  (percentile_cont(0.5) within group (order by pf)) AS median_profit_factor,
  avg(win_rate) AS avg_win_rate,
  avg(dd) AS avg_drawdown,
  avg(trades) AS avg_trades,
  avg(hold_h) AS avg_holding_hours
FROM j LEFT JOIN e ON e.fam = j.fam
GROUP BY j.fam
"""


def classify_family(stats: dict[str, Any]) -> tuple[str, str]:
    """Deterministic classification of one family from audited statistics.

    Mirrors the 2026-07-23 library audit exactly, so re-running the audit and
    refreshing the registry always agree. Returns (classification, reason).
    """
    med_pf = stats.get("median_profit_factor")
    avg_trades = float(stats.get("avg_trades") or 0)
    elites = int(stats.get("elites") or 0)
    if med_pf is None or avg_trades == 0:
        return "Retire: dead (never trades)", "No variant ever executed a trade."
    med_pf = float(med_pf)
    if elites > 0 and med_pf >= 1.30 and avg_trades >= 20:
        return "Excellent", f"Elite family with robust median PF {med_pf:.2f} and {avg_trades:.0f} avg trades."
    if elites > 0 and med_pf < 1.05:
        return "Broken elite: median unprofitable", f"Holds an elite but the family's median backtest (PF {med_pf:.2f}) does not clear costs."
    if med_pf >= 1.50 and avg_trades < 15:
        return "Too restrictive", f"Strong median PF {med_pf:.2f} but only {avg_trades:.0f} avg trades; needs re-sampling, not redesign."
    if med_pf >= 1.30 and avg_trades >= 15:
        return "Good: promising, under-promoted", f"Median PF {med_pf:.2f} with {avg_trades:.0f} avg trades and no elite yet."
    if med_pf < 0.95 and avg_trades >= 25:
        return "Too noisy", f"Trades often ({avg_trades:.0f} avg) with losing median PF {med_pf:.2f}."
    if med_pf < 0.90:
        return "Retire: negative edge", f"Median PF {med_pf:.2f}; the family loses money."
    if med_pf < 1.30:
        return "Redesign: weak edge", f"Median PF {med_pf:.2f} is too weak to promote and too active to retire."
    return "Redesign", f"Median PF {med_pf:.2f} with mixed evidence."


def refresh_family_registry(conn: psycopg.Connection) -> dict[str, Any]:
    """Re-audit every family from immutable job evidence and update the registry.

    Active = Excellent / promising / too-restrictive (the hidden gems worth
    re-sampling). Everything else becomes 'legacy' -- archived, never deleted,
    and excluded from future candidate generation. Idempotent + deterministic.
    """
    rows = [dict(row) for row in conn.execute(FAMILY_AUDIT_SQL).fetchall()]
    counts = {"active": 0, "legacy": 0}
    by_class: dict[str, int] = {}
    for stats in rows:
        classification, reason = classify_family(stats)
        status = "active" if classification in ACTIVE_CLASSIFICATIONS else "legacy"
        counts[status] += 1
        by_class[classification] = by_class.get(classification, 0) + 1
        conn.execute(
            """
            INSERT INTO research_family_registry(
                family_id, classification, status, classification_version, jobs, candidates,
                promoted_jobs, elites, median_profit_factor, avg_win_rate, avg_drawdown,
                avg_trades, avg_holding_hours, reason, simulation_only
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            ON CONFLICT(family_id) DO UPDATE
            SET classification = EXCLUDED.classification,
                status = EXCLUDED.status,
                classification_version = EXCLUDED.classification_version,
                jobs = EXCLUDED.jobs,
                candidates = EXCLUDED.candidates,
                promoted_jobs = EXCLUDED.promoted_jobs,
                elites = EXCLUDED.elites,
                median_profit_factor = EXCLUDED.median_profit_factor,
                avg_win_rate = EXCLUDED.avg_win_rate,
                avg_drawdown = EXCLUDED.avg_drawdown,
                avg_trades = EXCLUDED.avg_trades,
                avg_holding_hours = EXCLUDED.avg_holding_hours,
                reason = EXCLUDED.reason,
                updated_at = NOW()
            """,
            (
                stats["family_id"], classification, status, FAMILY_CLASSIFICATION_VERSION,
                int(stats.get("jobs") or 0), int(stats.get("candidates") or 0),
                int(stats.get("promoted_jobs") or 0), int(stats.get("elites") or 0),
                stats.get("median_profit_factor"), stats.get("avg_win_rate"),
                stats.get("avg_drawdown"), stats.get("avg_trades"), stats.get("avg_holding_hours"),
                reason,
            ),
        )
    conn.commit()
    log_event("Family registry refreshed", families=len(rows), active=counts["active"], legacy=counts["legacy"])
    return {
        "classification_version": FAMILY_CLASSIFICATION_VERSION,
        "families": len(rows),
        "active": counts["active"],
        "legacy": counts["legacy"],
        "by_classification": dict(sorted(by_class.items(), key=lambda item: -item[1])),
        "evidence_deleted": False,
    }


def legacy_family_ids(conn: psycopg.Connection) -> set[str]:
    try:
        rows = conn.execute("SELECT family_id FROM research_family_registry WHERE status = 'legacy'").fetchall()
    except psycopg.errors.UndefinedTable:
        conn.rollback()
        return set()
    return {str(row["family_id"]) for row in rows}


def list_family_registry(conn: psycopg.Connection, *, status: str | None = None) -> list[dict[str, Any]]:
    where = "WHERE status = %s" if status else ""
    params = (status,) if status else ()
    rows = conn.execute(
        f"SELECT * FROM research_family_registry {where} ORDER BY status, median_profit_factor DESC NULLS LAST",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def hidden_gem_families(conn: psycopg.Connection, *, limit: int = 27) -> list[dict[str, Any]]:
    """Active too-restrictive families, strongest median edge first."""
    rows = conn.execute(
        """
        SELECT * FROM research_family_registry
        WHERE status = 'active' AND classification = 'Too restrictive'
        ORDER BY median_profit_factor DESC NULLS LAST, family_id
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]
