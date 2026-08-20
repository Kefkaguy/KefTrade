"""Stage 4.0: information-assimilation feasibility audit -- the frozen plan.

Stage 3.6 tested one economic specification and failed. This stage tests none.
It asks a prior question: is the data we already hold rich enough to *identify*
information-dislocation states at all? That is a question about inventory,
clocks and event supply, and none of those require a forward return to answer.

So this module declares what the audit is allowed to look at, what every
timestamp in the system actually means, and what each verdict would require --
all before a single row is read. The point is the same as in every prior stage:
a threshold chosen after seeing the number it judges is not a threshold.

Nothing here consumes an effective trial. The ledger stands at 531 before this
stage and at 531 after it, because no economic specification is being tested.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STAGE40_PLAN_VERSION = "tier1_stage40_information_assimilation_audit_v1"

# The ledger does not move. This stage exposes no economic outcome, so there is
# no multiple-comparisons cost to pay.
EFFECTIVE_TRIALS_BEFORE = 531
EFFECTIVE_TRIALS_AFTER = 531

REPORT_RELATIVE_DIR = Path("reports") / "tier1_stage40_audit" / "v1"
MANIFEST_FILENAME = "stage40_audit_manifest.json"

NANOS_PER_MICROSECOND = 1_000
NANOS_PER_SECOND = 1_000_000_000


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------
#
# The single most consequential fact this audit records: the two richest data
# sources do not overlap in time. L3 order-book data is a frozen June-2025
# Databento batch. Option chains are a *latest-snapshot* endpoint that began
# collecting in 2026 and, by its own documentation, "does not reconstruct old
# option surfaces for historical 2024/2025 decisions".
#
# Auditing them as one population would average a present source against an
# absent one and report something true of neither. They are audited as separate
# windows, with separate verdicts, and their overlap is measured rather than
# assumed.


@dataclass(frozen=True, slots=True)
class AuditWindow:
    """One coherent span of time with its own data availability."""

    name: str
    start_date: str
    end_date: str | None
    symbols: tuple[str, ...]
    rationale: str

    @property
    def is_open_ended(self) -> bool:
        return self.end_date is None


CERTIFIED_L3_SYMBOLS: tuple[str, ...] = (
    "AAPL",
    "AMD",
    "CMCSA",
    "CSCO",
    "INTC",
    "MSFT",
    "NVDA",
    "TSLA",
)

CERTIFIED_L3_WINDOW = AuditWindow(
    name="certified_l3_2025_06",
    start_date="2025-06-02",
    end_date="2025-06-30",
    symbols=CERTIFIED_L3_SYMBOLS,
    rationale=(
        "The frozen Stage-1 Databento XNAS.ITCH MBO batch: 8 symbols over 20 "
        "certified Nasdaq sessions. The only window in which L3 book state "
        "exists at all."
    ),
)

# Deliberately open-ended. The collector runs on a loop, so the end of this
# window is whatever the database says it is on the day the audit runs, and
# hard-coding a date here would silently truncate the measurement.
#
# Named "collection window" rather than "forward": the outcome filter strips any
# key containing "forward", and the original name collided with it, deleting
# this entire section from the emitted report. The window is a span of wall
# time, nothing to do with forward returns -- but the filter cannot know that,
# and weakening the filter to teach it would be the wrong trade. Renaming the
# innocent party is cheaper and leaves the prohibition intact.
OPTIONS_COLLECTION_WINDOW = AuditWindow(
    name="options_2026_collection_window",
    start_date="2026-08-14",
    end_date=None,
    symbols=(),
    rationale=(
        "The span over which option-chain snapshots and SIP quote snapshots "
        "have actually been collected. No L3 book data exists here, so any "
        "mechanism found in this window is a different mechanism from one "
        "found in the certified window -- not the same one re-tested."
    ),
)

AUDIT_WINDOWS: tuple[AuditWindow, ...] = (CERTIFIED_L3_WINDOW, OPTIONS_COLLECTION_WINDOW)


# The cadences whose feature files define certified L3 availability. These are
# exactly the cadences behind the four frozen Stage-2 cells, so "L3 data exists
# here" means the same thing it meant when those cells were fitted.
#
# Coverage is the INTERSECTION across them, not the union: an instant covered by
# one cadence and not the other is not a moment at which the certified book
# state was fully observable, and counting it would overstate supply.
CERTIFIED_L3_CADENCES: tuple[str, ...] = ("50ev", "200ev")

# Column carrying the certified receive instant inside each feature file.
L3_AVAILABILITY_COLUMN = "feature_available_ts_recv"


# ---------------------------------------------------------------------------
# Timestamp semantics
# ---------------------------------------------------------------------------
#
# Every clock in the system is declared here, with what it means and whether a
# decision at time T may read it. Anything not declared is refused rather than
# guessed: Stage 3.6 already lost a day to a timestamp that parsed without
# error and was quietly wrong by 570 nanoseconds, and the failure mode of an
# undeclared clock is worse -- it reads future information and nothing raises.

KIND_EVENT = "event"  # when the thing happened, per its own source
KIND_RECEIVE = "receive"  # when we received it
KIND_BACKFILL = "backfill"  # when a later backfill wrote the row
KIND_OPERATIONAL = "operational"  # bookkeeping; never a research clock
KIND_DERIVED = "derived"  # computed from another column


@dataclass(frozen=True, slots=True)
class TimestampSemantics:
    """What one timestamp column means, and whether research may use it."""

    table: str
    column: str
    kind: str
    resolution_ns: int
    timezone: str
    decision_safe: bool
    note: str


# Postgres TIMESTAMPTZ resolves to microseconds. A column storing an exchange
# instant therefore cannot carry nanoseconds regardless of what the source sent,
# which is a hard ceiling on cross-source alignment with nanosecond MBO data.
TIMESTAMP_REGISTRY: tuple[TimestampSemantics, ...] = (
    # --- news ---------------------------------------------------------------
    TimestampSemantics(
        table="intraday_news_articles",
        column="known_at",
        kind=KIND_EVENT,
        resolution_ns=NANOS_PER_SECOND,
        timezone="UTC",
        decision_safe=True,
        note=(
            "max(created_at, updated_at), so a decision cannot see a later "
            "revision of the same story. Publisher-stamped, and observed to be "
            "whole-second across the entire Stage-3.6 population -- this is the "
            "binding constraint on cross-source alignment, not the MBO clock."
        ),
    ),
    TimestampSemantics(
        table="intraday_news_articles",
        column="created_at",
        kind=KIND_EVENT,
        resolution_ns=NANOS_PER_SECOND,
        timezone="UTC",
        decision_safe=True,
        note="Publisher's first-publication stamp. Subsumed by known_at.",
    ),
    TimestampSemantics(
        table="intraday_news_articles",
        column="updated_at",
        kind=KIND_EVENT,
        resolution_ns=NANOS_PER_SECOND,
        timezone="UTC",
        decision_safe=True,
        note="Publisher's revision stamp. Subsumed by known_at.",
    ),
    TimestampSemantics(
        table="intraday_news_articles",
        column="received_at",
        kind=KIND_BACKFILL,
        resolution_ns=NANOS_PER_MICROSECOND,
        timezone="UTC",
        decision_safe=False,
        note=(
            "DEFAULT NOW() at ingest. These rows were backfilled in 2026, so "
            "this is a 2026 clock on a 2025 event. Using it as an event clock "
            "would place every historical story in the future."
        ),
    ),
    # --- options ------------------------------------------------------------
    TimestampSemantics(
        table="intraday_option_chain_snapshots",
        column="observed_at",
        kind=KIND_RECEIVE,
        resolution_ns=NANOS_PER_MICROSECOND,
        timezone="UTC",
        decision_safe=True,
        note=(
            "When we polled the chain endpoint. Safe as a decision clock "
            "because it is our own receive time, but it is a poll cadence, not "
            "an event time: nothing happened at observed_at."
        ),
    ),
    TimestampSemantics(
        table="intraday_option_chain_snapshots",
        column="quote_timestamp",
        kind=KIND_EVENT,
        resolution_ns=NANOS_PER_MICROSECOND,
        timezone="UTC",
        decision_safe=True,
        note=(
            "Exchange stamp on the latest quote in the snapshot. Nullable, and "
            "may be arbitrarily stale relative to observed_at -- staleness is "
            "measured by this audit rather than assumed away."
        ),
    ),
    TimestampSemantics(
        table="intraday_option_chain_snapshots",
        column="trade_timestamp",
        kind=KIND_EVENT,
        resolution_ns=NANOS_PER_MICROSECOND,
        timezone="UTC",
        decision_safe=True,
        note=(
            "Exchange stamp on the latest trade. One trade, not a tape: it "
            "cannot be sequenced into a flow series."
        ),
    ),
    TimestampSemantics(
        table="intraday_option_chain_snapshots",
        column="created_at",
        kind=KIND_OPERATIONAL,
        resolution_ns=NANOS_PER_MICROSECOND,
        timezone="UTC",
        decision_safe=False,
        note="Row-write bookkeeping. Never a research clock.",
    ),
    # --- consolidated quotes ------------------------------------------------
    TimestampSemantics(
        table="intraday_quote_snapshots",
        column="timestamp",
        kind=KIND_EVENT,
        resolution_ns=NANOS_PER_MICROSECOND,
        timezone="UTC",
        decision_safe=True,
        note="SIP NBBO update instant, stored at microsecond resolution.",
    ),
    TimestampSemantics(
        table="intraday_quote_snapshots",
        column="timestamp_ns",
        kind=KIND_DERIVED,
        resolution_ns=NANOS_PER_MICROSECOND,
        timezone="UTC",
        decision_safe=True,
        note=(
            "Named nanoseconds but NOT nanosecond-resolved for historical rows: "
            "migration 080 derived it as the stored microsecond value scaled by "
            "1000. Its true resolution is microseconds, and treating it as a "
            "nanosecond clock would fabricate precision the rows never had."
        ),
    ),
    # --- aggregated stock flow ---------------------------------------------
    TimestampSemantics(
        table="intraday_trade_flow_features",
        column="timestamp",
        kind=KIND_EVENT,
        resolution_ns=15 * 60 * NANOS_PER_SECOND,
        timezone="UTC",
        decision_safe=True,
        note=(
            "The left edge of a 15m or 30m aggregation bucket. The underlying "
            "trade prints are not persisted, so this is the finest signed-flow "
            "clock that exists -- three orders of magnitude coarser than the "
            "dislocation states this stage is scoping."
        ),
    ),
    TimestampSemantics(
        table="candles",
        column="timestamp",
        kind=KIND_EVENT,
        resolution_ns=60 * NANOS_PER_SECOND,
        timezone="UTC",
        decision_safe=True,
        note="Bar left edge. OHLCV only; no VWAP and no trade count.",
    ),
    # --- L3 -----------------------------------------------------------------
    TimestampSemantics(
        table="mbo_features_parquet",
        column="feature_available_ts_recv",
        kind=KIND_RECEIVE,
        resolution_ns=1,
        timezone="UTC",
        decision_safe=True,
        note=(
            "Certified nanosecond receive time of the last event in the "
            "feature window. Stage 3.5/3.6's decision clock."
        ),
    ),
    TimestampSemantics(
        table="mbo_features_parquet",
        column="ts_event",
        kind=KIND_EVENT,
        resolution_ns=1,
        timezone="UTC",
        decision_safe=True,
        note="Nanosecond exchange event time from the XNAS.ITCH feed.",
    ),
)

TIMESTAMP_INDEX: dict[tuple[str, str], TimestampSemantics] = {
    (entry.table, entry.column): entry for entry in TIMESTAMP_REGISTRY
}


# ---------------------------------------------------------------------------
# Option field semantics
# ---------------------------------------------------------------------------
#
# Two fields in this table are actively misleading under their own names, and
# both would read as order flow to anyone who did not check. They are declared
# here so the audit reports what they are rather than what they are called.


@dataclass(frozen=True, slots=True)
class FieldSemantics:
    """What one stored column actually contains."""

    column: str
    present: bool
    interpretation: str
    usable_as_flow: bool


OPTION_FIELD_SEMANTICS: tuple[FieldSemantics, ...] = (
    FieldSemantics("bid_price", True, "Latest quote bid from the chain snapshot.", False),
    FieldSemantics("ask_price", True, "Latest quote ask from the chain snapshot.", False),
    FieldSemantics("bid_size", True, "Size at the latest quote bid.", False),
    FieldSemantics("ask_size", True, "Size at the latest quote ask.", False),
    FieldSemantics("strike_price", True, "Contract strike, parsed from the OCC symbol.", False),
    FieldSemantics("expiration_date", True, "Contract expiry, parsed from the OCC symbol.", False),
    FieldSemantics("option_type", True, "call or put, parsed from the OCC symbol.", False),
    FieldSemantics("implied_volatility", True, "Provider-computed IV for the contract.", False),
    FieldSemantics("delta", True, "Provider-computed greek.", False),
    FieldSemantics("gamma", True, "Provider-computed greek.", False),
    FieldSemantics("theta", True, "Provider-computed greek.", False),
    FieldSemantics("vega", True, "Provider-computed greek.", False),
    FieldSemantics("rho", True, "Provider-computed greek.", False),
    FieldSemantics("open_interest", True, "Contract open interest as of the snapshot.", False),
    FieldSemantics(
        "trade_price",
        True,
        "Price of the single most recent trade at snapshot time. Not a tape.",
        False,
    ),
    FieldSemantics(
        "trade_size",
        True,
        "Size of the single most recent trade -- NOT cumulative volume. It does "
        "not accumulate between snapshots, and two polls taken minutes apart "
        "report the same value if no new trade occurred.",
        False,
    ),
    # Absent columns are declared explicitly. An audit that lists only what is
    # present cannot answer "can we do X", which is the entire question here.
    FieldSemantics(
        "volume",
        False,
        "No volume column exists. option_call_volume / option_put_volume in the "
        "feature vocabulary are sums of trade_size across contracts, which is a "
        "sum of last-trade sizes and not a volume.",
        False,
    ),
    FieldSemantics(
        "underlying_price",
        False,
        "Not stored. _surface_features falls back to the median listed strike as "
        "its ATM anchor, so moneyness, put-call parity and synthetic-forward "
        "deviations cannot be computed from this table alone.",
        False,
    ),
    FieldSemantics(
        "exchange",
        False,
        "No venue identifier on quotes or trades.",
        False,
    ),
    FieldSemantics(
        "trade_conditions",
        False,
        "No condition codes, so trades cannot be filtered for eligibility.",
        False,
    ),
    FieldSemantics(
        "trade_sequence",
        False,
        "Only a latest-trade snapshot exists. Without a sequence there is no "
        "series to sign, so no trade-classification rule can be applied.",
        False,
    ),
    FieldSemantics(
        "is_nbbo",
        False,
        "Nothing records whether the quote is a consolidated NBBO or one venue's "
        "book, so quoted sizes cannot be interpreted as consolidated depth.",
        False,
    ),
)

# The misnamed feature names, recorded so the audit can name them directly.
MISNAMED_OPTION_FEATURES: tuple[str, ...] = (
    "option_call_volume",
    "option_put_volume",
    "option_put_call_volume_ratio",
)


# ---------------------------------------------------------------------------
# L3 state variables
# ---------------------------------------------------------------------------
#
# The audit's job here is to say which of the requested dislocation states are
# already constructible from the certified 59-feature vocabulary and which are
# not. Mapping a requested concept onto features that exist is a data fact;
# whether the state predicts anything is not asked and not answered.

L3_STATE_COVERAGE: dict[str, tuple[str, ...]] = {
    "depth_consumed": ("execution_volume", "queue_depletion_events", "mean_touch_depth"),
    "replenishment_after_consumption": (
        "touch_replenishment_volume",
        "touch_replenishment_events",
        "refill_after_execution_volume",
    ),
    "cancellation_addition_imbalance": (
        "add_count",
        "add_volume",
        "cancel_count",
        "cancel_volume",
        "cancel_add_ratio",
        "cancel_volume_ratio",
    ),
    "persistent_aggressive_direction": (
        "buy_aggressor_volume",
        "sell_aggressor_volume",
        "signed_trade_volume",
        "signed_trade_volume_z",
        "aggressor_imbalance",
    ),
    "spread_depth_stress": (
        "spread",
        "spread_bps",
        "spread_bps_z",
        "bid_depth_5",
        "ask_depth_5",
        "bid_depth_10",
        "ask_depth_10",
    ),
    "liquidity_vacuum_state": (
        "queue_depletion_events",
        "depletion_followed_by_quote_move",
        "queue_persistence",
        "bid_levels",
        "ask_levels",
    ),
    "absorption": (
        "absorption_ratio",
        "executions_without_price_move",
        "execution_volume_without_price_move",
    ),
    "event_intensity": ("execution_intensity", "trade_count", "trade_volume", "resting_orders"),
}

# The one requested state with no existing feature behind it. Constructing it is
# possible but has a sharp edge: the outcome-blind form regresses the midpoint
# change on signed flow *within one closed window*, and the forbidden form uses
# the midpoint change *after* it. They differ by one index and by everything
# that matters, so this stage records the gap and declines to build it silently.
L3_STATE_GAPS: dict[str, str] = {
    "local_lambda_price_sensitivity": (
        "No feature measures price change per unit signed flow. It is "
        "constructible from execution_volume, signed_trade_volume and midpoint "
        "within a single closed window, but only under an explicit declaration "
        "that the window is closed: the same computation reaching one event "
        "past the window is a forward return."
    ),
}


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

OPTIONS_SIGNED_FLOW = "signed_options_flow_available"
OPTIONS_STATE_ONLY = "options_cross_market_state_only"
OPTIONS_NOT_SUITABLE = "options_data_not_suitable"
OPTIONS_VERDICTS = (OPTIONS_SIGNED_FLOW, OPTIONS_STATE_ONLY, OPTIONS_NOT_SUITABLE)

RECOMMEND_IAG = "proceed_to_IAG_design"
RECOMMEND_OPTIONS_STOCK = "proceed_to_options_stock_design"
RECOMMEND_ACQUIRE = "acquire_missing_data_first"
RECOMMEND_NO_SUPPLY = "insufficient_event_supply"
RECOMMEND_NO_CAUSAL = "insufficient_causal_information"
RECOMMENDATIONS = (
    RECOMMEND_IAG,
    RECOMMEND_OPTIONS_STOCK,
    RECOMMEND_ACQUIRE,
    RECOMMEND_NO_SUPPLY,
    RECOMMEND_NO_CAUSAL,
)

# Below this many usable events a mechanism cannot be evaluated at all, whatever
# its economics. Declared here, before any count is read, so the number cannot be
# chosen to make a population look adequate. Matches the Stage-3.6 sample gate.
MIN_EVENTS_FOR_MECHANISM = 100
MIN_SESSIONS_FOR_MECHANISM = 15


# ---------------------------------------------------------------------------
# Outcome blindness
# ---------------------------------------------------------------------------
#
# Same defence as Stage 3.5 and 3.6: a filter over the emitted payload, plus
# tests that the audit never computes the quantities in the first place. The
# filter is the second line, not the first.

OUTCOME_BEARING_TOKENS: tuple[str, ...] = (
    "return",
    "pnl",
    "p_and_l",
    "profit",
    "bps_result",
    "expectancy",
    "sharpe",
    "alpha",
    "edge",
    "verdict_bps",
    "net_bps",
    "gross_bps",
    "win_rate",
    "hit_rate",
    "forward",
    "future",
    "holding_period",
    "t_stat",
    "clustered_t",
)

# Words that legitimately appear in an audit about *data* and must survive the
# filter: refusing them would strip the report of its own subject matter.
OUTCOME_TOKEN_EXEMPTIONS: tuple[str, ...] = (
    "returned_rows",
    "future_data_acquisition",
    # The governance flags assert the absence of these quantities. Stripping
    # them would delete the report's own proof that it is outcome-blind.
    "contains_pnl",
    "contains_post_decision_return",
    "contains_strategy_outcome",
)


def sha256_of(path: Path) -> str:
    """The hash of a file's bytes, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def timestamp_semantics(table: str, column: str) -> TimestampSemantics:
    """The declared meaning of one clock, or a refusal.

    Fail closed. An undeclared timestamp is not assumed safe: the whole point of
    the registry is that a clock nobody has reasoned about is exactly the one
    that leaks future information without raising.
    """
    entry = TIMESTAMP_INDEX.get((table, column))
    if entry is None:
        raise ValueError(
            f"{table}.{column} has no declared timestamp semantics. Stage 4.0 "
            "will not infer a clock: declare what it means, at what resolution, "
            "and whether a decision may read it."
        )
    return entry


def assert_decision_safe(table: str, column: str) -> TimestampSemantics:
    """Refuse a clock that a decision at time T may not read."""
    entry = timestamp_semantics(table, column)
    if not entry.decision_safe:
        raise ValueError(
            f"{table}.{column} is a {entry.kind} clock and is not safe at "
            f"decision time: {entry.note}"
        )
    return entry


def alignment_resolution_ns(tables_and_columns: list[tuple[str, str]]) -> int:
    """The coarsest clock in a join -- which is the one that binds.

    Aligning a nanosecond book against a whole-second news stamp does not give
    nanosecond alignment. It gives one second, and reporting otherwise would
    overstate how tightly a dislocation window can be drawn.
    """
    if not tables_and_columns:
        raise ValueError("no clocks given; alignment resolution is undefined")
    return max(
        assert_decision_safe(table, column).resolution_ns
        for table, column in tables_and_columns
    )


@dataclass(frozen=True, slots=True)
class PlanIdentity:
    """What this audit is, for the manifest."""

    version: str = STAGE40_PLAN_VERSION
    effective_trials_before: int = EFFECTIVE_TRIALS_BEFORE
    effective_trials_after: int = EFFECTIVE_TRIALS_AFTER
    contains_strategy_outcome: bool = False
    contains_post_decision_return: bool = False
    contains_pnl: bool = False
    windows: tuple[str, ...] = field(
        default_factory=lambda: tuple(w.name for w in AUDIT_WINDOWS)
    )


def statistical_plan() -> dict[str, Any]:
    """The declared plan, emitted before anything is measured."""
    identity = PlanIdentity()
    return {
        "stage40_plan_version": identity.version,
        "purpose": (
            "Determine whether existing data can identify information-"
            "dislocation states. No economic specification is tested."
        ),
        "contains_strategy_outcome": identity.contains_strategy_outcome,
        "contains_post_decision_return": identity.contains_post_decision_return,
        "contains_pnl": identity.contains_pnl,
        "effective_trials_before": identity.effective_trials_before,
        "effective_trials_after": identity.effective_trials_after,
        "why_no_trial_consumed": (
            "A trial is spent when an economic specification is exposed to an "
            "outcome. This stage measures inventory, clocks and event supply; "
            "there is no specification and no outcome to expose."
        ),
        "windows": [
            {
                "name": window.name,
                "start_date": window.start_date,
                "end_date": window.end_date,
                "symbols": list(window.symbols),
                "rationale": window.rationale,
            }
            for window in AUDIT_WINDOWS
        ],
        "timestamp_registry": [
            {
                "table": entry.table,
                "column": entry.column,
                "kind": entry.kind,
                "resolution_ns": entry.resolution_ns,
                "timezone": entry.timezone,
                "decision_safe": entry.decision_safe,
                "note": entry.note,
            }
            for entry in TIMESTAMP_REGISTRY
        ],
        "options_verdicts_possible": list(OPTIONS_VERDICTS),
        "recommendations_possible": list(RECOMMENDATIONS),
        "min_events_for_mechanism": MIN_EVENTS_FOR_MECHANISM,
        "min_sessions_for_mechanism": MIN_SESSIONS_FOR_MECHANISM,
        "l3_state_coverage": {k: list(v) for k, v in L3_STATE_COVERAGE.items()},
        "l3_state_gaps": dict(L3_STATE_GAPS),
    }
