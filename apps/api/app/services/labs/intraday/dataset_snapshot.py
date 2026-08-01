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

from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.services.research_architecture import jsonable, load_snapshot_candles, stable_hash

__all__ = ["record_intraday_dataset_snapshot", "load_snapshot_intraday_features", "load_snapshot_candles"]
INTRADAY_DATASET_VERSION = "intraday_dataset_v2_sip_microstructure"


def record_intraday_dataset_snapshot(
    conn: psycopg.Connection,
    *,
    assets: list[str],
    timeframes: list[str],
    mode: str = "rolling",
    name: str | None = None,
    universe_key: str | None = None,
    window_end: datetime | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Materialize an exact immutable candles + intraday_features snapshot.

    Idempotent by content hash, exactly like the swing
    `record_dataset_snapshot`: an identical (assets, timeframes, mode) combo
    over unchanged underlying data reuses the existing manifest row instead
    of creating a duplicate.
    """

    if mode not in {"rolling", "reproducibility"}:
        raise ValueError("dataset mode must be 'rolling' or 'reproducibility'")
    if window_end is not None and window_end.tzinfo is None:
        raise ValueError("dataset window_end must be timezone-aware")
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
                  AND (%s::text IS NULL OR source = %s::text)
                  AND (%s::timestamptz IS NULL OR timestamp <= %s::timestamptz)
                  AND (
                      %s::text IS NULL
                      OR EXISTS (
                          SELECT 1
                          FROM research_point_in_time_universe_membership membership
                          WHERE membership.universe_key = %s::text
                            AND membership.symbol = candles.symbol
                            AND membership.effective_from <=
                                (candles.timestamp AT TIME ZONE 'America/New_York')::date
                            AND (
                                membership.effective_to IS NULL
                                OR membership.effective_to >=
                                    (candles.timestamp AT TIME ZONE 'America/New_York')::date
                            )
                      )
                  )
                """,
                (
                    symbol,
                    timeframe,
                    source,
                    source,
                    window_end,
                    window_end,
                    universe_key,
                    universe_key,
                ),
            ).fetchone()
            candle_count = int(candle_row.get("candle_count") or 0)
            if candle_count == 0:
                raise ValueError(f"cannot snapshot missing candle dataset {symbol} {timeframe}")

            feature_row = conn.execute(
                """
                SELECT COUNT(*) AS feature_count,
                       MD5(COALESCE(STRING_AGG(
                           CONCAT_WS(
                               '|',
                               intraday_features.timestamp::text,
                               intraday_features.session_date::text,
                               COALESCE(intraday_features.minutes_from_open::text, ''),
                               COALESCE(intraday_features.minutes_to_close::text, ''),
                               COALESCE(intraday_features.session_vwap::text, ''),
                               COALESCE(intraday_features.gap_percent::text, ''),
                               COALESCE(micro.feed, ''),
                               COALESCE(micro.quote_count::text, ''),
                               COALESCE(micro.median_spread_bps::text, ''),
                               COALESCE(
                                   micro.normalized_order_flow_imbalance::text,
                                   ''
                               )
                           ),
                           '||' ORDER BY intraday_features.timestamp
                       ), '')) AS feature_hash
                FROM intraday_features
                LEFT JOIN LATERAL (
                    SELECT provider, feed, quote_count, median_spread_bps,
                           normalized_order_flow_imbalance
                    FROM intraday_microstructure_features
                    WHERE symbol = intraday_features.symbol
                      AND timeframe = intraday_features.timeframe
                      AND timestamp = intraday_features.timestamp
                    ORDER BY
                        CASE WHEN LOWER(feed) = 'sip' THEN 0 ELSE 1 END,
                        created_at DESC
                    LIMIT 1
                ) micro ON TRUE
                WHERE intraday_features.symbol = %s
                  AND intraday_features.timeframe = %s
                  AND (%s::timestamptz IS NULL OR intraday_features.timestamp <= %s::timestamptz)
                  AND (
                      %s::text IS NULL
                      OR EXISTS (
                          SELECT 1
                          FROM research_point_in_time_universe_membership membership
                          WHERE membership.universe_key = %s::text
                            AND membership.symbol = intraday_features.symbol
                            AND membership.effective_from <= intraday_features.session_date
                            AND (
                                membership.effective_to IS NULL
                                OR membership.effective_to >= intraday_features.session_date
                            )
                      )
                  )
                """,
                (
                    symbol,
                    timeframe,
                    window_end,
                    window_end,
                    universe_key,
                    universe_key,
                ),
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
            "universe_key": universe_key,
            "requested_window_end": window_end,
            # The feed is part of the dataset's identity: the same symbols over
            # the same window on a different feed are different prices.
            "source": source,
            "datasets": [
                {key: jsonable(item[key]) for key in ("key", "candle_count", "feature_count", "window_start", "window_end", "candle_hash", "feature_hash", "sources")}
                for item in summaries
            ],
            "calculation_version": INTRADAY_DATASET_VERSION,
        }
    )
    dataset_key = f"intraday_dataset_{content_hash[:24]}"
    counts = {item["key"]: item["candle_count"] for item in summaries}
    hashes = {item["key"]: item["candle_hash"] for item in summaries}
    sources = sorted({source for item in summaries for source in item["sources"]})
    adjusted_prices = bool(sources) and all(
        str(source).lower().startswith("alpaca") for source in sources
    )
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
            name
            or (
                f"Intraday {mode} snapshot: {', '.join(normalized_assets)} / "
                f"{', '.join(normalized_timeframes)}"
                + (f" through {window_end.isoformat()}" if window_end else "")
            ),
            mode,
            Jsonb(normalized_assets),
            Jsonb(normalized_timeframes),
            min(window_starts) if window_starts else None,
            max(window_ends) if window_ends else None,
            Jsonb(counts),
            Jsonb(hashes),
            Jsonb(sources),
            content_hash,
            Jsonb(
                {
                    "verified_at_creation": True,
                    "dataset_count": len(summaries),
                    "exact_candles_materialized": True,
                    "exact_intraday_features_materialized": True,
                    "point_in_time_universe": universe_key is not None,
                    "universe_key": universe_key,
                    "requested_window_end": jsonable(window_end),
                    "pinned_source": source,
                    "single_source": len(sources) == 1,
                    "corporate_actions_adjusted": adjusted_prices,
                    "adjustment_policy": "all" if adjusted_prices else "unverified",
                }
            ),
            INTRADAY_DATASET_VERSION,
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
              AND (%s::text IS NULL OR source = %s::text)
              AND (
                  %s::text IS NULL
                  OR EXISTS (
                      SELECT 1
                      FROM research_point_in_time_universe_membership membership
                      WHERE membership.universe_key = %s::text
                        AND membership.symbol = candles.symbol
                        AND membership.effective_from <=
                            (candles.timestamp AT TIME ZONE 'America/New_York')::date
                        AND (
                            membership.effective_to IS NULL
                            OR membership.effective_to >=
                                (candles.timestamp AT TIME ZONE 'America/New_York')::date
                        )
                  )
              )
            ON CONFLICT(dataset_id, symbol, timeframe, timestamp, source) DO NOTHING
            """,
            (
                dataset_id,
                item["symbol"],
                item["timeframe"],
                item["window_start"],
                item["window_end"],
                source,
                source,
                universe_key,
                universe_key,
            ),
        )
        conn.execute(
            """
            INSERT INTO research_dataset_intraday_features(
                dataset_id, symbol, timeframe, timestamp, session_date, minutes_from_open, minutes_to_close,
                session_vwap, distance_from_session_vwap, opening_range_high, opening_range_low,
                opening_range_position, gap_percent, session_relative_volume,
                microstructure_provider, microstructure_feed, quote_count,
                median_spread_bps, p90_spread_bps, mean_depth,
                order_flow_imbalance, normalized_order_flow_imbalance
            )
            SELECT %s, intraday_features.symbol, intraday_features.timeframe,
                   intraday_features.timestamp, intraday_features.session_date,
                   intraday_features.minutes_from_open,
                   intraday_features.minutes_to_close,
                   intraday_features.session_vwap,
                   intraday_features.distance_from_session_vwap,
                   intraday_features.opening_range_high,
                   intraday_features.opening_range_low,
                   intraday_features.opening_range_position,
                   intraday_features.gap_percent,
                   intraday_features.session_relative_volume,
                   micro.provider, micro.feed, micro.quote_count,
                   micro.median_spread_bps, micro.p90_spread_bps, micro.mean_depth,
                   micro.order_flow_imbalance, micro.normalized_order_flow_imbalance
            FROM intraday_features
            LEFT JOIN LATERAL (
                SELECT provider, feed, quote_count, median_spread_bps,
                       p90_spread_bps, mean_depth, order_flow_imbalance,
                       normalized_order_flow_imbalance
                FROM intraday_microstructure_features
                WHERE symbol = intraday_features.symbol
                  AND timeframe = intraday_features.timeframe
                  AND timestamp = intraday_features.timestamp
                ORDER BY
                    CASE WHEN LOWER(feed) = 'sip' THEN 0 ELSE 1 END,
                    created_at DESC
                LIMIT 1
            ) micro ON TRUE
            WHERE intraday_features.symbol = %s
              AND intraday_features.timeframe = %s
              AND intraday_features.timestamp BETWEEN %s AND %s
              AND (
                  %s::text IS NULL
                  OR EXISTS (
                      SELECT 1
                      FROM research_point_in_time_universe_membership membership
                      WHERE membership.universe_key = %s::text
                        AND membership.symbol = intraday_features.symbol
                        AND membership.effective_from <= intraday_features.session_date
                        AND (
                            membership.effective_to IS NULL
                            OR membership.effective_to >= intraday_features.session_date
                        )
                  )
              )
            ON CONFLICT (dataset_id, symbol, timeframe, timestamp) DO NOTHING
            """,
            (
                dataset_id,
                item["symbol"],
                item["timeframe"],
                item["window_start"],
                item["window_end"],
                universe_key,
                universe_key,
            ),
        )
    _ensure_nested_splits(conn, dataset_id)
    conn.commit()
    return jsonable(dict(row))


def _ensure_nested_splits(conn: psycopg.Connection, dataset_id: int) -> None:
    """Fix the discovery/validation/confirmation boundaries at snapshot time.

    Deliberately at creation, before any research has run against the data:
    boundaries chosen later -- once results are known -- could be nudged until
    the confirmation window cooperated. Written once and never moved (see
    `persist_dataset_splits`). Failure to split is not fatal to snapshotting;
    a dataset without splits simply cannot be used for confirmation, which the
    protocol reports rather than silently working around.
    """
    from app.services.research_splits import (
        compute_nested_splits,
        compute_session_nested_splits,
        persist_dataset_splits,
    )

    rows = conn.execute(
        """
        SELECT DISTINCT timestamp, session_date
        FROM research_dataset_intraday_features
        WHERE dataset_id = %s
        ORDER BY timestamp
        """,
        (dataset_id,),
    ).fetchall()
    if rows:
        observations = [(row["timestamp"], row["session_date"]) for row in rows]
        if len({session for _, session in observations}) >= 3:
            persist_dataset_splits(
                conn,
                dataset_id=dataset_id,
                splits=compute_session_nested_splits(observations),
            )
            return
    fallback = conn.execute(
        "SELECT DISTINCT timestamp FROM research_dataset_candles WHERE dataset_id = %s ORDER BY timestamp",
        (dataset_id,),
    ).fetchall()
    timestamps = [row["timestamp"] for row in fallback]
    if len(timestamps) >= 3:
        persist_dataset_splits(
            conn,
            dataset_id=dataset_id,
            splits=compute_nested_splits(timestamps),
        )


def load_snapshot_intraday_features(
    conn: psycopg.Connection,
    dataset_id: int,
    symbol: str,
    timeframe: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    """Snapshot features for one symbol, optionally bounded to a time window.

    The window must match the one used for the candles it is joined to;
    loading a symbol's whole history beside a single batch of bars is how a
    time-batched pass would reintroduce the memory it exists to avoid.
    """
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT symbol, timeframe, timestamp, session_date, minutes_from_open, minutes_to_close,
                   session_vwap, distance_from_session_vwap, opening_range_high, opening_range_low,
                   opening_range_position, gap_percent, session_relative_volume
            FROM research_dataset_intraday_features
            WHERE dataset_id = %s AND symbol = %s AND timeframe = %s
              AND (%s::timestamptz IS NULL OR timestamp >= %s)
              AND (%s::timestamptz IS NULL OR timestamp < %s)
            ORDER BY timestamp ASC
            """,
            (dataset_id, symbol, timeframe, start, start, end, end),
        ).fetchall()
    ]
