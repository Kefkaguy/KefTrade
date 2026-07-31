"""Point-in-time liquid-universe construction for intraday research.

Index membership is licensed data this system does not have, so the universe
is constructed from liquidity instead -- but constructed *point in time*: each
rebalance ranks symbols using only bars that existed before its effective
date, and membership intervals are stored so a symbol that dropped out later
is still a member for the sessions it qualified for.

The harder problem is the candidate pool.  Ranking today's tradable symbols
over ten years of history is survivorship bias no matter how carefully the
ranking itself is done, because the names that failed are simply absent.  This
module therefore takes the pool as an explicit input, records how it was
assembled, and audits whether it contains any symbol that stopped trading
during the window.  A pool of exclusively still-active names is reported as
biased rather than quietly treated as a universe.

The module contains no campaign, broker, order-submission, or UI code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256
from json import dumps
from statistics import median
from typing import Any, Sequence

import psycopg
from psycopg.types.json import Jsonb

UNIVERSE_VERSION = "intraday_universe_v1"

DEFAULT_REBALANCE_MONTHS = 3
DEFAULT_RANK_LOOKBACK_SESSIONS = 60
DEFAULT_MINIMUM_SESSIONS = 40


@dataclass(frozen=True)
class UniverseRule:
    """The construction rule, frozen and hashed so a universe is reproducible."""

    universe_key: str
    target_size: int
    rebalance_months: int = DEFAULT_REBALANCE_MONTHS
    rank_lookback_sessions: int = DEFAULT_RANK_LOOKBACK_SESSIONS
    minimum_sessions: int = DEFAULT_MINIMUM_SESSIONS
    minimum_median_dollar_volume: float = 20_000_000.0
    timeframe: str = "30m"
    source: str = "alpaca_sip"
    metric: str = "trailing_median_daily_dollar_volume"

    def frozen(self) -> dict[str, Any]:
        return {
            "universe_version": UNIVERSE_VERSION,
            "universe_key": self.universe_key,
            "target_size": self.target_size,
            "rebalance_months": self.rebalance_months,
            "rank_lookback_sessions": self.rank_lookback_sessions,
            "minimum_sessions": self.minimum_sessions,
            "minimum_median_dollar_volume": self.minimum_median_dollar_volume,
            "timeframe": self.timeframe,
            "source": self.source,
            "metric": self.metric,
        }

    def rule_hash(self) -> str:
        return sha256(dumps(self.frozen(), sort_keys=True).encode()).hexdigest()


def rebalance_dates(start: date, end: date, *, months: int) -> list[date]:
    """First-of-month rebalance dates covering the window."""
    if months < 1:
        raise ValueError("rebalance_months must be at least 1")
    dates: list[date] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        dates.append(cursor)
        month = cursor.month - 1 + months
        cursor = date(cursor.year + month // 12, month % 12 + 1, 1)
    return dates


def session_dollar_volume(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    timeframe: str,
    source: str,
    start: date,
    end: date,
) -> dict[str, dict[date, float]]:
    """Daily traded value per symbol, from regular-session bars only."""
    rows = conn.execute(
        """
        SELECT symbol,
               (timestamp AT TIME ZONE 'America/New_York')::date AS session_date,
               SUM(close * volume) AS dollar_volume
        FROM candles
        WHERE symbol = ANY(%s) AND timeframe = %s AND source = %s
          AND (timestamp AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s
          AND (timestamp AT TIME ZONE 'America/New_York')::time >= TIME '09:30'
          AND (timestamp AT TIME ZONE 'America/New_York')::time < TIME '16:00'
        GROUP BY 1, 2
        """,
        ([item.upper() for item in symbols], timeframe, source, start, end),
    ).fetchall()
    output: dict[str, dict[date, float]] = {}
    for row in rows:
        output.setdefault(str(row["symbol"]).upper(), {})[row["session_date"]] = float(
            row["dollar_volume"] or 0
        )
    return output


def rank_universe_at(
    liquidity: dict[str, dict[date, float]],
    *,
    as_of: date,
    rule: UniverseRule,
) -> list[dict[str, Any]]:
    """Rank symbols using only sessions strictly before ``as_of``."""
    scored: list[dict[str, Any]] = []
    for symbol, by_session in liquidity.items():
        history = [
            value
            for session_date, value in sorted(by_session.items())
            if session_date < as_of
        ][-rule.rank_lookback_sessions :]
        if len(history) < rule.minimum_sessions:
            continue
        value = median(history)
        if value < rule.minimum_median_dollar_volume:
            continue
        scored.append(
            {
                "symbol": symbol,
                "median_dollar_volume": round(value, 2),
                "sessions_scored": len(history),
            }
        )
    scored.sort(key=lambda row: (-row["median_dollar_volume"], row["symbol"]))
    for rank, row in enumerate(scored[: rule.target_size], start=1):
        row["rank"] = rank
    return scored[: rule.target_size]


def build_membership_intervals(
    liquidity: dict[str, dict[date, float]],
    *,
    rule: UniverseRule,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    """Turn per-rebalance memberships into contiguous effective intervals."""
    schedule = rebalance_dates(start, end, months=rule.rebalance_months)
    memberships: dict[str, list[tuple[date, date]]] = {}
    for index, as_of in enumerate(schedule):
        period_end = (
            schedule[index + 1] - timedelta(days=1) if index + 1 < len(schedule) else end
        )
        for row in rank_universe_at(liquidity, as_of=as_of, rule=rule):
            memberships.setdefault(row["symbol"], []).append((as_of, period_end))

    intervals: list[dict[str, Any]] = []
    for symbol, periods in memberships.items():
        periods.sort()
        current_start, current_end = periods[0]
        for next_start, next_end in periods[1:]:
            if next_start <= current_end + timedelta(days=1):
                current_end = max(current_end, next_end)
                continue
            intervals.append({"symbol": symbol, "effective_from": current_start, "effective_to": current_end})
            current_start, current_end = next_start, next_end
        intervals.append({"symbol": symbol, "effective_from": current_start, "effective_to": current_end})
    intervals.sort(key=lambda row: (row["symbol"], row["effective_from"]))
    return intervals


def survivorship_audit(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    timeframe: str,
    source: str,
    end: date,
    inactive_grace_days: int = 30,
) -> dict[str, Any]:
    """Does the candidate pool contain names that stopped trading?

    If every candidate is still trading at the end of the window, the pool was
    drawn from survivors and the universe inherits that bias regardless of how
    the ranking was done.
    """
    rows = conn.execute(
        """
        SELECT symbol, MAX((timestamp AT TIME ZONE 'America/New_York')::date) AS last_session
        FROM candles
        WHERE symbol = ANY(%s) AND timeframe = %s AND source = %s
        GROUP BY symbol
        """,
        ([item.upper() for item in symbols], timeframe, source),
    ).fetchall()
    cutoff = end - timedelta(days=inactive_grace_days)
    ceased = sorted(
        str(row["symbol"]) for row in rows if row["last_session"] and row["last_session"] < cutoff
    )
    inactive_registered = conn.execute(
        "SELECT COUNT(*) AS total FROM symbols WHERE symbol = ANY(%s) AND is_active = FALSE",
        ([item.upper() for item in symbols],),
    ).fetchone()
    return {
        "candidate_symbols": len(symbols),
        "symbols_with_history": len(rows),
        "symbols_that_stopped_trading": len(ceased),
        "ceased_symbols": ceased[:50],
        "registered_inactive_symbols": int((inactive_registered or {}).get("total") or 0),
        # The honest verdict: a pool of only-survivors cannot produce an
        # unbiased universe, and callers must see that as a limitation.
        "survivorship_bias_present": len(ceased) == 0,
        "limitations": (
            [
                "candidate_pool_contains_no_delisted_or_ceased_symbols",
                "membership_is_liquidity_derived_not_licensed_index_membership",
            ]
            if len(ceased) == 0
            else ["membership_is_liquidity_derived_not_licensed_index_membership"]
        ),
    }


def record_universe_definition(
    conn: psycopg.Connection,
    *,
    rule: UniverseRule,
    start: date,
    end: date,
    candidate_symbols: Sequence[str],
    audit: dict[str, Any],
) -> dict[str, Any]:
    row = conn.execute(
        """
        INSERT INTO research_universe_definitions(
            universe_key, rule_hash, universe_version, construction_rule,
            candidate_symbols, window_start, window_end, survivorship_audit
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (universe_key, rule_hash) DO NOTHING
        RETURNING id, created_at
        """,
        (
            rule.universe_key,
            rule.rule_hash(),
            UNIVERSE_VERSION,
            Jsonb(rule.frozen()),
            Jsonb(sorted(item.upper() for item in candidate_symbols)),
            start,
            end,
            Jsonb(audit),
        ),
    ).fetchone()
    conn.commit()
    if row:
        return {"universe_definition_id": int(row["id"]), "created": True}
    existing = conn.execute(
        "SELECT id FROM research_universe_definitions WHERE universe_key = %s AND rule_hash = %s",
        (rule.universe_key, rule.rule_hash()),
    ).fetchone()
    return {"universe_definition_id": int(existing["id"]), "created": False}


def build_point_in_time_universe(
    conn: psycopg.Connection,
    *,
    rule: UniverseRule,
    candidate_symbols: Sequence[str],
    start: date,
    end: date,
) -> dict[str, Any]:
    """Construct and persist point-in-time membership for the window."""
    from app.services.intraday_research_data import persist_point_in_time_membership

    liquidity = session_dollar_volume(
        conn,
        symbols=candidate_symbols,
        timeframe=rule.timeframe,
        source=rule.source,
        start=start - timedelta(days=int(rule.rank_lookback_sessions * 2.2)),
        end=end,
    )
    intervals = build_membership_intervals(liquidity, rule=rule, start=start, end=end)
    audit = survivorship_audit(
        conn,
        symbols=candidate_symbols,
        timeframe=rule.timeframe,
        source=rule.source,
        end=end,
    )
    definition = record_universe_definition(
        conn,
        rule=rule,
        start=start,
        end=end,
        candidate_symbols=candidate_symbols,
        audit=audit,
    )
    inserted = persist_point_in_time_membership(
        conn,
        [
            {
                "universe_key": rule.universe_key,
                "symbol": item["symbol"],
                "effective_from": item["effective_from"],
                "effective_to": item["effective_to"],
                "source": f"{UNIVERSE_VERSION}:{rule.rule_hash()[:12]}",
                "evidence": {
                    "rule": rule.frozen(),
                    "universe_definition_id": definition["universe_definition_id"],
                },
            }
            for item in intervals
        ],
    )
    conn.commit()
    distinct = sorted({item["symbol"] for item in intervals})
    return {
        "universe_version": UNIVERSE_VERSION,
        "universe_key": rule.universe_key,
        "rule_hash": rule.rule_hash(),
        "universe_definition_id": definition["universe_definition_id"],
        "rebalances": len(rebalance_dates(start, end, months=rule.rebalance_months)),
        "distinct_members": len(distinct),
        "membership_intervals": len(intervals),
        "intervals_inserted": inserted,
        "members": distinct,
        "survivorship_audit": audit,
    }


def membership_coverage(
    conn: psycopg.Connection,
    *,
    universe_key: str,
    dataset_id: int,
    timeframe: str,
) -> dict[str, Any]:
    """Was every snapshot bar inside its symbol's membership interval?"""
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE membership.symbol IS NOT NULL) AS covered
        FROM research_dataset_candles candle
        LEFT JOIN research_point_in_time_universe_membership membership
          ON membership.universe_key = %s
         AND membership.symbol = candle.symbol
         AND membership.effective_from <=
             (candle.timestamp AT TIME ZONE 'America/New_York')::date
         AND (membership.effective_to IS NULL OR membership.effective_to >=
             (candle.timestamp AT TIME ZONE 'America/New_York')::date)
        WHERE candle.dataset_id = %s AND candle.timeframe = %s
        """,
        (universe_key, dataset_id, timeframe),
    ).fetchone()
    total = int((row or {}).get("total") or 0)
    covered = int((row or {}).get("covered") or 0)
    coverage = covered / total if total else None
    return {
        "universe_key": universe_key,
        "dataset_id": dataset_id,
        "candle_rows": total,
        "rows_inside_membership": covered,
        "coverage": round(coverage, 6) if coverage is not None else None,
        "every_observation_inside_membership": total > 0 and covered == total,
    }
