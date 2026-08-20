"""Stage 4.0 audit: measure what we hold, decide what it can support.

Split deliberately into two halves. Everything that *decides* is a pure function
of measured facts, so the reasoning can be tested exhaustively without a
database; everything that *measures* is a thin query returning those facts. A
verdict that could only be exercised against production data would be a verdict
nobody could check.

No function here reads a price after a decision instant. There is no horizon
parameter, no holding period and no return: the audit stops at "what states can
be measured, on how many events, with which clocks", which is the whole
question Stage 4.0 was asked.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.services.stage40_audit_plan import (
    L3_STATE_COVERAGE,
    L3_STATE_GAPS,
    MIN_EVENTS_FOR_MECHANISM,
    MIN_SESSIONS_FOR_MECHANISM,
    MISNAMED_OPTION_FEATURES,
    OPTION_FIELD_SEMANTICS,
    OPTIONS_NOT_SUITABLE,
    OPTIONS_SIGNED_FLOW,
    OPTIONS_STATE_ONLY,
    RECOMMEND_ACQUIRE,
    RECOMMEND_IAG,
    RECOMMEND_NO_CAUSAL,
    RECOMMEND_NO_SUPPLY,
    RECOMMEND_OPTIONS_STOCK,
    STAGE40_PLAN_VERSION,
    AuditWindow,
    alignment_resolution_ns,
    assert_decision_safe,
    timestamp_semantics,
)

STAGE40_AUDIT_VERSION = "tier1_stage40_audit_v1"

# Tables this audit inventories, with the clock each is indexed by. A table with
# no declared clock cannot be audited, because "coverage" is meaningless without
# one.
AUDITED_TABLES: tuple[tuple[str, str, str | None], ...] = (
    ("intraday_news_articles", "known_at", "symbol"),
    ("intraday_option_chain_snapshots", "observed_at", "underlying_symbol"),
    ("intraday_quote_snapshots", "timestamp", "symbol"),
    ("intraday_trade_flow_features", "timestamp", "symbol"),
    ("candles", "timestamp", "symbol"),
)


# ---------------------------------------------------------------------------
# Measured facts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TableCoverage:
    """What one table actually holds inside one window."""

    table: str
    window: str
    rows: int
    first_instant: str | None
    last_instant: str | None
    distinct_symbols: int
    distinct_days: int

    @property
    def is_empty(self) -> bool:
        return self.rows == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "window": self.window,
            "rows": self.rows,
            "first_instant": self.first_instant,
            "last_instant": self.last_instant,
            "distinct_symbols": self.distinct_symbols,
            "distinct_days": self.distinct_days,
            "present": not self.is_empty,
        }


@dataclass(frozen=True, slots=True)
class OptionQuality:
    """Outcome-blind quality of the option snapshots inside one window."""

    window: str
    rows: int
    rows_with_quote: int
    rows_with_trade: int
    rows_with_iv: int
    rows_with_greeks: int
    rows_with_open_interest: int
    crossed_or_locked: int
    max_quote_staleness_seconds: float | None
    median_quote_staleness_seconds: float | None

    def as_dict(self) -> dict[str, Any]:
        def share(count: int) -> float | None:
            return round(count / self.rows, 6) if self.rows else None

        return {
            "window": self.window,
            "rows": self.rows,
            "quote_present_share": share(self.rows_with_quote),
            "trade_present_share": share(self.rows_with_trade),
            "implied_volatility_share": share(self.rows_with_iv),
            "greeks_share": share(self.rows_with_greeks),
            "open_interest_share": share(self.rows_with_open_interest),
            "crossed_or_locked_rows": self.crossed_or_locked,
            "crossed_or_locked_share": share(self.crossed_or_locked),
            "max_quote_staleness_seconds": self.max_quote_staleness_seconds,
            "median_quote_staleness_seconds": self.median_quote_staleness_seconds,
        }


# ---------------------------------------------------------------------------
# Pure reasoning: options
# ---------------------------------------------------------------------------


def signed_flow_requirements() -> dict[str, bool]:
    """What reconstructing signed option flow would actually need.

    Stated as requirements rather than a conclusion, so the verdict is derived
    from the data rather than asserted. Signing a trade needs the trade, a
    quote to compare it against, and a *sequence* -- one latest-trade snapshot
    cannot be classified no matter how good its fields are.
    """
    present = {field.column: field.present for field in OPTION_FIELD_SEMANTICS}
    return {
        "trade_sequence": present.get("trade_sequence", False),
        "trade_conditions": present.get("trade_conditions", False),
        "exchange": present.get("exchange", False),
        "quote_at_trade_time": present.get("bid_price", False)
        and present.get("ask_price", False),
        "cumulative_volume": present.get("volume", False),
    }


def options_feasibility(
    *,
    coverage: TableCoverage | None,
    quality: OptionQuality | None = None,
    overlaps_l3: bool = False,
) -> dict[str, Any]:
    """One of the three declared option verdicts, with its reasons.

    Availability is checked before field quality. A perfect schema over a window
    that holds no rows is not "state only"; it is not suitable, and saying so
    plainly is more useful than grading fields nobody can read.
    """
    requirements = signed_flow_requirements()
    blocking = sorted(name for name, met in requirements.items() if not met)

    if coverage is None or coverage.is_empty:
        return {
            "verdict": OPTIONS_NOT_SUITABLE,
            "reason": (
                "no option-chain rows exist in this window, so no option-derived "
                "state can be constructed here regardless of field quality"
            ),
            "signed_flow_requirements": requirements,
            "blocking_for_signed_flow": blocking,
            "overlaps_l3_window": overlaps_l3,
            "state_variables_available": [],
            "misnamed_fields_not_to_use": list(MISNAMED_OPTION_FEATURES),
        }

    if not blocking:
        return {
            "verdict": OPTIONS_SIGNED_FLOW,
            "reason": "every requirement for causal trade signing is present",
            "signed_flow_requirements": requirements,
            "blocking_for_signed_flow": [],
            "overlaps_l3_window": overlaps_l3,
            "state_variables_available": _option_state_variables(),
            "misnamed_fields_not_to_use": list(MISNAMED_OPTION_FEATURES),
        }

    return {
        "verdict": OPTIONS_STATE_ONLY,
        "reason": (
            "the chain endpoint stores a latest-quote and latest-trade snapshot "
            "per contract, not a tape. Without a trade sequence there is no "
            "series to sign, so these are cross-market state variables and must "
            "not be described as informed order flow"
        ),
        "signed_flow_requirements": requirements,
        "blocking_for_signed_flow": blocking,
        "overlaps_l3_window": overlaps_l3,
        "state_variables_available": _option_state_variables(),
        "misnamed_fields_not_to_use": list(MISNAMED_OPTION_FEATURES),
        "quality": quality.as_dict() if quality is not None else None,
    }


def _option_state_variables() -> list[dict[str, str]]:
    """What the snapshots *can* support, stated without overclaiming."""
    return [
        {
            "name": "implied_volatility_change",
            "basis": "implied_volatility across consecutive snapshots of one contract",
            "caveat": "provider-computed; not independently re-derived",
        },
        {
            "name": "put_call_iv_skew_change",
            "basis": "IV difference between matched put and call strikes",
            "caveat": "requires both legs present in the same snapshot",
        },
        {
            "name": "iv_term_structure_change",
            "basis": "IV across expiries at comparable moneyness",
            "caveat": "moneyness is approximate without a stored underlying price",
        },
        {
            "name": "quoted_spread_change",
            "basis": "ask_price minus bid_price on near-the-money contracts",
            "caveat": "single-source quote; not a consolidated NBBO",
        },
        {
            "name": "open_interest_change",
            "basis": "open_interest across sessions",
            "caveat": "settles once daily; not an intraday signal",
        },
        {
            "name": "quoted_size_change",
            "basis": "bid_size and ask_size across snapshots",
            "caveat": "one venue's displayed size, not consolidated depth",
        },
    ]


def put_call_parity_feasible() -> dict[str, Any]:
    """Whether synthetic-forward / parity deviations can be computed.

    They cannot, from this table alone: parity needs the underlying price at the
    same instant as the option quotes, and no underlying price is stored. The
    ATM anchor falls back to the median listed strike, which is a property of
    the strike ladder rather than of the market.
    """
    present = {field.column: field.present for field in OPTION_FIELD_SEMANTICS}
    return {
        "feasible_from_option_table_alone": False,
        "missing": ["underlying_price"],
        "atm_anchor_fallback": "median listed strike",
        "note": (
            "Parity would need a synchronized underlying price. Joining one from "
            "candles or quote snapshots is possible but introduces a second "
            "clock, and the join must then be audited at the coarser of the two "
            "resolutions rather than the finer."
        ),
        "requires_present": sorted(k for k, v in present.items() if v),
    }


# ---------------------------------------------------------------------------
# Pure reasoning: stock flow
# ---------------------------------------------------------------------------


def stock_flow_feasibility(
    *,
    trade_flow_coverage: TableCoverage | None,
    quote_coverage: TableCoverage | None,
    decertified_fields: Sequence[str] = (),
) -> dict[str, Any]:
    """Whether market-wide signed stock flow can be estimated.

    Two independent obstacles, and either alone is decisive: the raw prints are
    not persisted, and the consolidated quote sizes were decertified by Stage 0.
    """
    finest = timestamp_semantics("intraday_trade_flow_features", "timestamp")
    return {
        "raw_trade_prints_persisted": False,
        "finest_signed_flow_resolution_ns": finest.resolution_ns,
        "finest_signed_flow_resolution_human": "15 minutes",
        "aggregation_note": (
            "Trades are ingested and aggregated straight into "
            "intraday_trade_flow_features at 15m/30m. The individual prints are "
            "not stored, so no finer signed series can be rebuilt from what we "
            "hold."
        ),
        "quoted_size_usable_as_depth": False,
        "decertified_fields": list(decertified_fields),
        "decertification_note": (
            "Stage 0 measured venue rotation at 45.224% of gross |e_n| against a "
            "30% ceiling, retiring the quoted-size-derived fields on "
            "intraday_quote_snapshots. NBBO *prices* were explicitly not "
            "certified against and remain usable. The identically named L3 "
            "features are book-derived and are NOT covered by that "
            "certification; retiring them by name collision would discard good "
            "data."
        ),
        "sufficient_for_market_wide_signed_flow": False,
        "trade_flow_present": trade_flow_coverage is not None
        and not trade_flow_coverage.is_empty,
        "quote_present": quote_coverage is not None and not quote_coverage.is_empty,
        "would_require": [
            {
                "data": "SIP / TAQ trade prints",
                "why": "signing requires individual trades, not bucket aggregates",
            },
            {
                "data": "NBBO quotes at trade resolution",
                "why": "a trade is signed against the quote prevailing at its instant",
            },
            {
                "data": "exchange identifiers",
                "why": "to separate genuine venue rotation from real size change",
            },
            {
                "data": "trade condition codes",
                "why": "to exclude odd-lot, derivatively-priced and out-of-sequence prints",
            },
        ],
    }


# ---------------------------------------------------------------------------
# Pure reasoning: L3 state
# ---------------------------------------------------------------------------


def mbo_state_feature_feasibility(available_features: Sequence[str]) -> dict[str, Any]:
    """Which requested dislocation states the certified vocabulary can express.

    A concept counts as covered only if every feature backing it is actually
    present in the frozen vocabulary. Partial coverage is reported as partial
    rather than rounded up.
    """
    present = set(available_features)
    covered: dict[str, Any] = {}
    for concept, features in L3_STATE_COVERAGE.items():
        missing = [name for name in features if name not in present]
        covered[concept] = {
            "backing_features": list(features),
            "missing_features": missing,
            "constructible": not missing,
        }
    fully = sorted(k for k, v in covered.items() if v["constructible"])
    partial = sorted(k for k, v in covered.items() if not v["constructible"])
    return {
        "vocabulary_size": len(present),
        "concepts": covered,
        "constructible_now": fully,
        "partially_backed": partial,
        "gaps": dict(L3_STATE_GAPS),
        "gap_count": len(L3_STATE_GAPS),
        "note": (
            "Constructibility is a statement about the data only. Whether any "
            "of these states carries information about anything is not asked "
            "here and is not answered."
        ),
    }


# ---------------------------------------------------------------------------
# Pure reasoning: cross-source alignment and supply
# ---------------------------------------------------------------------------


def cross_source_overlap(coverages: Sequence[TableCoverage]) -> dict[str, Any]:
    """Which sources coexist in each window, and at what resolution they join."""
    by_window: dict[str, list[TableCoverage]] = {}
    for coverage in coverages:
        by_window.setdefault(coverage.window, []).append(coverage)

    report: dict[str, Any] = {}
    for window, entries in sorted(by_window.items()):
        present = sorted(e.table for e in entries if not e.is_empty)
        absent = sorted(e.table for e in entries if e.is_empty)
        clocks = [
            (table, column)
            for table, column, _symbol in AUDITED_TABLES
            if table in present
        ]
        report[window] = {
            "sources_present": present,
            "sources_absent": absent,
            "news_and_options_coexist": (
                "intraday_news_articles" in present
                and "intraday_option_chain_snapshots" in present
            ),
            "join_resolution_ns": alignment_resolution_ns(clocks) if clocks else None,
            "join_resolution_note": (
                "The coarsest clock in a join binds the whole join. Aligning a "
                "nanosecond book against a whole-second news stamp yields "
                "one-second alignment, not nanosecond."
            ),
        }
    return report


@dataclass(frozen=True, slots=True)
class EventSupply:
    """Outcome-blind event counts for one window."""

    window: str
    raw_events: int
    isolated_events: int
    with_l3_coverage: int
    with_option_observation: int
    with_all_sources: int
    distinct_symbols: int
    distinct_sessions: int
    by_hour_utc: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "raw_events": self.raw_events,
            "isolated_events": self.isolated_events,
            "with_l3_coverage": self.with_l3_coverage,
            "with_option_observation": self.with_option_observation,
            "with_all_sources": self.with_all_sources,
            "distinct_symbols": self.distinct_symbols,
            "distinct_sessions": self.distinct_sessions,
            "by_hour_utc": dict(sorted(self.by_hour_utc.items())),
        }


def event_supply_adequacy(supply: EventSupply) -> dict[str, Any]:
    """Whether a window holds enough events to evaluate any mechanism.

    The thresholds were declared in the plan before any count existed. This only
    compares against them.
    """
    usable = supply.with_l3_coverage
    return {
        "window": supply.window,
        "usable_events": usable,
        "distinct_sessions": supply.distinct_sessions,
        "min_events_required": MIN_EVENTS_FOR_MECHANISM,
        "min_sessions_required": MIN_SESSIONS_FOR_MECHANISM,
        "meets_event_floor": usable >= MIN_EVENTS_FOR_MECHANISM,
        "meets_session_floor": supply.distinct_sessions >= MIN_SESSIONS_FOR_MECHANISM,
        "adequate": usable >= MIN_EVENTS_FOR_MECHANISM
        and supply.distinct_sessions >= MIN_SESSIONS_FOR_MECHANISM,
    }


# ---------------------------------------------------------------------------
# Pure reasoning: the recommendation
# ---------------------------------------------------------------------------


def recommend(
    *,
    l3_state: dict[str, Any],
    options: dict[str, Any],
    stock_flow: dict[str, Any],
    supply: Sequence[dict[str, Any]],
    overlap: dict[str, Any],
) -> dict[str, Any]:
    """The single declared recommendation, derived from the measured facts.

    Ordered so that the binding constraint decides. Event supply is checked
    before mechanism richness because a mechanism nobody can evaluate is not a
    mechanism, however well the data describes it.
    """
    adequate = [entry for entry in supply if entry["adequate"]]
    ranked = _rank_mechanisms(
        l3_state=l3_state, options=options, stock_flow=stock_flow, overlap=overlap
    )

    if not l3_state["constructible_now"]:
        recommendation = RECOMMEND_NO_CAUSAL
        because = (
            "no dislocation state is constructible from the certified feature "
            "vocabulary"
        )
    elif not adequate:
        recommendation = RECOMMEND_NO_SUPPLY
        because = (
            "no window holds enough usable events to evaluate any mechanism at "
            f"the declared floor of {MIN_EVENTS_FOR_MECHANISM} events and "
            f"{MIN_SESSIONS_FOR_MECHANISM} sessions"
        )
    elif ranked and ranked[0]["mechanism"] == "l3_liquidity_vacuum_state":
        recommendation = RECOMMEND_IAG
        because = (
            "L3 state variables are constructible on a window that also holds "
            "adequate event supply, and require no new data"
        )
    elif ranked and ranked[0]["mechanism"] == "options_cross_market_state":
        recommendation = RECOMMEND_OPTIONS_STOCK
        because = "option-derived state is the highest-ranked feasible mechanism"
    else:
        recommendation = RECOMMEND_ACQUIRE
        because = "no currently held source supports a mechanism without new data"

    return {
        "recommendation": recommendation,
        "because": because,
        "ranked_mechanisms": ranked,
        "ranking_note": (
            "Ranked on data sufficiency alone -- what can be measured, on how "
            "many events, with which clocks. No economic outcome was computed "
            "for any of these, so the ranking says nothing about which is "
            "profitable."
        ),
        "windows_with_adequate_supply": [entry["window"] for entry in adequate],
    }


def _rank_mechanisms(
    *,
    l3_state: dict[str, Any],
    options: dict[str, Any],
    stock_flow: dict[str, Any],
    overlap: dict[str, Any],
) -> list[dict[str, Any]]:
    """Candidate mechanisms ordered by how well the data supports them."""
    candidates: list[dict[str, Any]] = []

    if l3_state["constructible_now"]:
        candidates.append(
            {
                "rank": 0,
                "mechanism": "l3_liquidity_vacuum_state",
                "feasible": True,
                "requires_new_data": False,
                "basis": sorted(l3_state["constructible_now"]),
                "known_gaps": sorted(l3_state["gaps"]),
                "limitation": (
                    "XNAS only, 8 symbols, 20 sessions. No cross-venue view, so "
                    "displayed depletion may reflect venue rotation rather than "
                    "genuine liquidity withdrawal."
                ),
            }
        )

    if options["verdict"] != OPTIONS_NOT_SUITABLE:
        candidates.append(
            {
                "rank": 0,
                "mechanism": "options_cross_market_state",
                "feasible": True,
                "requires_new_data": False,
                "basis": [v["name"] for v in options["state_variables_available"]],
                "known_gaps": options["blocking_for_signed_flow"],
                "limitation": (
                    "State only, never flow. No L3 data coexists with this "
                    "window, so it cannot be combined with book state."
                ),
            }
        )

    candidates.append(
        {
            "rank": 0,
            "mechanism": "market_wide_signed_flow",
            "feasible": bool(stock_flow["sufficient_for_market_wide_signed_flow"]),
            "requires_new_data": True,
            "basis": [],
            "known_gaps": [item["data"] for item in stock_flow["would_require"]],
            "limitation": (
                "Blocked. Raw prints are not persisted and quoted sizes are "
                "decertified."
            ),
        }
    )

    # Feasible before infeasible; no-new-data before needs-new-data; then by how
    # much of the concept space the source actually covers.
    candidates.sort(
        key=lambda c: (
            not c["feasible"],
            c["requires_new_data"],
            -len(c["basis"]),
        )
    )
    for index, candidate in enumerate(candidates, start=1):
        candidate["rank"] = index
    return candidates


# ---------------------------------------------------------------------------
# Measurement (database)
# ---------------------------------------------------------------------------


def _window_bounds(window: AuditWindow) -> tuple[str, str]:
    """Half-open [start, end) bounds covering whole days."""
    start = f"{window.start_date} 00:00:00+00"
    if window.end_date is None:
        return start, "9999-12-31 00:00:00+00"
    last = datetime.fromisoformat(window.end_date) + timedelta(days=1)
    return start, f"{last.date().isoformat()} 00:00:00+00"


def measure_table_coverage(
    cursor: Any, *, table: str, clock: str, symbol_column: str | None, window: AuditWindow
) -> TableCoverage:
    """Row count, span and breadth for one table inside one window.

    The clock is validated against the registry first, so a table can never be
    summarised along a column nobody has declared safe.
    """
    assert_decision_safe(table, clock)
    start, end = _window_bounds(window)
    symbol_expr = f"COUNT(DISTINCT {symbol_column})" if symbol_column else "0"
    filters = [f"{clock} >= %s", f"{clock} < %s"]
    params: list[Any] = [start, end]
    if symbol_column and window.symbols:
        filters.append(f"{symbol_column} = ANY(%s)")
        params.append(list(window.symbols))

    cursor.execute(
        f"""
        SELECT COUNT(*) AS rows,
               MIN({clock}) AS first_instant,
               MAX({clock}) AS last_instant,
               {symbol_expr} AS distinct_symbols,
               COUNT(DISTINCT ({clock} AT TIME ZONE 'UTC')::date) AS distinct_days
          FROM {table}
         WHERE {" AND ".join(filters)}
        """,
        params,
    )
    row = cursor.fetchone() or {}
    return TableCoverage(
        table=table,
        window=window.name,
        rows=int(row.get("rows") or 0),
        first_instant=_iso(row.get("first_instant")),
        last_instant=_iso(row.get("last_instant")),
        distinct_symbols=int(row.get("distinct_symbols") or 0),
        distinct_days=int(row.get("distinct_days") or 0),
    )


def measure_option_quality(cursor: Any, *, window: AuditWindow) -> OptionQuality:
    """Outcome-blind quality of option snapshots: presence, crossing, staleness.

    Staleness is observed_at minus quote_timestamp -- how old the quote already
    was when we saw it. A snapshot whose quote predates the poll by minutes is
    not a measurement of the market at poll time.

    Trade presence is counted on ``trade_timestamp`` rather than ``trade_price``.
    The two are equivalent as a presence signal, and counting the timestamp keeps
    every price column out of this audit's SQL entirely -- which is a property
    worth being able to assert mechanically rather than argue about.
    """
    start, end = _window_bounds(window)
    cursor.execute(
        """
        SELECT COUNT(*) AS rows,
               COUNT(bid_price) FILTER (WHERE ask_price IS NOT NULL) AS with_quote,
               COUNT(trade_timestamp) AS with_trade,
               COUNT(implied_volatility) AS with_iv,
               COUNT(delta) AS with_greeks,
               COUNT(open_interest) AS with_open_interest,
               COUNT(*) FILTER (
                   WHERE bid_price IS NOT NULL
                     AND ask_price IS NOT NULL
                     AND bid_price >= ask_price
               ) AS crossed_or_locked,
               MAX(EXTRACT(EPOCH FROM (observed_at - quote_timestamp))) AS max_stale,
               PERCENTILE_CONT(0.5) WITHIN GROUP (
                   ORDER BY EXTRACT(EPOCH FROM (observed_at - quote_timestamp))
               ) AS median_stale
          FROM intraday_option_chain_snapshots
         WHERE observed_at >= %s AND observed_at < %s
        """,
        [start, end],
    )
    row = cursor.fetchone() or {}
    return OptionQuality(
        window=window.name,
        rows=int(row.get("rows") or 0),
        rows_with_quote=int(row.get("with_quote") or 0),
        rows_with_trade=int(row.get("with_trade") or 0),
        rows_with_iv=int(row.get("with_iv") or 0),
        rows_with_greeks=int(row.get("with_greeks") or 0),
        rows_with_open_interest=int(row.get("with_open_interest") or 0),
        crossed_or_locked=int(row.get("crossed_or_locked") or 0),
        max_quote_staleness_seconds=_float(row.get("max_stale")),
        median_quote_staleness_seconds=_float(row.get("median_stale")),
    )


def measure_news_event_supply(
    cursor: Any,
    *,
    window: AuditWindow,
    quiet_minutes: int,
    l3_sessions: Sequence[str] = (),
    option_days: Sequence[str] = (),
) -> EventSupply:
    """Deterministic, outcome-blind event counts.

    Isolation uses the same quiet-period concept Stage 3.6 declared: a story
    counts as isolated when no earlier same-symbol story falls inside the quiet
    window. Eligibility is decided entirely from timestamps and coverage --
    nothing about what the price did afterwards enters it.
    """
    start, end = _window_bounds(window)
    filters = ["known_at >= %s", "known_at < %s"]
    params: list[Any] = [start, end]
    if window.symbols:
        filters.append("symbol = ANY(%s)")
        params.append(list(window.symbols))

    cursor.execute(
        f"""
        SELECT symbol,
               COALESCE(content_hash, article_id) AS story_id,
               MIN(known_at) AS known_at
          FROM intraday_news_articles
         WHERE {" AND ".join(filters)}
         GROUP BY symbol, COALESCE(content_hash, article_id)
         ORDER BY symbol, MIN(known_at)
        """,
        params,
    )
    rows = list(cursor.fetchall() or [])
    return summarise_event_supply(
        rows,
        window=window.name,
        quiet_minutes=quiet_minutes,
        l3_sessions=l3_sessions,
        option_days=option_days,
    )


def summarise_event_supply(
    rows: Iterable[dict[str, Any]],
    *,
    window: str,
    quiet_minutes: int,
    l3_sessions: Sequence[str] = (),
    option_days: Sequence[str] = (),
) -> EventSupply:
    """The counting rule itself, separated from the query that feeds it.

    Pure, so the isolation logic can be tested against constructed sequences
    rather than only against whatever the database happens to hold.
    """
    quiet = timedelta(minutes=quiet_minutes)
    sessions = set(l3_sessions)
    option_dates = set(option_days)

    last_seen: dict[str, datetime] = {}
    raw = isolated = with_l3 = with_options = with_all = 0
    symbols: set[str] = set()
    days: set[str] = set()
    by_hour: dict[str, int] = {}

    for row in rows:
        symbol = row["symbol"]
        moment = row["known_at"]
        if moment.tzinfo is None:
            raise ValueError(
                f"news known_at for {symbol} carries no timezone; Stage 4.0 "
                "will not assume one"
            )
        raw += 1
        symbols.add(symbol)
        day = moment.date().isoformat()
        days.add(day)
        hour = f"{moment.hour:02d}"
        by_hour[hour] = by_hour.get(hour, 0) + 1

        previous = last_seen.get(symbol)
        # The quiet period counts ALL previous same-symbol stories, including
        # ones that were themselves not isolated. A cluster of five stories
        # yields one isolated event, not five.
        is_isolated = previous is None or (moment - previous) >= quiet
        last_seen[symbol] = moment
        if not is_isolated:
            continue

        isolated += 1
        has_l3 = day in sessions
        has_options = day in option_dates
        if has_l3:
            with_l3 += 1
        if has_options:
            with_options += 1
        if has_l3 and has_options:
            with_all += 1

    return EventSupply(
        window=window,
        raw_events=raw,
        isolated_events=isolated,
        with_l3_coverage=with_l3,
        with_option_observation=with_options,
        with_all_sources=with_all,
        distinct_symbols=len(symbols),
        distinct_sessions=len(days),
        by_hour_utc=by_hour,
    )


def inventory_columns(cursor: Any, tables: Sequence[str]) -> dict[str, Any]:
    """The real column list for each table, straight from the catalogue.

    Read rather than transcribed: a hand-maintained schema list drifts from the
    database, and an audit that reports the drifted copy is worse than none.
    """
    cursor.execute(
        """
        SELECT table_name, column_name, data_type, is_nullable
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = ANY(%s)
         ORDER BY table_name, ordinal_position
        """,
        [list(tables)],
    )
    catalogue: dict[str, Any] = {name: {"present": False, "columns": []} for name in tables}
    for row in cursor.fetchall() or []:
        entry = catalogue.setdefault(row["table_name"], {"present": False, "columns": []})
        entry["present"] = True
        entry["columns"].append(
            {
                "name": row["column_name"],
                "type": row["data_type"],
                "nullable": row["is_nullable"] == "YES",
            }
        )
    return catalogue


def measure_field_certifications(cursor: Any) -> list[dict[str, Any]]:
    """Fields an earlier stage already retired, so this audit honours them."""
    cursor.execute(
        """
        SELECT table_name, field_name, certification, reason
          FROM research_dataset_field_certifications
         ORDER BY table_name, field_name
        """
    )
    return [dict(row) for row in (cursor.fetchall() or [])]


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def write_report(payload: dict[str, Any], path: Path) -> None:
    """Write one audit artifact, creating its directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def build_manifest(directory: Path, *, artifacts: Sequence[str]) -> dict[str, Any]:
    """Hash every artifact so the report is checkable after the fact."""
    from app.services.stage40_audit_plan import (
        EFFECTIVE_TRIALS_AFTER,
        EFFECTIVE_TRIALS_BEFORE,
        sha256_of,
    )

    files = []
    for name in sorted(artifacts):
        path = directory / name
        if not path.is_file():
            raise ValueError(f"audit artifact {name} was not written to {directory}")
        files.append(
            {"name": name, "sha256": sha256_of(path), "bytes": path.stat().st_size}
        )
    return {
        "stage40_plan_version": STAGE40_PLAN_VERSION,
        "stage40_audit_version": STAGE40_AUDIT_VERSION,
        "status": "outcome_blind_feasibility_audit",
        "contains_strategy_outcome": False,
        "contains_post_decision_return": False,
        "contains_pnl": False,
        "effective_trials_before": EFFECTIVE_TRIALS_BEFORE,
        "effective_trials_after": EFFECTIVE_TRIALS_AFTER,
        "authorizes_paper_or_live": False,
        "files": files,
    }
