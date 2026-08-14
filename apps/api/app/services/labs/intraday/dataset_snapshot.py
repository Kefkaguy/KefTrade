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
from app.services.intraday_trade_flow import TRADE_FLOW_VERSION

__all__ = ["record_intraday_dataset_snapshot", "load_snapshot_intraday_features", "load_snapshot_candles"]
INTRADAY_DATASET_VERSION = "intraday_dataset_v4_bounded_window_and_outcome_grid"


def record_intraday_dataset_snapshot(
    conn: psycopg.Connection,
    *,
    assets: list[str],
    timeframes: list[str],
    mode: str = "rolling",
    name: str | None = None,
    universe_key: str | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    outcome_timeframes: list[str] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Materialize an exact immutable candles + intraday_features snapshot.

    Idempotent by content hash, exactly like the swing
    `record_dataset_snapshot`: an identical (assets, timeframes, mode) combo
    over unchanged underlying data reuses the existing manifest row instead
    of creating a duplicate.

    ``window_start`` bounds the snapshot from below.  Without it the only
    control was ``window_end``, so a dataset always reached back to the
    earliest candle a symbol had -- which is how a snapshot taken to study a
    recent order-flow window ends up carrying years of history that has no
    trade-flow evidence beside it, and then hands those sessions to the split
    calculator as though they were usable.

    ``outcome_timeframes`` freezes a finer candle grid alongside the signal
    timeframes, candles only.  It exists because forward-return horizons cannot
    be resolved on the grid that produced the signal: a 30m bar can only answer
    30/60/90-minute questions.  These timeframes deliberately skip the
    intraday-feature and trade-flow requirements -- `intraday_features` is
    CHECK-constrained to 15m/30m, and an outcome grid needs prices, not
    signals.
    """

    if mode not in {"rolling", "reproducibility"}:
        raise ValueError("dataset mode must be 'rolling' or 'reproducibility'")
    for label, bound in (("window_start", window_start), ("window_end", window_end)):
        if bound is not None and bound.tzinfo is None:
            raise ValueError(f"dataset {label} must be timezone-aware")
    if window_start is not None and window_end is not None and window_start >= window_end:
        raise ValueError("dataset window_start must be earlier than window_end")
    normalized_assets = sorted({item.strip().upper() for item in assets if item.strip()})
    normalized_timeframes = sorted({item.strip() for item in timeframes if item.strip()})
    normalized_outcomes = sorted(
        {item.strip() for item in (outcome_timeframes or []) if item.strip()}
    )
    if not normalized_assets or not normalized_timeframes:
        raise ValueError("dataset snapshot requires at least one asset and timeframe")
    overlap = sorted(set(normalized_outcomes) & set(normalized_timeframes))
    if overlap:
        raise ValueError(
            f"{overlap} cannot be both a signal timeframe and an outcome timeframe; "
            "the outcome grid exists to measure horizons the signal grid cannot express"
        )

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
                  AND (%s::timestamptz IS NULL OR timestamp >= %s::timestamptz)
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
                    window_start,
                    window_start,
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
                  AND (%s::timestamptz IS NULL OR intraday_features.timestamp >= %s::timestamptz)
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
                    window_start,
                    window_start,
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

            trade_feed = "sip" if source == "alpaca_sip" else "iex" if source == "alpaca_iex" else None
            trade_row = conn.execute(
                """
                SELECT COUNT(*) AS trade_flow_count,
                       MD5(COALESCE(STRING_AGG(
                           CONCAT_WS(
                               '|', timestamp::text, trade_count::text,
                               total_volume::text, classified_volume::text,
                               trade_size_squared_sum::text,
                               effective_trade_count::text,
                               signed_trade_imbalance::text,
                               unclassified_share::text,
                               classification_method
                           ),
                           '||' ORDER BY timestamp
                       ), '')) AS trade_flow_hash
                FROM intraday_trade_flow_features
                WHERE symbol = %s AND timeframe = %s
                  AND (%s::text IS NULL OR feed = %s::text)
                  AND timestamp BETWEEN %s AND %s
                  AND calculation_version = %s
                """,
                (
                    symbol, timeframe, trade_feed, trade_feed,
                    candle_row.get("window_start"), candle_row.get("window_end"),
                    TRADE_FLOW_VERSION,
                ),
            ).fetchone()

            summaries.append(
                {
                    "key": f"{symbol}|{timeframe}",
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "candle_count": candle_count,
                    "feature_count": feature_count,
                    "trade_flow_count": int((trade_row or {}).get("trade_flow_count") or 0),
                    "trade_flow_hash": str((trade_row or {}).get("trade_flow_hash") or ""),
                    "window_start": candle_row.get("window_start"),
                    "window_end": candle_row.get("window_end"),
                    "candle_hash": str(candle_row.get("candle_hash") or ""),
                    "feature_hash": str(feature_row.get("feature_hash") or ""),
                    "sources": list(candle_row.get("sources") or []),
                }
            )

    outcome_summaries: list[dict[str, Any]] = []
    for symbol in normalized_assets:
        for timeframe in normalized_outcomes:
            outcome_row = conn.execute(
                """
                SELECT COUNT(*) AS candle_count, MIN(timestamp) AS window_start,
                       MAX(timestamp) AS window_end,
                       MD5(COALESCE(STRING_AGG(
                           CONCAT_WS('|', source, timestamp::text, open::text, high::text, low::text, close::text, volume::text),
                           '||' ORDER BY timestamp, source
                       ), '')) AS candle_hash,
                       ARRAY_AGG(DISTINCT source ORDER BY source) AS sources
                FROM candles
                WHERE symbol = %s AND timeframe = %s
                  AND (%s::text IS NULL OR source = %s::text)
                  AND (%s::timestamptz IS NULL OR timestamp >= %s::timestamptz)
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
                    window_start,
                    window_start,
                    window_end,
                    window_end,
                    universe_key,
                    universe_key,
                ),
            ).fetchone()
            outcome_count = int((outcome_row or {}).get("candle_count") or 0)
            if outcome_count == 0:
                # Refusing here rather than freezing a partial grid: a dataset
                # missing its outcome bars for one symbol would measure that
                # symbol's horizons as unavailable and quietly drop it from
                # every cell, which looks like a thin result rather than a
                # missing ingest.
                raise ValueError(
                    f"cannot snapshot outcome grid {symbol} {timeframe}: no candles in the "
                    "requested window. Ingest the finer grid before freezing the dataset."
                )
            outcome_summaries.append(
                {
                    "key": f"{symbol}|{timeframe}",
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "candle_count": outcome_count,
                    "window_start": outcome_row.get("window_start"),
                    "window_end": outcome_row.get("window_end"),
                    "candle_hash": str(outcome_row.get("candle_hash") or ""),
                    "sources": list(outcome_row.get("sources") or []),
                }
            )

    content_hash = stable_hash(
        {
            "kind": "intraday",
            "mode": mode,
            "assets": normalized_assets,
            "timeframes": normalized_timeframes,
            "outcome_timeframes": normalized_outcomes,
            "universe_key": universe_key,
            # Both bounds belong to the dataset's identity. The same symbols
            # over a different window are a different dataset, and leaving the
            # lower bound out of the hash would let a bounded snapshot collide
            # with an unbounded one taken earlier.
            "requested_window_start": window_start,
            "requested_window_end": window_end,
            # The feed is part of the dataset's identity: the same symbols over
            # the same window on a different feed are different prices.
            "source": source,
            "datasets": [
                {key: jsonable(item[key]) for key in ("key", "candle_count", "feature_count", "trade_flow_count", "window_start", "window_end", "candle_hash", "feature_hash", "trade_flow_hash", "sources")}
                for item in summaries
            ],
            "outcome_datasets": [
                {key: jsonable(item[key]) for key in ("key", "candle_count", "window_start", "window_end", "candle_hash", "sources")}
                for item in outcome_summaries
            ],
            "calculation_version": INTRADAY_DATASET_VERSION,
        }
    )
    dataset_key = f"intraday_dataset_{content_hash[:24]}"
    counts = {item["key"]: item["candle_count"] for item in [*summaries, *outcome_summaries]}
    hashes = {item["key"]: item["candle_hash"] for item in [*summaries, *outcome_summaries]}
    sources = sorted(
        {source for item in [*summaries, *outcome_summaries] for source in item["sources"]}
    )
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
            # The manifest lists both layers, so a loader reading `timeframes`
            # can see the outcome grid exists without opening the integrity blob.
            Jsonb(sorted({*normalized_timeframes, *normalized_outcomes})),
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
                    "signal_timeframes": normalized_timeframes,
                    "outcome_timeframes": normalized_outcomes,
                    "exact_outcome_grid_materialized": bool(normalized_outcomes),
                    "requested_window_start": jsonable(window_start),
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
            INSERT INTO research_dataset_trade_flow_features(
                dataset_id, symbol, timeframe, timestamp, feed, trade_count,
                total_volume, signed_trade_imbalance,
                signed_trade_count_imbalance, large_trade_share,
                unclassified_share, effective_spread_bps,
                classification_method, classified_volume,
                trade_size_squared_sum, effective_trade_count
            )
            SELECT %s, symbol, timeframe, timestamp, feed, trade_count,
                   total_volume, signed_trade_imbalance,
                   signed_trade_count_imbalance, large_trade_share,
                   unclassified_share, effective_spread_bps,
                   classification_method, classified_volume,
                   trade_size_squared_sum, effective_trade_count
            FROM intraday_trade_flow_features
            WHERE symbol = %s AND timeframe = %s
              AND timestamp BETWEEN %s AND %s
              AND (%s::text IS NULL OR feed = %s::text)
              AND calculation_version = %s
            ON CONFLICT (dataset_id, symbol, timeframe, timestamp) DO NOTHING
            """,
            (
                dataset_id,
                item["symbol"],
                item["timeframe"],
                item["window_start"],
                item["window_end"],
                "sip" if source == "alpaca_sip" else "iex" if source == "alpaca_iex" else None,
                "sip" if source == "alpaca_sip" else "iex" if source == "alpaca_iex" else None,
                TRADE_FLOW_VERSION,
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
    for item in outcome_summaries:
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
    _ensure_nested_splits(conn, dataset_id, signal_timeframes=normalized_timeframes)
    conn.commit()
    return jsonable(dict(row))


def _ensure_nested_splits(
    conn: psycopg.Connection,
    dataset_id: int,
    *,
    signal_timeframes: list[str] | None = None,
) -> None:
    """Fix the discovery/validation/confirmation boundaries at snapshot time.

    Deliberately at creation, before any research has run against the data:
    boundaries chosen later -- once results are known -- could be nudged until
    the confirmation window cooperated. Written once and never moved (see
    `persist_dataset_splits`). Failure to split is not fatal to snapshotting;
    a dataset without splits simply cannot be used for confirmation, which the
    protocol reports rather than silently working around.

    The boundaries come from the *signal* layer. A phase is a statement about
    which decisions a researcher was allowed to look at, and decisions happen
    on signal bars -- letting a 1m outcome grid into the calculation would put
    the boundaries wherever the finer grid happened to be dense.
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
          AND (%s::text[] IS NULL OR timeframe = ANY(%s::text[]))
        ORDER BY timestamp
        """,
        (dataset_id, signal_timeframes, signal_timeframes),
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
        """
        SELECT DISTINCT timestamp
        FROM research_dataset_candles
        WHERE dataset_id = %s
          AND (%s::text[] IS NULL OR timeframe = ANY(%s::text[]))
        ORDER BY timestamp
        """,
        (dataset_id, signal_timeframes, signal_timeframes),
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
