import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from app.db import connect
from app.services.evidence_alerts import create_scheduler_error_alert
from app.services.paper_trading import PaperTradingError, log_event, run_deployment_scan

SCHEDULER_TICK_SECONDS = 60
ALLOWED_CADENCES = {"manual", "15m", "30m", "60m"}
CADENCE_DELTAS = {
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "60m": timedelta(minutes=60),
}

_scheduler_task: asyncio.Task | None = None
_scan_lock = asyncio.Lock()


def ensure_scheduler_state(conn: psycopg.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM paper_scan_scheduler WHERE id = TRUE").fetchone()
    if row:
        return dict(row)
    row = conn.execute(
        """
        INSERT INTO paper_scan_scheduler(id, enabled, cadence, next_run_at)
        VALUES (TRUE, TRUE, '60m', NOW() + INTERVAL '60 minutes')
        RETURNING *
        """
    ).fetchone()
    conn.commit()
    return dict(row)


def get_scheduler_status(conn: psycopg.Connection) -> dict[str, Any]:
    return ensure_scheduler_state(conn)


def update_scheduler_status(conn: psycopg.Connection, enabled: bool | None = None, cadence: str | None = None) -> dict[str, Any]:
    current = ensure_scheduler_state(conn)
    next_enabled = current["enabled"] if enabled is None else enabled
    next_cadence = current["cadence"] if cadence is None else cadence
    if next_cadence not in ALLOWED_CADENCES:
        raise PaperTradingError("cadence must be manual, 15m, 30m, or 60m")
    next_run_at = None if next_cadence == "manual" or not next_enabled else datetime.now(UTC) + CADENCE_DELTAS[next_cadence]
    row = conn.execute(
        """
        UPDATE paper_scan_scheduler
        SET enabled = %s,
            cadence = %s,
            next_run_at = %s,
            latest_error = NULL,
            updated_at = NOW()
        WHERE id = TRUE
        RETURNING *
        """,
        (next_enabled, next_cadence, next_run_at),
    ).fetchone()
    log_event(
        conn,
        None,
        None,
        None,
        "paper_scheduler_configured",
        f"Paper scan scheduler set to {'enabled' if next_enabled else 'disabled'} / {next_cadence}.",
        {"enabled": next_enabled, "cadence": next_cadence, "simulation_only": True},
    )
    conn.commit()
    return dict(row)


def active_simulation_deployments(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return list(
        conn.execute(
            """
            SELECT *
            FROM strategy_deployments
            WHERE status = 'active'
              AND simulation_only = TRUE
            ORDER BY created_at, id
            """
        ).fetchall()
    )


async def run_scheduled_scan_once(conn: psycopg.Connection | None = None, *, force: bool = False) -> dict[str, Any]:
    owns_connection = conn is None
    conn = conn or connect()
    try:
        state = ensure_scheduler_state(conn)
        if _scan_lock.locked():
            return mark_scheduler_skipped(conn, state, "Previous scheduled paper scan is still running.")
        if not force and (not state["enabled"] or state["cadence"] == "manual"):
            return {"status": "idle", "message": "Paper scan scheduler is disabled or manual.", "scheduler": state, "simulation_only": True}
        if not force and state.get("next_run_at") and state["next_run_at"] > datetime.now(UTC):
            return {"status": "idle", "message": "Paper scan scheduler is not due yet.", "scheduler": state, "simulation_only": True}

        async with _scan_lock:
            mark_scheduler_running(conn)
            deployments = active_simulation_deployments(conn)
            if not deployments:
                return mark_scheduler_result(conn, state, "skipped", "No active simulation-only deployments to scan.", [])

            results = []
            errors = []
            log_event(conn, None, None, None, "paper_scheduler_run_started", "Scheduled paper scan started.", {"deployment_count": len(deployments), "simulation_only": True})
            conn.commit()
            for deployment in deployments:
                if deployment["status"] != "active" or not deployment["simulation_only"]:
                    log_event(conn, deployment.get("account_id"), deployment.get("id"), None, "paper_scheduler_scan_skipped", "Skipped non-active or non-simulation deployment.", deployment)
                    continue
                try:
                    result = await run_deployment_scan(conn, int(deployment["id"]))
                    results.append({"deployment_id": deployment["id"], "action": result["action"], "message": result["message"], "simulation_only": result["simulation_only"]})
                    log_event(conn, deployment["account_id"], deployment["id"], result.get("order", {}).get("id") if result.get("order") else None, "paper_scheduler_scan_result", result["message"], results[-1])
                    conn.commit()
                except Exception as error:  # noqa: BLE001 - scheduler must record and continue
                    message = str(error)
                    errors.append({"deployment_id": deployment["id"], "error": message, "simulation_only": True})
                    log_event(conn, deployment.get("account_id"), deployment.get("id"), None, "paper_scheduler_scan_error", message, errors[-1])
                    create_scheduler_error_alert(conn, deployment, message)
                    conn.commit()

            status = "error" if errors else "completed"
            summary = f"Scheduled scan {status}: {len(results)} result(s), {len(errors)} error(s)."
            return mark_scheduler_result(conn, state, status, summary, results, errors)
    finally:
        if owns_connection:
            conn.close()


def mark_scheduler_running(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        UPDATE paper_scan_scheduler
        SET is_running = TRUE,
            running_since = NOW(),
            latest_error = NULL,
            updated_at = NOW()
        WHERE id = TRUE
        """
    )
    conn.commit()


def mark_scheduler_skipped(conn: psycopg.Connection, state: dict[str, Any], message: str, *, commit: bool = True) -> dict[str, Any]:
    log_event(conn, None, None, None, "paper_scheduler_run_skipped", message, {"reason": message, "simulation_only": True})
    if commit:
        row = conn.execute(
            """
            UPDATE paper_scan_scheduler
            SET latest_result = %s,
                latest_error = NULL,
                updated_at = NOW()
            WHERE id = TRUE
            RETURNING *
            """,
            (message,),
        ).fetchone()
        conn.commit()
        state = dict(row)
    return {"status": "skipped", "message": message, "scheduler": state, "simulation_only": True}


def mark_scheduler_result(
    conn: psycopg.Connection,
    state: dict[str, Any],
    status: str,
    message: str,
    results: list[dict[str, Any]],
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    errors = errors or []
    next_run_at = None
    if state["enabled"] and state["cadence"] != "manual":
        next_run_at = datetime.now(UTC) + CADENCE_DELTAS[state["cadence"]]
    latest_error = "; ".join(error["error"] for error in errors) if errors else None
    row = conn.execute(
        """
        UPDATE paper_scan_scheduler
        SET last_run_at = NOW(),
            next_run_at = %s,
            latest_result = %s,
            latest_error = %s,
            is_running = FALSE,
            running_since = NULL,
            updated_at = NOW()
        WHERE id = TRUE
        RETURNING *
        """,
        (next_run_at, message, latest_error),
    ).fetchone()
    log_event(conn, None, None, None, "paper_scheduler_run_finished", message, {"status": status, "results": results, "errors": errors, "simulation_only": True})
    conn.commit()
    return {"status": status, "message": message, "results": results, "errors": errors, "scheduler": dict(row), "simulation_only": True}


async def scheduler_loop() -> None:
    while True:
        try:
            await run_scheduled_scan_once()
        except Exception:
            with suppress(Exception):
                conn = connect()
                try:
                    message = "Paper scheduler loop error."
                    conn.execute(
                        """
                        UPDATE paper_scan_scheduler
                        SET latest_error = %s,
                            is_running = FALSE,
                            running_since = NULL,
                            updated_at = NOW()
                        WHERE id = TRUE
                        """,
                        (message,),
                    )
                    log_event(conn, None, None, None, "paper_scheduler_loop_error", message, {"simulation_only": True})
                    conn.commit()
                finally:
                    conn.close()
        await asyncio.sleep(SCHEDULER_TICK_SECONDS)


def start_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(scheduler_loop())


async def stop_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task is None:
        return
    _scheduler_task.cancel()
    with suppress(asyncio.CancelledError):
        await _scheduler_task
    _scheduler_task = None
