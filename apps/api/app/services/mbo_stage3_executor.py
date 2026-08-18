"""Stage 3: executable economics against the certified MBO stream.

The whole point of this module is that a prediction and a fill are separated by
time, and that time costs money. Three instants govern every trade and they are
never allowed to collapse into one another:

    t_decision  = feature_available_ts_recv     (nothing may be decided earlier)
    t_arrival   = t_decision + latency          (the order reaches the book)
    t_exit      = t_decision + horizon + latency (the flat-out order arrives)

Between decision and arrival the market moves without you. That drift is
adverse selection and it is reported on its own line, not buried in slippage --
if the edge is gone by the time you can act, the honest report says the edge was
gone by the time you could act.

## The fill model

Marketable on both legs, walking the resting book that exists **at arrival**:
buy consumes ask levels upward, sell consumes bid levels downward, level by
level, until the size is filled or ``MAX_BOOK_LEVELS_WALKED`` is exhausted. A
trade that cannot fill is recorded as unfilled and contributes to no return. It
is never assumed to have filled somewhere worse, and never dropped silently.

Displayed liquidity only. There is no hidden-liquidity model here, and inventing
one that helps would be exactly the sort of assumption this stage exists to
avoid. Under-counting available size is conservative; over-counting is not.

## What is not here

No fitting. The coefficients arrive frozen from Stage 2 and are applied. No
threshold is searched. No signal is constructed. The rules come from
``mbo_stage3_plan`` and this module refuses to run if that plan's design hash
has moved.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from math import sqrt
from pathlib import Path
from typing import Any

import numpy as np
from app.services.mbo_stage2_executor import _student_t_sf, benjamini_hochberg
from app.services.mbo_stage2_plan import PLAN_DESIGN_HASH as STAGE2_PLAN_DESIGN_HASH
from app.services.mbo_stage3_plan import (
    ECONOMIC_GATES,
    F_BAD_TS_RECV,
    FEE_SCHEDULES,
    FROZEN_SURVIVORS,
    MAX_BOOK_LEVELS_WALKED,
    PLAN_DESIGN_HASH,
    PRIMARY_FEE_SCHEDULE_NAME,
    PRIMARY_LATENCY,
    PRIMARY_RULE,
    SECONDARY_RULE,
    STAGE3_PLAN_VERSION,
    SURVIVOR_HASH,
    TRADE_SIZE_SHARES,
)

STAGE3_EXECUTOR_VERSION = "tier1_stage3_executor_v2"

EXPECTED_PLAN_DESIGN_HASH = (
    "874292555a9e136294f36c45a69c402a8448213652cdf9a1aa867638b5529ff3"
)
EXPECTED_SURVIVOR_HASH = (
    "bea300ba23327075909e37e36864feee6087dc85a5d55108cb53a615c7046f00"
)

BPS = 10_000.0

# Reasons a candidate produced no trade. Counted, never silently dropped.
NO_TRADE_BELOW_HURDLE = "below_cost_hurdle"
NO_TRADE_NO_BOOK = "no_two_sided_book_at_arrival"
NO_TRADE_NO_LIQUIDITY = "insufficient_displayed_liquidity"
NO_TRADE_NO_EXIT = "no_two_sided_book_at_exit"
NO_TRADE_SESSION_END = "horizon_beyond_session_end"
# The target event resolved before we could even arrive. Not a loss, not a
# trade: a missed opportunity, and it must be visible as one.
NO_TRADE_RESOLVED_BEFORE_ENTRY = "horizon_resolved_before_entry"
# A flagged receive timestamp anywhere in the timing window. Excluded rather
# than repaired.
NO_TRADE_UNCERTIFIABLE_TIMING = "uncertifiable_timing_bad_ts_recv"
NO_TRADE_UNRESOLVED_TARGET = "stage2_target_did_not_resolve"


# ---------------------------------------------------------------------------
# Book snapshots taken at an instant
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BookLevels:
    """Resting displayed liquidity at one instant, best price first.

    ``bids`` descend in price, ``asks`` ascend. Each entry is ``(price, size)``
    in fixed-point price units and shares.
    """

    ts: int
    bids: tuple[tuple[int, int], ...]
    asks: tuple[tuple[int, int], ...]

    @property
    def best_bid(self) -> int | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> int | None:
        return self.asks[0][0] if self.asks else None

    @property
    def midpoint(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0][0] + self.asks[0][0]) / 2.0

    @property
    def two_sided(self) -> bool:
        return bool(self.bids) and bool(self.asks)

    def displayed(self, side: str) -> int:
        levels = self.asks if side == "buy" else self.bids
        return sum(size for _, size in levels[:MAX_BOOK_LEVELS_WALKED])


def walk_book(book: BookLevels, side: str, shares: int) -> tuple[float, int] | None:
    """Volume-weighted fill price for a marketable order, or ``None``.

    ``side`` is the direction of *our* order: "buy" consumes asks upward, "sell"
    consumes bids downward. Returns ``(vwap_price, levels_consumed)``. Returns
    ``None`` when the displayed book cannot fill the whole order inside the
    frozen level budget -- a partial fill is not silently treated as a fill.
    """
    levels = book.asks if side == "buy" else book.bids
    remaining = shares
    notional = 0.0
    consumed = 0
    for price, size in levels[:MAX_BOOK_LEVELS_WALKED]:
        if remaining <= 0:
            break
        take = min(remaining, size)
        notional += price * take
        remaining -= take
        consumed += 1
    if remaining > 0:
        return None
    return notional / shares, consumed


# ---------------------------------------------------------------------------
# One evaluated candidate
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Trade:
    symbol: str
    session_date: str
    decision_ts: int
    arrival_ts: int
    exit_ts: int
    # The frozen Stage-2 event-horizon resolution this exit is anchored to.
    exit_resolution_ts: int
    direction: int  # +1 long, -1 short
    predicted_bps: float
    decision_midpoint: float
    arrival_midpoint: float
    entry_price: float
    exit_price: float
    shares: int
    levels_consumed_entry: int
    levels_consumed_exit: int
    displayed_entry: int
    displayed_exit: int
    # Set immediately after construction, once the exit book is known.
    exit_midpoint: float = 0.0

    @property
    def holding_ns(self) -> int:
        return self.exit_ts - self.arrival_ts

    @property
    def realized_lag_ns(self) -> int:
        """How long the frozen Stage-2 target actually took to resolve.

        Reported because it is the event clock's own answer to a question a time
        horizon would have assumed away.
        """
        return self.exit_resolution_ts - self.decision_ts

    @property
    def adverse_selection_bps(self) -> float:
        """Signed move against us between deciding and arriving.

        Positive means the market moved our way before we got there (rare);
        negative means the edge had already been taken. Reported as the signed
        drift so its sign carries meaning.
        """
        drift = (self.arrival_midpoint - self.decision_midpoint) / self.decision_midpoint
        return self.direction * drift * BPS

    @property
    def gross_return_bps(self) -> float:
        """Midpoint-to-midpoint at the same two instants the fills happened.

        This is the return an infinitely liquid, fee-free version of the trade
        would have earned given the same latency, so gross minus net isolates
        exactly what execution cost.
        """
        entry_mid = self.arrival_midpoint
        exit_mid = self.exit_midpoint
        return self.direction * (exit_mid - entry_mid) / entry_mid * BPS

    @property
    def spread_paid_bps(self) -> float:
        """Both legs, measured against the midpoint prevailing at each fill."""
        entry_cost = self.direction * (self.entry_price - self.arrival_midpoint)
        exit_cost = self.direction * (self.exit_midpoint - self.exit_price)
        return (entry_cost + exit_cost) / self.arrival_midpoint * BPS

    @property
    def realized_return_bps(self) -> float:
        """Fill to fill, before fees."""
        return (
            self.direction
            * (self.exit_price - self.entry_price)
            / self.entry_price
            * BPS
        )

    def fees_bps(self, price_scale: float, schedule: dict[str, Any]) -> float:
        """The named schedule, converted to basis points of entry notional.

        Two schedules exist and neither is silently the real one: a
        commission-free retail account is not billed the venue's per-share
        remove fee, while a direct member is. Section 31 and TAF attach to the
        sale leg only, whichever leg that is.
        """
        entry_notional = self.entry_price / price_scale * self.shares
        exit_notional = self.exit_price / price_scale * self.shares
        per_share = (
            schedule["commission_usd_per_share"]
            + schedule["exchange_take_fee_usd_per_share"]
            + schedule["clearing_usd_per_share"]
            + schedule["cat_usd_per_share"]
        ) * self.shares * 2
        sale_notional = exit_notional if self.direction > 0 else entry_notional
        sec = (
            schedule["sec_section_31_usd_per_million_sold"]
            * sale_notional
            / 1_000_000.0
        )
        taf = min(
            schedule["finra_taf_usd_per_share_sold"] * self.shares,
            schedule["finra_taf_cap_usd_per_trade"],
        )
        return (per_share + sec + taf) / entry_notional * BPS

    def net_return_bps(self, price_scale: float, schedule: dict[str, Any]) -> float:
        return self.realized_return_bps - self.fees_bps(price_scale, schedule)


# ---------------------------------------------------------------------------
# The cost hurdle -- computed from decision-time information only
# ---------------------------------------------------------------------------


def cost_hurdle_bps(
    book: BookLevels, shares: int, price_scale: float, schedule: dict[str, Any]
) -> float | None:
    """Round-trip break-even in bps, quoted by the market at decision time.

    Half the spread on the way in, half on the way out, plus the schedule's
    per-share charges. Nothing here looks forward: it is what the book is
    charging to round-trip right now, under the fee schedule being evaluated.
    """
    if not book.two_sided:
        return None
    mid = book.midpoint
    assert mid is not None
    half_spread_bps = (book.asks[0][0] - book.bids[0][0]) / 2.0 / mid * BPS
    notional = mid / price_scale * shares
    if notional <= 0:
        return None
    per_share = (
        schedule["commission_usd_per_share"]
        + schedule["exchange_take_fee_usd_per_share"]
        + schedule["clearing_usd_per_share"]
        + schedule["cat_usd_per_share"]
    ) * shares * 2
    sec = schedule["sec_section_31_usd_per_million_sold"] * notional / 1_000_000.0
    taf = min(
        schedule["finra_taf_usd_per_share_sold"] * shares,
        schedule["finra_taf_cap_usd_per_trade"],
    )
    fee_bps = (per_share + sec + taf) / notional * BPS
    return 2 * half_spread_bps + fee_bps


# ---------------------------------------------------------------------------
# Evaluating one candidate at one latency
# ---------------------------------------------------------------------------


def evaluate_candidate(
    *,
    predicted_bps: float,
    decision_ts: int,
    exit_resolution_ts: int | None,
    latency_ns: int,
    book_at,
    price_scale: float,
    schedule: dict[str, Any],
    shares: int = TRADE_SIZE_SHARES,
    rule: str = PRIMARY_RULE,
    decile_threshold_bps: float | None = None,
    timing_certified=None,
) -> tuple[Trade | None, str | None]:
    """One candidate at one latency rung. Returns ``(trade, no_trade_reason)``.

    ``exit_resolution_ts`` is the **availability timestamp of the frozen Stage-2
    target event** -- ``<prefix>_available_ts_recv`` from the label table. It is
    not derived from a duration and there is no ``horizon_ns`` parameter to pass
    one through. All four survivors are ``next_change`` / ``next_2_changes``, for
    which no duration exists: when the midpoint next moves is the thing being
    measured, not an input.

    ``book_at(ts)`` must return the displayed book as it stood at ``ts`` using
    only records with ``ts_recv <= ts``.

    ``timing_certified(lo, hi)`` returns False when any record in the window
    carried ``F_BAD_TS_RECV``. Such candidates are excluded, never repaired.
    """
    if exit_resolution_ts is None:
        return None, NO_TRADE_UNRESOLVED_TARGET

    arrival_ts = decision_ts + latency_ns
    exit_ts = exit_resolution_ts + latency_ns

    # The target event happened before we could get there. The prediction may
    # have been perfectly correct and is still unharvestable.
    if exit_resolution_ts <= arrival_ts:
        return None, NO_TRADE_RESOLVED_BEFORE_ENTRY

    if timing_certified is not None and not timing_certified(decision_ts, exit_ts):
        return None, NO_TRADE_UNCERTIFIABLE_TIMING

    decision_book = book_at(decision_ts)
    if decision_book is None or not decision_book.two_sided:
        return None, NO_TRADE_NO_BOOK

    if rule == PRIMARY_RULE:
        hurdle = cost_hurdle_bps(decision_book, shares, price_scale, schedule)
        if hurdle is None or abs(predicted_bps) <= hurdle:
            return None, NO_TRADE_BELOW_HURDLE
    elif rule == SECONDARY_RULE:
        if decile_threshold_bps is None or abs(predicted_bps) < decile_threshold_bps:
            return None, NO_TRADE_BELOW_HURDLE
    else:  # pragma: no cover - guarded by the plan
        raise ValueError(f"unknown trading rule {rule!r}")

    direction = 1 if predicted_bps > 0 else -1

    arrival_book = book_at(arrival_ts)
    if arrival_book is None or not arrival_book.two_sided:
        return None, NO_TRADE_NO_BOOK
    exit_book = book_at(exit_ts)
    if exit_book is None or not exit_book.two_sided:
        return None, NO_TRADE_NO_EXIT

    entry_side = "buy" if direction > 0 else "sell"
    exit_side = "sell" if direction > 0 else "buy"
    entry = walk_book(arrival_book, entry_side, shares)
    exit_fill = walk_book(exit_book, exit_side, shares)
    if entry is None or exit_fill is None:
        return None, NO_TRADE_NO_LIQUIDITY

    entry_price, entry_levels = entry
    exit_price, exit_levels = exit_fill
    trade = Trade(
        symbol="",
        session_date="",
        decision_ts=decision_ts,
        arrival_ts=arrival_ts,
        exit_ts=exit_ts,
        exit_resolution_ts=exit_resolution_ts,
        direction=direction,
        predicted_bps=predicted_bps,
        decision_midpoint=float(decision_book.midpoint),  # type: ignore[arg-type]
        arrival_midpoint=float(arrival_book.midpoint),  # type: ignore[arg-type]
        entry_price=entry_price,
        exit_price=exit_price,
        shares=shares,
        levels_consumed_entry=entry_levels,
        levels_consumed_exit=exit_levels,
        displayed_entry=arrival_book.displayed(entry_side),
        displayed_exit=exit_book.displayed(exit_side),
    )
    trade.exit_midpoint = float(exit_book.midpoint)  # type: ignore[assignment]
    return trade, None


# ---------------------------------------------------------------------------
# Aggregation and inference
# ---------------------------------------------------------------------------


def clustered_t(values: Sequence[float]) -> tuple[float | None, float | None]:
    """Session-clustered t, identical in form to Stage 2's.

    One observation per session date. The degenerate guard is relative to the
    mean's own scale, because a constant sequence leaves float dust in the
    denominator that an exact ``== 0`` test does not catch.
    """
    clean = [v for v in values if v is not None and np.isfinite(v)]
    if len(clean) < 2:
        return None, None
    array = np.asarray(clean, dtype=float)
    mean = float(array.mean())
    deviation = float(array.std(ddof=1))
    if not deviation > abs(mean) * 1e-9:
        return None, None
    statistic = mean / (deviation / sqrt(len(array)))
    return statistic, _student_t_sf(abs(statistic), len(array) - 1) * 2.0


def factor_beta(
    net_by_date: dict[str, float], market_by_date: dict[str, float]
) -> dict[str, float | None]:
    """Sensitivity of daily net return to the equal-weighted cross-symbol move.

    A strategy whose profit is really a directional bet on the tape is not a
    microstructure edge. The residual intercept is what survives that.
    """
    shared = sorted(set(net_by_date) & set(market_by_date))
    if len(shared) < 3:
        return {"beta": None, "alpha_bps": None, "r_squared": None, "dates": len(shared)}
    y = np.array([net_by_date[d] for d in shared], dtype=float)
    x = np.array([market_by_date[d] for d in shared], dtype=float)
    if not np.isfinite(y).all() or not np.isfinite(x).all():
        return {"beta": None, "alpha_bps": None, "r_squared": None, "dates": len(shared)}
    variance = float(((x - x.mean()) ** 2).sum())
    if variance <= 0:
        return {"beta": None, "alpha_bps": None, "r_squared": None, "dates": len(shared)}
    beta = float(((x - x.mean()) * (y - y.mean())).sum() / variance)
    alpha = float(y.mean() - beta * x.mean())
    residual = y - (alpha + beta * x)
    total = float(((y - y.mean()) ** 2).sum())
    r_squared = float(1 - (residual**2).sum() / total) if total > 0 else None
    return {
        "beta": beta,
        "alpha_bps": alpha,
        "r_squared": r_squared,
        "dates": len(shared),
    }


@dataclass
class CellEconomics:
    """Everything measured for one survivor at one latency and one rule."""

    cell: str
    latency: str
    rule: str
    price_scale: float
    fee_schedule_name: str = PRIMARY_FEE_SCHEDULE_NAME
    trades: list[Trade] = field(default_factory=list)
    no_trade_reasons: dict[str, int] = field(default_factory=dict)

    def record_no_trade(self, reason: str) -> None:
        self.no_trade_reasons[reason] = self.no_trade_reasons.get(reason, 0) + 1

    def summary(self, market_by_date: dict[str, float] | None = None) -> dict[str, Any]:
        trades = self.trades
        if not trades:
            return {
                "cell": self.cell,
                "latency": self.latency,
                "rule": self.rule,
                "fee_schedule": self.fee_schedule_name,
                "trade_count": 0,
                "no_trade_reasons": dict(sorted(self.no_trade_reasons.items())),
                "reached_inference": False,
                "reason": "no trades were taken",
            }

        schedule = FEE_SCHEDULES[self.fee_schedule_name]
        net = np.array([t.net_return_bps(self.price_scale, schedule) for t in trades])
        gross = np.array([t.gross_return_bps for t in trades])
        spread = np.array([t.spread_paid_bps for t in trades])
        fees = np.array([t.fees_bps(self.price_scale, schedule) for t in trades])
        adverse = np.array([t.adverse_selection_bps for t in trades])
        realized = np.array([t.realized_return_bps for t in trades])
        # Slippage is what walking the book cost beyond the quoted touch, i.e.
        # the part of execution cost the half-spread does not explain.
        slippage = gross - realized - spread

        by_date: dict[str, list[float]] = {}
        by_symbol: dict[str, list[float]] = {}
        for trade, value in zip(trades, net, strict=True):
            by_date.setdefault(trade.session_date, []).append(float(value))
            by_symbol.setdefault(trade.symbol, []).append(float(value))
        date_means = {d: float(np.mean(v)) for d, v in sorted(by_date.items())}

        statistic, p_value = clustered_t(list(date_means.values()))
        gates = ECONOMIC_GATES
        reached = (
            len(trades) >= gates["minimum_trades_for_inference"]
            and len(date_means) >= gates["minimum_session_dates"]
        )

        return {
            "cell": self.cell,
            "latency": self.latency,
            "rule": self.rule,
            "fee_schedule": self.fee_schedule_name,
            "fee_schedule_version": schedule["schedule_version"],
            "trade_count": len(trades),
            "session_dates": len(date_means),
            "reached_inference": reached,
            "gross_return_bps": float(gross.mean()),
            "spread_paid_bps": float(spread.mean()),
            "slippage_bps": float(slippage.mean()),
            "adverse_selection_bps": float(adverse.mean()),
            "fees_bps": float(fees.mean()),
            "net_return_bps": float(net.mean()),
            "net_return_bps_median": float(np.median(net)),
            "win_rate": float((net > 0).mean()),
            "average_holding_ns": float(np.mean([t.holding_ns for t in trades])),
            "average_realized_lag_ns": float(
                np.mean([t.realized_lag_ns for t in trades])
            ),
            "displayed_liquidity_shares": float(
                np.mean([t.displayed_entry for t in trades])
            ),
            "capacity_shares": int(min(t.displayed_entry for t in trades)),
            "mean_levels_consumed": float(
                np.mean([t.levels_consumed_entry for t in trades])
            ),
            "clustered_t": statistic,
            "p_value": p_value,
            "per_session_date_net_bps": date_means,
            "by_symbol_net_bps": {
                s: float(np.mean(v)) for s, v in sorted(by_symbol.items())
            },
            "factor_sensitivity": factor_beta(date_means, market_by_date or {}),
            "no_trade_reasons": dict(sorted(self.no_trade_reasons.items())),
        }


# ---------------------------------------------------------------------------
# Survivor freeze
# ---------------------------------------------------------------------------


def freeze_survivors(stage2_results: dict[str, Any]) -> dict[str, Any]:
    """Take the survivors exactly as Stage 2 left them, and hash them.

    A survivor is a cell whose confirmation gate passed. Nothing here re-reads a
    statistic, re-applies a gate, or promotes a near-miss: Stage 2 already
    decided, and Stage 3 is not a court of appeal.
    """
    cells = stage2_results.get("cells", [])
    survivors = [
        f"{c['cadence']}|{c['horizon']}"
        for c in cells
        if (c.get("confirmation") or {}).get("passed") is True
    ]
    survivors.sort()
    digest = hashlib.sha256("\n".join(survivors).encode("utf-8")).hexdigest()
    return {
        "survivors": survivors,
        "survivor_count": len(survivors),
        "survivor_hash": digest,
        "stage2_plan_hash": stage2_results.get("plan_hash"),
        "stage2_verdict": stage2_results.get("verdict"),
        "frozen_from": "confirmation.passed is True",
    }


def load_frozen_survivors(path: Path, *, expected_count: int | None = None) -> dict[str, Any]:
    """Load and freeze, refusing anything that does not match what was declared."""
    if not path.is_file():
        raise ValueError(
            f"no Stage-2 results at {path}. Stage 3 has nothing to evaluate until "
            "Stage 2B has actually been run; it may not invent survivors."
        )
    frozen = freeze_survivors(json.loads(path.read_text(encoding="utf-8")))
    if not frozen["survivors"]:
        raise ValueError(
            "Stage 2 produced no surviving cell. Stage 3 does not run, and the "
            "honest conclusion is that the frozen L3 block showed no incremental "
            "predictive information to price."
        )
    if expected_count is not None and frozen["survivor_count"] != expected_count:
        raise ValueError(
            f"expected {expected_count} frozen survivors, found "
            f"{frozen['survivor_count']}: {frozen['survivors']}"
        )
    # The survivors named in the plan are the survivors Stage 2 confirmed, or
    # this is not the run the plan was frozen against.
    if frozen["survivor_hash"] != SURVIVOR_HASH:
        raise ValueError(
            "the survivors in stage2_results.json do not match the set "
            "frozen into the Stage-3 plan. results="
            f"{frozen['survivors']} plan={list(FROZEN_SURVIVORS)}"
        )
    return frozen


def assert_frozen_plan() -> None:
    """Refuse to compute economics against a plan or a survivor set that moved."""
    if SURVIVOR_HASH != EXPECTED_SURVIVOR_HASH:
        raise ValueError(
            "the frozen Stage-2 survivor set has changed; Stage 3 evaluates the "
            "four cells Stage 2 confirmed and no others"
        )
    if PLAN_DESIGN_HASH != EXPECTED_PLAN_DESIGN_HASH:
        raise ValueError(
            "the Stage-3 design hash has moved; a rule changed and that is a new "
            "trial, not a re-run"
        )


# ---------------------------------------------------------------------------
# Family-level assembly
# ---------------------------------------------------------------------------


def assemble_report(
    cell_results: Iterable[dict[str, Any]], frozen: dict[str, Any]
) -> dict[str, Any]:
    """The primary family is the 250 ms rung under the primary rule, and only that.

    Every other rung and rule is reported in full and corrected separately. A
    secondary result may inform, and may never answer the primary question.
    """
    results = list(cell_results)
    primary = [
        r
        for r in results
        if r["latency"] == PRIMARY_LATENCY
        and r["rule"] == PRIMARY_RULE
        and r.get("fee_schedule", PRIMARY_FEE_SCHEDULE_NAME) == PRIMARY_FEE_SCHEDULE_NAME
    ]
    p_values = {
        r["cell"]: (r.get("p_value") if r.get("reached_inference") else None)
        for r in primary
    }
    # Only a positive mean can pass; a significantly negative one is a failure,
    # not a discovery.
    for r in primary:
        if r.get("net_return_bps") is not None and r["net_return_bps"] <= 0:
            p_values[r["cell"]] = None

    bh = benjamini_hochberg(p_values, fdr=ECONOMIC_GATES["false_discovery_rate"])
    survivors_positive = [
        r["cell"]
        for r in primary
        if bh.get(r["cell"], {}).get("survives_bh")
        and (r.get("clustered_t") or 0) >= ECONOMIC_GATES["t_hurdle"]
    ]

    return {
        "stage3_executor_version": STAGE3_EXECUTOR_VERSION,
        "stage3_plan_version": STAGE3_PLAN_VERSION,
        "stage3_plan_design_hash": PLAN_DESIGN_HASH,
        "stage2_plan_design_hash": STAGE2_PLAN_DESIGN_HASH,
        "frozen_survivors": frozen,
        "survivor_hash": SURVIVOR_HASH,
        "governance": {
            "stage2_survivors_known": True,
            "stage3_economic_outcome_viewed": True,
            "stage3_rules_frozen_before_economic_outcomes": True,
        },
        "primary_question": ECONOMIC_GATES["primary_question"],
        "primary_family": {
            "latency": PRIMARY_LATENCY,
            "rule": PRIMARY_RULE,
            "fee_schedule": PRIMARY_FEE_SCHEDULE_NAME,
            "size": len(primary),
            "benjamini_hochberg": bh,
        },
        "economically_positive_at_primary": survivors_positive,
        "verdict": (
            "no_economically_viable_survivor"
            if not survivors_positive
            else "survivor_economically_positive_at_250ms"
        ),
        "cells": results,
        "authorizes": ECONOMIC_GATES["what_a_pass_authorizes"],
    }


def write_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, default=str, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Reconstructing the frozen Stage-2 fit
# ---------------------------------------------------------------------------


def reconstruct_confirmation_beta(
    per_date_grams: dict[str, Any],
    training_dates: Sequence[str],
    alpha: float,
    *,
    recorded_delta_r2: float | None = None,
    confirmation_dates: Sequence[str] = (),
    tolerance: float = 1e-9,
) -> Any:
    """Rebuild the exact coefficients Stage 2 used for confirmation.

    This is reproduction, not refitting. Stage 2 recorded which alpha it chose
    and which dates it trained on, and the Gram matrices are stored, so the
    normal equations have exactly one solution and it is the same one Stage 2
    solved. Nothing is re-selected and nothing is re-tuned.

    Because "reproduction" is easy to claim and easy to get wrong, it is checked:
    the rebuilt coefficients are scored on the confirmation dates and the
    resulting ``delta_R2`` must reproduce the value Stage 2 recorded. If it does
    not, the artefacts do not belong together and Stage 3 refuses rather than
    trading a model it cannot account for.
    """
    from app.services.mbo_stage2_executor import DESIGN_WIDTH, delta_r2, fit, sum_grams

    train = sum_grams(
        (per_date_grams[d] for d in training_dates if d in per_date_grams), DESIGN_WIDTH
    )
    beta = fit(train, alpha)
    if beta is None:
        raise ValueError("the frozen Stage-2 training Gram is singular; cannot reproduce")

    if recorded_delta_r2 is not None and confirmation_dates:
        test = sum_grams(
            (per_date_grams[d] for d in confirmation_dates if d in per_date_grams),
            DESIGN_WIDTH,
        )
        rebuilt = delta_r2(train, test, alpha)
        if rebuilt is None or abs(rebuilt - recorded_delta_r2) > tolerance:
            raise ValueError(
                "the reconstructed Stage-2 fit does not reproduce the recorded "
                f"confirmation delta_R2 ({rebuilt} vs {recorded_delta_r2}); the "
                "Grams and the results file do not belong to the same run"
            )
    return beta


# ---------------------------------------------------------------------------
# Single-pass book replay
# ---------------------------------------------------------------------------


class BookReplay:
    """Answers "what did the book look like at instant t" in one streaming pass.

    Every trade needs three instants and every latency rung shifts two of them,
    so a naive implementation would replay the file once per query. Instead the
    query instants are collected, sorted, and served during a single pass.

    The clock is ``ts_recv``, not ``ts_event``: the question Stage 3 asks is what
    a participant could have *known and acted on*, and receipt time is when they
    could know it. A record is visible at ``t`` only if ``ts_recv <= t``.

    The pass therefore requires ``ts_recv`` to be non-decreasing through the
    file. That is asserted rather than assumed -- a file that is out of order
    would silently produce fills from information that had not arrived yet, which
    is precisely the error this whole stage exists to avoid.
    """

    def __init__(self, book_factory, depth: int = MAX_BOOK_LEVELS_WALKED):
        self.book_factory = book_factory
        self.depth = depth
        self.out_of_order_records = 0
        # Receipt instants the venue itself declined to vouch for. Kept sorted
        # by construction, since the pass requires non-decreasing ts_recv.
        self.bad_recv_instants: list[int] = []

    def timing_certified(self, lo: int, hi: int) -> bool:
        """Is every receive timestamp in ``[lo, hi]`` one we can stand behind?

        A flagged instant anywhere inside a candidate's window makes that
        candidate's timing uncertifiable. The alternative -- substituting
        ts_event, interpolating, or simply trusting it -- would be inventing
        timing, and inventing timing is the one error that silently turns a
        losing strategy into a winning one.
        """
        from bisect import bisect_left

        index = bisect_left(self.bad_recv_instants, lo)
        return not (
            index < len(self.bad_recv_instants)
            and self.bad_recv_instants[index] <= hi
        )

    def snapshot(self, book, ts: int) -> BookLevels:
        """Read the top ``depth`` levels of each side as a frozen tuple."""
        levels = book.depth(self.depth)
        bids = tuple((lvl["price"], lvl["size"]) for lvl in levels["bids"])
        asks = tuple((lvl["price"], lvl["size"]) for lvl in levels["asks"])
        return BookLevels(ts=ts, bids=bids, asks=asks)

    def run(self, events: Iterable[Any], query_instants: Sequence[int]) -> dict[int, BookLevels]:
        """Replay once, snapshotting the book as each query instant is passed."""
        book = self.book_factory()
        pending = sorted(set(query_instants))
        answers: dict[int, BookLevels] = {}
        index = 0
        last_recv: int | None = None

        for event in events:
            recv = event.ts_recv
            if last_recv is not None and recv < last_recv:
                self.out_of_order_records += 1
                raise ValueError(
                    "ts_recv went backwards in the certified stream; a single-pass "
                    "latency replay would use information that had not arrived. "
                    "Refusing rather than producing a fill that could not have "
                    "happened."
                )
            # Everything strictly before this record's arrival is now answerable.
            while index < len(pending) and pending[index] < recv:
                answers[pending[index]] = self.snapshot(book, pending[index])
                index += 1
            if event.flags & F_BAD_TS_RECV:
                self.bad_recv_instants.append(recv)
            book.apply(event)
            last_recv = recv

        while index < len(pending):
            answers[pending[index]] = self.snapshot(book, pending[index])
            index += 1
        return answers
