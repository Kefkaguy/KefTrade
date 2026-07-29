"""Backend-only storage and readiness checks for institutional intraday data."""

from __future__ import annotations

from datetime import date
from typing import Any, Sequence

import psycopg
from psycopg.types.json import Jsonb

RESEARCH_DATA_VERSION = "intraday_research_data_v1"


def persist_auction_imbalances(
    conn: psycopg.Connection,
    rows: Sequence[dict[str, Any]],
) -> int:
    """Append event-time auction evidence; duplicate vendor messages are ignored."""
    inserted = 0
    for row in rows:
        result = conn.execute(
            """
            INSERT INTO intraday_auction_imbalances(
                symbol, exchange, auction_type, timestamp, session_date,
                reference_price, midpoint_at_message, near_clearing_price,
                far_clearing_price, auction_price,
                paired_quantity, imbalance_quantity, imbalance_side,
                provider, raw_payload
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s
            )
            ON CONFLICT(symbol, exchange, auction_type, timestamp, provider)
            DO NOTHING
            RETURNING id
            """,
            (
                str(row["symbol"]).upper(),
                str(row["exchange"]).upper(),
                row["auction_type"],
                row["timestamp"],
                row["session_date"],
                row.get("reference_price"),
                row.get("midpoint_at_message"),
                row.get("near_clearing_price"),
                row.get("far_clearing_price"),
                row.get("auction_price"),
                row.get("paired_quantity"),
                row["imbalance_quantity"],
                row["imbalance_side"],
                row["provider"],
                Jsonb(row.get("raw_payload") or {}),
            ),
        ).fetchone()
        inserted += int(result is not None)
    return inserted


def persist_point_in_time_membership(
    conn: psycopg.Connection,
    rows: Sequence[dict[str, Any]],
) -> int:
    inserted = 0
    for row in rows:
        result = conn.execute(
            """
            INSERT INTO research_point_in_time_universe_membership(
                universe_key, symbol, effective_from, effective_to, source, evidence
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT(universe_key, symbol, effective_from, source) DO NOTHING
            RETURNING id
            """,
            (
                row["universe_key"],
                str(row["symbol"]).upper(),
                row["effective_from"],
                row.get("effective_to"),
                row["source"],
                Jsonb(row.get("evidence") or {}),
            ),
        ).fetchone()
        inserted += int(result is not None)
    return inserted


def persist_corporate_actions(
    conn: psycopg.Connection,
    rows: Sequence[dict[str, Any]],
) -> int:
    inserted = 0
    for row in rows:
        result = conn.execute(
            """
            INSERT INTO research_corporate_actions(
                symbol, action_type, effective_date, adjustment_factor,
                cash_amount, source, evidence
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(symbol, action_type, effective_date, source) DO NOTHING
            RETURNING id
            """,
            (
                str(row["symbol"]).upper(),
                row["action_type"],
                row["effective_date"],
                row.get("adjustment_factor"),
                row.get("cash_amount"),
                row["source"],
                Jsonb(row.get("evidence") or {}),
            ),
        ).fetchone()
        inserted += int(result is not None)
    return inserted


def research_data_readiness(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    timeframe: str,
    universe_key: str | None = None,
) -> dict[str, Any]:
    """Audit evidence coverage without touching campaigns or broker state."""
    manifest = conn.execute(
        "SELECT * FROM research_dataset_manifests WHERE id = %s",
        (dataset_id,),
    ).fetchone()
    if not manifest:
        raise ValueError(f"No dataset manifest for dataset_id={dataset_id}.")
    assets = {str(value).upper() for value in (manifest["assets"] or [])}
    candle = conn.execute(
        """
        SELECT COUNT(*) AS rows,
               COUNT(DISTINCT symbol) AS symbols,
               COUNT(DISTINCT session_date) AS sessions,
               COUNT(*) FILTER (
                   WHERE midpoint_at_message IS NOT NULL
                     AND auction_price IS NOT NULL
               ) AS executable_rows
        FROM research_dataset_intraday_features
        WHERE dataset_id = %s AND timeframe = %s
        """,
        (dataset_id, timeframe),
    ).fetchone()
    micro = conn.execute(
        """
        SELECT COUNT(DISTINCT (micro.symbol, micro.timestamp)) AS rows,
               COUNT(DISTINCT micro.symbol) AS symbols
        FROM intraday_microstructure_features micro
        JOIN research_dataset_intraday_features snapshot
          ON snapshot.dataset_id = %s
         AND snapshot.symbol = micro.symbol
         AND snapshot.timeframe = micro.timeframe
         AND snapshot.timestamp = micro.timestamp
        WHERE micro.timeframe = %s
          AND micro.symbol = ANY(%s)
        """,
        (
            dataset_id,
            timeframe,
            list(assets),
        ),
    ).fetchone()
    auction = conn.execute(
        """
        SELECT COUNT(*) AS rows,
               COUNT(DISTINCT symbol) AS symbols,
               COUNT(DISTINCT session_date) AS sessions
        FROM intraday_auction_imbalances
        WHERE symbol = ANY(%s)
          AND (%s IS NULL OR timestamp >= %s)
          AND (%s IS NULL OR timestamp <= %s)
        """,
        (
            list(assets),
            manifest["window_start"],
            manifest["window_start"],
            manifest["window_end"],
            manifest["window_end"],
        ),
    ).fetchone()
    membership_symbols = 0
    if universe_key:
        first_date = _as_date(manifest["window_start"])
        last_date = _as_date(manifest["window_end"])
        membership = conn.execute(
            """
            SELECT COUNT(DISTINCT symbol) AS symbols
            FROM research_point_in_time_universe_membership
            WHERE universe_key = %s
              AND effective_from <= %s
              AND (effective_to IS NULL OR effective_to >= %s)
            """,
            (universe_key, last_date, first_date),
        ).fetchone()
        membership_symbols = int((membership or {}).get("symbols") or 0)

    candle_rows = int((candle or {}).get("rows") or 0)
    micro_rows = int((micro or {}).get("rows") or 0)
    quote_coverage = min(1.0, micro_rows / max(1, candle_rows))
    integrity = dict(manifest["integrity"] or {})
    gates = {
        "minimum_20_symbols": int((candle or {}).get("symbols") or 0) >= 20,
        "minimum_252_sessions": int((candle or {}).get("sessions") or 0) >= 252,
        "microstructure_80pct_coverage": quote_coverage >= 0.80,
        "point_in_time_universe": (
            membership_symbols >= max(1, int(len(assets) * 0.80))
            if universe_key
            else bool(integrity.get("point_in_time_universe"))
        ),
        "corporate_actions_adjusted": bool(
            integrity.get("corporate_actions_adjusted")
            or integrity.get("adjusted_prices")
        ),
    }
    return {
        "calculation_version": RESEARCH_DATA_VERSION,
        "dataset_id": dataset_id,
        "timeframe": timeframe,
        "manifest_window": {
            "start": manifest["window_start"],
            "end": manifest["window_end"],
        },
        "assets": len(assets),
        "candle_features": {
            "rows": candle_rows,
            "symbols": int((candle or {}).get("symbols") or 0),
            "sessions": int((candle or {}).get("sessions") or 0),
        },
        "microstructure": {
            "rows": micro_rows,
            "symbols": int((micro or {}).get("symbols") or 0),
            "coverage": round(quote_coverage, 6),
        },
        "auction_imbalances": {
            "rows": int((auction or {}).get("rows") or 0),
            "symbols": int((auction or {}).get("symbols") or 0),
            "sessions": int((auction or {}).get("sessions") or 0),
            "executable_rows": int((auction or {}).get("executable_rows") or 0),
            "ready": int((auction or {}).get("executable_rows") or 0) > 0,
        },
        "point_in_time_membership_symbols": membership_symbols,
        "gates": gates,
        "institutional_candle_ready": all(
            gates[key]
            for key in (
                "minimum_20_symbols",
                "minimum_252_sessions",
                "point_in_time_universe",
                "corporate_actions_adjusted",
            )
        ),
        "institutional_execution_ready": all(gates.values()),
        "limitations": [key for key, passed in gates.items() if not passed],
    }


def _as_date(value: Any) -> date:
    if value is None:
        return date.min
    return value.date() if hasattr(value, "date") else value
