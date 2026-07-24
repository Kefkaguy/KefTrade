"""Phase 12.5 Step 2: specialist candidate lifecycle plumbing.

Implements `research_specialist_threads` / `research_specialist_investigations`
(migration 049) as application code: creating a thread, transitioning its
status, and recording an append-only investigation. No deployment or
promotion logic lives here -- see the architecture proposal (section 5) for
the explicit promotion boundary this module deliberately does not cross.

This module records evidence; it never runs a campaign, a backtest, or a
dataset snapshot itself. Callers pass in whatever campaign_id/dataset_id/
findings they already have from elsewhere.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

VALID_THREAD_STATUSES = ("active_research", "confirmed_specialist", "invalidated", "retired")
VALID_INVESTIGATION_TYPES = (
    "unseen_holdout_performance",
    "forward_validation",
    "parameter_robustness",
    "cost_robustness",
    "stability_across_years",
    "similarity_to_declared_securities",
)


def create_specialist_thread(
    conn: psycopg.Connection,
    *,
    thread_key: str,
    title: str,
    origin_candidate_id: str,
    frozen_parameters: dict[str, Any],
    scope_timeframe: str,
    scope_direction: str,
    origin_campaign_id: int | None = None,
    scope_symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Freeze a narrow-but-real finding as a long-lived research object.

    `frozen_parameters` is protected from mutation by a dedicated database
    trigger the moment this row exists (see migration 049) -- there is no
    application-level re-tuning path for it. Idempotent by `thread_key`: an
    existing thread with the same key is returned unchanged rather than
    duplicated or overwritten.
    """

    if scope_direction not in ("long", "short"):
        raise ValueError(f"scope_direction must be 'long' or 'short', got {scope_direction!r}")

    row = conn.execute("SELECT * FROM research_specialist_threads WHERE thread_key = %s", (thread_key,)).fetchone()
    if row:
        return dict(row)

    row = conn.execute(
        """
        INSERT INTO research_specialist_threads(
            thread_key, title, origin_campaign_id, origin_candidate_id, frozen_parameters,
            scope_symbols, scope_timeframe, scope_direction
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            thread_key,
            title,
            origin_campaign_id,
            origin_candidate_id,
            Jsonb(frozen_parameters),
            Jsonb(list(scope_symbols or [])),
            scope_timeframe,
            scope_direction,
        ),
    ).fetchone()
    conn.commit()
    return dict(row)


def update_specialist_thread_status(conn: psycopg.Connection, *, thread_key: str, status: str) -> dict[str, Any]:
    if status not in VALID_THREAD_STATUSES:
        raise ValueError(f"unsupported specialist thread status {status!r}. Supported: {VALID_THREAD_STATUSES}")
    row = conn.execute(
        "UPDATE research_specialist_threads SET status = %s, updated_at = NOW() WHERE thread_key = %s RETURNING *",
        (status, thread_key),
    ).fetchone()
    if not row:
        raise ValueError(f"no specialist thread with thread_key {thread_key!r}")
    conn.commit()
    return dict(row)


def record_specialist_investigation(
    conn: psycopg.Connection,
    *,
    thread_key: str,
    investigation_type: str,
    findings: dict[str, Any],
    conclusion: str | None = None,
    dataset_id: int | None = None,
    campaign_id: int | None = None,
) -> dict[str, Any]:
    """Append one immutable investigation row to a thread's lab notebook.

    Never updates or deletes an existing investigation -- this is the
    append-only history the architecture proposal calls for; a correction is
    always a new row, never an edit to an old one.
    """

    if investigation_type not in VALID_INVESTIGATION_TYPES:
        raise ValueError(f"unsupported investigation_type {investigation_type!r}. Supported: {VALID_INVESTIGATION_TYPES}")
    thread = conn.execute("SELECT id FROM research_specialist_threads WHERE thread_key = %s", (thread_key,)).fetchone()
    if not thread:
        raise ValueError(f"no specialist thread with thread_key {thread_key!r}")

    row = conn.execute(
        """
        INSERT INTO research_specialist_investigations(thread_id, investigation_type, dataset_id, campaign_id, findings, conclusion)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (int(thread["id"]), investigation_type, dataset_id, campaign_id, Jsonb(findings), conclusion),
    ).fetchone()
    conn.commit()
    return dict(row)


def get_specialist_thread(conn: psycopg.Connection, thread_key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM research_specialist_threads WHERE thread_key = %s", (thread_key,)).fetchone()
    return dict(row) if row else None


def list_specialist_investigations(conn: psycopg.Connection, thread_key: str) -> list[dict[str, Any]]:
    thread = conn.execute("SELECT id FROM research_specialist_threads WHERE thread_key = %s", (thread_key,)).fetchone()
    if not thread:
        return []
    rows = conn.execute(
        "SELECT * FROM research_specialist_investigations WHERE thread_id = %s ORDER BY created_at ASC",
        (int(thread["id"]),),
    ).fetchall()
    return [dict(row) for row in rows]
