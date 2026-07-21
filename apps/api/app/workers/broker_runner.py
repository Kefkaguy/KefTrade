from __future__ import annotations

import argparse
import asyncio
from typing import Any

from psycopg.types.json import Jsonb

from app.db import connect
from app.services.broker_reconciliation import reconcile_broker_snapshot
from app.services.broker_sync import synchronize_broker
from app.services.external_execution import ensure_disabled_external_candidates, run_shadow_cycle
from app.settings import settings

BROKER_WORKER_LOCK = 918273645


async def run_broker_cycle() -> dict[str, Any]:
    with connect() as conn:
        claimed = conn.execute("SELECT pg_try_advisory_lock(%s) AS claimed", (BROKER_WORKER_LOCK,)).fetchone()
        if not claimed or not claimed["claimed"]:
            return {"status": "skipped", "reason": "broker worker lease is held"}
        try:
            sync = await synchronize_broker(conn)
            if sync.get("status") != "complete":
                return {"status": "sync_not_complete", "sync": sync}
            reconciliation = reconcile_broker_snapshot(conn, int(sync["sync_run"]["id"])) if settings.broker_reconciliation_enabled else {"status": "disabled"}
            ensure_disabled_external_candidates(conn)
            shadows = []
            if reconciliation.get("status") == "clean" and settings.broker_shadow_execution_enabled:
                deployments = conn.execute("""
                    SELECT x.id FROM external_paper_deployments x
                    JOIN elite_research_candidates e ON e.id=x.elite_candidate_id
                    WHERE x.state IN ('enabled_observe_only','enabled_execution')
                    ORDER BY e.research_score DESC, x.id
                """).fetchall()
                for deployment in deployments:
                    try:
                        shadows.append(await run_shadow_cycle(conn, int(deployment["id"]), int(sync["sync_run"]["id"])))
                    except Exception as error:  # worker records each isolated failure without weakening controls
                        conn.rollback()
                        shadows.append({"deployment_id": deployment["id"], "status": "failed", "error_class": error.__class__.__name__, "error": str(error)})
            persist_daily_summary(conn, sync, reconciliation, shadows)
            # Historical evidence is retained indefinitely. No runtime pruning occurs.
            return {"status": "complete", "sync": sync, "reconciliation": reconciliation, "shadow_executions": shadows, "broker_mutation": False}
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (BROKER_WORKER_LOCK,))
            conn.commit()


def prune_derived_snapshots(conn) -> None:
    """Compatibility no-op: historical evidence retention is now indefinite."""
    return None


def persist_daily_summary(conn, sync: dict[str, Any], reconciliation: dict[str, Any], shadows: list[dict[str, Any]]) -> None:
    counts = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM broker_raw_ingest_events WHERE received_at >= CURRENT_DATE) AS raw_events,
          (SELECT COUNT(*) FROM broker_reconciliation_findings WHERE created_at >= CURRENT_DATE) AS findings,
          (SELECT COUNT(*) FROM execution_halts WHERE first_seen_at >= CURRENT_DATE) AS halts,
          (SELECT COUNT(*) FROM shadow_executions WHERE created_at >= CURRENT_DATE) AS shadow_executions
        """
    ).fetchone()
    summary = {**dict(counts or {}), "latest_sync_status": sync.get("status"), "latest_reconciliation_status": reconciliation.get("status"), "cycle_shadow_results": len(shadows), "broker_mutation": False}
    conn.execute("INSERT INTO broker_daily_summaries(summary_date, summary) VALUES (CURRENT_DATE,%s) ON CONFLICT(summary_date) DO UPDATE SET summary=EXCLUDED.summary, updated_at=NOW()", (Jsonb(summary),))
    conn.commit()


async def loop(once: bool = False) -> None:
    while True:
        await run_broker_cycle()
        if once:
            return
        await asyncio.sleep(max(5, settings.broker_worker_poll_seconds))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the read-only Alpaca Paper synchronization and shadow worker.")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    asyncio.run(loop(once=args.once))


if __name__ == "__main__":
    main()
