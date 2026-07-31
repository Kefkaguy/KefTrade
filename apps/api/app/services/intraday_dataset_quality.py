"""Data-quality and power checks that must clear before factor calculation.

Two different questions are answered here, and the plan is right to separate
them.  Quality asks whether the rows describe the market at all: complete
sessions, coherent OHLC, plausible prices, live membership, features that line
up with their candles.  Power asks whether there are enough of the *events*
the hypothesis is about -- a dataset can be flawless and still be unable to
resolve a 30 bps effect across forty sessions.

Both run before discovery, not after, because after is too late: once the
numbers exist, the decision about whether the sample was adequate is no longer
independent of what the numbers said.

The module contains no campaign, broker, order-submission, or UI code.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Sequence

import psycopg
from psycopg.types.json import Jsonb

from app.services.intraday_research_power import required_sessions_for_power
from app.services.intraday_session_calendar import (
    MAX_CONSECUTIVE_SESSION_DAYS,
    early_close_session_slots,
    regular_session_slots,
)

DATASET_QUALITY_VERSION = "intraday_dataset_quality_v1"

# Targets for the bounded gap-down experiment, carrying margin above the
# 396/707 minimum the power report derived from the current sample.
GAP_EXPERIMENT_SESSION_TARGET = 475
GAP_EXPERIMENT_OBSERVATION_TARGET = 850

STALE_VOLUME_SHARE_LIMIT = 0.02
MAX_UNEXPLAINED_JUMP = 0.35


def _round(value: float | None) -> float | None:
    return round(float(value), 6) if value is not None else None


def duplicate_rows(conn: psycopg.Connection, *, dataset_id: int, timeframe: str) -> dict[str, Any]:
    """A snapshot must hold exactly one row per symbol/timestamp.

    Two feeds under two source labels both land in the snapshot, which would
    put two different prices on the same bar and double every observation.
    """
    rows = conn.execute(
        """
        SELECT symbol, timestamp, COUNT(*) AS rows, COUNT(DISTINCT source) AS sources
        FROM research_dataset_candles
        WHERE dataset_id = %s AND timeframe = %s
        GROUP BY symbol, timestamp
        HAVING COUNT(*) > 1
        LIMIT 200
        """,
        (dataset_id, timeframe),
    ).fetchall()
    sources = conn.execute(
        """
        SELECT source, COUNT(*) AS rows
        FROM research_dataset_candles
        WHERE dataset_id = %s AND timeframe = %s
        GROUP BY source ORDER BY source
        """,
        (dataset_id, timeframe),
    ).fetchall()
    return {
        "duplicate_symbol_timestamp_rows": len(rows),
        "examples": [
            {"symbol": str(row["symbol"]), "timestamp": str(row["timestamp"]), "rows": int(row["rows"])}
            for row in rows[:10]
        ],
        "sources": {str(row["source"]): int(row["rows"]) for row in sources},
        "single_source": len(sources) == 1,
        "passed": len(rows) == 0 and len(sources) == 1,
    }


def session_shape_report(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    timeframe: str,
) -> dict[str, Any]:
    """Normal sessions must hold exactly the full regular-hours complement."""
    full = len(regular_session_slots(timeframe))
    early = len(early_close_session_slots(timeframe))
    rows = conn.execute(
        """
        SELECT symbol,
               (timestamp AT TIME ZONE 'America/New_York')::date AS session_date,
               COUNT(*) FILTER (
                   WHERE (timestamp AT TIME ZONE 'America/New_York')::time >= TIME '09:30'
                     AND (timestamp AT TIME ZONE 'America/New_York')::time < TIME '16:00'
               ) AS regular_bars,
               COUNT(*) FILTER (
                   WHERE (timestamp AT TIME ZONE 'America/New_York')::time < TIME '09:30'
                      OR (timestamp AT TIME ZONE 'America/New_York')::time >= TIME '16:00'
               ) AS extended_bars
        FROM research_dataset_candles
        WHERE dataset_id = %s AND timeframe = %s
        GROUP BY 1, 2
        """,
        (dataset_id, timeframe),
    ).fetchall()
    counts = {"full": 0, "early_close": 0, "incomplete": 0}
    extended = 0
    for row in rows:
        regular = int(row["regular_bars"])
        extended += int(row["extended_bars"])
        if regular == full:
            counts["full"] += 1
        elif regular == early:
            counts["early_close"] += 1
        else:
            counts["incomplete"] += 1
    total = sum(counts.values())
    complete_share = (counts["full"] + counts["early_close"]) / total if total else None
    return {
        "expected_full_session_bars": full,
        "expected_early_close_bars": early,
        "symbol_sessions": total,
        "session_shapes": counts,
        "extended_hours_rows": extended,
        "complete_session_share": _round(complete_share),
        "passed": complete_share is not None and complete_share >= 0.95,
    }


def price_integrity(conn: psycopg.Connection, *, dataset_id: int, timeframe: str) -> dict[str, Any]:
    """OHLC coherence, impossible prices, and stale or absent volume."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS rows,
               COUNT(*) FILTER (WHERE high < low) AS inverted_range,
               COUNT(*) FILTER (WHERE high < open OR high < close) AS high_below_body,
               COUNT(*) FILTER (WHERE low > open OR low > close) AS low_above_body,
               COUNT(*) FILTER (WHERE open <= 0 OR high <= 0 OR low <= 0 OR close <= 0)
                   AS nonpositive_price,
               COUNT(*) FILTER (WHERE volume IS NULL OR volume <= 0) AS zero_volume
        FROM research_dataset_candles
        WHERE dataset_id = %s AND timeframe = %s
        """,
        (dataset_id, timeframe),
    ).fetchone()
    total = int((row or {}).get("rows") or 0)
    zero_volume = int((row or {}).get("zero_volume") or 0)
    violations = sum(
        int((row or {}).get(key) or 0)
        for key in ("inverted_range", "high_below_body", "low_above_body", "nonpositive_price")
    )
    stale_share = zero_volume / total if total else None
    return {
        "candle_rows": total,
        "inverted_range": int((row or {}).get("inverted_range") or 0),
        "high_below_body": int((row or {}).get("high_below_body") or 0),
        "low_above_body": int((row or {}).get("low_above_body") or 0),
        "nonpositive_price": int((row or {}).get("nonpositive_price") or 0),
        "zero_or_null_volume": zero_volume,
        "zero_volume_share": _round(stale_share),
        "passed": (
            total > 0
            and violations == 0
            and stale_share is not None
            and stale_share <= STALE_VOLUME_SHARE_LIMIT
        ),
    }


def corporate_action_consistency(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    timeframe: str,
) -> dict[str, Any]:
    """Overnight jumps that no recorded corporate action explains.

    Adjusted candles should not gap by a third overnight.  Where they do, a
    matching split or dividend must exist, or the adjustment policy did not
    reach that symbol.
    """
    rows = conn.execute(
        """
        WITH session_closes AS (
            SELECT symbol,
                   (timestamp AT TIME ZONE 'America/New_York')::date AS session_date,
                   (ARRAY_AGG(close ORDER BY timestamp DESC))[1] AS session_close,
                   (ARRAY_AGG(open ORDER BY timestamp ASC))[1] AS session_open
            FROM research_dataset_candles
            WHERE dataset_id = %s AND timeframe = %s
              AND (timestamp AT TIME ZONE 'America/New_York')::time >= TIME '09:30'
              AND (timestamp AT TIME ZONE 'America/New_York')::time < TIME '16:00'
            GROUP BY 1, 2
        ), gaps AS (
            SELECT symbol, session_date, session_open,
                   LAG(session_close) OVER (PARTITION BY symbol ORDER BY session_date)
                       AS previous_close,
                   LAG(session_date) OVER (PARTITION BY symbol ORDER BY session_date)
                       AS previous_session
            FROM session_closes
        ), jumps AS (
            SELECT symbol, session_date,
                   ABS(session_open / NULLIF(previous_close, 0) - 1) AS jump
            FROM gaps
            WHERE previous_close IS NOT NULL
              -- Point-in-time membership leaves holes. A move measured across
              -- one is not an overnight jump and no corporate action explains
              -- it, so comparing only adjacent sessions is the honest test.
              AND session_date - previous_session <= %s
              AND ABS(session_open / NULLIF(previous_close, 0) - 1) > %s
        )
        -- One join rather than a query per jump: a universe-scale dataset
        -- produces enough large moves that per-row lookups dominate the check.
        SELECT jumps.symbol, jumps.session_date, jumps.jump,
               (action.symbol IS NOT NULL) AS explained
        FROM jumps
        LEFT JOIN LATERAL (
            SELECT symbol FROM research_corporate_actions
            WHERE research_corporate_actions.symbol = jumps.symbol
              AND effective_date BETWEEN jumps.session_date - 3 AND jumps.session_date + 3
            LIMIT 1
        ) action ON TRUE
        ORDER BY jumps.jump DESC
        LIMIT 500
        """,
        (dataset_id, timeframe, MAX_CONSECUTIVE_SESSION_DAYS, MAX_UNEXPLAINED_JUMP),
    ).fetchall()
    unexplained: list[dict[str, Any]] = [
        {
            "symbol": str(row["symbol"]),
            "session_date": str(row["session_date"]),
            "jump": _round(float(row["jump"])),
        }
        for row in rows
        if not row["explained"]
    ]
    return {
        "large_overnight_jumps": len(rows),
        "unexplained_jumps": len(unexplained),
        "examples": unexplained[:10],
        "threshold": MAX_UNEXPLAINED_JUMP,
        # Reported, not fatal: a real 40% earnings move exists.  It becomes a
        # blocker only when the count is large enough to be systematic.
        "passed": len(unexplained) <= max(5, int(0.001 * max(1, len(rows)))),
    }


def feature_alignment(conn: psycopg.Connection, *, dataset_id: int, timeframe: str) -> dict[str, Any]:
    """Every candle inside a session should carry exactly one feature row.

    The comparison is bounded by the last feature timestamp of each
    symbol-session rather than by a fixed 09:30-16:00 window.  On an
    early-close day the exchange stops at 13:00 while the consolidated feed
    keeps printing, and counting those post-close bars as missing features
    would fail a dataset that is in fact complete.
    """
    row = conn.execute(
        """
        WITH feature_sessions AS (
            SELECT symbol, session_date, MAX(timestamp) AS last_feature,
                   COUNT(*) AS feature_rows
            FROM research_dataset_intraday_features
            WHERE dataset_id = %s AND timeframe = %s
            GROUP BY 1, 2
        ), in_session_candles AS (
            SELECT COUNT(*) AS candles
            FROM research_dataset_candles candle
            JOIN feature_sessions
              ON feature_sessions.symbol = candle.symbol
             AND feature_sessions.session_date =
                 (candle.timestamp AT TIME ZONE 'America/New_York')::date
            WHERE candle.dataset_id = %s AND candle.timeframe = %s
              AND candle.timestamp <= feature_sessions.last_feature
              AND (candle.timestamp AT TIME ZONE 'America/New_York')::time >= TIME '09:30'
        )
        SELECT
            (SELECT candles FROM in_session_candles) AS in_session_candles,
            (SELECT COALESCE(SUM(feature_rows), 0) FROM feature_sessions) AS feature_rows,
            (SELECT COUNT(*) FROM research_dataset_intraday_features feature
              WHERE feature.dataset_id = %s AND feature.timeframe = %s
                AND NOT EXISTS (
                    SELECT 1 FROM research_dataset_candles candle
                    WHERE candle.dataset_id = feature.dataset_id
                      AND candle.symbol = feature.symbol
                      AND candle.timeframe = feature.timeframe
                      AND candle.timestamp = feature.timestamp
                )) AS orphan_features
        """,
        (dataset_id, timeframe) * 3,
    ).fetchone()
    candles = int((row or {}).get("in_session_candles") or 0)
    features = int((row or {}).get("feature_rows") or 0)
    orphans = int((row or {}).get("orphan_features") or 0)
    coverage = features / candles if candles else None
    return {
        "in_session_candles": candles,
        "feature_rows": features,
        "orphan_feature_rows": orphans,
        "feature_coverage": _round(coverage),
        "passed": orphans == 0 and coverage is not None and coverage >= 0.98,
    }


def gap_event_power(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    timeframe: str,
    minimum_gap: float = 0.003,
    minimum_relative_volume: float = 1.5,
    maximum_acceptance_fill: float = 0.25,
    minimum_absorption_fill: float = 0.50,
    session_target: int = GAP_EXPERIMENT_SESSION_TARGET,
    observation_target: int = GAP_EXPERIMENT_OBSERVATION_TARGET,
) -> dict[str, Any]:
    """Count the events each of the six tests would actually receive.

    Two distinctions decide whether this gate means anything.

    First, acceptance and absorption are separate hypotheses drawing on
    disjoint subsets of the gap-down pool, and the fill band between the two
    thresholds belongs to neither.  Counting the pool would let a dataset pass
    while every individual test stayed underpowered.

    Second, the 396-session requirement was derived from the *validation*
    sample, which is 30% of the history, so the gate measures the validation
    split rather than the whole dataset.  Both flow states must clear it: the
    experiment is only as interpretable as its weaker half.
    """
    rows = conn.execute(
        """
        WITH regular AS (
            -- Candles only. This CTE is referenced twice, so Postgres
            -- materializes it; joining features here would materialize an
            -- 8M-row join whose feature column only the 10:00 bar ever uses.
            SELECT candle.symbol, candle.timestamp,
                   (candle.timestamp AT TIME ZONE 'America/New_York')::date AS session_date,
                   (candle.timestamp AT TIME ZONE 'America/New_York')::time AS session_time,
                   candle.open, candle.close
            FROM research_dataset_candles candle
            WHERE candle.dataset_id = %s AND candle.timeframe = %s
              AND (candle.timestamp AT TIME ZONE 'America/New_York')::time >= TIME '09:30'
              AND (candle.timestamp AT TIME ZONE 'America/New_York')::time < TIME '16:00'
        ), sessions AS (
            SELECT symbol, session_date,
                   MIN(session_time) AS first_slot,
                   (ARRAY_AGG(open ORDER BY timestamp ASC))[1] AS session_open,
                   (ARRAY_AGG(close ORDER BY timestamp DESC))[1] AS session_close,
                   COUNT(*) AS bars
            FROM regular GROUP BY 1, 2
        ), decision AS (
            -- Features joined only for the decision bar.
            SELECT regular.symbol, regular.session_date,
                   feature.session_relative_volume,
                   regular.close AS decision_close
            FROM regular
            LEFT JOIN research_dataset_intraday_features feature
              ON feature.dataset_id = %s
             AND feature.timeframe = %s
             AND feature.symbol = regular.symbol
             AND feature.timestamp = regular.timestamp
            WHERE regular.session_time = TIME '10:00'
        ), split AS (
            SELECT session_date,
                   PERCENT_RANK() OVER (ORDER BY session_date) AS chronological_position
            FROM (SELECT DISTINCT session_date FROM sessions) AS distinct_sessions
        ), with_previous AS (
            -- LAG rather than a correlated lookup: the sessions CTE carries no
            -- index, so a per-row scan for the prior close is quadratic and
            -- does not finish on a full universe.
            SELECT symbol, session_date, first_slot, session_open, bars,
                   LAG(session_close) OVER (
                       PARTITION BY symbol ORDER BY session_date
                   ) AS previous_close,
                   LAG(session_date) OVER (
                       PARTITION BY symbol ORDER BY session_date
                   ) AS previous_session
            FROM sessions
        ), gapped AS (
            SELECT s.symbol, s.session_date, s.bars,
                   d.session_relative_volume,
                   split.chronological_position,
                   s.session_open / NULLIF(s.previous_close, 0) - 1 AS gap,
                   CASE
                       WHEN s.session_open = s.previous_close THEN 0.0
                       ELSE (s.session_open - d.decision_close)
                            / NULLIF(s.session_open - s.previous_close, 0)
                   END AS gap_fill
            FROM with_previous s
            JOIN split ON split.session_date = s.session_date
            LEFT JOIN decision d ON d.symbol = s.symbol AND d.session_date = s.session_date
            WHERE s.first_slot = TIME '09:30'
              AND s.previous_close IS NOT NULL
              -- Count only events the factor builder would actually produce.
              AND s.session_date - s.previous_session <= %s
        ), qualifying AS (
            SELECT *,
                   CASE
                       WHEN gap_fill <= %s THEN 'acceptance'
                       WHEN gap_fill >= %s THEN 'absorption'
                   END AS flow_state
            FROM gapped
            WHERE gap <= -%s
              AND session_relative_volume >= %s
              -- A 4-bar hold entered on the third bar needs six regular bars.
              AND bars >= 6
        )
        SELECT flow_state,
               COUNT(*) AS total_observations,
               COUNT(DISTINCT session_date) AS total_sessions,
               COUNT(*) FILTER (
                   WHERE chronological_position >= 0.5 AND chronological_position < 0.8
               ) AS validation_observations,
               COUNT(DISTINCT session_date) FILTER (
                   WHERE chronological_position >= 0.5 AND chronological_position < 0.8
               ) AS validation_sessions
        FROM qualifying
        WHERE flow_state IS NOT NULL
        GROUP BY flow_state
        """,
        (
            dataset_id,
            timeframe,
            dataset_id,
            timeframe,
            MAX_CONSECUTIVE_SESSION_DAYS,
            maximum_acceptance_fill,
            minimum_absorption_fill,
            minimum_gap,
            minimum_relative_volume,
        ),
    ).fetchall()

    by_state: dict[str, Any] = {
        str(item["flow_state"]): {
            "total_observations": int(item["total_observations"]),
            "total_sessions": int(item["total_sessions"]),
            "validation_observations": int(item["validation_observations"]),
            "validation_sessions": int(item["validation_sessions"]),
        }
        for item in (rows or [])
    }
    for state in ("acceptance", "absorption"):
        by_state.setdefault(
            state,
            {
                "total_observations": 0,
                "total_sessions": 0,
                "validation_observations": 0,
                "validation_sessions": 0,
            },
        )
    for counts in by_state.values():
        counts["sessions_short_by"] = max(
            0, session_target - counts["validation_sessions"]
        )
        counts["observations_short_by"] = max(
            0, observation_target - counts["validation_observations"]
        )
        counts["passed"] = (
            counts["validation_sessions"] >= session_target
            and counts["validation_observations"] >= observation_target
        )

    limiting = min(
        by_state, key=lambda state: by_state[state]["validation_observations"]
    )
    return {
        "minimum_gap": minimum_gap,
        "minimum_relative_volume": minimum_relative_volume,
        "maximum_acceptance_fill": maximum_acceptance_fill,
        "minimum_absorption_fill": minimum_absorption_fill,
        "session_target": session_target,
        "observation_target": observation_target,
        "measured_on": "validation_split_50_to_80_percent",
        "by_flow_state": by_state,
        "limiting_flow_state": limiting,
        "passed": all(counts["passed"] for counts in by_state.values()),
    }


def factor_power_requirement(
    *,
    effect_bps: float,
    session_dispersion_bps: float,
    observations_per_session: float,
) -> dict[str, Any]:
    """Restate a power requirement for a predeclared effect size."""
    sessions = required_sessions_for_power(
        effect_bps=effect_bps, session_dispersion_bps=session_dispersion_bps
    )
    return {
        "assumed_effect_bps": effect_bps,
        "assumed_session_dispersion_bps": session_dispersion_bps,
        "sessions_required_for_80pct_power": sessions,
        "observations_required_for_80pct_power": (
            int(round(sessions * observations_per_session)) if sessions else None
        ),
    }


def dataset_quality_report(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    timeframe: str,
    universe_key: str | None = None,
) -> dict[str, Any]:
    """Every quality and power check, with one overall verdict."""
    from app.services.intraday_universe import membership_coverage

    checks = {
        "no_duplicate_rows": duplicate_rows(conn, dataset_id=dataset_id, timeframe=timeframe),
        "session_shapes": session_shape_report(conn, dataset_id=dataset_id, timeframe=timeframe),
        "price_integrity": price_integrity(conn, dataset_id=dataset_id, timeframe=timeframe),
        "corporate_actions": corporate_action_consistency(
            conn, dataset_id=dataset_id, timeframe=timeframe
        ),
        "feature_alignment": feature_alignment(conn, dataset_id=dataset_id, timeframe=timeframe),
    }
    if universe_key:
        coverage = membership_coverage(
            conn, universe_key=universe_key, dataset_id=dataset_id, timeframe=timeframe
        )
        checks["point_in_time_membership"] = {
            **coverage,
            "passed": bool(coverage["every_observation_inside_membership"]),
        }
    else:
        checks["point_in_time_membership"] = {
            "passed": False,
            "detail": "No universe_key supplied; membership at observation time is unverified.",
        }

    power = gap_event_power(conn, dataset_id=dataset_id, timeframe=timeframe)
    quality_passed = all(item["passed"] for item in checks.values())
    return {
        "quality_version": DATASET_QUALITY_VERSION,
        "dataset_id": dataset_id,
        "timeframe": timeframe,
        "universe_key": universe_key,
        "checks": {key: value["passed"] for key, value in checks.items()},
        "detail": checks,
        "gap_experiment_power": power,
        "quality_passed": quality_passed,
        "power_passed": power["passed"],
        # Both must hold: clean rows that cannot resolve the effect are not a
        # dataset the experiment may run on.
        "ready_for_discovery": quality_passed and power["passed"],
        "limitations": [key for key, value in checks.items() if not value["passed"]],
    }


def persist_quality_report(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    timeframe: str,
    report: dict[str, Any],
) -> int:
    row = conn.execute(
        """
        INSERT INTO intraday_dataset_quality_reports(
            dataset_id, timeframe, quality_passed, power_passed,
            ready_for_discovery, report, quality_version
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            dataset_id,
            timeframe,
            bool(report["quality_passed"]),
            bool(report["power_passed"]),
            bool(report["ready_for_discovery"]),
            Jsonb(report),
            DATASET_QUALITY_VERSION,
        ),
    ).fetchone()
    conn.commit()
    return int(row["id"])
