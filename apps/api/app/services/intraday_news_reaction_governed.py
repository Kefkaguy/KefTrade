"""Return-blind governance front-end for the 5m news-reaction study.

This module exists to make the ordering mechanical:

1. preflight counts event supply without selecting or reading any forward price;
2. declare freezes the entire 4-state x 4-horizon specification and news/cost evidence;
3. only the already-declared discovery code may read +5/+10/+15/+30m outcomes.

The implementation in :mod:`intraday_news_reaction` owns post-declaration
measurement.  This file deliberately does not expose any forward-return query.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.services.intraday_factor_diagnostics import load_cost_model
from app.services.intraday_news import NEGATIVE_TERMS, POSITIVE_TERMS
from app.services.intraday_research_integrity import exchange_session_date
from app.services.research_splits import get_dataset_splits
from app.services import intraday_news_reaction as measurement


def _return_blind_event_supply(conn: psycopg.Connection, dataset_id: int) -> list[dict[str, Any]]:
    """Return event timestamps only; never select an OHLC outcome field."""
    window_start, window_end = measurement._dataset_window(conn, dataset_id)
    rows = conn.execute(
        f"""
        WITH target_symbols AS (
            SELECT DISTINCT symbol
            FROM research_dataset_candles
            WHERE dataset_id = %(dataset_id)s
              AND timeframe = '1m'
              AND symbol <> ALL(%(excluded)s)
        ),
        first_versions AS (
            SELECT DISTINCT ON (n.provider, n.article_id, n.symbol)
                n.provider, n.article_id, n.symbol, n.known_at
            FROM intraday_news_articles n
            JOIN target_symbols u ON u.symbol = n.symbol
            WHERE n.known_at >= %(window_start)s
              AND n.known_at <= %(window_end)s + INTERVAL '1 day'
            ORDER BY n.provider, n.article_id, n.symbol, n.known_at
        ),
        sequenced AS (
            SELECT f.*,
                   LAG(known_at) OVER (
                       PARTITION BY symbol
                       ORDER BY known_at, provider, article_id
                   ) AS previous_symbol_news_at
            FROM first_versions f
        ),
        quiet_news AS (
            SELECT *
            FROM sequenced
            WHERE previous_symbol_news_at IS NULL
               OR known_at - previous_symbol_news_at >= INTERVAL '{measurement.QUIET_PERIOD_MINUTES} minutes'
        ),
        aligned AS (
            SELECT q.*,
                   date_trunc('minute', q.known_at)
                   + CASE
                       WHEN q.known_at = date_trunc('minute', q.known_at)
                       THEN INTERVAL '0 minute'
                       ELSE INTERVAL '1 minute'
                     END AS reaction_start
            FROM quiet_news q
        )
        SELECT
            f.provider,
            f.article_id,
            f.symbol,
            f.known_at,
            f.reaction_start,
            f.reaction_start + INTERVAL '5 minutes' AS decision_at
        FROM aligned f
        WHERE (f.reaction_start AT TIME ZONE 'America/New_York')::date
              = (f.known_at AT TIME ZONE 'America/New_York')::date
          AND (f.reaction_start AT TIME ZONE 'America/New_York')::time >= TIME '09:30'
          AND (f.reaction_start AT TIME ZONE 'America/New_York')::time <= TIME '15:25'
          AND NOT EXISTS (
              SELECT 1
              FROM generate_series(0, 34) AS minute_offset
              WHERE NOT EXISTS (
                  SELECT 1
                  FROM research_dataset_candles c
                  WHERE c.dataset_id = %(dataset_id)s
                    AND c.timeframe = '1m'
                    AND c.symbol = f.symbol
                    AND c.timestamp = f.reaction_start
                                      + minute_offset * INTERVAL '1 minute'
              )
          )
        ORDER BY f.reaction_start, f.symbol, f.article_id
        """,
        {
            "dataset_id": dataset_id,
            "excluded": list(measurement.EXCLUDED_NEWS_TARGETS),
            "window_start": window_start,
            "window_end": window_end,
        },
    ).fetchall()
    return [dict(row) for row in rows]


def preflight_news_reaction(conn: psycopg.Connection, *, dataset_id: int) -> dict[str, Any]:
    splits = get_dataset_splits(conn, dataset_id)
    if splits is None:
        raise ValueError(f"Dataset {dataset_id} has no frozen nested research split")
    rows = _return_blind_event_supply(conn, dataset_id)
    counts: Counter[str] = Counter()
    sessions: dict[str, set[date]] = {
        phase: set() for phase in ("discovery", "validation", "confirmation")
    }
    symbols: dict[str, set[str]] = {phase: set() for phase in sessions}
    for row in rows:
        phase = splits.phase_for(row["decision_at"])
        if phase is None:
            continue
        counts[phase] += 1
        sessions[phase].add(exchange_session_date(row["reaction_start"]))
        symbols[phase].add(str(row["symbol"]).upper())
    return {
        "protocol_version": measurement.NEWS_REACTION_VERSION,
        "dataset_id": dataset_id,
        "return_blind": True,
        "outcome_fields_accessed": [],
        "news_fingerprint": measurement.news_fingerprint(conn, dataset_id),
        "phases": {
            phase: {
                "events": counts[phase],
                "distinct_sessions": len(sessions[phase]),
                "symbols": len(symbols[phase]),
            }
            for phase in ("discovery", "validation", "confirmation")
        },
        "reaction_minutes": measurement.REACTION_MINUTES,
        "horizons_minutes": list(measurement.HORIZONS_MINUTES),
        "fresh_tests": measurement.FRESH_TESTS,
        "note": "Counts/timestamps only; no forward OHLC field was selected or read.",
    }


def declare_news_reaction(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    cost_calibration_id: int,
    prior_effective_trials: int,
    purpose: str,
) -> dict[str, Any]:
    splits = get_dataset_splits(conn, dataset_id)
    if splits is None:
        raise ValueError(f"Dataset {dataset_id} has no frozen nested research split")
    if prior_effective_trials < 0:
        raise ValueError("prior_effective_trials cannot be negative")

    preflight = preflight_news_reaction(conn, dataset_id=dataset_id)
    if preflight["phases"]["discovery"]["events"] < 200:
        raise ValueError("Insufficient discovery event supply")
    if preflight["phases"]["validation"]["events"] < 200:
        raise ValueError("Insufficient validation event supply")

    cost_model = load_cost_model(conn, cost_calibration_id)
    total_trials = prior_effective_trials + measurement.FRESH_TESTS
    specification = {
        "purpose": purpose,
        "dataset_id": dataset_id,
        "cost_calibration_id": cost_calibration_id,
        "cost_model_hash": measurement._stable_hash(cost_model),
        "reaction_minutes": measurement.REACTION_MINUTES,
        "horizons_minutes": list(measurement.HORIZONS_MINUTES),
        "quiet_period_minutes": measurement.QUIET_PERIOD_MINUTES,
        "excluded_news_targets": list(measurement.EXCLUDED_NEWS_TARGETS),
        "states": measurement.STATE_DIRECTIONS,
        "polarity_model": {
            "positive_terms": list(POSITIVE_TERMS),
            "negative_terms": list(NEGATIVE_TERMS),
            "ties": "excluded_as_neutral",
        },
        "reaction_measure": "stock_5m_return_minus_spy_5m_return",
        "entry": "open_of_first_1m_bar_after_completed_5m_reaction",
        "target_gross_block_bootstrap_lower_bound_bps": measurement.TARGET_GROSS_LOWER_BOUND_BPS,
        "prior_effective_trials": prior_effective_trials,
        "fresh_tests": measurement.FRESH_TESTS,
        "total_effective_trials": total_trials,
        "bonferroni_two_sided_t_threshold": measurement.selection_t_threshold(total_trials),
        "splits": splits.as_dict(),
        "confirmation_untouched": True,
        "price_source": "frozen_research_dataset_candles_1m",
        "trade_flow_used": False,
        "news_categories_are_labels_not_tests": True,
        "preflight_return_blind": True,
        "preflight_outcome_fields_accessed": [],
    }
    specification_hash = measurement._stable_hash(specification)
    fingerprint = preflight["news_fingerprint"]
    row = conn.execute(
        """
        INSERT INTO intraday_news_reaction_declarations(
            dataset_id, cost_calibration_id, specification, specification_hash,
            news_fingerprint, protocol_version
        ) VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (specification_hash) DO NOTHING
        RETURNING *
        """,
        (
            dataset_id,
            cost_calibration_id,
            Jsonb(measurement._jsonable(specification)),
            specification_hash,
            Jsonb(measurement._jsonable(fingerprint)),
            measurement.NEWS_REACTION_VERSION,
        ),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM intraday_news_reaction_declarations WHERE specification_hash = %s",
            (specification_hash,),
        ).fetchone()
    conn.commit()
    return {
        "declaration_id": int(row["id"]),
        "specification_hash": specification_hash,
        "total_effective_trials": total_trials,
        "selection_t_threshold": specification["bonferroni_two_sided_t_threshold"],
        "preflight": preflight,
        "next_command": f"discover --declaration-id {int(row['id'])}",
    }
