"""Stage 3.5: execution-timing mechanism, measured against the certified book.

Every fill in this module uses the Stage-3 semantics unchanged -- 100 shares,
marketable, at most 10 displayed levels, walked on the book as it stood at
``ts_recv <= arrival``. That is deliberate: if the timing mechanism appears to
help, the difference must come from *when* the order was sent and not from a
fill model quietly rewritten in its favour.

## The two counterfactuals

For a required parent order, both of these are evaluated on the same book:

    baseline_arrival = decision + 250ms                     send now
    timed_send       = min(target_available_ts_recv,
                           decision + 750ms)                wait, briefly
    timed_arrival    = timed_send + 250ms                   -> at most decision + 1s

The trigger is the *arrival* of the frozen Stage-2 target event, not its
outcome. A participant can observe "the midpoint has moved" in real time; they
cannot observe what the move was worth. Nothing here reads a label return, and
the deadline guarantees the wait terminates whether or not the event ever comes.

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
from collections.abc import Iterable, Sequence
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
    FROZEN_CELLS,
    LATENCY_NS,
    MIN_COMPARABLE_PAIRS,
    MIN_SESSION_DATES,
    PLAN_DESIGN_HASH,
    STAGE35_PLAN_VERSION,
    T_HURDLE,
)

STAGE35_EXECUTOR_VERSION = "tier1_stage35_executor_v1"

EXPECTED_PLAN_DESIGN_HASH = (
    "ab0d42679cbedf6ac6b23706766ad16896e7d86413162b8f66e42cd3153c9fa7"
)
EXPECTED_CELL_HASH = (
    "bea300ba23327075909e37e36864feee6087dc85a5d55108cb53a615c7046f00"
)

TRADE_SIZE_SHARES = 100

BUY = 1
SELL = -1

# Why a paired observation could not be evaluated. Counted, never dropped.
NOT_COMPARABLE_NO_TARGET = "target_did_not_resolve_and_no_deadline_fallback"
NOT_COMPARABLE_NO_BASELINE_BOOK = "no_two_sided_book_at_baseline_arrival"
NOT_COMPARABLE_NO_TIMED_BOOK = "no_two_sided_book_at_timed_arrival"
NOT_COMPARABLE_BASELINE_LIQUIDITY = "baseline_leg_insufficient_displayed_liquidity"
NOT_COMPARABLE_TIMED_LIQUIDITY = "timed_leg_insufficient_displayed_liquidity"
NOT_COMPARABLE_UNCERTIFIABLE = "uncertifiable_timing_bad_ts_recv"
NOT_COMPARABLE_NO_DECISION_BOOK = "no_two_sided_book_at_decision"

# Asymmetric failures get their own names, because "only one leg filled" is
# exactly the case that would bias the result if it were silently discarded.
ASYMMETRIC_FAILURES = (
    NOT_COMPARABLE_BASELINE_LIQUIDITY,
    NOT_COMPARABLE_TIMED_LIQUIDITY,
    NOT_COMPARABLE_NO_BASELINE_BOOK,
    NOT_COMPARABLE_NO_TIMED_BOOK,
)

TRIGGER_TARGET = "target"
TRIGGER_DEADLINE = "deadline"


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

    Stage 3 only needed the confirmation block. Stage 3.5 wants prediction rows
    on all twenty dates, and leave-one-out inside discovery is the cheapest
    construction that keeps them all without any date training on itself.
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
    ordered = list(blocks["discovery"]) + list(blocks["validation"]) + list(
        blocks["confirmation"]
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
                f"{session_date} ({entry['block']}) would train on later dates: "
                f"{ahead}"
            )


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
    baseline_arrival_ts: int
    timed_send_ts: int
    timed_arrival_ts: int
    trigger: str
    # Per side: fill price, arrival midpoint, displayed size, levels walked.
    buy: dict[str, float] = field(default_factory=dict)
    sell: dict[str, float] = field(default_factory=dict)

    @property
    def predicted_up(self) -> bool:
        return self.predicted_bps > 0

    @property
    def delayed_side(self) -> int:
        """Predicted up delays the SELL; predicted down delays the BUY."""
        return SELL if self.predicted_up else BUY

    def savings_bps(self, side: int) -> float:
        """Positive means the timing decision improved this required order.

        The non-delayed side executes immediately under both policies, so its
        savings are exactly zero -- not approximately, and not a small number
        that happens to average out.
        """
        if side != self.delayed_side:
            return 0.0
        leg = self.buy if side == BUY else self.sell
        baseline, timed = leg["baseline_fill"], leg["timed_fill"]
        raw = (baseline - timed) if side == BUY else (timed - baseline)
        return raw / self.decision_midpoint * BPS

    def midpoint_benefit_bps(self, side: int) -> float:
        if side != self.delayed_side:
            return 0.0
        leg = self.buy if side == BUY else self.sell
        raw = (
            leg["baseline_midpoint"] - leg["timed_midpoint"]
            if side == BUY
            else leg["timed_midpoint"] - leg["baseline_midpoint"]
        )
        return raw / self.decision_midpoint * BPS

    def book_walk_benefit_bps(self, side: int) -> float:
        """What crossing the book cost, baseline minus timed.

        ``e`` is the cost in the direction that hurts, so a positive value means
        the delayed order crossed a cheaper book.
        """
        if side != self.delayed_side:
            return 0.0
        leg = self.buy if side == BUY else self.sell
        sign = 1.0 if side == BUY else -1.0
        baseline_cost = sign * (leg["baseline_fill"] - leg["baseline_midpoint"])
        timed_cost = sign * (leg["timed_fill"] - leg["timed_midpoint"])
        return (baseline_cost - timed_cost) / self.decision_midpoint * BPS

    @property
    def delayed_savings_bps(self) -> float:
        return self.savings_bps(self.delayed_side)

    @property
    def balanced_savings_bps(self) -> float:
        """The mean over the required BUY and the required SELL.

        Exactly one side delays, so this is always half the delayed side. It is
        computed as the mean anyway rather than by halving, so that the identity
        is a property of the arithmetic rather than an assumption baked into it.
        """
        return (self.savings_bps(BUY) + self.savings_bps(SELL)) / 2.0

    def dollar_savings_per_100(self, side: int) -> float:
        if side != self.delayed_side:
            return 0.0
        leg = self.buy if side == BUY else self.sell
        raw = (
            leg["baseline_fill"] - leg["timed_fill"]
            if side == BUY
            else leg["timed_fill"] - leg["baseline_fill"]
        )
        return raw * TRADE_SIZE_SHARES


def timed_send_instant(
    decision_ts: int, target_available_ts_recv: int | None
) -> tuple[int, str]:
    """When the delayed order is released, and what released it.

    The deadline is not a fallback bolted on afterwards; it is what makes the
    wait bounded and therefore executable. A desk cannot wait indefinitely for an
    event that may never arrive, and a study that let it would be measuring a
    policy nobody could run.
    """
    deadline = decision_ts + DELAY_DEADLINE_NS
    if target_available_ts_recv is None or target_available_ts_recv >= deadline:
        return deadline, TRIGGER_DEADLINE
    return int(target_available_ts_recv), TRIGGER_TARGET


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
    timing_certified=None,
    shares: int = TRADE_SIZE_SHARES,
) -> tuple[PairedExecution | None, str | None]:
    """Both required counterfactuals at one prediction, or a named refusal.

    A pair is comparable only when every leg it needs can be evaluated under the
    frozen rules. Returning a half-evaluated pair would select on execution
    difficulty, which is correlated with exactly the book states this mechanism
    claims to exploit.
    """
    baseline_arrival = decision_ts + LATENCY_NS
    timed_send, trigger = timed_send_instant(decision_ts, target_available_ts_recv)
    timed_arrival = timed_send + LATENCY_NS

    if timing_certified is not None and not timing_certified(
        decision_ts, timed_arrival
    ):
        return None, NOT_COMPARABLE_UNCERTIFIABLE

    decision_book = book_at(decision_ts)
    if decision_book is None or not decision_book.two_sided:
        return None, NOT_COMPARABLE_NO_DECISION_BOOK
    baseline_book = book_at(baseline_arrival)
    if baseline_book is None or not baseline_book.two_sided:
        return None, NOT_COMPARABLE_NO_BASELINE_BOOK
    timed_book = book_at(timed_arrival)
    if timed_book is None or not timed_book.two_sided:
        return None, NOT_COMPARABLE_NO_TIMED_BOOK

    legs: dict[int, dict[str, float]] = {}
    for side in (BUY, SELL):
        action = "buy" if side == BUY else "sell"
        baseline_fill = walk_book(baseline_book, action, shares)
        if baseline_fill is None:
            return None, NOT_COMPARABLE_BASELINE_LIQUIDITY
        timed_fill = walk_book(timed_book, action, shares)
        if timed_fill is None:
            return None, NOT_COMPARABLE_TIMED_LIQUIDITY
        legs[side] = {
            "baseline_fill": baseline_fill[0],
            "baseline_levels": float(baseline_fill[1]),
            "baseline_midpoint": float(baseline_book.midpoint),  # type: ignore[arg-type]
            "baseline_displayed": float(baseline_book.displayed(action)),
            "timed_fill": timed_fill[0],
            "timed_levels": float(timed_fill[1]),
            "timed_midpoint": float(timed_book.midpoint),  # type: ignore[arg-type]
            "timed_displayed": float(timed_book.displayed(action)),
        }

    return (
        PairedExecution(
            cell=cell,
            symbol=symbol,
            session_date=session_date,
            block=block,
            decision_ts=decision_ts,
            decision_midpoint=float(decision_book.midpoint),  # type: ignore[arg-type]
            predicted_bps=predicted_bps,
            baseline_arrival_ts=baseline_arrival,
            timed_send_ts=timed_send,
            timed_arrival_ts=timed_arrival,
            trigger=trigger,
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
    """Whatever the two policies differ by in fees, which should be nothing.

    The parent order executes under both policies, so every per-share charge is
    identical and cancels. Only a per-dollar charge could differ, and Section 31
    was $0.00 per million across the whole June-2025 window. This is computed
    rather than asserted, because an expectation is not a measurement.
    """
    rate = schedule["sec_section_31_usd_per_million_sold"]
    if not rate:
        return 0.0
    # The sale leg is the SELL side's fill under either policy.
    baseline_notional = pair.sell["baseline_fill"] * shares
    timed_notional = (
        pair.sell["timed_fill"] if pair.delayed_side == SELL else pair.sell["baseline_fill"]
    ) * shares
    return (baseline_notional - timed_notional) * rate / 1_000_000.0


# ---------------------------------------------------------------------------
# Accumulation
# ---------------------------------------------------------------------------


@dataclass
class CellTiming:
    """Everything measured for one frozen cell."""

    cell: str
    pairs: list[PairedExecution] = field(default_factory=list)
    not_comparable: dict[str, int] = field(default_factory=dict)

    def record_not_comparable(self, reason: str) -> None:
        self.not_comparable[reason] = self.not_comparable.get(reason, 0) + 1

    @property
    def asymmetric_failures(self) -> int:
        return sum(self.not_comparable.get(r, 0) for r in ASYMMETRIC_FAILURES)

    def summary(self, schedule: dict[str, Any] | None = None) -> dict[str, Any]:
        pairs = self.pairs
        base = {
            "cell": self.cell,
            "comparable_pairs": len(pairs),
            "not_comparable": dict(sorted(self.not_comparable.items())),
            "asymmetric_fill_failures": self.asymmetric_failures,
        }
        if not pairs:
            return {**base, "reached_screen": False, "reason": "no comparable pairs"}

        balanced = np.array([p.balanced_savings_bps for p in pairs])
        delayed = np.array([p.delayed_savings_bps for p in pairs])
        midpoint = np.array(
            [p.midpoint_benefit_bps(p.delayed_side) for p in pairs]
        )
        book = np.array([p.book_walk_benefit_bps(p.delayed_side) for p in pairs])
        dollars = np.array([p.dollar_savings_per_100(p.delayed_side) for p in pairs])

        by_date: dict[str, list[float]] = {}
        by_symbol: dict[str, list[float]] = {}
        buy_savings: list[float] = []
        sell_savings: list[float] = []
        for pair, value in zip(pairs, balanced, strict=True):
            by_date.setdefault(pair.session_date, []).append(float(value))
            by_symbol.setdefault(pair.symbol, []).append(float(value))
            if pair.delayed_side == BUY:
                buy_savings.append(pair.savings_bps(BUY))
            else:
                sell_savings.append(pair.savings_bps(SELL))

        date_means = {d: float(np.mean(v)) for d, v in sorted(by_date.items())}
        statistic, p_value = clustered_t(list(date_means.values()))
        reached = (
            len(pairs) >= MIN_COMPARABLE_PAIRS
            and len(date_means) >= MIN_SESSION_DATES
        )
        fee_difference = (
            float(
                np.mean(
                    [price_dependent_fee_difference_usd(p, schedule) for p in pairs]
                )
            )
            if schedule
            else None
        )

        return {
            **base,
            "reached_screen": reached,
            "session_dates": len(date_means),
            "balanced_parent_flow_savings_bps": float(balanced.mean()),
            "delayed_side_savings_bps": float(delayed.mean()),
            "midpoint_timing_benefit_bps": float(midpoint.mean()),
            "book_walk_benefit_bps": float(book.mean()),
            "dollar_savings_per_100_shares": float(dollars.mean()),
            "buy_savings_bps": float(np.mean(buy_savings)) if buy_savings else None,
            "sell_savings_bps": float(np.mean(sell_savings)) if sell_savings else None,
            "delayed_buy_pairs": len(buy_savings),
            "delayed_sell_pairs": len(sell_savings),
            "delayed_fraction": 1.0,  # exactly one side delays in every pair
            "target_triggered_delays": sum(
                1 for p in pairs if p.trigger == TRIGGER_TARGET
            ),
            "deadline_triggered_delays": sum(
                1 for p in pairs if p.trigger == TRIGGER_DEADLINE
            ),
            "mean_displayed_liquidity_shares": float(
                np.mean(
                    [
                        (p.buy if p.delayed_side == BUY else p.sell)["timed_displayed"]
                        for p in pairs
                    ]
                )
            ),
            "mean_levels_walked": float(
                np.mean(
                    [
                        (p.buy if p.delayed_side == BUY else p.sell)["timed_levels"]
                        for p in pairs
                    ]
                )
            ),
            "price_dependent_fee_difference_usd": fee_difference,
            "clustered_t": statistic,
            "p_value": p_value,
            "per_session_date_balanced_bps": date_means,
            "by_symbol_balanced_bps": {
                s: float(np.mean(v)) for s, v in sorted(by_symbol.items())
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
    passing = [
        row["cell"] for row in results if bh.get(row["cell"], {}).get("survives_bh")
    ]

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
