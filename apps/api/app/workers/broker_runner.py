from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from app.db import connect
from app.services.broker_reconciliation import reconcile_broker_snapshot
from app.services.broker_sync import synchronize_broker
from app.services.external_execution import run_shadow_cycle
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
            shadows = []
            if reconciliation.get("status") == "clean" and settings.broker_shadow_execution_enabled:
                deployments = conn.execute("SELECT id FROM external_paper_deployments WHERE state='enabled_observe_only' ORDER BY id").fetchall()
                for deployment in deployments:
                    try:
                        shadows.append(run_shadow_cycle(conn, int(deployment["id"]), int(sync["sync_run"]["id"])))
                    except Exception as error:  # worker records each isolated failure without weakening controls
                        conn.rollback()
                        shadows.append({"deployment_id": deployment["id"], "status": "failed", "error_class": error.__class__.__name__, "error": str(error)})
            persist_daily_summary(conn, sync, reconciliation, shadows)
            prune_derived_snapshots(conn)
            return {"status": "complete", "sync": sync, "reconciliation": reconciliation, "shadow_executions": shadows, "broker_mutation": False}
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (BROKER_WORKER_LOCK,))
            conn.commit()


def prune_derived_snapshots(conn) -> None:
    raw_cutoff = datetime.now(UTC) - timedelta(days=settings.broker_raw_snapshot_retention_days)
    clean_reconciliation_cutoff = datetime.now(UTC) - timedelta(days=90)
    conn.execute("DELETE FROM broker_account_snapshots WHERE captured_at < %s", (raw_cutoff,))
    conn.execute("DELETE FROM broker_clock_snapshots WHERE captured_at < %s", (raw_cutoff,))
    conn.execute("DELETE FROM broker_position_snapshots WHERE captured_at < %s", (raw_cutoff,))
    conn.execute("DELETE FROM broker_reconciliation_runs r WHERE r.status='clean' AND r.completed_at < %s AND NOT EXISTS (SELECT 1 FROM broker_reconciliation_findings f WHERE f.reconciliation_run_id=r.id)", (clean_reconciliation_cutoff,))
    conn.execute("DELETE FROM broker_daily_summaries WHERE summary_date < CURRENT_DATE - INTERVAL '2 years'")
    conn.commit()


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
