"""Append-only research-trial ledger and predeclaration enforcement.

A multiple-testing correction applied to the tests in one run understates the
problem badly: the selection happened across every test ever run against this
data, not across the twelve that happen to be in today's argument list.  This
module reads the cumulative ledger so the deflated Sharpe and the false
discovery rate are charged the real trial count, and it refuses to score a
factor that was not declared before its result existed.

The module contains no campaign, broker, order-submission, or UI code.
"""

from __future__ import annotations

from hashlib import sha256
from json import dumps
from typing import Any, Sequence

import psycopg
from psycopg.types.json import Jsonb

TRIAL_LEDGER_VERSION = "intraday_trial_ledger_v1"


def trial_fingerprint(*, spec_hash: str, factor_key: str) -> str:
    """The identity `persist_factor_run` writes for a factor trial."""
    return sha256(f"{spec_hash}|{factor_key}".encode()).hexdigest()


def declaration_fingerprint(
    *,
    purpose: str,
    timeframe: str,
    factor_keys: Sequence[str],
    hypothesis: str | None,
) -> str:
    payload = {
        "purpose": purpose,
        "timeframe": timeframe,
        "factor_keys": sorted(set(factor_keys)),
        "hypothesis": hypothesis or "",
        "ledger_version": TRIAL_LEDGER_VERSION,
    }
    return sha256(dumps(payload, sort_keys=True).encode()).hexdigest()


def declare_trials(
    conn: psycopg.Connection,
    *,
    purpose: str,
    timeframe: str,
    factor_keys: Sequence[str],
    dataset_id: int | None = None,
    hypothesis: str | None = None,
    protocol_version: str,
) -> dict[str, Any]:
    """Register a predeclared test list. Idempotent on identical content."""
    keys = list(dict.fromkeys(factor_keys))
    if not keys:
        raise ValueError("A declaration must name at least one factor key.")
    fingerprint = declaration_fingerprint(
        purpose=purpose,
        timeframe=timeframe,
        factor_keys=keys,
        hypothesis=hypothesis,
    )
    existing = conn.execute(
        "SELECT * FROM intraday_research_trial_declarations WHERE declaration_fingerprint = %s",
        (fingerprint,),
    ).fetchone()
    if existing:
        return {**dict(existing), "already_declared": True}
    row = conn.execute(
        """
        INSERT INTO intraday_research_trial_declarations(
            declaration_fingerprint, purpose, timeframe, dataset_id,
            declared_factor_keys, declared_test_count, hypothesis, protocol_version
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            fingerprint,
            purpose,
            timeframe,
            dataset_id,
            Jsonb(keys),
            len(keys),
            hypothesis,
            protocol_version,
        ),
    ).fetchone()
    conn.commit()
    return {**dict(row), "already_declared": False}


def load_declaration(conn: psycopg.Connection, declaration_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM intraday_research_trial_declarations WHERE id = %s",
        (declaration_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"No trial declaration id={declaration_id}.")
    return dict(row)


def assert_declared(
    declaration: dict[str, Any],
    *,
    timeframe: str,
    factor_keys: Sequence[str],
) -> dict[str, Any]:
    """Refuse to score a factor that the declaration does not cover."""
    declared = {str(key) for key in (declaration.get("declared_factor_keys") or [])}
    requested = {str(key) for key in factor_keys}
    undeclared = sorted(requested - declared)
    if str(declaration.get("timeframe")) != timeframe:
        raise ValueError(
            f"Declaration {declaration.get('id')} is for timeframe "
            f"{declaration.get('timeframe')}, not {timeframe}."
        )
    if undeclared:
        raise ValueError(
            "These factors were not predeclared and cannot be scored under "
            f"declaration {declaration.get('id')}: {undeclared}. "
            "Declare them first, or run them as a new declaration whose tests "
            "all count toward the multiple-testing correction."
        )
    return {
        "declaration_id": declaration.get("id"),
        "declared_test_count": int(declaration.get("declared_test_count") or 0),
        "tested_factor_keys": sorted(requested),
        "declared_but_not_tested": sorted(declared - requested),
        "declared_at": declaration.get("created_at"),
    }


def ledger_trial_count(
    conn: psycopg.Connection,
    *,
    timeframe: str,
    trial_type: str = "factor",
) -> int:
    """Distinct trials of this type ever recorded at this timeframe."""
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT trial_fingerprint) AS trials
        FROM intraday_research_trials
        WHERE timeframe = %s AND trial_type = %s
        """,
        (timeframe, trial_type),
    ).fetchone()
    return int((row or {}).get("trials") or 0)


def declared_trial_count(conn: psycopg.Connection, *, timeframe: str) -> int:
    """Distinct factor keys ever predeclared at this timeframe.

    Declarations that were never run still consumed a look at the hypothesis
    space, so they count.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS trials FROM (
            SELECT DISTINCT jsonb_array_elements_text(declared_factor_keys) AS factor_key
            FROM intraday_research_trial_declarations
            WHERE timeframe = %s
        ) AS declared
        """,
        (timeframe,),
    ).fetchone()
    return int((row or {}).get("trials") or 0)


def effective_trials_for_run(
    conn: psycopg.Connection,
    *,
    timeframe: str,
    factor_keys: Sequence[str],
    spec_hash: str,
) -> dict[str, Any]:
    """The cumulative trial count this run must be corrected against.

    Historical trials plus the tests in this run that the ledger has not seen
    before, floored by the number of predeclared factor keys.  The count only
    ever grows, which is the point: reusing the same validation data for a
    thirteenth idea is a thirteen-trial problem, not a one-trial problem.
    """
    keys = list(dict.fromkeys(factor_keys))
    historical = ledger_trial_count(conn, timeframe=timeframe)
    fingerprints = [
        trial_fingerprint(spec_hash=spec_hash, factor_key=key) for key in keys
    ]
    rows = conn.execute(
        """
        SELECT DISTINCT trial_fingerprint
        FROM intraday_research_trials
        WHERE trial_fingerprint = ANY(%s)
        """,
        (fingerprints,),
    ).fetchall()
    already_recorded = {str(row["trial_fingerprint"]) for row in rows}
    new_trials = [item for item in fingerprints if item not in already_recorded]
    declared = declared_trial_count(conn, timeframe=timeframe)
    effective = max(len(keys), declared, historical + len(new_trials))
    return {
        "ledger_version": TRIAL_LEDGER_VERSION,
        "timeframe": timeframe,
        "historical_trials": historical,
        "declared_trials": declared,
        "repeat_trials_in_this_run": len(keys) - len(new_trials),
        "new_trials_in_this_run": len(new_trials),
        "effective_trials": effective,
    }


def record_declaration_use(
    conn: psycopg.Connection,
    *,
    declaration_id: int,
    run_id: int,
    factor_keys: Sequence[str],
) -> None:
    conn.execute(
        """
        INSERT INTO intraday_research_trial_declaration_uses(
            declaration_id, run_id, tested_factor_keys
        )
        VALUES (%s, %s, %s)
        ON CONFLICT (declaration_id, run_id) DO NOTHING
        """,
        (declaration_id, run_id, Jsonb(sorted(set(str(key) for key in factor_keys)))),
    )
    conn.commit()


def trial_ledger_summary(conn: psycopg.Connection, *, timeframe: str) -> dict[str, Any]:
    """Full accounting of what has been tried, for the diagnostic payload."""
    rows = conn.execute(
        """
        SELECT phase, architecture, COUNT(DISTINCT trial_fingerprint) AS trials,
               MIN(created_at) AS first_recorded, MAX(created_at) AS last_recorded
        FROM intraday_research_trials
        WHERE timeframe = %s AND trial_type = 'factor'
        GROUP BY phase, architecture
        ORDER BY phase, architecture
        """,
        (timeframe,),
    ).fetchall()
    return {
        "ledger_version": TRIAL_LEDGER_VERSION,
        "timeframe": timeframe,
        "total_distinct_trials": ledger_trial_count(conn, timeframe=timeframe),
        "declared_factor_keys": declared_trial_count(conn, timeframe=timeframe),
        "by_phase_and_architecture": [
            {
                "phase": str(row["phase"]),
                "architecture": str(row["architecture"]),
                "trials": int(row["trials"]),
                "first_recorded": row["first_recorded"],
                "last_recorded": row["last_recorded"],
            }
            for row in rows
        ],
    }
