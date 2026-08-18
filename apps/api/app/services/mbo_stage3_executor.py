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
from app.services.mbo_stage2_plan import PLAN_HASH as STAGE2_PLAN_HASH
from app.services.mbo_stage3_plan import (
    DISCOVERY_DECILE_QUANTILE,
    ECONOMIC_GATES,
    F_BAD_TS_RECV,
    FEE_SCHEDULES,
    FROZEN_SURVIVORS,
    LATENCY_RUNGS,
    MAX_BOOK_LEVELS_WALKED,
    PLAN_DESIGN_HASH,
    PRIMARY_FEE_SCHEDULE_NAME,
    PRIMARY_LATENCY,
    PRIMARY_RULE,
    RETAIL_CAT_STRESS_FEE_SCHEDULE,
    SECONDARY_RULE,
    STAGE3_PLAN_VERSION,
    SURVIVOR_HASH,
    TRADE_SIZE_SHARES,
)

STAGE3_EXECUTOR_VERSION = "tier1_stage3_executor_v2"

EXPECTED_PLAN_DESIGN_HASH = (
    "6908076a49a9ecf0b274fff9c1f482672abe3b65561f7f3c6d52c7702991d820"
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

# Verdicts. The distinction between the last two is the whole point: one says
# the strategy loses money, the other says it could not be executed often enough
# to find out. Collapsing them would be the most flattering possible error.
VERDICT_POSITIVE = "survivor_economically_positive_at_250ms"
VERDICT_NEGATIVE = "no_economically_viable_survivor"
VERDICT_INSUFFICIENT = "not_authorized_insufficient_executable_sample"


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


def _passes_economics(row: dict[str, Any]) -> bool:
    """Measured, positive, and past the frozen t hurdle."""
    return bool(
        row.get("reached_inference")
        and (row.get("net_return_bps") or 0) > 0
        and (row.get("clustered_t") or 0) >= ECONOMIC_GATES["t_hurdle"]
    )


def assemble_report(
    cell_results: Iterable[dict[str, Any]], frozen: dict[str, Any]
) -> dict[str, Any]:
    """The primary family is the 250 ms rung, primary rule, retail schedule.

    Two things are decided here and they are deliberately separate. The
    **verdict** is the scientific answer and comes from the primary family
    alone. **Authorization** is whether paper trading may proceed, and it
    additionally requires surviving the CAT-inclusive retail stress -- because
    the primary schedule excludes a charge whose June-2025 customer treatment
    could not be verified, and deploying on the convenient reading of an
    unverified fact is not something a positive result should buy.
    """
    results = list(cell_results)
    survivor_names = list(frozen.get("survivors") or FROZEN_SURVIVORS)

    def family(schedule: str) -> list[dict[str, Any]]:
        return [
            r
            for r in results
            if r["latency"] == PRIMARY_LATENCY
            and r["rule"] == PRIMARY_RULE
            and r.get("fee_schedule", PRIMARY_FEE_SCHEDULE_NAME) == schedule
        ]

    primary = family(PRIMARY_FEE_SCHEDULE_NAME)
    cat_stress = family(RETAIL_CAT_STRESS_FEE_SCHEDULE["name"])

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
        if bh.get(r["cell"], {}).get("survives_bh") and _passes_economics(r)
    ]

    # A survivor with no primary row at all was never measured either.
    measured = {r["cell"] for r in primary if r.get("reached_inference")}
    insufficient = [c for c in survivor_names if c not in measured]

    # Frozen precedence. An unmeasured survivor is never called economically
    # negative: if ANY frozen survivor could not be measured and none passed,
    # the family's answer is that it could not be established, not that it lost.
    if survivors_positive:
        verdict = VERDICT_POSITIVE
    elif insufficient:
        verdict = VERDICT_INSUFFICIENT
    else:
        verdict = VERDICT_NEGATIVE

    # --- authorization, which the verdict alone does not settle --------------
    cat_viable = {r["cell"] for r in cat_stress if _passes_economics(r)}
    cat_robust = [c for c in survivors_positive if c in cat_viable]

    authorizes = verdict == VERDICT_POSITIVE and bool(cat_robust)
    deployment_blocker = None
    if verdict == VERDICT_POSITIVE and not cat_robust:
        deployment_blocker = "unverified_historical_cat_treatment"

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
        "insufficient_executable_sample": insufficient,
        "minimum_trades_for_inference": ECONOMIC_GATES["minimum_trades_for_inference"],
        "minimum_session_dates": ECONOMIC_GATES["minimum_session_dates"],
        "verdict": verdict,
        "verdict_meaning": {
            VERDICT_POSITIVE: "at least one survivor is positive after costs at 250 ms",
            VERDICT_NEGATIVE: "no survivor is positive after costs at 250 ms",
            VERDICT_INSUFFICIENT: ECONOMIC_GATES["insufficient_sample_meaning"],
        }[verdict],
        "cat_stress": {
            "fee_schedule": RETAIL_CAT_STRESS_FEE_SCHEDULE["name"],
            "role": "secondary; cannot redefine or veto the scientific verdict",
            "viable_cells": sorted(cat_viable),
            "controls": "deployment authorization only",
        },
        "cat_robust_survivors": cat_robust,
        "authorizes_stage4_or_paper": authorizes,
        "authorized_survivors": cat_robust if authorizes else [],
        "deployment_blocker": deployment_blocker,
        "cells": results,
        "authorizes": ECONOMIC_GATES["what_a_pass_authorizes"],
    }


def write_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, default=str, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Reconstructing the frozen Stage-2 fit
# ---------------------------------------------------------------------------


def reconstruct_confirmation_fit(
    per_date_grams: dict[str, Any],
    training_dates: Sequence[str],
    confirmation_dates: Sequence[str],
    alpha: float,
    *,
    recorded_confirmation_delta_r2: float | None = None,
    recorded_per_date_delta_r2: Sequence[float] | None = None,
    tolerance: float = 1e-9,
) -> dict[str, Any]:
    """Rebuild the exact coefficients Stage 2 used, and prove it did.

    This is reproduction, not refitting. Stage 2 recorded which alpha it chose
    and which dates it trained on, and the per-date Grams are stored, so the
    normal equations have exactly one solution and it is the one Stage 2 solved.
    The fit is performed **once**, from the discovery+validation dates.

    The proof has to match how Stage 2 actually scored confirmation, which is
    *not* one ``delta_R2`` over an aggregated confirmation Gram. Stage 2 scored
    each confirmation date separately against the same training Gram and took
    the arithmetic mean of those per-date values -- ``_gate`` sets
    ``delta_r2 = mean(per_date_delta_r2)``. Those two quantities are different
    numbers: the aggregate is notional-weighted across dates, the mean is not.
    Comparing against the aggregate, as an earlier draft did, would have failed
    on a correct reproduction and passed on some incorrect ones.

    So the check is:

    * score each confirmation date individually against the single training fit;
    * compare the ordered per-date values with Stage 2's recorded
      ``per_date_delta_r2`` where that is available;
    * compare their arithmetic mean with the recorded confirmation ``delta_r2``.

    Any mismatch is a refusal. Stage 3 does not trade a model whose provenance
    it cannot demonstrate.
    """
    from app.services.mbo_stage2_executor import (
        DESIGN_WIDTH,
        PRICE_ONLY_WIDTH,
        _slice,
        delta_r2,
        fit,
        sum_grams,
    )

    train = sum_grams(
        (per_date_grams[d] for d in training_dates if d in per_date_grams), DESIGN_WIDTH
    )
    beta_l3 = fit(train, alpha)
    beta_base = fit(_slice(train, PRICE_ONLY_WIDTH), 0.0)
    if beta_l3 is None or beta_base is None:
        raise ValueError("the frozen Stage-2 training Gram is singular; cannot reproduce")

    scored_dates: list[str] = []
    per_date: list[float] = []
    for date in confirmation_dates:
        if date not in per_date_grams:
            continue
        value = delta_r2(train, per_date_grams[date], alpha)
        if value is None:
            continue
        scored_dates.append(date)
        per_date.append(float(value))

    if not per_date:
        raise ValueError("no confirmation date could be scored; cannot reproduce")

    mean_delta_r2 = float(np.mean(per_date))

    if recorded_per_date_delta_r2 is not None:
        recorded = [float(v) for v in recorded_per_date_delta_r2]
        if len(recorded) != len(per_date):
            raise ValueError(
                f"Stage 2 recorded {len(recorded)} confirmation dates, "
                f"reproduction scored {len(per_date)}"
            )
        for index, (rebuilt, original) in enumerate(zip(per_date, recorded, strict=True)):
            if abs(rebuilt - original) > tolerance:
                raise ValueError(
                    "the reconstructed Stage-2 fit does not reproduce the "
                    f"recorded per-date confirmation delta_R2 at position {index} "
                    f"({scored_dates[index]}): {rebuilt} vs {original}"
                )

    if recorded_confirmation_delta_r2 is not None and (
        abs(mean_delta_r2 - recorded_confirmation_delta_r2) > tolerance
    ):
        raise ValueError(
            "the mean of the reconstructed per-date confirmation delta_R2 does "
            f"not reproduce the recorded value ({mean_delta_r2} vs "
            f"{recorded_confirmation_delta_r2}); the Grams and the results file "
            "do not belong to the same run"
        )

    return {
        "beta": beta_l3,
        "beta_baseline": beta_base,
        "alpha": alpha,
        "training_dates": [d for d in training_dates if d in per_date_grams],
        "confirmation_dates": scored_dates,
        "per_date_delta_r2": per_date,
        "mean_delta_r2": mean_delta_r2,
        "reproduction_verified": True,
    }


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


# ---------------------------------------------------------------------------
# The complete run
# ---------------------------------------------------------------------------


def cell_prefix(horizon: str) -> str:
    from app.services.mbo_label_engine import HORIZONS_BY_NAME

    return HORIZONS_BY_NAME[horizon].prefix


def predict(design: np.ndarray, beta: np.ndarray) -> np.ndarray:
    """The frozen Stage-2 fit applied. No refitting, no rescaling, no clipping."""
    return design @ beta


def evaluate_symbol_day(
    *,
    symbol: str,
    session_date: str,
    design: np.ndarray,
    predictions: np.ndarray,
    decision_ts: np.ndarray,
    exit_resolution_ts: np.ndarray,
    usable: np.ndarray,
    cell: str,
    replay: BookReplay,
    books: dict[int, BookLevels],
    price_scale: float,
    sinks: dict[tuple[str, str, str, str], CellEconomics],
    decile_threshold_bps: float | None = None,
) -> None:
    """Evaluate one symbol-day of one cell across the whole grid.

    The grid is (latency rung x trading rule x fee schedule). Every combination
    is measured; which of them may answer the primary question is decided later
    and elsewhere, by ``assemble_report``.
    """
    def book_at(ts: int) -> BookLevels | None:
        return books.get(ts)

    for row in range(len(predictions)):
        if not usable[row]:
            continue
        decision = int(decision_ts[row])
        resolution = exit_resolution_ts[row]
        resolution = None if resolution is None or resolution < 0 else int(resolution)
        predicted = float(predictions[row])

        for latency_name, latency_ns in LATENCY_RUNGS:
            for rule in (PRIMARY_RULE, SECONDARY_RULE):
                for schedule_name, schedule in FEE_SCHEDULES.items():
                    trade, reason = evaluate_candidate(
                        predicted_bps=predicted,
                        decision_ts=decision,
                        exit_resolution_ts=resolution,
                        latency_ns=latency_ns,
                        book_at=book_at,
                        price_scale=price_scale,
                        schedule=schedule,
                        rule=rule,
                        decile_threshold_bps=decile_threshold_bps,
                        timing_certified=replay.timing_certified,
                    )
                    sink = sinks[(cell, latency_name, rule, schedule_name)]
                    if trade is None:
                        sink.record_no_trade(reason or "unknown")
                        continue
                    trade.symbol = symbol
                    trade.session_date = session_date
                    sink.trades.append(trade)


def query_instants(
    decision_ts: np.ndarray, exit_resolution_ts: np.ndarray, usable: np.ndarray
) -> list[int]:
    """Every instant the fill model will ask about, for a single replay pass."""
    instants: set[int] = set()
    for row in range(len(decision_ts)):
        if not usable[row]:
            continue
        decision = int(decision_ts[row])
        resolution = exit_resolution_ts[row]
        instants.add(decision)
        for _, latency in LATENCY_RUNGS:
            instants.add(decision + latency)
            if resolution is not None and resolution >= 0:
                instants.add(int(resolution) + latency)
    return sorted(instants)


def make_sinks(price_scale: float) -> dict[tuple[str, str, str, str], CellEconomics]:
    """One accumulator per (cell, latency, rule, fee schedule)."""
    return {
        (cell, latency, rule, schedule): CellEconomics(
            cell=cell,
            latency=latency,
            rule=rule,
            price_scale=price_scale,
            fee_schedule_name=schedule,
        )
        for cell in FROZEN_SURVIVORS
        for latency, _ in LATENCY_RUNGS
        for rule in (PRIMARY_RULE, SECONDARY_RULE)
        for schedule in FEE_SCHEDULES
    }


def summarize(
    sinks: dict[tuple[str, str, str, str], CellEconomics],
    market_by_date: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    return [sink.summary(market_by_date) for sink in sinks.values()]


# ---------------------------------------------------------------------------
# Binding Stage 3 to the exact bytes that produced Stage 1
# ---------------------------------------------------------------------------


def sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """Hash a source file without reading it into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_raw_source(
    manifest: dict[str, Any], raw_dir: Path, *, stem: str, verify_hash: bool = True
) -> Path:
    """Find the raw file Stage 1 actually read, and prove it is that file.

    The filename is taken from the Stage-1 manifest rather than reconstructed
    from the symbol-day stem. Reconstructing it guesses at a naming convention;
    the manifest records what was opened. Size and SHA-256 are then checked
    against the same manifest, so Stage 3 replays the exact bytes that produced
    the features it is predicting from -- not merely a file with a plausible name.
    """
    source = manifest.get("source")
    if not source or not source.get("filename"):
        raise ValueError(
            f"the Stage-1 manifest for {stem} records no source file; Stage 3 "
            "will not guess which raw input produced these features"
        )
    name = source["filename"]
    matches = [p for p in raw_dir.rglob(name) if p.is_file()]
    if not matches:
        raise ValueError(f"raw source {name!r} for {stem} not found under {raw_dir}")
    if len(matches) > 1:
        raise ValueError(
            f"raw source {name!r} for {stem} is ambiguous: {[str(m) for m in matches]}"
        )
    path = matches[0]

    expected_bytes = source.get("bytes")
    if expected_bytes is not None and path.stat().st_size != expected_bytes:
        raise ValueError(
            f"raw source {name!r} for {stem} is {path.stat().st_size} bytes, "
            f"Stage 1 recorded {expected_bytes}"
        )
    expected_sha = source.get("sha256")
    if verify_hash and expected_sha:
        observed = sha256_file(path)
        if observed != expected_sha:
            raise ValueError(
                f"raw source {name!r} for {stem} does not match the Stage-1 "
                f"SHA-256 ({observed} vs {expected_sha}); these are not the bytes "
                "that produced the features"
            )
    return path


def assert_feature_batch_is_frozen(batch_manifest: dict[str, Any]) -> None:
    """The supplied features must be the frozen v4 engine's own output.

    Stage-2 Grams being correct says nothing about whichever --features-dir was
    handed to Stage 3.
    """
    from app.services.mbo_feature_engine import (
        FEATURE_ENGINE_VERSION,
        FEATURE_SEMANTICS_HASH,
        FEATURE_VOCABULARY_HASH,
    )

    definitions = batch_manifest.get("definitions", {})
    checks = (
        ("feature_engine_version", FEATURE_ENGINE_VERSION),
        ("feature_semantics_hash", FEATURE_SEMANTICS_HASH),
        ("feature_vocabulary_hash", FEATURE_VOCABULARY_HASH),
    )
    for key, expected in checks:
        observed = definitions.get(key)
        if observed != expected:
            raise ValueError(
                f"the supplied feature batch declares {key}={observed!r}, not the "
                f"frozen {expected!r}"
            )
    if not batch_manifest.get("feature_semantics_consistent", True):
        raise ValueError("the supplied feature extraction is not semantics-consistent")


def assert_labels_align(
    stem: str,
    cadence: str,
    feature_sequence: np.ndarray,
    label_sequence: np.ndarray,
) -> None:
    """Labels must be this feature file's labels, row for row.

    The same check Stage-2 `grams` performs. Repeating it here is the point:
    Stage 3 is handed its own --features-dir and --labels-dir and may not assume
    they are the pair the Grams were built from.
    """
    if len(label_sequence) != len(feature_sequence) or not np.array_equal(
        label_sequence, feature_sequence
    ):
        raise ValueError(
            f"label rows for {stem} {cadence} do not align one-for-one with the "
            "feature snapshots; refusing to join on assumption"
        )


def event_horizon_availability(
    status: np.ndarray, available: Any
) -> list[int | None]:
    """Availability instants, with nulls handled rather than cast through.

    A non-OK label legitimately has no resolution instant, and the column is
    nullable. Casting the whole column to int64 turns those nulls into whatever
    the null sentinel happens to be -- a real timestamp, arithmetically valid,
    silently wrong. So the value is produced only where the status is OK, and is
    ``None`` everywhere else.
    """
    from app.services.mbo_label_engine import LABEL_OK

    values = available.to_pylist() if hasattr(available, "to_pylist") else list(available)
    resolved: list[int | None] = []
    for index, raw in enumerate(values):
        if index >= len(status) or status[index] != LABEL_OK or raw is None:
            resolved.append(None)
            continue
        try:
            resolved.append(int(raw))
        except (TypeError, ValueError):
            resolved.append(None)
    return resolved


# ---------------------------------------------------------------------------
# The discovery decile, and the common factor
# ---------------------------------------------------------------------------


def discovery_decile_threshold(absolute_predictions: Sequence[float]) -> float | None:
    """The frozen quantile of |prediction| over discovery dates.

    Predictions, not outcomes. No realized return, no economic result and no
    confirmation-date row enters this number.
    """
    clean = [
        float(v) for v in absolute_predictions if v is not None and np.isfinite(v)
    ]
    if not clean:
        return None
    return float(np.quantile(clean, DISCOVERY_DECILE_QUANTILE))


def session_return_bps(midpoints: np.ndarray) -> float | None:
    """First-to-last midpoint return of one symbol-day, in basis points."""
    finite = midpoints[np.isfinite(midpoints) & (midpoints > 0)]
    if finite.size < 2:
        return None
    return float((finite[-1] - finite[0]) / finite[0] * BPS)


def common_factor_by_date(
    per_symbol_day: Sequence[tuple[str, str, float]],
) -> dict[str, float]:
    """The frozen factor: equal-weighted cross-symbol session return per date.

    ``per_symbol_day`` is ``(session_date, symbol, session_return_bps)``. A
    symbol-day with no usable midpoints is omitted rather than counted as zero,
    because a missing observation is not a flat one.
    """
    grouped: dict[str, list[float]] = {}
    for session_date, _symbol, value in per_symbol_day:
        if value is None or not np.isfinite(value):
            continue
        grouped.setdefault(session_date, []).append(float(value))
    return {date: float(np.mean(values)) for date, values in sorted(grouped.items())}


# ---------------------------------------------------------------------------
# The full Stage-2 spine certification, repeated
# ---------------------------------------------------------------------------


def certify_spine(
    stem: str,
    cadence: str,
    *,
    feature_sequence: np.ndarray,
    feature_ts_event: np.ndarray,
    feature_midpoint: np.ndarray,
    label_sequence: np.ndarray,
    label_ts_event: np.ndarray,
    label_midpoint: np.ndarray,
) -> None:
    """Exactly what ``mbo_stage2 grams`` certifies, on exactly the same terms.

    Matching ``sequence_index`` alone is weaker than it looks: two extractions
    can agree on row *ordering* while disagreeing about which instants and which
    midpoints those rows describe. The labels carry the spine of the snapshot
    they were resolved against, so all three columns are compared, and the
    midpoint comparison is nan-safe in the same way -- ``nan`` is mapped to a
    sentinel so two missing midpoints compare equal to each other and unequal to
    any real price.

    Any mismatch is a refusal. These labels would belong to a different
    extraction.
    """
    if len(label_sequence) != len(feature_sequence) or not np.array_equal(
        label_sequence, feature_sequence
    ):
        raise ValueError(
            f"label rows for {stem} {cadence} do not align one-for-one with the "
            "feature snapshots; refusing to join on assumption"
        )
    if not np.array_equal(label_ts_event, feature_ts_event):
        raise ValueError(
            f"spine mismatch for {stem} {cadence}: label source_ts_event does not "
            "reproduce the feature snapshot timestamps, so these labels belong to "
            "a different extraction and must be rebuilt"
        )
    if not np.array_equal(
        np.nan_to_num(label_midpoint, nan=np.inf),
        np.nan_to_num(feature_midpoint, nan=np.inf),
    ):
        raise ValueError(
            f"spine mismatch for {stem} {cadence}: label source_midpoint does not "
            "reproduce the feature snapshot midpoints, so these labels must be "
            "rebuilt"
        )


# ---------------------------------------------------------------------------
# Batch completeness
# ---------------------------------------------------------------------------


def assert_batch_complete(
    *,
    features_dir: Path,
    labels_dir: Path,
    grams_dir: Path,
    stage2_results: dict[str, Any],
) -> dict[str, Any]:
    """Bind the supplied artefacts to the completed Stage-1 and Stage-2 batches.

    Both halves are required. A manifest alone can describe a batch that is no
    longer on disk; a file count alone can describe a batch nobody certified. So
    the declared counts are read from the Stage-1 batch manifest and the Stage-2
    grams manifest, checked against the frozen expectations, and then checked
    against the physical files.

    Anything missing is a refusal. Continuing with what happens to be present
    would change the universe the primary question was asked about without
    saying so.
    """
    from app.services.mbo_feature_engine import CADENCES, FEATURE_SEMANTICS_HASH
    from app.services.mbo_label_engine import (
        LABEL_DEFINITION_HASH,
        SUPERSEDED_LABEL_DEFINITION_HASHES,
    )
    from app.services.mbo_stage2_plan import SUPERSEDED_PLAN_HASHES
    from app.services.mbo_stage3_plan import (
        EXPECTED_CADENCE_PARQUETS,
        EXPECTED_LABEL_FILES,
        EXPECTED_SESSION_DATES,
        EXPECTED_STAGE1_MANIFESTS,
        EXPECTED_SYMBOL_DAYS,
        EXPECTED_SYMBOLS_PER_DATE,
    )

    problems: list[str] = []

    def require(label: str, observed: Any, expected: Any) -> None:
        if observed != expected:
            problems.append(f"{label}: {observed!r}, expected {expected!r}")

    # --- Stage-1 declarations ------------------------------------------------
    batch_path = features_dir / "batch_manifest.json"
    if not batch_path.is_file():
        raise ValueError(f"no Stage-1 batch manifest at {batch_path}")
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    require("stage1 files_completed", batch.get("files_completed"), EXPECTED_SYMBOL_DAYS)
    require("stage1 files_failed", batch.get("files_failed", 0), 0)
    if batch.get("failures"):
        problems.append(f"stage1 recorded {len(batch['failures'])} failures")

    # --- Stage-1 physical files ---------------------------------------------
    manifests = sorted((features_dir / "manifests").glob("*.manifest.json"))
    require("stage1 manifests present", len(manifests), EXPECTED_STAGE1_MANIFESTS)

    parquets = [p for c in CADENCES for p in (features_dir / c.name).glob("*.parquet")]
    require("cadence parquets present", len(parquets), EXPECTED_CADENCE_PARQUETS)
    for cadence in CADENCES:
        count = len(list((features_dir / cadence.name).glob("*.parquet")))
        require(f"{cadence.name} parquets", count, EXPECTED_SYMBOL_DAYS)

    label_files = sorted(labels_dir.glob("*.labels.parquet"))
    require("label files present", len(label_files), EXPECTED_LABEL_FILES)

    # --- the universe those files describe ----------------------------------
    stems = sorted({p.name.split(".")[0] for p in manifests})
    require("symbol-days", len(stems), EXPECTED_SYMBOL_DAYS)
    by_date: dict[str, set[str]] = {}
    for stem in stems:
        symbol, _, session_date = stem.rpartition("_")
        by_date.setdefault(session_date, set()).add(symbol)
    require("session dates", len(by_date), EXPECTED_SESSION_DATES)
    for session_date, symbols in sorted(by_date.items()):
        if len(symbols) != EXPECTED_SYMBOLS_PER_DATE:
            problems.append(
                f"{session_date}: {len(symbols)} symbols, expected "
                f"{EXPECTED_SYMBOLS_PER_DATE}"
            )

    # Every declared symbol-day must have its label file and its cadence files.
    label_stems = {p.name.split(".")[0] for p in label_files}
    missing_labels = sorted(set(stems) - label_stems)
    if missing_labels:
        problems.append(f"symbol-days with no label file: {missing_labels[:5]}")
    for cadence in CADENCES:
        present = {p.name.split(".")[0] for p in (features_dir / cadence.name).glob("*.parquet")}
        missing = sorted(set(stems) - present)
        if missing:
            problems.append(f"{cadence.name}: missing symbol-days {missing[:5]}")

    # --- Stage-2 grams declarations -----------------------------------------
    grams_manifest_path = grams_dir / "stage2_grams_manifest.json"
    if not grams_manifest_path.is_file():
        raise ValueError(f"no Stage-2 grams manifest at {grams_manifest_path}")
    grams_manifest = json.loads(grams_manifest_path.read_text(encoding="utf-8"))
    require(
        "stage2 symbol_day_cadence_files",
        grams_manifest.get("symbol_day_cadence_files"),
        EXPECTED_CADENCE_PARQUETS,
    )
    require(
        "stage2 spine_certified_files",
        grams_manifest.get("spine_certified_files"),
        EXPECTED_CADENCE_PARQUETS,
    )
    require(
        "stage2 spine_verified_every_file",
        (grams_manifest.get("label_reuse") or {}).get("spine_verified_every_file"),
        True,
    )
    require(
        "stage2 session_date_count",
        grams_manifest.get("session_date_count"),
        EXPECTED_SESSION_DATES,
    )

    # --- the physical label batch manifest -----------------------------------
    #
    # The grams manifest records what Stage 2 saw. The label manifest records
    # what is on disk now. Reading either alone proves half of it; only
    # comparing them proves they are the same artefact.
    label_manifest_path = labels_dir / "label_batch_manifest.json"
    if not label_manifest_path.is_file():
        raise ValueError(f"no label batch manifest at {label_manifest_path}")
    label_manifest = json.loads(label_manifest_path.read_text(encoding="utf-8"))

    require(
        "label symbol_days_discovered",
        label_manifest.get("symbol_days_discovered"),
        EXPECTED_SYMBOL_DAYS,
    )
    require(
        "label symbol_days_completed",
        label_manifest.get("symbol_days_completed"),
        EXPECTED_SYMBOL_DAYS,
    )
    require("label symbol_days_failed", label_manifest.get("symbol_days_failed"), 0)
    require("label failures", list(label_manifest.get("failures") or []), [])
    require(
        "label contains_predictive_result",
        label_manifest.get("contains_predictive_result"),
        False,
    )

    declared_label_hash = label_manifest.get("label_definition_hash")
    grams_declared = (grams_manifest.get("provenance") or {}).get("labels_declared_hash")
    if declared_label_hash != grams_declared:
        problems.append(
            "the label batch manifest on disk declares "
            f"label_definition_hash={declared_label_hash!r}, but the Stage-2 grams "
            f"manifest recorded labels_declared_hash={grams_declared!r}; these are "
            "not the labels the Grams were certified against"
        )

    # The declared definition must be one this programme accepts: the current
    # one, or a superseded one that was explicitly recorded as not having
    # changed label content.
    accepted_label_hashes = {LABEL_DEFINITION_HASH}
    for entry in SUPERSEDED_LABEL_DEFINITION_HASHES:
        if entry.get("label_content_changed") == "false":
            accepted_label_hashes.add(entry["label_definition_hash"])
    if declared_label_hash not in accepted_label_hashes:
        problems.append(
            f"label_definition_hash {declared_label_hash!r} is neither the current "
            "accepted definition nor a superseded one recorded with "
            "label_content_changed='false'"
        )

    accepted_plan_hashes = {STAGE2_PLAN_HASH} | {
        entry["plan_hash"] for entry in SUPERSEDED_PLAN_HASHES
    }
    if label_manifest.get("stage2_plan_hash") not in accepted_plan_hashes:
        problems.append(
            f"the label batch was built against Stage-2 plan hash "
            f"{label_manifest.get('stage2_plan_hash')!r}, which is neither the "
            "current plan nor an accepted superseded one"
        )

    # --- hashes agree with the frozen artefacts ------------------------------
    provenance = grams_manifest.get("provenance") or {}
    require(
        "grams feature_semantics_hash",
        provenance.get("feature_semantics_hash"),
        FEATURE_SEMANTICS_HASH,
    )
    require(
        "grams label_definition_hash",
        provenance.get("label_definition_hash"),
        LABEL_DEFINITION_HASH,
    )
    require("grams stage2_plan_hash", provenance.get("stage2_plan_hash"), STAGE2_PLAN_HASH)
    require("stage2 results plan_hash", stage2_results.get("plan_hash"), STAGE2_PLAN_HASH)

    if problems:
        raise ValueError(
            "the supplied batch is not the completed Stage-1/Stage-2 batch, so "
            "Stage 3 will not compute economics over a universe nobody declared:"
            + "".join(f"\n  - {p}" for p in problems)
        )

    return {
        "symbol_days": len(stems),
        "session_dates": len(by_date),
        "symbols_per_session_date": EXPECTED_SYMBOLS_PER_DATE,
        "cadence_parquets": len(parquets),
        "label_files": len(label_files),
        "stage1_manifests": len(manifests),
        "stage2_symbol_day_cadence_files": grams_manifest["symbol_day_cadence_files"],
        "stage2_spine_certified_files": grams_manifest["spine_certified_files"],
        "stage2_spine_verified_every_file": True,
        "label_batch": {
            "symbol_days_discovered": label_manifest["symbol_days_discovered"],
            "symbol_days_completed": label_manifest["symbol_days_completed"],
            "symbol_days_failed": label_manifest["symbol_days_failed"],
            "label_definition_hash": declared_label_hash,
            "matches_grams_declared_hash": True,
            "reused_under_supersession": declared_label_hash != LABEL_DEFINITION_HASH,
        },
        "verified_against": (
            "stage1 batch manifest + stage2 grams manifest + label batch "
            "manifest + disk"
        ),
    }
