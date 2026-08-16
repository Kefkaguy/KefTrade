"""Point-in-time predictor construction for the governed 5m sector lead/lag study."""

from __future__ import annotations

from typing import Any

import psycopg

from app.services.intraday_sector_leadlag_spec import (
    EXCLUDED_TARGETS,
    MIN_HISTORY_SESSIONS,
    MIN_SECTOR_MEMBERS,
    STATE_NEGATIVE_PEER_IMPULSE,
    STATE_POSITIVE_PEER_IMPULSE,
    Z_THRESHOLD,
    _stable_hash,
)
from app.services.research_splits import get_dataset_splits


def _dataset_fingerprint(conn: psycopg.Connection, dataset_id: int) -> dict[str, Any]:
    manifest = conn.execute(
        """
        SELECT id, content_hash, immutable, dataset_kind, assets, timeframes,
               window_start, window_end
        FROM research_dataset_manifests
        WHERE id = %s
        """,
        (dataset_id,),
    ).fetchone()
    if not manifest:
        raise ValueError(f"Unknown research dataset {dataset_id}")
    if not bool(manifest["immutable"]):
        raise ValueError("Sector lead/lag research requires an immutable dataset")

    sources = conn.execute(
        """
        SELECT ARRAY_AGG(DISTINCT source ORDER BY source) AS sources
        FROM research_dataset_candles
        WHERE dataset_id = %s AND timeframe = '1m'
        """,
        (dataset_id,),
    ).fetchone()

    mapping_rows = conn.execute(
        """
        WITH universe AS (
            SELECT DISTINCT symbol
            FROM research_dataset_candles
            WHERE dataset_id = %s
              AND timeframe = '1m'
              AND symbol <> ALL(%s)
        )
        SELECT u.symbol, s.sector
        FROM universe u
        LEFT JOIN symbols s ON s.symbol = u.symbol
        ORDER BY u.symbol
        """,
        (dataset_id, list(EXCLUDED_TARGETS)),
    ).fetchall()
    mapping = [
        {"symbol": str(row["symbol"]).upper(), "sector": row.get("sector")}
        for row in mapping_rows
    ]
    if any(row["sector"] is None for row in mapping):
        missing = [row["symbol"] for row in mapping if row["sector"] is None]
        raise ValueError(f"Missing sector mapping for dataset symbols: {missing}")

    return {
        "dataset_id": dataset_id,
        "dataset_content_hash": str(manifest["content_hash"]),
        "dataset_kind": str(manifest["dataset_kind"]),
        "window_start": manifest.get("window_start"),
        "window_end": manifest.get("window_end"),
        "sources": list((sources or {}).get("sources") or []),
        "sector_mapping": mapping,
        "sector_mapping_hash": _stable_hash(mapping),
    }


def _assert_fingerprint(conn: psycopg.Connection, declaration: dict[str, Any]) -> None:
    current = _dataset_fingerprint(conn, int(declaration["dataset_id"]))
    expected = dict(declaration["predictor_fingerprint"])
    if _stable_hash(current) != _stable_hash(expected):
        raise ValueError(
            "Dataset or sector mapping changed after declaration. Create a new declaration; "
            "do not mix a revised peer universe into spent evidence."
        )


def _drop_predictor_temp_tables(conn: psycopg.Connection) -> None:
    for table in (
        "tmp_sector_leadlag_final_states",
        "tmp_sector_leadlag_grid",
        "tmp_sector_leadlag_states",
        "tmp_sector_leadlag_scores",
        "tmp_sector_leadlag_peer_impulses",
        "tmp_sector_leadlag_spy_blocks",
        "tmp_sector_leadlag_sector_blocks",
        "tmp_sector_leadlag_blocks",
        "tmp_sector_leadlag_eligible",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def _build_predictor_states(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    include_confirmation: bool,
) -> None:
    """Materialize point-in-time predictor states without reading forward prices."""
    splits = get_dataset_splits(conn, dataset_id)
    if splits is None:
        raise ValueError(f"Dataset {dataset_id} has no frozen nested research split")

    _drop_predictor_temp_tables(conn)
    cutoff = None if include_confirmation else splits.confirmation_start
    cutoff_clause = "" if cutoff is None else "AND c.timestamp < %(cutoff)s"
    params = {"dataset_id": dataset_id, "cutoff": cutoff}

    conn.execute(
        """
        CREATE TEMP TABLE tmp_sector_leadlag_eligible ON COMMIT DROP AS
        WITH universe AS (
            SELECT DISTINCT symbol
            FROM research_dataset_candles
            WHERE dataset_id = %(dataset_id)s
              AND timeframe = '1m'
              AND symbol <> ALL(%(excluded)s)
        ), mapped AS (
            SELECT u.symbol, s.sector
            FROM universe u
            JOIN symbols s ON s.symbol = u.symbol
            WHERE s.sector IS NOT NULL
        ), sector_counts AS (
            SELECT sector, COUNT(*) AS members
            FROM mapped
            GROUP BY sector
        )
        SELECT m.symbol, m.sector, sc.members
        FROM mapped m
        JOIN sector_counts sc ON sc.sector = m.sector
        WHERE sc.members >= %(min_sector_members)s
        """,
        {
            "dataset_id": dataset_id,
            "excluded": list(EXCLUDED_TARGETS),
            "min_sector_members": MIN_SECTOR_MEMBERS,
        },
    )
    conn.execute("CREATE UNIQUE INDEX ON tmp_sector_leadlag_eligible(symbol)")

    conn.execute(
        f"""
        CREATE TEMP TABLE tmp_sector_leadlag_blocks ON COMMIT DROP AS
        WITH minute_rows AS (
            SELECT
                e.symbol,
                e.sector,
                e.members,
                c.timestamp,
                c.open,
                c.close,
                (c.timestamp AT TIME ZONE 'America/New_York')::date AS session_date,
                (
                    EXTRACT(HOUR FROM c.timestamp AT TIME ZONE 'America/New_York')::int * 60
                    + EXTRACT(MINUTE FROM c.timestamp AT TIME ZONE 'America/New_York')::int
                ) AS minute_of_day
            FROM research_dataset_candles c
            JOIN tmp_sector_leadlag_eligible e ON e.symbol = c.symbol
            WHERE c.dataset_id = %(dataset_id)s
              AND c.timeframe = '1m'
              AND (
                    EXTRACT(HOUR FROM c.timestamp AT TIME ZONE 'America/New_York')::int * 60
                    + EXTRACT(MINUTE FROM c.timestamp AT TIME ZONE 'America/New_York')::int
                  ) BETWEEN 570 AND 944
              {cutoff_clause}
        ), tagged AS (
            SELECT *,
                   (minute_of_day - 570) / 5 AS slot_index,
                   MOD(minute_of_day - 570, 5) AS minute_offset
            FROM minute_rows
        ), aggregated AS (
            SELECT
                symbol, sector, members, session_date, slot_index,
                MIN(timestamp) AS block_start,
                MAX(open) FILTER (WHERE minute_offset = 0) AS block_open,
                MAX(close) FILTER (WHERE minute_offset = 4) AS block_close,
                COUNT(*) AS minute_count,
                COUNT(DISTINCT minute_offset) AS distinct_offsets
            FROM tagged
            GROUP BY symbol, sector, members, session_date, slot_index
            HAVING COUNT(*) = 5
               AND COUNT(DISTINCT minute_offset) = 5
               AND MIN(minute_offset) = 0
               AND MAX(minute_offset) = 4
        )
        SELECT
            symbol, sector, members, block_start,
            block_start + INTERVAL '5 minutes' AS decision_at,
            session_date,
            slot_index * 5 AS slot_minute,
            block_close / NULLIF(block_open, 0) - 1.0 AS return_5m
        FROM aggregated
        WHERE block_open > 0
          AND slot_index BETWEEN 0 AND 74
        """,
        params,
    )
    conn.execute("CREATE INDEX ON tmp_sector_leadlag_blocks(sector, block_start)")
    conn.execute(
        "CREATE INDEX ON tmp_sector_leadlag_blocks(symbol, slot_minute, session_date)"
    )

    conn.execute(
        """
        CREATE TEMP TABLE tmp_sector_leadlag_sector_blocks ON COMMIT DROP AS
        SELECT
            sector,
            block_start,
            COUNT(*) AS observed_members,
            SUM(return_5m) AS sector_return_sum
        FROM tmp_sector_leadlag_blocks
        GROUP BY sector, block_start
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX ON tmp_sector_leadlag_sector_blocks(sector, block_start)"
    )

    conn.execute(
        """
        CREATE TEMP TABLE tmp_sector_leadlag_spy_blocks ON COMMIT DROP AS
        WITH minute_rows AS (
            SELECT
                c.timestamp,
                c.open,
                c.close,
                (c.timestamp AT TIME ZONE 'America/New_York')::date AS session_date,
                (
                    EXTRACT(HOUR FROM c.timestamp AT TIME ZONE 'America/New_York')::int * 60
                    + EXTRACT(MINUTE FROM c.timestamp AT TIME ZONE 'America/New_York')::int
                ) AS minute_of_day
            FROM research_dataset_candles c
            WHERE c.dataset_id = %(dataset_id)s
              AND c.timeframe = '1m'
              AND c.symbol = 'SPY'
              AND (
                    EXTRACT(HOUR FROM c.timestamp AT TIME ZONE 'America/New_York')::int * 60
                    + EXTRACT(MINUTE FROM c.timestamp AT TIME ZONE 'America/New_York')::int
                  ) BETWEEN 570 AND 944
              """ + cutoff_clause + """
        ), tagged AS (
            SELECT *,
                   (minute_of_day - 570) / 5 AS slot_index,
                   MOD(minute_of_day - 570, 5) AS minute_offset
            FROM minute_rows
        ), aggregated AS (
            SELECT
                session_date, slot_index,
                MIN(timestamp) AS block_start,
                MAX(open) FILTER (WHERE minute_offset = 0) AS block_open,
                MAX(close) FILTER (WHERE minute_offset = 4) AS block_close,
                COUNT(*) AS minute_count,
                COUNT(DISTINCT minute_offset) AS distinct_offsets
            FROM tagged
            GROUP BY session_date, slot_index
            HAVING COUNT(*) = 5
               AND COUNT(DISTINCT minute_offset) = 5
               AND MIN(minute_offset) = 0
               AND MAX(minute_offset) = 4
        )
        SELECT
            block_start,
            block_start + INTERVAL '5 minutes' AS decision_at,
            session_date,
            slot_index * 5 AS slot_minute,
            block_close / NULLIF(block_open, 0) - 1.0 AS spy_return_5m
        FROM aggregated
        WHERE block_open > 0
          AND slot_index BETWEEN 0 AND 74
        """,
        params,
    )
    conn.execute("CREATE UNIQUE INDEX ON tmp_sector_leadlag_spy_blocks(block_start)")

    conn.execute(
        """
        CREATE TEMP TABLE tmp_sector_leadlag_peer_impulses ON COMMIT DROP AS
        SELECT
            b.symbol,
            b.sector,
            b.block_start,
            b.decision_at,
            b.session_date,
            b.slot_minute,
            sb.observed_members - 1 AS peer_count,
            ((sb.sector_return_sum - b.return_5m) / NULLIF(sb.observed_members - 1, 0))
                AS peer_return_5m,
            spy.spy_return_5m,
            ((sb.sector_return_sum - b.return_5m) / NULLIF(sb.observed_members - 1, 0))
                - spy.spy_return_5m AS peer_excess_5m
        FROM tmp_sector_leadlag_blocks b
        JOIN tmp_sector_leadlag_sector_blocks sb
          ON sb.sector = b.sector
         AND sb.block_start = b.block_start
        JOIN tmp_sector_leadlag_spy_blocks spy
          ON spy.block_start = b.block_start
        WHERE sb.observed_members = b.members
        """
    )
    conn.execute(
        "CREATE INDEX ON tmp_sector_leadlag_peer_impulses(symbol, slot_minute, session_date)"
    )

    conn.execute(
        """
        CREATE TEMP TABLE tmp_sector_leadlag_scores ON COMMIT DROP AS
        WITH normalized AS (
            SELECT
                p.*,
                COUNT(*) OVER (
                    PARTITION BY symbol, slot_minute
                    ORDER BY session_date
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                ) AS history_n,
                AVG(peer_excess_5m) OVER (
                    PARTITION BY symbol, slot_minute
                    ORDER BY session_date
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                ) AS historical_mean,
                STDDEV_SAMP(peer_excess_5m) OVER (
                    PARTITION BY symbol, slot_minute
                    ORDER BY session_date
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                ) AS historical_sd
            FROM tmp_sector_leadlag_peer_impulses p
        )
        SELECT
            n.*,
            (n.peer_excess_5m - n.historical_mean) / NULLIF(n.historical_sd, 0)
                AS peer_impulse_z
        FROM normalized n
        WHERE n.history_n >= %(min_history)s
          AND n.historical_sd > 0
        """,
        {"min_history": MIN_HISTORY_SESSIONS},
    )

    conn.execute(
        """
        CREATE TEMP TABLE tmp_sector_leadlag_states ON COMMIT DROP AS
        SELECT
            x.*,
            CASE
                WHEN x.peer_impulse_z >= %(z)s THEN %(positive)s
                WHEN x.peer_impulse_z <= -%(z)s THEN %(negative)s
            END AS state,
            CASE
                WHEN x.decision_at < %(validation_start)s THEN 'discovery'
                WHEN x.decision_at < %(confirmation_start)s THEN 'validation'
                ELSE 'confirmation'
            END AS phase
        FROM tmp_sector_leadlag_scores x
        WHERE ABS(x.peer_impulse_z) >= %(z)s
        """,
        {
            "z": Z_THRESHOLD,
            "positive": STATE_POSITIVE_PEER_IMPULSE,
            "negative": STATE_NEGATIVE_PEER_IMPULSE,
            "validation_start": splits.validation_start,
            "confirmation_start": splits.confirmation_start,
        },
    )
    conn.execute("CREATE INDEX ON tmp_sector_leadlag_states(symbol, decision_at)")

    grid_cutoff_clause = "" if cutoff is None else "AND c.timestamp < %(grid_cutoff)s"
    grid_params = {"dataset_id": dataset_id, "grid_cutoff": cutoff}
    conn.execute(
        f"""
        CREATE TEMP TABLE tmp_sector_leadlag_grid ON COMMIT DROP AS
        WITH symbols_needed AS (
            SELECT symbol FROM tmp_sector_leadlag_eligible
            UNION ALL SELECT 'SPY'
        ), distinct_minutes AS (
            SELECT DISTINCT
                c.symbol,
                c.timestamp,
                (c.timestamp AT TIME ZONE 'America/New_York')::date AS session_date
            FROM research_dataset_candles c
            JOIN symbols_needed s ON s.symbol = c.symbol
            WHERE c.dataset_id = %(dataset_id)s
              AND c.timeframe = '1m'
              AND (c.timestamp AT TIME ZONE 'America/New_York')::time
                    BETWEEN TIME '09:30' AND TIME '15:59'
              {grid_cutoff_clause}
        ), sequenced AS (
            SELECT
                symbol,
                timestamp,
                LEAD(timestamp, 14) OVER (
                    PARTITION BY symbol, session_date
                    ORDER BY timestamp
                ) AS minute_14
            FROM distinct_minutes
        )
        SELECT symbol, timestamp AS decision_at
        FROM sequenced
        WHERE minute_14 = timestamp + INTERVAL '14 minutes'
        """,
        grid_params,
    )
    conn.execute("CREATE UNIQUE INDEX ON tmp_sector_leadlag_grid(symbol, decision_at)")

    conn.execute(
        """
        CREATE TEMP TABLE tmp_sector_leadlag_final_states ON COMMIT DROP AS
        SELECT s.*
        FROM tmp_sector_leadlag_states s
        JOIN tmp_sector_leadlag_grid target_grid
          ON target_grid.symbol = s.symbol
         AND target_grid.decision_at = s.decision_at
        JOIN tmp_sector_leadlag_grid spy_grid
          ON spy_grid.symbol = 'SPY'
         AND spy_grid.decision_at = s.decision_at
        """
    )
    conn.execute("CREATE INDEX ON tmp_sector_leadlag_final_states(phase, state)")


def _predictor_supply(conn: psycopg.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT
            phase,
            state,
            COUNT(*) AS events,
            COUNT(DISTINCT symbol) AS symbols,
            COUNT(DISTINCT sector) AS sectors,
            COUNT(DISTINCT session_date) AS sessions,
            PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY ABS(peer_impulse_z)) AS median_abs_z,
            PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY ABS(peer_impulse_z)) AS p90_abs_z
        FROM tmp_sector_leadlag_final_states
        GROUP BY phase, state
        ORDER BY phase, state
        """
    ).fetchall()
    concentration = conn.execute(
        """
        SELECT
            phase, state, sector,
            COUNT(*) AS events,
            COUNT(DISTINCT symbol) AS symbols,
            COUNT(DISTINCT session_date) AS sessions
        FROM tmp_sector_leadlag_final_states
        GROUP BY phase, state, sector
        ORDER BY phase, state, events DESC
        """
    ).fetchall()

    summary: dict[str, Any] = {}
    for row in rows:
        phase = str(row["phase"])
        state = str(row["state"])
        summary.setdefault(phase, {})[state] = {
            "events": int(row["events"]),
            "symbols": int(row["symbols"]),
            "sectors": int(row["sectors"]),
            "sessions": int(row["sessions"]),
            "events_per_session": float(row["events"]) / max(int(row["sessions"]), 1),
            "median_abs_z": float(row["median_abs_z"]),
            "p90_abs_z": float(row["p90_abs_z"]),
        }
    for row in concentration:
        phase = str(row["phase"])
        state = str(row["state"])
        if phase not in summary or state not in summary[phase]:
            continue
        total = int(summary[phase][state]["events"])
        summary[phase][state].setdefault("sector_concentration", []).append(
            {
                "sector": str(row["sector"]),
                "events": int(row["events"]),
                "symbols": int(row["symbols"]),
                "sessions": int(row["sessions"]),
                "share": int(row["events"]) / max(total, 1),
            }
        )
    return summary
