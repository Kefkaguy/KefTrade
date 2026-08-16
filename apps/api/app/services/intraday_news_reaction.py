"""Governed 5-minute reaction research around point-in-time company news.

The event, reaction window, directions, horizons, costs, and multiplicity budget
are frozen before forward returns are read.  Discovery may read discovery and
validation only; confirmation is a one-shot read of the final locked split.

This module is deliberately price/news-only.  One-minute signed trade flow is
not frozen for the development window of the current dataset, so adding it here
would turn a missing side channel into an ambiguous result.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from hashlib import sha256
from json import dumps
from statistics import NormalDist
from typing import Any, Sequence

import psycopg
from psycopg.types.json import Jsonb

from app.services.intraday_factor_diagnostics import load_cost_model
from app.services.intraday_news import CATEGORY_TERMS, NEGATIVE_TERMS, POSITIVE_TERMS
from app.services.intraday_research_integrity import (
    clustered_outcome_statistics,
    estimated_round_trip_cost_bps,
)
from app.services.research_splits import get_dataset_splits, record_split_access

NEWS_REACTION_VERSION = "intraday_news_reaction_5m_v1_price_only"
REACTION_MINUTES = 5
HORIZONS_MINUTES = (5, 10, 15, 30)
QUIET_PERIOD_MINUTES = 60
TARGET_GROSS_LOWER_BOUND_BPS = 5.0
FRESH_TESTS = 16  # four predeclared states x four horizons
EXCLUDED_NEWS_TARGETS = ("SPY", "QQQ")

STATE_POSITIVE_CONTINUATION = "positive_news_positive_reaction_continuation"
STATE_POSITIVE_FAILURE = "positive_news_failed_reaction_reversal"
STATE_NEGATIVE_CONTINUATION = "negative_news_negative_reaction_continuation"
STATE_NEGATIVE_FAILURE = "negative_news_failed_reaction_reversal"

STATE_DIRECTIONS: dict[str, int] = {
    STATE_POSITIVE_CONTINUATION: 1,
    STATE_POSITIVE_FAILURE: -1,
    STATE_NEGATIVE_CONTINUATION: -1,
    STATE_NEGATIVE_FAILURE: 1,
}


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _stable_hash(payload: Any) -> str:
    return sha256(dumps(_jsonable(payload), sort_keys=True, default=str).encode()).hexdigest()


def _term_score(text: str, terms: Sequence[str]) -> int:
    lowered = text.lower()
    return sum(1 for term in terms if term in lowered)


def classify_news_polarity(headline: str, summary: str | None, content: str | None) -> tuple[str | None, int, int]:
    text = " ".join(item for item in (headline, summary or "", content or "") if item)
    positive = _term_score(text, POSITIVE_TERMS)
    negative = _term_score(text, NEGATIVE_TERMS)
    if positive > negative and positive > 0:
        return "positive", positive, negative
    if negative > positive and negative > 0:
        return "negative", positive, negative
    return None, positive, negative


def classify_news_categories(headline: str, summary: str | None, content: str | None) -> list[str]:
    text = " ".join(item for item in (headline, summary or "", content or "") if item).lower()
    return [name for name, terms in CATEGORY_TERMS.items() if any(term in text for term in terms)]


def classify_state(polarity: str | None, market_residual_reaction: float) -> str | None:
    """Map one event to exactly one of the four predeclared states.

    Reaction is stock five-minute return minus SPY five-minute return.  Ties at
    zero count as a failed reaction rather than being silently discarded.
    """
    if polarity == "positive":
        return STATE_POSITIVE_CONTINUATION if market_residual_reaction > 0 else STATE_POSITIVE_FAILURE
    if polarity == "negative":
        return STATE_NEGATIVE_CONTINUATION if market_residual_reaction < 0 else STATE_NEGATIVE_FAILURE
    return None


def selection_t_threshold(total_trials: int, familywise_alpha: float = 0.05) -> float:
    if total_trials <= 0:
        raise ValueError("total_trials must be positive")
    tail = familywise_alpha / (2.0 * total_trials)
    return NormalDist().inv_cdf(1.0 - tail)


def _dataset_window(conn: psycopg.Connection, dataset_id: int) -> tuple[datetime, datetime]:
    row = conn.execute(
        """
        SELECT MIN(timestamp) AS first_at, MAX(timestamp) AS last_at
        FROM research_dataset_candles
        WHERE dataset_id = %s AND timeframe = '1m'
        """,
        (dataset_id,),
    ).fetchone()
    if not row or row.get("first_at") is None or row.get("last_at") is None:
        raise ValueError(f"Dataset {dataset_id} has no frozen 1m outcome grid")
    return row["first_at"], row["last_at"]


def _target_symbols(conn: psycopg.Connection, dataset_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT symbol
        FROM research_dataset_candles
        WHERE dataset_id = %s AND timeframe = '1m'
          AND symbol <> ALL(%s)
        ORDER BY symbol
        """,
        (dataset_id, list(EXCLUDED_NEWS_TARGETS)),
    ).fetchall()
    return [str(row["symbol"]).upper() for row in rows]


def news_fingerprint(conn: psycopg.Connection, dataset_id: int) -> dict[str, Any]:
    start, end = _dataset_window(conn, dataset_id)
    symbols = _target_symbols(conn, dataset_id)
    row = conn.execute(
        """
        WITH first_versions AS (
            SELECT DISTINCT ON (provider, article_id, symbol)
                provider, article_id, symbol, known_at, content_hash
            FROM intraday_news_articles
            WHERE symbol = ANY(%s)
              AND known_at >= %s
              AND known_at <= %s + INTERVAL '1 day'
            ORDER BY provider, article_id, symbol, known_at
        )
        SELECT COUNT(*) AS rows,
               MIN(known_at) AS first_at,
               MAX(known_at) AS last_at,
               MD5(COALESCE(STRING_AGG(
                   CONCAT_WS('|', provider, article_id, symbol, known_at::text, content_hash),
                   '||' ORDER BY provider, article_id, symbol
               ), '')) AS content_hash
        FROM first_versions
        """,
        (symbols, start, end),
    ).fetchone()
    return {
        "dataset_id": dataset_id,
        "symbols": symbols,
        "rows": int((row or {}).get("rows") or 0),
        "first_at": (row or {}).get("first_at"),
        "last_at": (row or {}).get("last_at"),
        "content_hash": str((row or {}).get("content_hash") or ""),
    }


def _assert_news_fingerprint(conn: psycopg.Connection, declaration: dict[str, Any]) -> None:
    current = news_fingerprint(conn, int(declaration["dataset_id"]))
    expected = dict(declaration["news_fingerprint"])
    if _stable_hash(current) != _stable_hash(expected):
        raise ValueError(
            "Point-in-time news evidence changed after declaration. Create a new declaration; "
            "do not mix revised news into an already-declared study."
        )


def _event_rows_sql(include_text: bool) -> str:
    text_columns = ", f.headline, f.summary, f.content" if include_text else ""
    return f"""
        WITH target_symbols AS (
            SELECT DISTINCT symbol
            FROM research_dataset_candles
            WHERE dataset_id = %(dataset_id)s
              AND timeframe = '1m'
              AND symbol <> ALL(%(excluded)s)
        ),
        first_versions AS (
            SELECT DISTINCT ON (n.provider, n.article_id, n.symbol)
                n.provider, n.article_id, n.symbol, n.known_at,
                n.headline, n.summary, n.content
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
               OR known_at - previous_symbol_news_at >= INTERVAL '{QUIET_PERIOD_MINUTES} minutes'
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
            f.reaction_start + INTERVAL '5 minutes' AS decision_at,
            r0.open AS reaction_open,
            r4.close AS reaction_close,
            agg.reaction_high,
            agg.reaction_low,
            agg.reaction_volume,
            s0.open AS spy_reaction_open,
            s4.close AS spy_reaction_close,
            e5.open AS entry_open,
            x5.close AS exit_5m_close,
            x10.close AS exit_10m_close,
            x15.close AS exit_15m_close,
            x30.close AS exit_30m_close
            {text_columns}
        FROM aligned f
        JOIN research_dataset_candles r0
          ON r0.dataset_id = %(dataset_id)s
         AND r0.timeframe = '1m'
         AND r0.symbol = f.symbol
         AND r0.timestamp = f.reaction_start
        JOIN research_dataset_candles r4
          ON r4.dataset_id = %(dataset_id)s
         AND r4.timeframe = '1m'
         AND r4.symbol = f.symbol
         AND r4.timestamp = f.reaction_start + INTERVAL '4 minutes'
        JOIN LATERAL (
            SELECT COUNT(*) AS n,
                   MAX(high) AS reaction_high,
                   MIN(low) AS reaction_low,
                   SUM(volume) AS reaction_volume
            FROM research_dataset_candles c
            WHERE c.dataset_id = %(dataset_id)s
              AND c.timeframe = '1m'
              AND c.symbol = f.symbol
              AND c.timestamp BETWEEN f.reaction_start
                                  AND f.reaction_start + INTERVAL '4 minutes'
        ) agg ON agg.n = 5
        JOIN research_dataset_candles s0
          ON s0.dataset_id = %(dataset_id)s
         AND s0.timeframe = '1m'
         AND s0.symbol = 'SPY'
         AND s0.timestamp = f.reaction_start
        JOIN research_dataset_candles s4
          ON s4.dataset_id = %(dataset_id)s
         AND s4.timeframe = '1m'
         AND s4.symbol = 'SPY'
         AND s4.timestamp = f.reaction_start + INTERVAL '4 minutes'
        JOIN research_dataset_candles e5
          ON e5.dataset_id = %(dataset_id)s
         AND e5.timeframe = '1m'
         AND e5.symbol = f.symbol
         AND e5.timestamp = f.reaction_start + INTERVAL '5 minutes'
        JOIN research_dataset_candles x5
          ON x5.dataset_id = %(dataset_id)s
         AND x5.timeframe = '1m'
         AND x5.symbol = f.symbol
         AND x5.timestamp = f.reaction_start + INTERVAL '9 minutes'
        JOIN research_dataset_candles x10
          ON x10.dataset_id = %(dataset_id)s
         AND x10.timeframe = '1m'
         AND x10.symbol = f.symbol
         AND x10.timestamp = f.reaction_start + INTERVAL '14 minutes'
        JOIN research_dataset_candles x15
          ON x15.dataset_id = %(dataset_id)s
         AND x15.timeframe = '1m'
         AND x15.symbol = f.symbol
         AND x15.timestamp = f.reaction_start + INTERVAL '19 minutes'
        JOIN research_dataset_candles x30
          ON x30.dataset_id = %(dataset_id)s
         AND x30.timeframe = '1m'
         AND x30.symbol = f.symbol
         AND x30.timestamp = f.reaction_start + INTERVAL '34 minutes'
        WHERE (f.reaction_start AT TIME ZONE 'America/New_York')::date
              = (f.known_at AT TIME ZONE 'America/New_York')::date
          AND (f.reaction_start AT TIME ZONE 'America/New_York')::time >= TIME '09:30'
          AND (f.reaction_start AT TIME ZONE 'America/New_York')::time <= TIME '15:25'
        ORDER BY f.reaction_start, f.symbol, f.article_id
    """


def _load_events(conn: psycopg.Connection, dataset_id: int, *, include_text: bool) -> list[dict[str, Any]]:
    start, end = _dataset_window(conn, dataset_id)
    rows = conn.execute(
        _event_rows_sql(include_text),
        {
            "dataset_id": dataset_id,
            "excluded": list(EXCLUDED_NEWS_TARGETS),
            "window_start": start,
            "window_end": end,
        },
    ).fetchall()
    return [dict(row) for row in rows]


def preflight_news_reaction(conn: psycopg.Connection, *, dataset_id: int) -> dict[str, Any]:
    splits = get_dataset_splits(conn, dataset_id)
    if splits is None:
        raise ValueError(f"Dataset {dataset_id} has no frozen nested research split")
    rows = _load_events(conn, dataset_id, include_text=False)
    counts: Counter[str] = Counter()
    sessions: dict[str, set[date]] = {phase: set() for phase in ("discovery", "validation", "confirmation")}
    symbols: dict[str, set[str]] = {phase: set() for phase in sessions}
    for row in rows:
        phase = splits.phase_for(row["decision_at"])
        if phase is None:
            continue
        counts[phase] += 1
        sessions[phase].add(row["reaction_start"].astimezone().date() if row["reaction_start"].tzinfo else row["reaction_start"].date())
        symbols[phase].add(str(row["symbol"]).upper())
    return {
        "protocol_version": NEWS_REACTION_VERSION,
        "dataset_id": dataset_id,
        "return_blind": True,
        "outcome_fields_accessed": [],
        "news_fingerprint": news_fingerprint(conn, dataset_id),
        "phases": {
            phase: {
                "events": counts[phase],
                "distinct_sessions_approx": len(sessions[phase]),
                "symbols": len(symbols[phase]),
            }
            for phase in ("discovery", "validation", "confirmation")
        },
        "reaction_minutes": REACTION_MINUTES,
        "horizons_minutes": list(HORIZONS_MINUTES),
        "fresh_tests": FRESH_TESTS,
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
    cost_model = load_cost_model(conn, cost_calibration_id)
    preflight = preflight_news_reaction(conn, dataset_id=dataset_id)
    if preflight["phases"]["discovery"]["events"] < 200 or preflight["phases"]["validation"]["events"] < 200:
        raise ValueError("Insufficient event supply for governed discovery/validation")
    total_trials = prior_effective_trials + FRESH_TESTS
    specification = {
        "purpose": purpose,
        "dataset_id": dataset_id,
        "cost_calibration_id": cost_calibration_id,
        "cost_model_hash": _stable_hash(cost_model),
        "reaction_minutes": REACTION_MINUTES,
        "horizons_minutes": list(HORIZONS_MINUTES),
        "quiet_period_minutes": QUIET_PERIOD_MINUTES,
        "excluded_news_targets": list(EXCLUDED_NEWS_TARGETS),
        "states": STATE_DIRECTIONS,
        "polarity_model": {
            "positive_terms": list(POSITIVE_TERMS),
            "negative_terms": list(NEGATIVE_TERMS),
            "ties": "excluded_as_neutral",
        },
        "reaction_measure": "stock_5m_return_minus_spy_5m_return",
        "entry": "open_of_first_1m_bar_after_completed_5m_reaction",
        "target_gross_block_bootstrap_lower_bound_bps": TARGET_GROSS_LOWER_BOUND_BPS,
        "prior_effective_trials": prior_effective_trials,
        "fresh_tests": FRESH_TESTS,
        "total_effective_trials": total_trials,
        "bonferroni_two_sided_t_threshold": selection_t_threshold(total_trials),
        "splits": splits.as_dict(),
        "confirmation_untouched": True,
        "price_source": "frozen_research_dataset_candles_1m",
        "trade_flow_used": False,
        "news_categories_are_labels_not_tests": True,
    }
    specification_hash = _stable_hash(specification)
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
            Jsonb(_jsonable(specification)),
            specification_hash,
            Jsonb(_jsonable(fingerprint)),
            NEWS_REACTION_VERSION,
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


def _prepare_events(
    conn: psycopg.Connection,
    declaration: dict[str, Any],
    *,
    allowed_phases: set[str],
) -> list[dict[str, Any]]:
    splits = get_dataset_splits(conn, int(declaration["dataset_id"]))
    if splits is None:
        raise ValueError("Frozen split disappeared")
    cost_model = load_cost_model(conn, int(declaration["cost_calibration_id"]))
    if _stable_hash(cost_model) != str(declaration["specification"]["cost_model_hash"]):
        raise ValueError("Cost calibration changed after declaration")
    rows = _load_events(conn, int(declaration["dataset_id"]), include_text=True)
    prepared: list[dict[str, Any]] = []
    for row in rows:
        phase = splits.phase_for(row["decision_at"])
        if phase not in allowed_phases:
            continue
        reaction_open = float(row["reaction_open"])
        reaction_close = float(row["reaction_close"])
        spy_open = float(row["spy_reaction_open"])
        spy_close = float(row["spy_reaction_close"])
        entry_open = float(row["entry_open"])
        if min(reaction_open, spy_open, entry_open) <= 0:
            continue
        stock_reaction = reaction_close / reaction_open - 1.0
        spy_reaction = spy_close / spy_open - 1.0
        residual = stock_reaction - spy_reaction
        polarity, positive_score, negative_score = classify_news_polarity(
            str(row["headline"]), row.get("summary"), row.get("content")
        )
        state = classify_state(polarity, residual)
        if state is None:
            continue
        direction = STATE_DIRECTIONS[state]
        cost_bps = estimated_round_trip_cost_bps(
            cost_model,
            symbol=str(row["symbol"]),
            timestamp=row["decision_at"],
            stressed=True,
        )
        outcomes: dict[int, dict[str, float]] = {}
        for horizon in HORIZONS_MINUTES:
            exit_close = float(row[f"exit_{horizon}m_close"])
            raw_return = exit_close / entry_open - 1.0
            gross = direction * raw_return
            outcomes[horizon] = {
                "raw_return": raw_return,
                "gross_return": gross,
                "net_return": gross - cost_bps / 10_000.0,
            }
        high = float(row["reaction_high"])
        low = float(row["reaction_low"])
        close_location = (reaction_close - low) / (high - low) if high > low else 0.5
        prepared.append(
            {
                "provider": row["provider"],
                "article_id": row["article_id"],
                "symbol": str(row["symbol"]).upper(),
                "session_date": row["reaction_start"].astimezone().date() if row["reaction_start"].tzinfo else row["reaction_start"].date(),
                "known_at": row["known_at"],
                "reaction_start": row["reaction_start"],
                "decision_at": row["decision_at"],
                "phase": phase,
                "polarity": polarity,
                "positive_news_score": positive_score,
                "negative_news_score": negative_score,
                "categories": classify_news_categories(str(row["headline"]), row.get("summary"), row.get("content")),
                "state": state,
                "direction": direction,
                "reaction_return_bps": stock_reaction * 10_000.0,
                "spy_reaction_return_bps": spy_reaction * 10_000.0,
                "market_residual_reaction_bps": residual * 10_000.0,
                "reaction_range_bps": (high - low) / reaction_open * 10_000.0,
                "reaction_close_location": close_location,
                "reaction_volume": float(row["reaction_volume"] or 0),
                "cost_bps": cost_bps,
                "outcomes": outcomes,
            }
        )
    return prepared


def _phase_cell_report(
    events: Sequence[dict[str, Any]],
    *,
    state: str,
    horizon: int,
    total_trials: int,
) -> dict[str, Any]:
    selected = [row for row in events if row["state"] == state]
    gross_rows = [
        {
            "value": row["outcomes"][horizon]["gross_return"],
            "session_date": row["session_date"],
            "symbol": row["symbol"],
            "timestamp": row["decision_at"],
        }
        for row in selected
    ]
    net_rows = [
        {
            "value": row["outcomes"][horizon]["net_return"],
            "session_date": row["session_date"],
            "symbol": row["symbol"],
            "timestamp": row["decision_at"],
        }
        for row in selected
    ]
    gross = clustered_outcome_statistics(gross_rows, effective_trials=total_trials, require_symbol_diversification=True)
    net = clustered_outcome_statistics(net_rows, effective_trials=total_trials, require_symbol_diversification=True)
    categories = Counter(category for row in selected for category in row["categories"])
    return {
        "state": state,
        "direction": "long" if STATE_DIRECTIONS[state] > 0 else "short",
        "horizon_minutes": horizon,
        "events": len(selected),
        "gross": gross,
        "net": net,
        "mean_reaction_bps": (
            sum(float(row["market_residual_reaction_bps"]) for row in selected) / len(selected)
            if selected else None
        ),
        "mean_cost_bps": (
            sum(float(row["cost_bps"]) for row in selected) / len(selected)
            if selected else None
        ),
        "category_counts": dict(categories),
    }


def cell_passes_promotion(discovery: dict[str, Any], validation: dict[str, Any], *, t_threshold: float) -> bool:
    def lower(report: dict[str, Any], side: str) -> float | None:
        ci = (report.get(side) or {}).get("block_bootstrap", {}).get("confidence_interval_95")
        return float(ci[0]) if ci else None

    d_gross = lower(discovery, "gross")
    d_net = lower(discovery, "net")
    v_gross = lower(validation, "gross")
    v_net = lower(validation, "net")
    v_t = (validation.get("net") or {}).get("day_clustered_t_statistic")
    return bool(
        d_gross is not None and d_gross >= TARGET_GROSS_LOWER_BOUND_BPS
        and d_net is not None and d_net > 0
        and v_gross is not None and v_gross >= TARGET_GROSS_LOWER_BOUND_BPS
        and v_net is not None and v_net > 0
        and v_t is not None and float(v_t) >= t_threshold
        and bool((discovery.get("gross") or {}).get("independent_evidence_ready"))
        and bool((validation.get("gross") or {}).get("independent_evidence_ready"))
    )


def run_news_reaction_discovery(conn: psycopg.Connection, *, declaration_id: int) -> dict[str, Any]:
    declaration_row = conn.execute(
        "SELECT * FROM intraday_news_reaction_declarations WHERE id = %s",
        (declaration_id,),
    ).fetchone()
    if not declaration_row:
        raise ValueError(f"Unknown news-reaction declaration {declaration_id}")
    declaration = dict(declaration_row)
    if conn.execute(
        "SELECT 1 FROM intraday_news_reaction_runs WHERE declaration_id = %s", (declaration_id,)
    ).fetchone():
        raise ValueError("This declaration already has a discovery run; do not re-run spent evidence")
    _assert_news_fingerprint(conn, declaration)
    spec = dict(declaration["specification"])
    total_trials = int(spec["total_effective_trials"])
    t_threshold = float(spec["bonferroni_two_sided_t_threshold"])
    events = _prepare_events(conn, declaration, allowed_phases={"discovery", "validation"})
    discovery_events = [row for row in events if row["phase"] == "discovery"]
    validation_events = [row for row in events if row["phase"] == "validation"]
    cells: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for state in STATE_DIRECTIONS:
        for horizon in HORIZONS_MINUTES:
            discovery = _phase_cell_report(discovery_events, state=state, horizon=horizon, total_trials=total_trials)
            validation = _phase_cell_report(validation_events, state=state, horizon=horizon, total_trials=total_trials)
            passed = cell_passes_promotion(discovery, validation, t_threshold=t_threshold)
            cell = {
                "state": state,
                "horizon_minutes": horizon,
                "discovery": discovery,
                "validation": validation,
                "promotion_passed": passed,
            }
            cells.append(cell)
            if passed:
                candidates.append({"state": state, "horizon_minutes": horizon})
    results = {
        "protocol_version": NEWS_REACTION_VERSION,
        "declaration_id": declaration_id,
        "dataset_id": int(declaration["dataset_id"]),
        "effective_trials": total_trials,
        "fresh_tests": FRESH_TESTS,
        "selection_t_threshold": t_threshold,
        "target_gross_lower_bound_bps": TARGET_GROSS_LOWER_BOUND_BPS,
        "development_events_after_polarity_filter": len(events),
        "discovery_events_after_polarity_filter": len(discovery_events),
        "validation_events_after_polarity_filter": len(validation_events),
        "cells": cells,
        "candidate_cells": candidates,
        "strategy_construction_authorized": False,
        "confirmation_accessed": False,
    }
    record_split_access(
        conn,
        dataset_id=int(declaration["dataset_id"]),
        phase="discovery",
        decision_type="news_reaction_5m_discovery",
        detail={"declaration_id": declaration_id, "fresh_tests": FRESH_TESTS},
    )
    record_split_access(
        conn,
        dataset_id=int(declaration["dataset_id"]),
        phase="validation",
        decision_type="news_reaction_5m_validation",
        detail={"declaration_id": declaration_id, "fresh_tests": FRESH_TESTS},
    )
    run = conn.execute(
        """
        INSERT INTO intraday_news_reaction_runs(
            declaration_id, dataset_id, results, effective_trials, candidate_cells, protocol_version
        ) VALUES (%s,%s,%s,%s,%s,%s)
        RETURNING id, created_at
        """,
        (
            declaration_id,
            int(declaration["dataset_id"]),
            Jsonb(_jsonable(results)),
            total_trials,
            Jsonb(candidates),
            NEWS_REACTION_VERSION,
        ),
    ).fetchone()
    conn.commit()
    return {
        "run_id": int(run["id"]),
        **results,
        "next_command": f"confirm --run-id {int(run['id'])}" if candidates else None,
    }


def confirm_news_reaction(conn: psycopg.Connection, *, run_id: int) -> dict[str, Any]:
    run_row = conn.execute(
        "SELECT * FROM intraday_news_reaction_runs WHERE id = %s", (run_id,)
    ).fetchone()
    if not run_row:
        raise ValueError(f"Unknown news-reaction run {run_id}")
    if conn.execute(
        "SELECT 1 FROM intraday_news_reaction_confirmation_runs WHERE discovery_run_id = %s",
        (run_id,),
    ).fetchone():
        raise ValueError("Confirmation already spent for this run")
    candidates = list(run_row["candidate_cells"] or [])
    if not candidates:
        raise ValueError("Discovery produced no candidate cells; confirmation is not justified")
    declaration_row = conn.execute(
        "SELECT * FROM intraday_news_reaction_declarations WHERE id = %s",
        (int(run_row["declaration_id"]),),
    ).fetchone()
    declaration = dict(declaration_row)
    _assert_news_fingerprint(conn, declaration)
    spec = dict(declaration["specification"])
    total_trials = int(spec["total_effective_trials"])
    t_threshold = float(spec["bonferroni_two_sided_t_threshold"])
    events = _prepare_events(conn, declaration, allowed_phases={"confirmation"})
    reports: list[dict[str, Any]] = []
    all_passed = True
    for candidate in candidates:
        report = _phase_cell_report(
            events,
            state=str(candidate["state"]),
            horizon=int(candidate["horizon_minutes"]),
            total_trials=total_trials,
        )
        gross_ci = report["gross"]["block_bootstrap"]["confidence_interval_95"]
        net_ci = report["net"]["block_bootstrap"]["confidence_interval_95"]
        t_value = report["net"]["day_clustered_t_statistic"]
        passed = bool(
            gross_ci and float(gross_ci[0]) >= TARGET_GROSS_LOWER_BOUND_BPS
            and net_ci and float(net_ci[0]) > 0
            and t_value is not None and float(t_value) >= t_threshold
            and report["gross"]["independent_evidence_ready"]
        )
        reports.append({**report, "confirmation_passed": passed})
        all_passed = all_passed and passed
    results = {
        "protocol_version": NEWS_REACTION_VERSION,
        "discovery_run_id": run_id,
        "confirmation_events_after_polarity_filter": len(events),
        "candidate_reports": reports,
        "passed": all_passed,
        "strategy_construction_authorized": all_passed,
    }
    record_split_access(
        conn,
        dataset_id=int(declaration["dataset_id"]),
        phase="confirmation",
        decision_type="news_reaction_5m_confirmation",
        detail={"run_id": run_id, "candidate_cells": candidates},
    )
    row = conn.execute(
        """
        INSERT INTO intraday_news_reaction_confirmation_runs(
            discovery_run_id, declaration_id, results, passed, protocol_version
        ) VALUES (%s,%s,%s,%s,%s)
        RETURNING id, created_at
        """,
        (
            run_id,
            int(run_row["declaration_id"]),
            Jsonb(_jsonable(results)),
            all_passed,
            NEWS_REACTION_VERSION,
        ),
    ).fetchone()
    conn.commit()
    return {"confirmation_run_id": int(row["id"]), **results}


def news_reaction_report(conn: psycopg.Connection, *, run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT r.*, d.specification, d.specification_hash, d.news_fingerprint
        FROM intraday_news_reaction_runs r
        JOIN intraday_news_reaction_declarations d ON d.id = r.declaration_id
        WHERE r.id = %s
        """,
        (run_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Unknown news-reaction run {run_id}")
    confirmation = conn.execute(
        "SELECT id, results, passed, created_at FROM intraday_news_reaction_confirmation_runs WHERE discovery_run_id = %s",
        (run_id,),
    ).fetchone()
    return {
        "run_id": run_id,
        "declaration_id": int(row["declaration_id"]),
        "dataset_id": int(row["dataset_id"]),
        "effective_trials": int(row["effective_trials"]),
        "specification_hash": row["specification_hash"],
        "specification": row["specification"],
        "results": row["results"],
        "confirmation": dict(confirmation) if confirmation else None,
        "created_at": row["created_at"],
    }
