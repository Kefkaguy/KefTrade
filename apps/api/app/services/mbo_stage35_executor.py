"""Stage 3.5: execution-timing mechanism, measured against the certified book.

Every fill in this module uses the Stage-3 semantics unchanged -- 100 shares,
marketable, at most 10 displayed levels, walked on the book as it stood at
``ts_recv <= arrival``. That is deliberate: if the timing mechanism appears to
help, the difference must come from *when* the order was sent and not from a
fill model quietly rewritten in its favour.

## The two counterfactuals

For a required parent order, both of these are evaluated on the same book:

    baseline_arrival = decision + 250ms                     send now
    timed_send       = min(max(target_available_ts_recv,
                               decision), decision + 750ms) wait, briefly
    timed_arrival    = timed_send + 250ms                   -> at most decision + 1s

The clamp to ``decision`` is not cosmetic. A target whose availability
timestamp precedes the decision instant would otherwise produce a *delayed*
order sent before the prediction existed -- a policy nobody could run, quietly
scoring as though they had.

The trigger is the *arrival* of the frozen Stage-2 target event, not its
outcome. A participant can observe "the midpoint has moved" in real time; they
cannot observe what the move was worth. Nothing here reads a label return, and
the deadline guarantees the wait terminates whether or not the event ever comes.

## Only the delayed side needs a future fill

Exactly one parent side delays. The other executes at the baseline instant under
both policies, so its future liquidity is a market state the policy never
touches. Requiring it would exclude observations for a reason the mechanism does
not depend on -- and the excluded ones would not be a random sample, because
future thinness correlates with exactly the book dynamics being studied.

So the non-delayed side's "policy" execution literally *is* its baseline
execution: the same numbers, reused, which makes its savings exactly zero as an
arithmetic fact rather than as a special case.

## The decomposition identity

With ``e`` defined as the execution cost in the direction that hurts -- ``fill -
mid`` for a buy, ``mid - fill`` for a sell -- both sides satisfy

    savings = midpoint_timing_benefit + book_walk_benefit

exactly. That identity is asserted, not assumed, because it is the only thing
separating "the price moved our way" from "the book was cheaper to cross", and
those have very different implications for whether the mechanism is real.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from app.services.mbo_stage3_executor import (
    BPS,
    benjamini_hochberg,
    clustered_t,
    walk_book,
)
from app.services.mbo_stage35_plan import (
    BH_FALSE_DISCOVERY_RATE,
    CELL_HASH,
    DELAY_DEADLINE_NS,
    ELIGIBLE_LABEL_STATUSES,
    EXCLUDED_LABEL_STATUSES,
    FROZEN_CELLS,
    LATENCY_NS,
    MIN_COMPARABLE_PAIRS,
    MIN_SESSION_DATES,
    PLAN_DESIGN_HASH,
    STAGE35_PLAN_VERSION,
    T_HURDLE,
)

STAGE35_EXECUTOR_VERSION = "tier1_stage35_executor_v5"

EXPECTED_PLAN_DESIGN_HASH = (
    "097b5d65dfd49d9c648865df3b31c716b51b0c685c6e8b347c772a3b6992ba94"
)
EXPECTED_CELL_HASH = (
    "bea300ba23327075909e37e36864feee6087dc85a5d55108cb53a615c7046f00"
)

TRADE_SIZE_SHARES = 100

BUY = 1
SELL = -1

# Why a paired observation could not be evaluated. Counted, never dropped.
NOT_COMPARABLE_NO_BASELINE_BOOK = "no_two_sided_book_at_baseline_arrival"
NOT_COMPARABLE_NO_TIMED_BOOK = "no_two_sided_book_at_timed_arrival"
NOT_COMPARABLE_BASELINE_LIQUIDITY = "baseline_leg_insufficient_displayed_liquidity"
NOT_COMPARABLE_TIMED_LIQUIDITY = "delayed_leg_insufficient_displayed_liquidity"
NOT_COMPARABLE_UNCERTIFIABLE = "uncertifiable_timing_bad_ts_recv"
NOT_COMPARABLE_NO_DECISION_BOOK = "no_two_sided_book_at_decision"
NOT_COMPARABLE_OUTSIDE_COVERAGE = "arrival_outside_certified_market_data_coverage"
# An exact tie. Not a magnitude threshold: the model expressed no direction, so
# the policy expresses no timing preference.
ZERO_PREDICTION = "no_direction_zero_prediction"

ASYMMETRIC_FAILURES = (
    NOT_COMPARABLE_BASELINE_LIQUIDITY,
    NOT_COMPARABLE_TIMED_LIQUIDITY,
    NOT_COMPARABLE_NO_BASELINE_BOOK,
    NOT_COMPARABLE_NO_TIMED_BOOK,
)

# The complete frozen block. Reproduction must cover all of it, per cell.
EXPECTED_REPRODUCTION_COUNTS: dict[str, int] = {
    "discovery": 10,
    "validation": 6,
    "confirmation": 4,
}
EXPECTED_REPRODUCTION_TOTAL = sum(EXPECTED_REPRODUCTION_COUNTS.values())

TRIGGER_TARGET = "target"
TRIGGER_DEADLINE = "deadline"
TRIGGER_NONE = "none_zero_prediction"


def assert_frozen_plan() -> None:
    """Refuse to compute anything against a plan or a cell set that moved."""
    if CELL_HASH != EXPECTED_CELL_HASH:
        raise ValueError(
            "the frozen predictor set has changed; Stage 3.5 studies the four "
            "cells Stage 2 confirmed and no others"
        )
    if PLAN_DESIGN_HASH != EXPECTED_PLAN_DESIGN_HASH:
        raise ValueError(
            "the Stage-3.5 design hash has moved; a rule changed and that is a "
            "new mechanism specification, not a re-run"
        )


# ---------------------------------------------------------------------------
# Row-level out-of-sample chronology
# ---------------------------------------------------------------------------


def training_dates_for(
    session_date: str, blocks: dict[str, Sequence[str]]
) -> tuple[str, list[str]]:
    """Which dates may train the fit that predicts ``session_date``.

    Returns ``(block_name, training_dates)``. The rule is the same in every
    block -- never train on the date being predicted -- but the construction
    differs because the blocks differ in what "already happened" means:

    * discovery: leave-one-discovery-date-out over the other 9;
    * validation: the 10 discovery dates;
    * confirmation: the 16 discovery + validation dates.
    """
    discovery = list(blocks["discovery"])
    validation = list(blocks["validation"])
    confirmation = list(blocks["confirmation"])

    if session_date in discovery:
        return "discovery", [d for d in discovery if d != session_date]
    if session_date in validation:
        return "validation", discovery
    if session_date in confirmation:
        return "confirmation", discovery + validation
    raise ValueError(f"{session_date!r} is in none of the frozen blocks")


def chronology_map(blocks: dict[str, Sequence[str]]) -> dict[str, dict[str, Any]]:
    """The exact training set behind every date, recorded for the artefact."""
    ordered = (
        list(blocks["discovery"]) + list(blocks["validation"]) + list(blocks["confirmation"])
    )
    mapping: dict[str, dict[str, Any]] = {}
    for session_date in ordered:
        block, training = training_dates_for(session_date, blocks)
        mapping[session_date] = {
            "block": block,
            "training_dates": training,
            "training_date_count": len(training),
            "trains_on_itself": session_date in training,
        }
    return mapping


def assert_chronology_is_clean(mapping: dict[str, dict[str, Any]]) -> None:
    """No date may appear in its own training set, and none may see the future."""
    offenders = [d for d, entry in mapping.items() if entry["trains_on_itself"]]
    if offenders:
        raise ValueError(f"dates trained on themselves: {offenders}")
    for session_date, entry in mapping.items():
        ahead = [d for d in entry["training_dates"] if d > session_date]
        if entry["block"] != "discovery" and ahead:
            raise ValueError(
                f"{session_date} ({entry['block']}) would train on later dates: {ahead}"
            )


# ---------------------------------------------------------------------------
# Which rows may be executed
# ---------------------------------------------------------------------------


def execution_eligibility(status, finite):
    """Which rows the frozen policy may act on, and the status counts.

    ``ok`` resolves to a trigger instant. ``no_further_midpoint_change``
    resolves to nothing -- which is exactly the case the policy already covers,
    by sending at the deadline. Discarding those rows would make the plan's own
    unresolved-target rule unreachable, and would do it selectively: a midpoint
    that never moves again is a quiet period, so excluding them biases the study
    toward markets that move.

    ``source_midpoint_unavailable`` stays out, because without a decision
    midpoint there is nothing to measure savings against. A status this plan has
    not considered is a refusal, not a row to guess about.
    """
    unique = {str(v) for v in np.unique(status)}
    unknown = unique - set(ELIGIBLE_LABEL_STATUSES) - set(EXCLUDED_LABEL_STATUSES)
    if unknown:
        raise ValueError(
            f"unrecognised label statuses {sorted(unknown)}; Stage 3.5 will not "
            "silently admit or discard a status its plan has not considered"
        )
    eligible = np.zeros(len(status), dtype=bool)
    for value in ELIGIBLE_LABEL_STATUSES:
        eligible |= status == value
    counts = {value: int((status == value).sum()) for value in sorted(unique)}
    return eligible & finite, counts


# ---------------------------------------------------------------------------
# Certified market-data coverage
# ---------------------------------------------------------------------------


class CoverageTracker:
    """Records the receive-time span the certified file actually covers.

    ``BookReplay`` answers any pending instant past the last record by
    snapshotting the final book. That is correct for its own purpose and wrong
    for this one: a delayed order arriving after the stream ends would fill
    against a book that no longer exists, and the fill would look perfectly
    ordinary. So the span is tracked as events stream past, and any arrival
    outside it is refused rather than served from a stale final state.
    """

    def __init__(self) -> None:
        self.first_ts_recv: int | None = None
        self.last_ts_recv: int | None = None
        self.records = 0

    def wrap(self, events: Iterable[Any]) -> Iterator[Any]:
        for event in events:
            recv = event.ts_recv
            if self.first_ts_recv is None:
                self.first_ts_recv = recv
            self.last_ts_recv = recv
            self.records += 1
            yield event

    def covers(self, lo: int, hi: int) -> bool:
        if self.first_ts_recv is None or self.last_ts_recv is None:
            return False
        return self.first_ts_recv <= lo and hi <= self.last_ts_recv

    def as_dict(self) -> dict[str, Any]:
        return {
            "first_ts_recv": self.first_ts_recv,
            "last_ts_recv": self.last_ts_recv,
            "records": self.records,
        }


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


def timed_send_instant(
    decision_ts: int, target_available_ts_recv: int | None
) -> tuple[int, str]:
    """When the delayed order is released, and what released it.

    Clamped at both ends. The deadline bounds the wait, so the policy terminates
    whether or not the event arrives. The floor at ``decision_ts`` bounds it the
    other way: a target timestamp earlier than the decision would otherwise send
    a "delayed" order *before* the prediction was available, which is not a
    delay and not executable.
    """
    deadline = decision_ts + DELAY_DEADLINE_NS
    if target_available_ts_recv is None:
        return deadline, TRIGGER_DEADLINE
    clamped = min(max(int(target_available_ts_recv), decision_ts), deadline)
    trigger = TRIGGER_DEADLINE if clamped == deadline else TRIGGER_TARGET
    return clamped, trigger


# ---------------------------------------------------------------------------
# One paired observation
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PairedExecution:
    """A required BUY and a required SELL at one prediction instant."""

    cell: str
    symbol: str
    session_date: str
    block: str
    decision_ts: int
    decision_midpoint: float
    predicted_bps: float
    price_scale: float
    baseline_arrival_ts: int
    timed_send_ts: int
    timed_arrival_ts: int
    trigger: str
    delayed_side: int | None
    buy: dict[str, float] = field(default_factory=dict)
    sell: dict[str, float] = field(default_factory=dict)

    def leg(self, side: int) -> dict[str, float]:
        return self.buy if side == BUY else self.sell

    def savings_bps(self, side: int) -> float:
        """Positive means the timing decision improved this required order.

        The non-delayed side reuses its baseline fill verbatim, so this is
        exactly zero for it by arithmetic rather than by special case.
        """
        leg = self.leg(side)
        baseline, timed = leg["baseline_fill"], leg["timed_fill"]
        raw = (baseline - timed) if side == BUY else (timed - baseline)
        return raw / self.decision_midpoint * BPS

    def midpoint_benefit_bps(self, side: int) -> float:
        leg = self.leg(side)
        raw = (
            leg["baseline_midpoint"] - leg["timed_midpoint"]
            if side == BUY
            else leg["timed_midpoint"] - leg["baseline_midpoint"]
        )
        return raw / self.decision_midpoint * BPS

    def book_walk_benefit_bps(self, side: int) -> float:
        """What crossing the book cost, baseline minus timed."""
        leg = self.leg(side)
        sign = 1.0 if side == BUY else -1.0
        baseline_cost = sign * (leg["baseline_fill"] - leg["baseline_midpoint"])
        timed_cost = sign * (leg["timed_fill"] - leg["timed_midpoint"])
        return (baseline_cost - timed_cost) / self.decision_midpoint * BPS

    @property
    def delayed_savings_bps(self) -> float:
        if self.delayed_side is None:
            return 0.0
        return self.savings_bps(self.delayed_side)

    @property
    def balanced_savings_bps(self) -> float:
        """The mean over the required BUY and the required SELL.

        At most one side delays, so this is the delayed side halved -- computed
        as the mean anyway, so the relationship is a property of the arithmetic
        rather than an assumption baked into it.
        """
        return (self.savings_bps(BUY) + self.savings_bps(SELL)) / 2.0

    def dollar_savings_per_100(self, side: int) -> float:
        """Real dollars, not fixed-point units.

        ``walk_book`` returns prices in the MBO fixed-point scale, so the raw
        difference must be divided by ``price_scale`` before it means money.
        """
        leg = self.leg(side)
        raw = (
            leg["baseline_fill"] - leg["timed_fill"]
            if side == BUY
            else leg["timed_fill"] - leg["baseline_fill"]
        )
        return raw / self.price_scale * TRADE_SIZE_SHARES

    @property
    def delayed_dollar_savings(self) -> float:
        if self.delayed_side is None:
            return 0.0
        return self.dollar_savings_per_100(self.delayed_side)


def evaluate_pair(
    *,
    cell: str,
    symbol: str,
    session_date: str,
    block: str,
    predicted_bps: float,
    decision_ts: int,
    target_available_ts_recv: int | None,
    book_at,
    price_scale: float,
    timing_certified=None,
    within_coverage=None,
    shares: int = TRADE_SIZE_SHARES,
) -> tuple[PairedExecution | None, str | None]:
    """Both required counterfactuals at one prediction, or a named refusal.

    Baseline fills are required for **both** sides, because both parent orders
    exist. A timed fill is required only for the side the sign policy actually
    delays; the other reuses its baseline execution verbatim, since that is
    literally what it does under both policies.
    """
    baseline_arrival = decision_ts + LATENCY_NS

    # An exact tie expresses no direction, so the policy expresses no timing
    # preference and neither side delays.
    if predicted_bps == 0.0:
        delayed_side: int | None = None
        timed_send, trigger = decision_ts, TRIGGER_NONE
        timed_arrival = baseline_arrival
    else:
        delayed_side = SELL if predicted_bps > 0 else BUY
        timed_send, trigger = timed_send_instant(decision_ts, target_available_ts_recv)
        timed_arrival = timed_send + LATENCY_NS

    span_end = max(baseline_arrival, timed_arrival)
    if within_coverage is not None and not within_coverage(decision_ts, span_end):
        return None, NOT_COMPARABLE_OUTSIDE_COVERAGE
    if timing_certified is not None and not timing_certified(decision_ts, span_end):
        return None, NOT_COMPARABLE_UNCERTIFIABLE

    decision_book = book_at(decision_ts)
    if decision_book is None or not decision_book.two_sided:
        return None, NOT_COMPARABLE_NO_DECISION_BOOK
    baseline_book = book_at(baseline_arrival)
    if baseline_book is None or not baseline_book.two_sided:
        return None, NOT_COMPARABLE_NO_BASELINE_BOOK

    legs: dict[int, dict[str, float]] = {}
    for side in (BUY, SELL):
        action = "buy" if side == BUY else "sell"
        baseline_fill = walk_book(baseline_book, action, shares)
        if baseline_fill is None:
            return None, NOT_COMPARABLE_BASELINE_LIQUIDITY
        legs[side] = {
            "baseline_fill": baseline_fill[0],
            "baseline_levels": float(baseline_fill[1]),
            "baseline_midpoint": float(baseline_book.midpoint),  # type: ignore[arg-type]
            "baseline_displayed": float(baseline_book.displayed(action)),
            # Provisional: the non-delayed side keeps these verbatim.
            "timed_fill": baseline_fill[0],
            "timed_levels": float(baseline_fill[1]),
            "timed_midpoint": float(baseline_book.midpoint),  # type: ignore[arg-type]
            "timed_displayed": float(baseline_book.displayed(action)),
        }

    if delayed_side is not None:
        timed_book = book_at(timed_arrival)
        if timed_book is None or not timed_book.two_sided:
            return None, NOT_COMPARABLE_NO_TIMED_BOOK
        action = "buy" if delayed_side == BUY else "sell"
        timed_fill = walk_book(timed_book, action, shares)
        if timed_fill is None:
            return None, NOT_COMPARABLE_TIMED_LIQUIDITY
        legs[delayed_side].update(
            {
                "timed_fill": timed_fill[0],
                "timed_levels": float(timed_fill[1]),
                "timed_midpoint": float(timed_book.midpoint),  # type: ignore[arg-type]
                "timed_displayed": float(timed_book.displayed(action)),
            }
        )

    return (
        PairedExecution(
            cell=cell,
            symbol=symbol,
            session_date=session_date,
            block=block,
            decision_ts=decision_ts,
            decision_midpoint=float(decision_book.midpoint),  # type: ignore[arg-type]
            predicted_bps=predicted_bps,
            price_scale=float(price_scale),
            baseline_arrival_ts=baseline_arrival,
            timed_send_ts=timed_send,
            timed_arrival_ts=timed_arrival,
            trigger=trigger,
            delayed_side=delayed_side,
            buy=legs[BUY],
            sell=legs[SELL],
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------


def price_dependent_fee_difference_usd(
    pair: PairedExecution, schedule: dict[str, Any], *, shares: int = TRADE_SIZE_SHARES
) -> float:
    """Whatever the two policies differ by in fees, in real dollars.

    The parent order executes under both policies, so every per-share charge is
    identical and cancels. Only a per-dollar charge could differ, and Section 31
    was $0.00 per million across the whole June-2025 window. Computed rather
    than asserted, because an expectation is not a measurement -- and converted
    out of fixed point, because a notional in raw price units is not money.
    """
    rate = schedule["sec_section_31_usd_per_million_sold"]
    if not rate:
        return 0.0
    sell = pair.sell
    baseline_notional = sell["baseline_fill"] / pair.price_scale * shares
    timed_notional = sell["timed_fill"] / pair.price_scale * shares
    return (baseline_notional - timed_notional) * rate / 1_000_000.0


# ---------------------------------------------------------------------------
# Accumulation
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Running:
    """A sum and a count. Enough for every mean this study reports."""

    total: float = 0.0
    count: int = 0

    def add(self, value: float) -> None:
        self.total += float(value)
        self.count += 1

    @property
    def mean(self) -> float | None:
        return self.total / self.count if self.count else None


@dataclass
class CellTiming:
    """Everything measured for one frozen cell, accumulated in constant space.

    The diagnostic counted 11,534,742 eligible cell-rows. Retaining a
    ``PairedExecution`` per comparable observation -- each carrying two
    eight-entry dictionaries -- would have cost tens of gigabytes and bought
    nothing: every statistic this study reports is a mean, and a mean needs a
    sum and a count, not the observations.

    So each pair is folded in on arrival and discarded. The only state that
    grows with the data is the per-date and per-symbol bookkeeping, and that is
    bounded by 20 dates and 8 symbols.
    """

    cell: str
    price_scale: float = 1.0
    fee_schedule_name: str | None = None

    comparable_pairs: int = 0
    directional_pairs: int = 0
    zero_prediction_pairs: int = 0
    target_triggered: int = 0
    deadline_triggered: int = 0

    balanced: _Running = field(default_factory=_Running)
    delayed: _Running = field(default_factory=_Running)
    midpoint: _Running = field(default_factory=_Running)
    book_walk: _Running = field(default_factory=_Running)
    dollars: _Running = field(default_factory=_Running)
    buy_savings: _Running = field(default_factory=_Running)
    sell_savings: _Running = field(default_factory=_Running)
    displayed: _Running = field(default_factory=_Running)
    levels: _Running = field(default_factory=_Running)
    fees: _Running = field(default_factory=_Running)

    by_date: dict[str, _Running] = field(default_factory=dict)
    by_symbol: dict[str, _Running] = field(default_factory=dict)
    not_comparable: dict[str, int] = field(default_factory=dict)

    def record_not_comparable(self, reason: str) -> None:
        self.not_comparable[reason] = self.not_comparable.get(reason, 0) + 1

    def record_pair(
        self, pair: PairedExecution, schedule: dict[str, Any] | None = None
    ) -> None:
        """Fold one comparable pair into the running statistics and let it go.

        Every quantity below is exactly the one the batch implementation
        averaged; only the order of summation differs, and the parity tests
        pin that.
        """
        self.comparable_pairs += 1
        balanced = pair.balanced_savings_bps
        self.balanced.add(balanced)
        self.by_date.setdefault(pair.session_date, _Running()).add(balanced)
        self.by_symbol.setdefault(pair.symbol, _Running()).add(balanced)

        if schedule is not None:
            if self.fee_schedule_name is None:
                self.fee_schedule_name = schedule.get("name")
            self.fees.add(price_dependent_fee_difference_usd(pair, schedule))

        side = pair.delayed_side
        if side is None:
            self.zero_prediction_pairs += 1
            return

        self.directional_pairs += 1
        self.delayed.add(pair.savings_bps(side))
        self.midpoint.add(pair.midpoint_benefit_bps(side))
        self.book_walk.add(pair.book_walk_benefit_bps(side))
        self.dollars.add(pair.delayed_dollar_savings)
        (self.buy_savings if side == BUY else self.sell_savings).add(
            pair.savings_bps(side)
        )
        leg = pair.leg(side)
        self.displayed.add(leg["timed_displayed"])
        self.levels.add(leg["timed_levels"])
        if pair.trigger == TRIGGER_TARGET:
            self.target_triggered += 1
        elif pair.trigger == TRIGGER_DEADLINE:
            self.deadline_triggered += 1

    @property
    def asymmetric_failures(self) -> int:
        return sum(self.not_comparable.get(r, 0) for r in ASYMMETRIC_FAILURES)

    def summary(self) -> dict[str, Any]:
        base = {
            "cell": self.cell,
            "comparable_pairs": self.comparable_pairs,
            "not_comparable": dict(sorted(self.not_comparable.items())),
            "asymmetric_fill_failures": self.asymmetric_failures,
        }
        if not self.comparable_pairs:
            return {**base, "reached_screen": False, "reason": "no comparable pairs"}

        date_means = {
            date: running.total / running.count
            for date, running in sorted(self.by_date.items())
        }
        statistic, p_value = clustered_t(list(date_means.values()))
        reached = (
            self.comparable_pairs >= MIN_COMPARABLE_PAIRS
            and len(date_means) >= MIN_SESSION_DATES
        )
        total = self.comparable_pairs

        return {
            **base,
            "reached_screen": reached,
            "session_dates": len(date_means),
            "balanced_parent_flow_savings_bps": self.balanced.mean,
            "delayed_side_savings_bps": self.delayed.mean,
            "midpoint_timing_benefit_bps": self.midpoint.mean,
            "book_walk_benefit_bps": self.book_walk.mean,
            "dollar_savings_per_100_shares": self.dollars.mean,
            "buy_savings_bps": self.buy_savings.mean,
            "sell_savings_bps": self.sell_savings.mean,
            "delayed_buy_pairs": self.buy_savings.count,
            "delayed_sell_pairs": self.sell_savings.count,
            "zero_prediction_pairs": self.zero_prediction_pairs,
            # Every directional pair contains a delay, but it contains TWO parent
            # orders and only one of them delays. Calling that 1.0 would overstate
            # how much of the flow the mechanism actually touches.
            "pairs_with_a_delay_fraction": self.directional_pairs / total,
            "parent_orders_delayed_fraction": self.directional_pairs / (2 * total),
            "target_triggered_delays": self.target_triggered,
            "deadline_triggered_delays": self.deadline_triggered,
            "mean_displayed_liquidity_shares": self.displayed.mean,
            "mean_levels_walked": self.levels.mean,
            "price_dependent_fee_difference_usd": self.fees.mean,
            "fee_schedule": self.fee_schedule_name,
            "clustered_t": statistic,
            "p_value": p_value,
            "per_session_date_balanced_bps": date_means,
            "by_symbol_balanced_bps": {
                symbol: running.total / running.count
                for symbol, running in sorted(self.by_symbol.items())
            },
        }


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------


def assemble_report(
    cell_summaries: Iterable[dict[str, Any]], chronology: dict[str, Any]
) -> dict[str, Any]:
    """Apply the mechanism screen to the four cells, and to nothing else."""
    results = list(cell_summaries)
    p_values: dict[str, float | None] = {}
    for row in results:
        eligible = (
            row.get("reached_screen")
            and (row.get("balanced_parent_flow_savings_bps") or 0) > 0
            and (row.get("clustered_t") or 0) >= T_HURDLE
        )
        p_values[row["cell"]] = row.get("p_value") if eligible else None

    bh = benjamini_hochberg(p_values, fdr=BH_FALSE_DISCOVERY_RATE)
    passing = [row["cell"] for row in results if bh.get(row["cell"], {}).get("survives_bh")]

    return {
        "stage35_executor_version": STAGE35_EXECUTOR_VERSION,
        "stage35_plan_version": STAGE35_PLAN_VERSION,
        "stage35_plan_design_hash": PLAN_DESIGN_HASH,
        "cell_hash": CELL_HASH,
        "evidence_class": "exploratory mechanism development",
        "confirmatory": False,
        "chronology": chronology,
        "family": {
            "cells": list(FROZEN_CELLS),
            "size": len(FROZEN_CELLS),
            "benjamini_hochberg": bh,
            "t_hurdle": T_HURDLE,
            "minimum_session_dates": MIN_SESSION_DATES,
            "minimum_comparable_pairs": MIN_COMPARABLE_PAIRS,
        },
        "cells_passing_mechanism_screen": passing,
        "verdict": (
            "execution_timing_mechanism_supported_exploratory"
            if passing
            else "no_execution_timing_mechanism"
        ),
        "authorizes": (
            "an external, untouched execution-timing confirmation experiment"
            if passing
            else "nothing; close this mechanism and move to passive-fill or "
            "order-flow-toxicity research"
        ),
        "authorizes_paper_or_live": False,
        "reinterprets_stage3_verdict": False,
        "cells": results,
    }


def write_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, default=str, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-date fits, and proving they are Stage 2's own
# ---------------------------------------------------------------------------


def per_date_betas(
    per_date_grams: dict[str, Any],
    blocks: dict[str, Sequence[str]],
    alpha: float,
) -> dict[str, dict[str, Any]]:
    """One beta per evaluation date, from Stage 2's own ``fit``.

    Ridge is *not* reimplemented here. Stage 2's ``fit`` penalizes only the 59
    L3 columns and leaves the intercept and the ten price-only columns
    unpenalized; a local reimplementation would be a second definition of the
    model, and the two would eventually disagree.
    """
    from app.services.mbo_stage2_executor import DESIGN_WIDTH, fit, sum_grams

    betas: dict[str, dict[str, Any]] = {}
    ordered = (
        list(blocks["discovery"]) + list(blocks["validation"]) + list(blocks["confirmation"])
    )
    for session_date in ordered:
        block, training = training_dates_for(session_date, blocks)
        # The certified Stage-2 Gram batch is complete, so a missing training
        # Gram means the inputs are wrong. Shortening the training set instead
        # would silently fit a different model from the one Stage 2 fitted.
        missing = [d for d in training if d not in per_date_grams]
        if missing:
            raise ValueError(
                f"training Grams absent for {session_date} ({block}): {missing}. "
                "The declared training set must be present in full; Stage 3.5 "
                "will not shorten it."
            )
        train = sum_grams((per_date_grams[d] for d in training), DESIGN_WIDTH)
        beta = fit(train, alpha)
        if beta is None:
            raise ValueError(
                f"the training Gram for {session_date} ({block}) is singular; "
                "the frozen Stage-2 fit cannot be reproduced"
            )
        betas[session_date] = {
            "beta": beta,
            "block": block,
            "training_dates": list(training),
            "train_gram": train,
        }
    return betas


def reproduce_stage2_delta_r2(
    cell: str,
    per_date_grams: dict[str, Any],
    betas: dict[str, dict[str, Any]],
    alpha: float,
    recorded: dict[str, dict[str, float]],
    *,
    tolerance: float = 1e-9,
) -> dict[str, Any]:
    """Prove the Stage-3.5 chronology yields Stage 2's own out-of-sample numbers.

    Stage 2 recorded, per block, the per-date ``delta_R2`` it obtained from a
    training Gram built exactly this way. If the Stage-3.5 reconstruction
    reproduces those values on every date where Stage 2 recorded one, the models
    feeding this experiment are demonstrably the same out-of-sample models --
    not merely models built by a procedure described in the same words.

    Any mismatch is a refusal. A near-match is a mismatch: these are
    deterministic linear solves over identical sufficient statistics, so they
    agree to floating-point noise or they are not the same fit.
    """
    from app.services.mbo_stage2_executor import delta_r2

    checked: dict[str, dict[str, float]] = {}
    per_block: dict[str, int] = {"discovery": 0, "validation": 0, "confirmation": 0}
    mismatches: list[str] = []
    for session_date, entry in sorted(betas.items()):
        block = entry["block"]
        expected = (recorded.get(block) or {}).get(session_date)
        if expected is None:
            mismatches.append(f"{session_date} ({block}): Stage 2 recorded no value")
            continue
        if session_date not in per_date_grams:
            mismatches.append(f"{session_date} ({block}): no Gram to score against")
            continue
        rebuilt = delta_r2(entry["train_gram"], per_date_grams[session_date], alpha)
        if rebuilt is None:
            mismatches.append(f"{session_date} ({block}): could not be scored")
            continue
        checked[session_date] = {"stage2": float(expected), "stage35": float(rebuilt)}
        per_block[block] += 1
        if abs(rebuilt - expected) > tolerance:
            mismatches.append(
                f"{session_date} ({block}): {rebuilt} vs Stage-2 {expected}"
            )

    # Fail closed. Checking a subset and reporting success would let an
    # unverified chronology through on the strength of whichever dates happened
    # to line up.
    for block, expected_count in EXPECTED_REPRODUCTION_COUNTS.items():
        if per_block[block] != expected_count:
            mismatches.append(
                f"{block}: {per_block[block]} dates reproduced, expected {expected_count}"
            )
    if len(checked) != EXPECTED_REPRODUCTION_TOTAL:
        mismatches.append(
            f"total: {len(checked)} dates reproduced, expected "
            f"{EXPECTED_REPRODUCTION_TOTAL}"
        )

    if mismatches:
        raise ValueError(
            f"the Stage-3.5 per-date fits for {cell} do not reproduce Stage 2's "
            "recorded delta_R2, so they are not the same out-of-sample models:"
            + "".join(f"\n  - {m}" for m in mismatches)
        )

    return {
        "dates_checked": len(checked),
        "dates_checked_by_block": dict(per_block),
        "per_date": checked,
        "reproduction_verified": True,
        "tolerance": tolerance,
    }


def recorded_stage2_per_date(record: dict[str, Any], blocks: dict[str, Sequence[str]]):
    """Stage 2's per-date delta_R2, keyed by block and then by date.

    Stage 2 stored ``per_date_delta_r2`` as an ordered list alongside the dates
    it scored, so the reconstruction has to pair them back up in the same order
    Stage 2 used.
    """
    out: dict[str, dict[str, float]] = {}
    problems: list[str] = []
    for block in ("discovery", "validation", "confirmation"):
        entry = record.get(block) or {}
        values = entry.get("per_date_delta_r2")
        dates = list(blocks[block])
        if not values:
            problems.append(f"{block}: Stage 2 recorded no per-date values")
            continue
        if len(values) != len(dates):
            # Stage 2 stored these positionally. If the count does not match the
            # block, the identities are unrecoverable, and guessing at the
            # alignment would silently compare the wrong dates.
            problems.append(
                f"{block}: {len(values)} recorded values for {len(dates)} dates, "
                "so they cannot be associated unambiguously"
            )
            continue
        out[block] = {d: float(v) for d, v in zip(dates, values, strict=True)}

    if problems:
        raise ValueError(
            "Stage 2's recorded per-date delta_R2 cannot be mapped onto the "
            "complete frozen date block:" + "".join(f"\n  - {p}" for p in problems)
        )
    return out


def cell_prefix(horizon: str) -> str:
    from app.services.mbo_label_engine import HORIZONS_BY_NAME

    return HORIZONS_BY_NAME[horizon].prefix


def query_instants(
    decision_ts, availability, usable, predictions
) -> list[int]:
    """Every instant the paired evaluation will ask about, for one replay pass.

    The non-delayed side never queries a future instant, so its arrival is not
    collected -- the same asymmetry the comparability rule relies on.
    """
    instants: set[int] = set()
    for row in range(len(decision_ts)):
        if not usable[row]:
            continue
        decision = int(decision_ts[row])
        instants.add(decision)
        instants.add(decision + LATENCY_NS)
        if float(predictions[row]) == 0.0:
            continue
        send, _ = timed_send_instant(decision, availability[row])
        instants.add(send + LATENCY_NS)
    return sorted(instants)
