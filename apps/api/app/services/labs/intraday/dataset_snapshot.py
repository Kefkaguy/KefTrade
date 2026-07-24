"""Phase 12.5 Step 2: immutable dataset snapshots for intraday research.

Mirrors `app.services.research_architecture.record_dataset_snapshot` /
`load_snapshot_candles`, which already materialize exact, content-hashed,
trigger-enforced-immutable candle snapshots for swing research (that
function's own SQL reads the generic `candles` table, unmodified here). This
module adds the intraday-specific half: also snapshotting `intraday_features`
into `research_dataset_intraday_features` (added in migration 047), and
tagging the manifest row `dataset_kind='intraday'` so a loader knows which
companion table backs it.

Nothing in this module touches `backtester.py`, any strategy's decision
logic, or the elite/validation gates. It only decides which frozen rows a
future backtest reads from -- the simulation itself is unchanged.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.services.research_architecture import DATASET_VERSION, jsonable, load_snapshot_candles, stable_hash

__all__ = ["record_intraday_dataset_snapshot", "load_snapshot_intraday_features", "load_snapshot_candles"]


def record_intraday_dataset_snapshot(
    conn: psycopg.Connection,
    *,
    assets: list[str],
    timeframes: list[str],
    mode: str = "rolling",
    name: str | None = None,
) -> dict[str, Any]:
    """Materialize an exact immutable candles + intraday_features snapshot.

    Idempotent by content hash, exactly like the swing
    `record_dataset_snapshot`: an identical (assets, timeframes, mode) combo
    over unchanged underlying data reuses the existing manifest row instead
    of creating a duplicate.
    """

    if mode not in {"rolling", "reproducibility"}:
        raise ValueError("dataset mode must be 'rolling' or 'reproducibility'")
    normalized_assets = sorted({item.strip().upper() for item in assets if item.strip()})
    normalized_timeframes = sorted({item.strip() for item in timeframes if item.strip()})
    if not normalized_assets or not normalized_timeframes:
        raise ValueError("dataset snapshot requires at least one asset and timeframe")

    summaries: list[dict[str, Any]] = []
    for symbol in normalized_assets:
        for timeframe in normalized_timeframes:
            candle_row = conn.execute(
                """
                SELECT COUNT(*) AS candle_count, MIN(timestamp) AS window_start, MAX(timestamp) AS window_end,
                       MD5(COALESCE(STRING_AGG(
                           CONCAT_WS('|', source, timestamp::text, open::text, high::text, low::text, close::text, volume::text),
                           '||' ORDER BY timestamp, source
                       ), '')) AS candle_hash,
                       ARRAY_AGG(DISTINCT source ORDER BY source) AS sources
                FROM candles
                WHERE symbol = %s AND timeframe = %s
                """,
                (symbol, timeframe),
            ).fetchone()
            candle_count = int(candle_row.get("candle_count") or 0)
            if candle_count == 0:
                raise ValueError(f"cannot snapshot missing candle dataset {symbol} {timeframe}")

            feature_row = conn.execute(
                """
                SELECT COUNT(*) AS feature_count,
                       MD5(COALESCE(STRING_AGG(
                           CONCAT_WS('|', timestamp::text, session_date::text,
                               COALESCE(minutes_from_open::text, ''), COALESCE(minutes_to_close::text, ''),
                               COALESCE(session_vwap::text, ''), COALESCE(gap_percent::text, '')),
                           '||' ORDER BY timestamp
                       ), '')) AS feature_hash
                FROM intraday_features
                WHERE symbol = %s AND timeframe = %s
                """,
                (symbol, timeframe),
            ).fetchone()
            feature_count = int(feature_row.get("feature_count") or 0)
            if feature_count == 0:
                raise ValueError(
                    f"cannot snapshot {symbol} {timeframe}: no intraday_features rows. "
                    "Run the intraday features backfill first."
                )

            summaries.append(
                {
                    "key": f"{symbol}|{timeframe}",
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "candle_count": candle_count,
                    "feature_count": feature_count,
                    "window_start": candle_row.get("window_start"),
                    "window_end": candle_row.get("window_end"),
                    "candle_hash": str(candle_row.get("candle_hash") or ""),
                    "feature_hash": str(feature_row.get("feature_hash") or ""),
                    "sources": list(candle_row.get("sources") or []),
                }
            )

    content_hash = stable_hash(
        {
            "kind": "intraday",
            "mode": mode,
            "assets": normalized_assets,
            "timeframes": normalized_timeframes,
            "datasets": [
                {key: jsonable(item[key]) for key in ("key", "candle_count", "feature_count", "window_start", "window_end", "candle_hash", "feature_hash", "sources")}
                for item in summaries
            ],
            "calculation_version": DATASET_VERSION,
        }
    )
    dataset_key = f"intraday_dataset_{content_hash[:24]}"
    counts = {item["key"]: item["candle_count"] for item in summaries}
    hashes = {item["key"]: item["candle_hash"] for item in summaries}
    sources = sorted({source for item in summaries for source in item["sources"]})
    window_starts = [item["window_start"] for item in summaries if item["window_start"] is not None]
    window_ends = [item["window_end"] for item in summaries if item["window_end"] is not None]

    row = conn.execute(
        """
        INSERT INTO research_dataset_manifests(
            dataset_key, name, mode, snapshot_version, assets, timeframes, window_start, window_end,
            candle_counts, candle_hashes, source_providers, content_hash, integrity,
            calculation_version, dataset_kind, immutable, simulation_only
        ) VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'intraday', TRUE, TRUE)
        ON CONFLICT(dataset_key) DO NOTHING
        RETURNING *
        """,
        (
            dataset_key,
            name or f"Intraday {mode} snapshot: {', '.join(normalized_assets)} / {', '.join(normalized_timeframes)}",
            mode,
            Jsonb(normalized_assets),
            Jsonb(normalized_timeframes),
            min(window_starts) if window_starts else None,
            max(window_ends) if window_ends else None,
            Jsonb(counts),
            Jsonb(hashes),
            Jsonb(sources),
            content_hash,
            Jsonb({"verified_at_creation": True, "dataset_count": len(summaries), "exact_candles_materialized": True, "exact_intraday_features_materialized": True}),
            DATASET_VERSION,
        ),
    ).fetchone()
    if not row:
        row = conn.execute("SELECT * FROM research_dataset_manifests WHERE dataset_key = %s", (dataset_key,)).fetchone()
    dataset_id = int(row["id"])

    for item in summaries:
        conn.execute(
            """
            INSERT INTO research_dataset_candles(dataset_id, symbol, source, timeframe, timestamp, open, high, low, close, volume)
            SELECT %s, symbol, source, timeframe, timestamp, open, high, low, close, volume
            FROM candles
            WHERE symbol = %s AND timeframe = %s AND timestamp BETWEEN %s AND %s
            ON CONFLICT(dataset_id, symbol, timeframe, timestamp, source) DO NOTHING
            """,
            (dataset_id, item["symbol"], item["timeframe"], item["window_start"], item["window_end"]),
        )
        conn.execute(
            """
            INSERT INTO research_dataset_intraday_features(
                dataset_id, symbol, timeframe, timestamp, session_date, minutes_from_open, minutes_to_close,
                session_vwap, distance_from_session_vwap, opening_range_high, opening_range_low,
                opening_range_position, gap_percent, session_relative_volume
            )
            SELECT %s, symbol, timeframe, timestamp, session_date, minutes_from_open, minutes_to_close,
                   session_vwap, distance_from_session_vwap, opening_range_high, opening_range_low,
                   opening_range_position, gap_percent, session_relative_volume
            FROM intraday_features
            WHERE symbol = %s AND timeframe = %s AND timestamp BETWEEN %s AND %s
            ON CONFLICT (dataset_id, symbol, timeframe, timestamp) DO NOTHING
            """,
            (dataset_id, item["symbol"], item["timeframe"], item["window_start"], item["window_end"]),
        )
    conn.commit()
    return jsonable(dict(row))


def load_snapshot_intraday_features(conn: psycopg.Connection, dataset_id: int, symbol: str, timeframe: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT symbol, timeframe, timestamp, session_date, minutes_from_open, minutes_to_close,
                   session_vwap, distance_from_session_vwap, opening_range_high, opening_range_low,
                   opening_range_position, gap_percent, session_relative_volume
            FROM research_dataset_intraday_features
            WHERE dataset_id = %s AND symbol = %s AND timeframe = %s
            ORDER BY timestamp ASC
            """,
            (dataset_id, symbol, timeframe),
        ).fetchall()
    ]
