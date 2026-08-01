"""Deterministic execution simulation for a frozen strategy family.

The evidence gates already in place judge *trades*: whether the decision
preceded the fill, whether the correct side of the spread was crossed, whether
costs were charged, whether the edge survives stressing them.  Nothing was
producing those trades.  A factor observation is not a trade -- it has no
position limit, no capital, no spread to cross and no competitor for the same
slot -- so this is where a confirmed statistical edge is forced to become
something an account could actually have done.

Three things are deliberately harsher here than in the factor diagnostics:

* Capacity is finite.  A factor may fire on forty symbols at 10:00; a book
  with room for five takes five.  Which five is decided by a rule fixed in
  the recipe, never by which turned out well.
* Both sides of the spread are paid.  The factor measures open-to-close on
  the mid; a long buys the offer and sells the bid.
* A position that cannot be closed inside its session is not opened.

The module contains no campaign, broker, order-submission, or UI code.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Sequence

from app.services.intraday_research_integrity import estimated_round_trip_cost_bps
from app.services.intraday_session_calendar import bar_slot

SIMULATION_VERSION = "intraday_strategy_simulation_v1"

# Ranking rule for competing signals in the same bar, fixed here rather than
# chosen per run: the strongest score wins the scarce slot. Any rule that
# consulted the outcome would be selecting winners after the fact.
ENTRY_PRIORITY = "highest_absolute_score"


def _round(value: float | None, places: int = 8) -> float | None:
    return round(float(value), places) if value is not None else None


def _session_date(timestamp: datetime) -> date:
    from app.services.intraday_research_integrity import exchange_session_date

    return exchange_session_date(timestamp)


class _Book:
    """Tracks open positions so capacity is a real constraint, not a note."""

    def __init__(self, *, max_concurrent: int, max_gross_exposure: float):
        self.max_concurrent = max_concurrent
        self.max_gross_exposure = max_gross_exposure
        self._open: list[dict[str, Any]] = []

    def release(self, now: datetime) -> None:
        # A position occupies the book through its exit bar inclusive. A
        # one-bar hold enters at that bar's open and exits at its close, so
        # releasing it before another signal in the same bar would let the
        # book hold more positions at once than the recipe permits.
        self._open = [row for row in self._open if row["exit_timestamp"] >= now]

    def gross(self) -> float:
        return sum(row["size_fraction"] for row in self._open)

    def can_open(self, *, size_fraction: float, symbol: str) -> bool:
        if len(self._open) >= self.max_concurrent:
            return False
        if self.gross() + size_fraction > self.max_gross_exposure + 1e-12:
            return False
        # One position per symbol: two entries in the same name are one
        # concentrated bet wearing two tickets.
        return all(row["symbol"] != symbol for row in self._open)

    def open(self, position: dict[str, Any]) -> None:
        self._open.append(position)


def simulate_family(
    observations: Sequence[dict[str, Any]],
    *,
    recipe: Any,
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    cost_model: dict[str, Any],
    capital: float,
    stressed: bool = True,
) -> dict[str, Any]:
    """Turn factor observations into trades an account could have taken.

    ``observations`` come from the same builder the confirmation run used, so
    the entry timing, horizon and score sign are the confirmed ones and are
    not re-derived here.
    """
    eligible_symbols = set(recipe.eligible_symbols or ())
    eligible_slots = set(recipe.eligible_session_slots or ())
    bars_by_symbol = {
        str(symbol).upper(): {row["timestamp"]: row for row in rows}
        for symbol, rows in candles_by_symbol.items()
    }

    ordered = sorted(
        observations,
        key=lambda row: (
            row["entry_bar_timestamp"],
            -abs(float(row["score"])),
            str(row["symbol"]),
        ),
    )
    book = _Book(
        max_concurrent=recipe.max_concurrent_positions,
        max_gross_exposure=recipe.max_gross_exposure,
    )

    trades: list[dict[str, Any]] = []
    skipped: dict[str, int] = {
        "ineligible_symbol": 0,
        "ineligible_slot": 0,
        "no_capacity": 0,
        "missing_bar": 0,
        "would_cross_session_close": 0,
        "wrong_direction": 0,
    }

    for row in ordered:
        symbol = str(row["symbol"]).upper()
        entry_timestamp = row["entry_bar_timestamp"]
        exit_timestamp = row["exit_bar_timestamp"]
        book.release(entry_timestamp)

        if eligible_symbols and symbol not in eligible_symbols:
            skipped["ineligible_symbol"] += 1
            continue
        if eligible_slots and bar_slot(entry_timestamp) not in eligible_slots:
            skipped["ineligible_slot"] += 1
            continue

        entry_bar = bars_by_symbol.get(symbol, {}).get(entry_timestamp)
        exit_bar = bars_by_symbol.get(symbol, {}).get(exit_timestamp)
        signal_bar = bars_by_symbol.get(symbol, {}).get(row["signal_bar_timestamp"])
        if entry_bar is None or exit_bar is None or signal_bar is None:
            skipped["missing_bar"] += 1
            continue

        entry_session = _session_date(entry_timestamp)
        exit_session = _session_date(exit_timestamp)
        if exit_session != entry_session and not recipe.forced_session_close_exit:
            skipped["would_cross_session_close"] += 1
            continue
        if exit_session != entry_session:
            # The recipe forbids carrying the position, and shortening the
            # horizon would be a different strategy from the confirmed one.
            skipped["would_cross_session_close"] += 1
            continue

        # The score already carries the profitable direction; the recipe's
        # declared direction filters, it never flips.
        score = float(row["score"])
        side = "long" if score > 0 else "short"
        if recipe.direction in ("long", "short") and side != recipe.direction:
            skipped["wrong_direction"] += 1
            continue

        size_fraction = recipe.position_size_fraction
        if not book.can_open(size_fraction=size_fraction, symbol=symbol):
            skipped["no_capacity"] += 1
            continue

        cost_bps = estimated_round_trip_cost_bps(
            cost_model, symbol=symbol, timestamp=entry_timestamp, stressed=stressed
        )
        # The quoted spread is modelled from the cost calibration rather than
        # observed: quotes do not exist for the whole history. It is applied
        # as a real price, not as a footnote -- a long lifts the offer.
        half_spread = cost_bps / 2 / 10_000
        mid_entry = float(entry_bar["open"])
        mid_exit = float(exit_bar["close"])
        if mid_entry <= 0 or mid_exit <= 0:
            skipped["missing_bar"] += 1
            continue
        ask = mid_entry * (1 + half_spread)
        bid = mid_entry * (1 - half_spread)
        entry_price = ask if side == "long" else bid
        exit_price = (
            mid_exit * (1 - half_spread) if side == "long" else mid_exit * (1 + half_spread)
        )

        gross = (mid_exit - mid_entry) / mid_entry
        if side == "short":
            gross = -gross
        realized = (
            (exit_price - entry_price) / entry_price
            if side == "long"
            else (entry_price - exit_price) / entry_price
        )

        shares = (capital * size_fraction) / entry_price if entry_price > 0 else 0.0
        entry_volume = float(entry_bar.get("volume") or 0)
        participation = shares / entry_volume if entry_volume > 0 else None

        trades.append(
            {
                "factor_key": recipe.factor_key,
                "symbol": symbol,
                "side": side,
                "score": _round(score),
                "signal_bar_timestamp": row["signal_bar_timestamp"],
                "signal_close": _round(float(signal_bar["close"]), 6),
                "decision_timestamp": row["decision_timestamp"],
                "entry_timestamp": entry_timestamp,
                "exit_timestamp": exit_timestamp,
                "entry_session_date": entry_session,
                "exit_session_date": exit_session,
                "overnight_declared": False,
                "bid": _round(bid, 6),
                "ask": _round(ask, 6),
                "entry_price": _round(entry_price, 6),
                "exit_price": _round(exit_price, 6),
                "gross_return": _round(gross),
                # Net of the spread actually crossed on both sides. The cost
                # is in the prices, not subtracted from a mid-to-mid return.
                "net_return": _round(realized),
                "cost_bps": _round(cost_bps, 4),
                "size_fraction": size_fraction,
                "shares": _round(shares, 4),
                "participation_rate": _round(participation),
                "execution_evidence_present": True,
                "simulation_version": SIMULATION_VERSION,
            }
        )
        book.open(
            {
                "symbol": symbol,
                "exit_timestamp": exit_timestamp,
                "size_fraction": size_fraction,
            }
        )

    return {
        "simulation_version": SIMULATION_VERSION,
        "factor_key": recipe.factor_key,
        "recipe_hash": recipe.recipe_hash(),
        "entry_priority": ENTRY_PRIORITY,
        "capital": capital,
        "cost_scenario": "stressed_p90" if stressed else "observed_median",
        "observations": len(observations),
        "trades": trades,
        "trade_count": len(trades),
        # A large skip count is not a defect; it is the capacity constraint
        # doing its job, and it is reported so the shortfall between a
        # factor's events and a book's trades is visible rather than implied.
        "skipped": skipped,
        "fill_rate": (
            _round(len(trades) / len(observations), 6) if observations else None
        ),
    }
