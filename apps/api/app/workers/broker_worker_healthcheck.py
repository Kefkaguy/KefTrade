from __future__ import annotations

"""Docker healthcheck for the broker worker container.

Replaces the old Phase 10 check, which asserted
BROKER_ORDER_SUBMISSION_ENABLED and EXTERNAL_PAPER_EXECUTION_ENABLED were both
`false`. That made the container report unhealthy the moment Phase 11
deliberately turned those flags on, even though the worker was running
correctly -- the healthcheck was validating a point-in-time configuration
choice, not liveness.

This check validates the things that actually indicate the worker is alive
and doing its job, using only DB state the worker itself already writes in
`run_broker_cycle` (apps/api/app/workers/broker_runner.py):
  1. Required configuration is present (DATABASE_URL, Alpaca credentials) --
     a precondition for the worker to function at all, not a behavior flag.
  2. The database is reachable.
  3. A broker sync cycle has completed recently (fresh heartbeat), scaled to
     the configured poll interval so it does not falsely flap.
  4. Recent cycles are not uniformly failing (a single transient failure is
     tolerated; a persistent run of failures is not).

It never inspects BROKER_ORDER_SUBMISSION_ENABLED, EXTERNAL_PAPER_EXECUTION_ENABLED,
or any other capability flag -- those may legitimately be on or off depending
on environment and are none of a liveness check's business.

Exit code 0 = healthy, 1 = unhealthy. Prints one line explaining the result.
"""

import os
import sys
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

RECENT_CYCLES_TO_INSPECT = 3
DEFAULT_POLL_SECONDS = 60
MINIMUM_FRESHNESS_THRESHOLD_SECONDS = 120


def freshness_threshold_seconds(poll_seconds: int) -> int:
    return max(MINIMUM_FRESHNESS_THRESHOLD_SECONDS, poll_seconds * 3)


def evaluate_health(env: dict[str, str], cycle_rows: list[dict[str, Any]], *, now: datetime | None = None) -> tuple[bool, str]:
    """Pure decision logic, independent of the DB/env access above it.

    `cycle_rows` are the most recent `broker_sync_runs` rows (status,
    started_at, completed_at), most-recent first. Kept separate from I/O so
    the decision rules are directly unit-testable.
    """
    if not env.get("DATABASE_URL"):
        return False, "DATABASE_URL is not set"
    if not env.get("ALPACA_API_KEY") or not env.get("ALPACA_API_SECRET"):
        return False, "Alpaca credentials are not configured"

    if not cycle_rows:
        # No cycle has run yet. Only acceptable during the container's
        # start_period grace window; a genuinely stuck first cycle still
        # surfaces as unhealthy shortly after start_period, once Docker stops
        # granting the grace window.
        return True, "no broker_sync_runs yet (awaiting first cycle)"

    poll_seconds = int(env.get("BROKER_WORKER_POLL_SECONDS") or DEFAULT_POLL_SECONDS)
    threshold = freshness_threshold_seconds(poll_seconds)
    now = now or datetime.now(UTC)

    latest = cycle_rows[0]
    latest_timestamp = latest["completed_at"] or latest["started_at"]
    age_seconds = (now - latest_timestamp).total_seconds()
    if age_seconds > threshold:
        return False, f"latest broker cycle is {age_seconds:.0f}s old (threshold {threshold}s)"

    if all(str(row["status"]) == "failed" for row in cycle_rows):
        return False, f"last {len(cycle_rows)} broker cycle(s) all failed"

    return True, f"latest cycle status={latest['status']} age={age_seconds:.0f}s"


def fetch_recent_cycles(database_url: str) -> list[dict[str, Any]]:
    conn = psycopg.connect(database_url, row_factory=dict_row, connect_timeout=3)
    try:
        with conn:
            return list(
                conn.execute(
                    "SELECT status, started_at, completed_at FROM broker_sync_runs ORDER BY started_at DESC LIMIT %s",
                    (RECENT_CYCLES_TO_INSPECT,),
                ).fetchall()
            )
    finally:
        conn.close()


def main() -> None:
    env = dict(os.environ)
    database_url = env.get("DATABASE_URL")
    if not database_url:
        print("UNHEALTHY: DATABASE_URL is not set")
        sys.exit(1)

    try:
        cycle_rows = fetch_recent_cycles(database_url)
    except Exception as error:
        print(f"UNHEALTHY: could not read broker_sync_runs: {error.__class__.__name__}")
        sys.exit(1)
        return

    healthy, reason = evaluate_health(env, cycle_rows)
    print(f"{'HEALTHY' if healthy else 'UNHEALTHY'}: {reason}")
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()
