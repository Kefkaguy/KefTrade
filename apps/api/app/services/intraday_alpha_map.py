"""Alpha cartography: measure information before constructing a strategy.

Every other intraday research module in this codebase takes a strategy as its
unit of work -- a factor, a threshold, a direction, a holding period -- and asks
whether that strategy is profitable.  That ordering is backwards, and it is
expensive in a specific way: a hypothesis with no forecast in it still consumes
a simulation, a qualification, a cost calibration and a Paper Lab session before
anything reports the one fact that mattered, which is that there was never
anything to trade.

This module asks the prior question and refuses to ask any other:

    Does this feature predict any future return?
    At what horizon?  For which symbols, at which times, in which states?
    By how many basis points?
    Is that materially larger than the cost of harvesting it?
    Does it hold up out of sample once every look is charged for?

There is no threshold, no entry rule, no stop, no target, no position size and
no P/L anywhere in this file.  Those are strategy-construction decisions and
they belong strictly downstream of a cell verdict.

Four design choices carry most of the weight.

**Horizons are measured on a finer grid than the signal.**  A 30m bar can only
express 30/60/90-minute questions, so a 30m research primitive cannot discover
that a feature's information decays in four minutes.  It reports a failure at
30m and the finding -- short-lived continuation followed by reversal -- never
appears.  Forward returns here are measured on a 1m grid (or finer, if a finer
one is ever ingested) against a horizon ladder in *seconds*.

**Features are normalized per symbol and time-of-day before they are compared.**
An absolute cut like ``imbalance < -0.20`` asserts that a given imbalance means
the same thing in AAPL at 09:45 as in VZ at 15:15.  It does not.  Every feature
is therefore also measured as an expanding-window z-score and percentile within
its own (symbol, time-of-day) history, using strictly prior observations only.

**Features are decomposed into market, sector and idiosyncratic parts.**  When
market-wide flow turns negative, a raw per-symbol signal fires on eight symbols
at once and produces one market bet wearing eight tickers.  The idiosyncratic
residual is measured as its own transform so the difference between "this stock
is doing something" and "everything is doing something" is visible rather than
implied.

**Cost is a gate at the front, not an adjustment at the end.**  A cell whose
conditional move is smaller than its round-trip cost times a safety multiple is
killed as ``information_below_cost`` -- a different finding from
``no_information``, with a different follow-up -- and the horizon preflight
kills horizons where even a perfect forecast could not clear the hurdle, before
any feature is scored against them at all.

The module contains no campaign, broker, order-submission, or UI code.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
from itertools import combinations
from json import dumps
from math import erfc, isfinite, log, sqrt
from statistics import fmean, pstdev
from typing import Any, Iterable, Iterator, Sequence
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from app.services.intraday_factor_diagnostics import (
    benjamini_hochberg,
    load_cost_model,
    sector_map,
)
from app.services.intraday_research_integrity import (
    clustered_outcome_statistics,
    estimated_round_trip_cost_bps,
    exchange_session_date,
)
from app.services.intraday_session_calendar import (
    bar_close_timestamp,
    bar_slot,
    regular_session_slots,
    timeframe_minutes,
)
from app.services.intraday_trial_ledger import declare_trials, effective_trials_for_run
from app.services.research_architecture import jsonable
from app.services.research_splits import get_dataset_splits, record_split_access

ALPHA_MAP_VERSION = "intraday_alpha_map_v1_pre_strategy_cartography"

# Seconds, deliberately dense at the short end.  Order-flow information is
# documented to concentrate near the flow itself and decay quickly; a ladder
# that starts at 15m cannot see that shape, and a ladder that starts at 30m
# cannot even see that it is missing it.
DEFAULT_HORIZONS_SECONDS = (60, 120, 300, 600, 900, 1_800, 3_600)

# Sub-minute rungs are declared here rather than omitted so the availability
# report names them explicitly.  They resolve only if a sub-minute bar grid is
# ever ingested; until then the run says "unavailable, and here is what it
# would take", which is more useful than silence.
SUB_MINUTE_HORIZONS_SECONDS = (10, 30)

TRANSFORMS = (
    "raw",
    "zscore_symbol_tod",
    "percentile_symbol_tod",
    "idiosyncratic",
)

# A cell needs enough observations for a decile table to mean anything and
# enough distinct sessions that one unusual day cannot carry it.
MINIMUM_CELL_OBSERVATIONS = 200
MINIMUM_CELL_SESSIONS = 20
MINIMUM_NORMALIZATION_HISTORY = 20
MINIMUM_IC_T_STATISTIC = 3.0
MINIMUM_MONOTONICITY = 0.70
DEFAULT_COST_SAFETY_MULTIPLE = 2.0
DEFAULT_BUCKETS = 10
CSCV_BLOCKS = 8

VERDICT_INSUFFICIENT = "insufficient_data"
VERDICT_NO_INFORMATION = "no_information"
VERDICT_BELOW_COST = "information_below_cost"
VERDICT_UNSTABLE = "unstable"
VERDICT_TRADABLE = "tradable_candidate"


# ---------------------------------------------------------------------------
# Small statistics helpers
#
# These are local rather than imported from a stats package because every one
# of them has to survive ties, constant inputs and n<2 without raising.  A
# research pass over a thousand cells will contain degenerate cells, and the
# correct response to a degenerate cell is a None, not an exception that loses
# the other 999.
# ---------------------------------------------------------------------------


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _round(value: float | None, places: int = 6) -> float | None:
    return round(float(value), places) if value is not None else None


def ranks(values: Sequence[float]) -> list[float]:
    """Average ranks, so tied feature values cannot fabricate an ordering."""
    order = sorted(range(len(values)), key=lambda index: values[index])
    output = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        average = (position + end) / 2 + 1
        for index in range(position, end + 1):
            output[order[index]] = average
        position = end + 1
    return output


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mean_x = fmean(xs)
    mean_y = fmean(ys)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    variance_x = sum((x - mean_x) ** 2 for x in xs)
    variance_y = sum((y - mean_y) ** 2 for y in ys)
    if variance_x <= 0 or variance_y <= 0:
        return None
    return covariance / sqrt(variance_x * variance_y)


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    return pearson(ranks(xs), ranks(ys))


def t_statistic(values: Sequence[float]) -> float | None:
    """One-sample t against zero.  None when dispersion cannot be estimated."""
    if len(values) < 2:
        return None
    mean = fmean(values)
    deviation = pstdev(values)
    if deviation <= 0:
        return None
    return mean / (deviation / sqrt(len(values)))


def percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _two_sided_p_from_t(statistic: float | None, sample: int) -> float | None:
    """Normal approximation to the two-sided p-value of a t statistic.

    The sample here is a count of trading days, which is in the hundreds for
    any run that clears the minimum-session gate, so the normal tail is close
    enough and avoids a scipy dependency this codebase does not otherwise
    carry.
    """
    if statistic is None or sample < 2:
        return None
    return min(1.0, float(erfc(abs(statistic) / sqrt(2.0))))


# ---------------------------------------------------------------------------
# Horizon ladder
# ---------------------------------------------------------------------------


def horizon_availability(
    horizons_seconds: Sequence[int],
    *,
    grid_seconds: int,
) -> dict[str, Any]:
    """Which requested horizons the measurement grid can actually resolve.

    A horizon shorter than one grid bar is not "approximately one bar", it is
    unmeasurable, and rounding it up would report a 60-second result under a
    10-second label.  Those rungs are refused by name.
    """
    if grid_seconds <= 0:
        raise ValueError("grid_seconds must be positive")
    available: list[int] = []
    unavailable: dict[str, str] = {}
    for horizon in sorted({int(item) for item in horizons_seconds}):
        if horizon <= 0:
            unavailable[f"{horizon}s"] = "non_positive_horizon"
        elif horizon < grid_seconds:
            unavailable[f"{horizon}s"] = (
                f"below_measurement_grid: requires bars of at most {horizon}s; "
                f"the finest ingested grid is {grid_seconds}s"
            )
        elif horizon % grid_seconds:
            unavailable[f"{horizon}s"] = (
                f"not_on_measurement_grid: {horizon}s is not a multiple of {grid_seconds}s"
            )
        else:
            available.append(horizon)
    return {
        "grid_seconds": grid_seconds,
        "available_seconds": available,
        "unavailable": unavailable,
    }


def forward_return_ladder(
    grid_rows: Sequence[dict[str, Any]],
    *,
    decision_timestamp: datetime,
    horizons_seconds: Sequence[int],
    grid_seconds: int,
    session_close_timestamp: datetime | None,
) -> dict[int, dict[str, Any]]:
    """Executable forward returns from one decision instant.

    Entry is the open of the first grid bar at or after the decision instant,
    never the close of the bar that produced the signal: the signal is not
    knowable until that bar has closed, so its own close is not a price anyone
    could have traded at.  Exit is the close of the last grid bar that ends at
    or before ``decision + horizon``.

    ``grid_rows`` must be one symbol's bars for one exchange session date, in
    chronological order.  Grouping by session is not a convenience -- carrying
    the ladder across an overnight gap would score an overnight move as a
    five-minute forecast.

    ``session_close_timestamp`` is the hard boundary, and grouping by date is
    not a substitute for it.  A frozen 1m grid built from a SIP feed contains
    after-hours bars, and they sit on the same exchange date and are perfectly
    contiguous with the regular session, so neither the date grouping nor the
    gap check excludes them: a 15:30 decision would happily "hold" to 16:30
    through bars nobody in this strategy's universe was going to trade.  Every
    rung is therefore required to close at or before the session close.

    The boundary must be passed in rather than derived from a clock here.  It
    comes from ``minutes_to_close`` on the frozen signal feature, which the
    feature engine computes as ``market_close - bar_timestamp`` against the
    XNYS calendar -- so it is already correct on early-close days, which a
    hardcoded 16:00 would not be.
    """
    if session_close_timestamp is None:
        # Refusing rather than assuming a close: guessing 16:00 would be wrong
        # on exactly the sessions where being wrong is hardest to notice.
        return {
            int(horizon): {"available": False, "reason": "session_close_unknown"}
            for horizon in horizons_seconds
        }
    if decision_timestamp >= session_close_timestamp:
        # The last signal bar of a session decides exactly at the close, so
        # there is no time left to hold anything. This is a property of the
        # observation, not of any particular horizon.
        return {
            int(horizon): {"available": False, "reason": "decision_at_or_after_session_close"}
            for horizon in horizons_seconds
        }
    timestamps = [row["timestamp"] for row in grid_rows]
    entry_index = bisect_left(timestamps, decision_timestamp)
    result: dict[int, dict[str, Any]] = {}
    if entry_index >= len(grid_rows):
        return {
            int(horizon): {"available": False, "reason": "no_grid_bar_after_decision"}
            for horizon in horizons_seconds
        }
    entry_price = _finite(grid_rows[entry_index].get("open"))
    if entry_price is None or entry_price <= 0:
        return {
            int(horizon): {"available": False, "reason": "missing_entry_price"}
            for horizon in horizons_seconds
        }
    for horizon in horizons_seconds:
        horizon = int(horizon)
        if horizon < grid_seconds or horizon % grid_seconds:
            result[horizon] = {"available": False, "reason": "horizon_not_on_grid"}
            continue
        bars = horizon // grid_seconds
        exit_index = entry_index + bars - 1
        if exit_index >= len(grid_rows):
            result[horizon] = {"available": False, "reason": "session_end_before_horizon"}
            continue
        path = grid_rows[entry_index : exit_index + 1]
        # A gap in the grid makes the elapsed time longer than the horizon, so
        # the rung would silently measure a longer forecast than it claims.
        elapsed = (path[-1]["timestamp"] - path[0]["timestamp"]).total_seconds()
        if elapsed != (bars - 1) * grid_seconds:
            result[horizon] = {"available": False, "reason": "gap_in_measurement_grid"}
            continue
        # Measured from the exit bar's own close rather than from
        # `decision + horizon`, so a grid whose first bar starts after the
        # decision instant is still bounded by where the position actually
        # ends up rather than by where it was supposed to.
        if path[-1]["timestamp"] + timedelta(seconds=grid_seconds) > session_close_timestamp:
            result[horizon] = {"available": False, "reason": "session_end_before_horizon"}
            continue
        exit_price = _finite(path[-1].get("close"))
        highs = [_finite(row.get("high")) for row in path]
        lows = [_finite(row.get("low")) for row in path]
        if exit_price is None or any(value is None for value in highs + lows):
            result[horizon] = {"available": False, "reason": "missing_ohlc"}
            continue
        gross = exit_price / entry_price - 1
        result[horizon] = {
            "available": True,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "gross_return": gross,
            "gross_return_bps": gross * 10_000,
            "max_favourable_bps": (max(float(v) for v in highs) / entry_price - 1) * 10_000,
            "max_adverse_bps": (min(float(v) for v in lows) / entry_price - 1) * 10_000,
            "bars": bars,
        }
    return result


# ---------------------------------------------------------------------------
# Feature transforms
# ---------------------------------------------------------------------------


def expanding_normalization(
    observations: Sequence[dict[str, Any]],
    *,
    feature: str,
    minimum_history: int = MINIMUM_NORMALIZATION_HISTORY,
) -> None:
    """Attach (symbol, time-of-day) z-score and percentile, in place.

    Each observation is normalized against *strictly prior* observations for
    the same symbol and the same exchange-local bar slot.  Using the full-sample
    mean and deviation instead would leak the future into the signal and make
    the whole map look better than it is -- the failure is subtle because the
    leak is only a few basis points of scaling, and it flatters exactly the
    extreme buckets the verdict depends on.

    An observation without enough prior history gets ``None`` rather than a
    normalization computed from four points, because a z-score over four points
    is mostly a statement about those four points.
    """
    history: dict[tuple[str, str], list[float]] = defaultdict(list)
    for observation in sorted(observations, key=lambda row: row["timestamp"]):
        raw = _finite((observation.get("features") or {}).get(feature))
        key = (str(observation["symbol"]).upper(), str(observation["slot"]))
        prior = history[key]
        transforms = observation.setdefault("transforms", {})
        if raw is None or len(prior) < minimum_history:
            transforms[f"{feature}::zscore_symbol_tod"] = None
            transforms[f"{feature}::percentile_symbol_tod"] = None
        else:
            mean = fmean(prior)
            deviation = pstdev(prior)
            transforms[f"{feature}::zscore_symbol_tod"] = (
                (raw - mean) / deviation if deviation > 0 else 0.0
            )
            below = sum(1 for value in prior if value < raw)
            ties = sum(1 for value in prior if value == raw)
            transforms[f"{feature}::percentile_symbol_tod"] = (
                (below + 0.5 * ties) / len(prior)
            )
        transforms[f"{feature}::raw"] = raw
        if raw is not None:
            prior.append(raw)


def residualize_cross_section(
    observations: Sequence[dict[str, Any]],
    *,
    feature: str,
    sectors: dict[str, str] | None = None,
    minimum_peers: int = 4,
) -> dict[str, Any]:
    """Split a feature into market, sector and idiosyncratic parts, in place.

    The decomposition is a same-instant cross-sectional mean removal: the
    market component is the mean across every symbol observed at that instant,
    the sector component is the residual mean within the symbol's sector, and
    what is left is idiosyncratic.

    This exists because a per-symbol signal that fires on eight names when
    market-wide flow turns is one bet, not eight, and nothing downstream can
    tell the difference from the raw value alone.  The returned variance shares
    say how much of the feature was ever specific to the symbol.
    """
    by_timestamp: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for observation in observations:
        by_timestamp[observation["timestamp"]].append(observation)
    sectors = {key.upper(): value for key, value in (sectors or {}).items()}

    raw_values: list[float] = []
    market_values: list[float] = []
    idiosyncratic_values: list[float] = []
    instants_with_peers = 0
    for timestamp in sorted(by_timestamp):
        rows = by_timestamp[timestamp]
        usable = [
            (row, value)
            for row in rows
            if (value := _finite((row.get("features") or {}).get(feature))) is not None
        ]
        if len(usable) < minimum_peers:
            # Too thin a cross-section to separate "this stock" from
            # "everything": leaving the residual undefined is the honest
            # answer, and it keeps a two-symbol instant from producing a
            # residual that is really just half the spread between them.
            for row, _ in usable:
                row.setdefault("transforms", {})[f"{feature}::idiosyncratic"] = None
            continue
        instants_with_peers += 1
        market = fmean(value for _, value in usable)
        by_sector: dict[str, list[float]] = defaultdict(list)
        for row, value in usable:
            by_sector[sectors.get(str(row["symbol"]).upper(), "UNKNOWN")].append(value)
        sector_component = {
            name: fmean(values) - market if len(values) >= 2 else 0.0
            for name, values in by_sector.items()
        }
        for row, value in usable:
            sector = sector_component[sectors.get(str(row["symbol"]).upper(), "UNKNOWN")]
            residual = value - market - sector
            row.setdefault("transforms", {})[f"{feature}::idiosyncratic"] = residual
            raw_values.append(value)
            market_values.append(market + sector)
            idiosyncratic_values.append(residual)

    raw_variance = pstdev(raw_values) ** 2 if len(raw_values) > 1 else 0.0
    idiosyncratic_variance = (
        pstdev(idiosyncratic_values) ** 2 if len(idiosyncratic_values) > 1 else 0.0
    )
    common_variance = pstdev(market_values) ** 2 if len(market_values) > 1 else 0.0
    return {
        "feature": feature,
        "instants_decomposed": instants_with_peers,
        "observations_decomposed": len(raw_values),
        "raw_variance": _round(raw_variance),
        "common_variance_share": _round(
            common_variance / raw_variance if raw_variance > 0 else None
        ),
        "idiosyncratic_variance_share": _round(
            idiosyncratic_variance / raw_variance if raw_variance > 0 else None
        ),
    }


def cross_sectional_dependence(
    observations: Sequence[dict[str, Any]],
    *,
    feature: str,
    transform: str = "raw",
) -> dict[str, Any]:
    """How much of a firing set is one market-level bet wearing many tickers.

    ``same_sign_share`` is the headline: if 90% of the symbols observed at an
    instant carry the same signal sign, then a rule reading that signal opens
    correlated positions and the apparent diversification of "eight names" is
    arithmetic, not risk reduction.
    """
    key = f"{feature}::{transform}"
    by_timestamp: dict[datetime, list[float]] = defaultdict(list)
    for observation in observations:
        value = _finite((observation.get("transforms") or {}).get(key))
        if value is not None:
            by_timestamp[observation["timestamp"]].append(value)

    same_sign_shares: list[float] = []
    breadth: list[int] = []
    for values in by_timestamp.values():
        if len(values) < 2:
            continue
        positives = sum(1 for value in values if value > 0)
        negatives = sum(1 for value in values if value < 0)
        same_sign_shares.append(max(positives, negatives) / len(values))
        breadth.append(len(values))
    if not same_sign_shares:
        return {
            "feature": feature,
            "transform": transform,
            "instants": 0,
            "reason": "no instant carried two comparable symbols",
        }
    average_same_sign = fmean(same_sign_shares)
    return {
        "feature": feature,
        "transform": transform,
        "instants": len(same_sign_shares),
        "mean_symbols_per_instant": _round(fmean(breadth)),
        "same_sign_share": _round(average_same_sign),
        # Under independence the expected majority share of n coin flips is
        # near 0.5 for wide cross-sections; anything approaching 1.0 means the
        # signal is measuring the market.
        "effective_independent_bets": _round(
            fmean(breadth) * (2 * (1 - average_same_sign)) if average_same_sign < 1 else 1.0
        ),
        "pseudo_diversification_warning": bool(average_same_sign >= 0.80),
    }


# ---------------------------------------------------------------------------
# Cell measurement
# ---------------------------------------------------------------------------


def bucket_profile(
    pairs: Sequence[tuple[float, float]],
    *,
    buckets: int = DEFAULT_BUCKETS,
) -> list[dict[str, Any]]:
    """Equal-count buckets of the feature with their mean forward return.

    Bucketing is what distinguishes a real signal from a fitted threshold: a
    feature that only pays in the top decile and does nothing monotone below it
    is describing a handful of observations, not a relationship.
    """
    usable = [(x, y) for x, y in pairs if _finite(x) is not None and _finite(y) is not None]
    if len(usable) < buckets * 3:
        buckets = max(2, len(usable) // 3)
    if buckets < 2 or not usable:
        return []
    ordered = sorted(usable, key=lambda item: item[0])
    size = len(ordered) / buckets
    profile: list[dict[str, Any]] = []
    for index in range(buckets):
        start = int(round(index * size))
        end = int(round((index + 1) * size))
        chunk = ordered[start:end]
        if not chunk:
            continue
        forwards = [y for _, y in chunk]
        median = percentile(forwards, 0.5)
        profile.append(
            {
                "bucket": index,
                "observations": len(chunk),
                "feature_min": _round(chunk[0][0]),
                "feature_max": _round(chunk[-1][0]),
                "mean_forward_bps": _round(fmean(forwards) * 10_000, 4),
                "median_forward_bps": _round(median * 10_000 if median is not None else None, 4),
                "hit_rate": _round(sum(1 for value in forwards if value > 0) / len(forwards)),
            }
        )
    return profile


def monotonicity(profile: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Does the response progress with the feature, or only spike somewhere.

    Reported as the rank correlation between bucket index and bucket mean,
    plus the fraction of consecutive steps moving in the dominant direction.
    A signal that pays only in one interior bucket scores near zero here and is
    treated as noise regardless of how large that bucket's mean is.
    """
    means = [row["mean_forward_bps"] for row in profile if row.get("mean_forward_bps") is not None]
    if len(means) < 3:
        return {"rank_correlation": None, "monotone_step_fraction": None, "buckets": len(means)}
    indices = [float(index) for index in range(len(means))]
    correlation = spearman(indices, means)
    steps = [means[index + 1] - means[index] for index in range(len(means) - 1)]
    positive = sum(1 for step in steps if step > 0)
    negative = sum(1 for step in steps if step < 0)
    return {
        "rank_correlation": _round(correlation),
        "monotone_step_fraction": _round(max(positive, negative) / len(steps)) if steps else None,
        "buckets": len(means),
    }


def session_clustered_ic(
    rows: Sequence[dict[str, Any]],
    *,
    minimum_per_session: int = 3,
) -> dict[str, Any]:
    """Rank IC computed within each session, then averaged across sessions.

    Pooling every observation into one correlation would treat 40 symbols on
    one shocked day as 40 independent observations.  Computing the correlation
    inside a session and then treating sessions as the unit of inference is the
    same clustering the rest of this codebase applies to returns.
    """
    by_session: dict[date, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        by_session[row["session_date"]].append((row["value"], row["forward"]))
    per_session: list[float] = []
    for session in sorted(by_session):
        pairs = by_session[session]
        if len(pairs) < minimum_per_session:
            continue
        correlation = spearman([x for x, _ in pairs], [y for _, y in pairs])
        if correlation is not None:
            per_session.append(correlation)
    if not per_session:
        return {
            "rank_ic": None,
            "t_statistic": None,
            "p_value": None,
            "sessions_scored": 0,
            "positive_session_share": None,
        }
    statistic = t_statistic(per_session)
    return {
        "rank_ic": _round(fmean(per_session)),
        "t_statistic": _round(statistic),
        "p_value": _round(_two_sided_p_from_t(statistic, len(per_session))),
        "sessions_scored": len(per_session),
        "positive_session_share": _round(
            sum(1 for value in per_session if value > 0) / len(per_session)
        ),
        "session_values": [_round(value) for value in per_session],
    }


def cost_hurdle(
    *,
    cost_bps: float,
    safety_multiple: float = DEFAULT_COST_SAFETY_MULTIPLE,
) -> dict[str, Any]:
    """The gross edge a single-leg expression must clear to be worth pursuing.

    Break-even is the wrong bar.  The cost model is itself an estimate built
    from quoted spreads and a limited set of matched fills, so a measured edge
    that merely equals modelled cost is inside the error of the thing it is
    being compared against.  The multiple is the margin for that, not a
    profitability target.
    """
    if safety_multiple < 1:
        raise ValueError("cost safety multiple must be at least 1")
    return {
        "estimated_round_trip_cost_bps": _round(cost_bps, 4),
        "safety_multiple": safety_multiple,
        "required_gross_bps": _round(cost_bps * safety_multiple, 4),
        "required_gross_bps_two_leg": _round(2 * cost_bps * safety_multiple, 4),
    }


def horizon_cost_feasibility(
    observations: Sequence[dict[str, Any]],
    *,
    horizons_seconds: Sequence[int],
    safety_multiple: float = DEFAULT_COST_SAFETY_MULTIPLE,
) -> dict[str, Any]:
    """Kill horizons where even a perfect forecast could not pay the cost.

    At a given horizon the dispersion of forward returns bounds what *any*
    signal can extract from it.  If the mean absolute forward move over one
    minute is 3bps and a round trip costs 4bps, then no feature -- not the ones
    already built, not the ones not yet thought of -- makes that horizon
    tradable, and scoring fourteen features against it is wasted work.

    The ceiling used is the mean of the top decile of absolute forward moves:
    an oracle that traded only its best 10% of opportunities and got the
    direction right every time.  Anything real is far below it.
    """
    report: dict[str, Any] = {}
    for horizon in sorted({int(item) for item in horizons_seconds}):
        moves: list[float] = []
        costs: list[float] = []
        for observation in observations:
            outcome = (observation.get("forward") or {}).get(horizon)
            if not outcome or not outcome.get("available"):
                continue
            moves.append(abs(float(outcome["gross_return_bps"])))
            costs.append(float(observation.get("cost_bps") or 0.0))
        if len(moves) < 30:
            report[f"{horizon}s"] = {"feasible": None, "reason": "insufficient_observations"}
            continue
        threshold = percentile(moves, 0.90) or 0.0
        ceiling = fmean([value for value in moves if value >= threshold] or [0.0])
        cost = fmean(costs) if costs else 0.0
        required = cost * safety_multiple
        report[f"{horizon}s"] = {
            "observations": len(moves),
            "median_absolute_move_bps": _round(percentile(moves, 0.5), 4),
            "oracle_ceiling_bps": _round(ceiling, 4),
            "mean_round_trip_cost_bps": _round(cost, 4),
            "required_gross_bps": _round(required, 4),
            "feasible": bool(ceiling >= required),
            "reason": None if ceiling >= required else "alpha_ceiling_below_cost",
        }
    return report


@dataclass(frozen=True)
class CellKey:
    feature: str
    transform: str
    horizon_seconds: int
    slice_kind: str
    slice_value: str

    def key(self) -> str:
        return (
            f"{self.feature}::{self.transform}::{self.horizon_seconds}s::"
            f"{self.slice_kind}={self.slice_value}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "cell_key": self.key(),
            "feature": self.feature,
            "feature_transform": self.transform,
            "horizon_seconds": self.horizon_seconds,
            "slice_kind": self.slice_kind,
            "slice_value": self.slice_value,
        }


def measure_cell(
    rows: Sequence[dict[str, Any]],
    *,
    cell: CellKey,
    safety_multiple: float = DEFAULT_COST_SAFETY_MULTIPLE,
    effective_trials: int = 1,
    buckets: int = DEFAULT_BUCKETS,
    minimum_observations: int = MINIMUM_CELL_OBSERVATIONS,
    minimum_sessions: int = MINIMUM_CELL_SESSIONS,
) -> dict[str, Any]:
    """Everything one (feature, transform, horizon, slice) has to say.

    ``rows`` carry ``value`` (the transformed feature), ``forward`` (the gross
    forward return as a fraction), ``cost_bps``, ``symbol`` and
    ``session_date``.  No threshold is chosen anywhere in here: the extreme
    buckets are read off an equal-count partition, which is a description of
    the data rather than a parameter fitted to it.
    """
    usable = [
        row
        for row in rows
        if _finite(row.get("value")) is not None and _finite(row.get("forward")) is not None
    ]
    sessions = {row["session_date"] for row in usable}
    symbols = {str(row["symbol"]).upper() for row in usable}
    base = {
        **cell.as_dict(),
        "observations": len(usable),
        "distinct_sessions": len(sessions),
        "distinct_symbols": len(symbols),
    }
    if len(usable) < minimum_observations or len(sessions) < minimum_sessions:
        return {
            **base,
            "verdict": VERDICT_INSUFFICIENT,
            "reasons": [
                f"needs {minimum_observations} observations across {minimum_sessions} "
                f"sessions; has {len(usable)} across {len(sessions)}"
            ],
        }

    profile = bucket_profile(
        [(float(row["value"]), float(row["forward"])) for row in usable], buckets=buckets
    )
    shape = monotonicity(profile)
    ic = session_clustered_ic(usable)

    top = profile[-1] if profile else {}
    bottom = profile[0] if profile else {}
    top_bps = _finite(top.get("mean_forward_bps")) or 0.0
    bottom_bps = _finite(bottom.get("mean_forward_bps")) or 0.0
    # Which end of the feature is the favourable one is decided by the sign of
    # the rank IC, not by which extreme came out larger.  Taking the max over
    # extremes and then testing it for significance is a selection the
    # significance test knows nothing about, and under pure noise it is
    # reliably positive -- an early version of this function did exactly that
    # and labelled random data "information_below_cost" instead of
    # "no_information".  When the IC is too weak to imply a direction the
    # observed spread is used instead, and the verdict below then requires the
    # selection-adjusted gates rather than the IC to carry the cell.
    ic_t = ic["t_statistic"]
    significant = ic_t is not None and abs(ic_t) >= MINIMUM_IC_T_STATISTIC
    if significant and ic["rank_ic"] is not None:
        direction = 1 if ic["rank_ic"] > 0 else -1
    else:
        direction = 1 if (top_bps - bottom_bps) >= 0 else -1

    # A negative relationship is expressed by buying the bottom bucket and
    # selling the top one.  Hard-coding "long the top bucket" would report a
    # perfectly good inverse signal as a loss.
    long_end = "top" if direction > 0 else "bottom"
    short_end = "bottom" if direction > 0 else "top"
    long_leg = top_bps if long_end == "top" else bottom_bps
    short_leg = -(bottom_bps if short_end == "bottom" else top_bps)
    if long_leg >= short_leg:
        extreme_bps, extreme_end, extreme_sign = long_leg, long_end, 1
    else:
        extreme_bps, extreme_end, extreme_sign = short_leg, short_end, -1
    extreme_side = f"{'long' if extreme_sign > 0 else 'short'}_{extreme_end}_bucket"
    extreme_rows = [
        {
            "value": extreme_sign * float(row["forward"]),
            "session_date": row["session_date"],
            "symbol": row["symbol"],
        }
        for row in _extreme_bucket_rows(usable, end=extreme_end, buckets=len(profile) or buckets)
    ]
    long_short_bps = long_leg + short_leg

    costs = [float(row.get("cost_bps") or 0.0) for row in usable]
    hurdle = cost_hurdle(cost_bps=fmean(costs) if costs else 0.0, safety_multiple=safety_multiple)
    required = float(hurdle["required_gross_bps"] or 0.0)

    clustered = clustered_outcome_statistics(
        extreme_rows,
        effective_trials=max(1, int(effective_trials)),
    )
    stability = _stability(usable)

    reasons: list[str] = []
    # An event-shaped signal can be real and still have a weak overall IC --
    # only the tail carries it -- so a second route is left open.  It is the
    # strict one: every clustered gate including the trial-charged deflated
    # Sharpe, not a bare bootstrap interval, which a picked extreme clears too
    # easily.
    extreme_significant = bool(clustered.get("selection_adjusted_signal"))

    if not significant and not extreme_significant:
        verdict = VERDICT_NO_INFORMATION
        reasons.append(
            f"session-clustered rank IC t={ic['t_statistic']} is below "
            f"{MINIMUM_IC_T_STATISTIC} and the extreme bucket does not clear the "
            "selection-adjusted gates"
        )
    elif extreme_bps < required and long_short_bps < 2 * required:
        verdict = VERDICT_BELOW_COST
        reasons.append(
            f"best single-leg conditional move is {_round(extreme_bps, 2)}bps against a "
            f"{hurdle['required_gross_bps']}bps hurdle "
            f"({hurdle['estimated_round_trip_cost_bps']}bps round trip x {safety_multiple}); "
            "the forecast exists but is not harvestable at this cost"
        )
    else:
        unstable: list[str] = []
        if (shape["monotone_step_fraction"] or 0.0) < MINIMUM_MONOTONICITY:
            unstable.append(
                f"bucket response is not monotone (step agreement "
                f"{shape['monotone_step_fraction']} < {MINIMUM_MONOTONICITY})"
            )
        if not stability["sign_agreement"]:
            unstable.append(
                "rank IC changes sign between the first and second half of the sessions"
            )
        deflated = (clustered.get("deflated_sharpe") or {}).get("deflated_sharpe")
        if deflated is None or float(deflated) < 0.95:
            unstable.append(
                f"deflated Sharpe {deflated} against {clustered.get('effective_trials')} "
                "cumulative trials does not clear 0.95"
            )
        if unstable:
            verdict = VERDICT_UNSTABLE
            reasons.extend(unstable)
        else:
            verdict = VERDICT_TRADABLE
            reasons.append(
                f"{_round(extreme_bps, 2)}bps conditional move clears the "
                f"{hurdle['required_gross_bps']}bps hurdle with a monotone bucket response "
                "and a selection-adjusted signal"
            )

    return {
        **base,
        "verdict": verdict,
        "reasons": reasons,
        "rank_ic": ic["rank_ic"],
        "rank_ic_t_statistic": ic["t_statistic"],
        "rank_ic_p_value": ic["p_value"],
        "sessions_scored": ic["sessions_scored"],
        "positive_session_share": ic["positive_session_share"],
        "bucket_profile": profile,
        "monotonicity": shape,
        "extreme_side": extreme_side,
        "extreme_bucket_end": extreme_end,
        "extreme_trade_sign": extreme_sign,
        "extreme_bucket_gross_bps": _round(extreme_bps, 4),
        "top_bucket_gross_bps": _round(top_bps, 4),
        "bottom_bucket_gross_bps": _round(bottom_bps, 4),
        "long_short_gross_bps": _round(long_short_bps, 4),
        "net_bps": _round(extreme_bps - float(hurdle["estimated_round_trip_cost_bps"] or 0.0), 4),
        "cost_hurdle": hurdle,
        "clustered_evidence": clustered,
        "stability": stability,
    }


def _extreme_bucket_rows(
    rows: Sequence[dict[str, Any]],
    *,
    end: str,
    buckets: int,
) -> list[dict[str, Any]]:
    """The observations in the top or bottom bucket of the feature."""
    ordered = sorted(rows, key=lambda row: float(row["value"]))
    size = max(1, len(ordered) // max(2, buckets))
    return ordered[-size:] if end == "top" else ordered[:size]


def _stability(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Does the relationship hold in both halves of the sample.

    A signal that is strongly positive in the first half and flat or negative
    in the second is not a signal with a good average, it is two different
    regimes averaged into one misleading number.
    """
    sessions = sorted({row["session_date"] for row in rows})
    if len(sessions) < 8:
        return {"sign_agreement": False, "reason": "fewer than eight sessions to split"}
    midpoint = sessions[len(sessions) // 2]
    first = session_clustered_ic([row for row in rows if row["session_date"] < midpoint])
    second = session_clustered_ic([row for row in rows if row["session_date"] >= midpoint])
    first_ic = first["rank_ic"]
    second_ic = second["rank_ic"]
    agreement = bool(
        first_ic is not None
        and second_ic is not None
        and first_ic != 0
        and second_ic != 0
        and (first_ic > 0) == (second_ic > 0)
    )
    return {
        "first_half_rank_ic": first_ic,
        "second_half_rank_ic": second_ic,
        "first_half_sessions": first["sessions_scored"],
        "second_half_sessions": second["sessions_scored"],
        "sign_agreement": agreement,
    }


# ---------------------------------------------------------------------------
# Selection pressure across the whole grid
# ---------------------------------------------------------------------------


def probability_of_backtest_overfitting(
    performance_by_cell: dict[str, dict[date, float]],
    *,
    blocks: int = CSCV_BLOCKS,
) -> dict[str, Any]:
    """Combinatorially symmetric cross-validation over session blocks.

    Every cell in the run is scored on many disjoint in-sample/out-of-sample
    partitions of the same sessions.  For each partition the best in-sample
    cell is selected and its out-of-sample rank recorded.  If selection carried
    real information the winner should stay near the top; if it did not, its
    out-of-sample rank lands below the median about half the time.

    This is the measurement that catches what a per-cell t-statistic cannot:
    the search itself.  Testing a grid of features against a grid of horizons
    will always produce a best cell, and its t-statistic says nothing about how
    many cells it beat to get there.
    """
    cells = sorted(performance_by_cell)
    if len(cells) < 2:
        return {"probability": None, "reason": "at least two cells are required"}
    sessions = sorted({session for row in performance_by_cell.values() for session in row})
    if len(sessions) < blocks * 2:
        return {
            "probability": None,
            "reason": f"at least {blocks * 2} sessions are required for {blocks} blocks",
        }
    size = len(sessions) / blocks
    partition = [
        sessions[int(round(index * size)) : int(round((index + 1) * size))]
        for index in range(blocks)
    ]
    partition = [group for group in partition if group]
    if len(partition) < 4:
        return {"probability": None, "reason": "session blocks collapsed"}

    def score(cell: str, groups: Iterable[Sequence[date]]) -> float | None:
        values = [
            performance_by_cell[cell][session]
            for group in groups
            for session in group
            if session in performance_by_cell[cell]
        ]
        return fmean(values) if values else None

    logits: list[float] = []
    half = len(partition) // 2
    for selection in combinations(range(len(partition)), half):
        in_sample = [partition[index] for index in selection]
        out_sample = [partition[index] for index in range(len(partition)) if index not in selection]
        in_scores = {cell: score(cell, in_sample) for cell in cells}
        out_scores = {cell: score(cell, out_sample) for cell in cells}
        comparable = [
            cell
            for cell in cells
            if in_scores[cell] is not None and out_scores[cell] is not None
        ]
        if len(comparable) < 2:
            continue
        best = max(comparable, key=lambda cell: in_scores[cell])
        ordered = sorted(comparable, key=lambda cell: out_scores[cell])
        relative_rank = (ordered.index(best) + 1) / (len(ordered) + 1)
        relative_rank = min(max(relative_rank, 1e-6), 1 - 1e-6)
        logits.append(log(relative_rank / (1 - relative_rank)))
    if not logits:
        return {"probability": None, "reason": "no comparable partition"}
    return {
        "probability": _round(sum(1 for value in logits if value <= 0) / len(logits)),
        "partitions": len(logits),
        "blocks": len(partition),
        "median_logit": _round(percentile(logits, 0.5)),
        "interpretation": (
            "share of in-sample winners that landed in the bottom half out of sample; "
            "above 0.5 means the ranking of these cells is a selection artefact"
        ),
    }


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def measure_alpha_map(
    observations: Sequence[dict[str, Any]],
    *,
    features: Sequence[str],
    horizons_seconds: Sequence[int],
    grid_seconds: int,
    sectors: dict[str, str] | None = None,
    transforms: Sequence[str] = TRANSFORMS,
    slices: Sequence[str] = ("all",),
    safety_multiple: float = DEFAULT_COST_SAFETY_MULTIPLE,
    effective_trials: int | None = None,
    minimum_observations: int = MINIMUM_CELL_OBSERVATIONS,
    minimum_sessions: int = MINIMUM_CELL_SESSIONS,
) -> dict[str, Any]:
    """Score the whole feature x horizon x slice grid in one pass.

    Each observation must already carry ``symbol``, ``timestamp`` (the decision
    instant), ``session_date``, ``slot``, ``features`` and a ``forward`` ladder
    keyed by horizon in seconds -- ``load_alpha_map_panel`` followed by
    ``attach_forward_returns`` produces exactly that shape.  Transforms are
    derived here rather than by the caller, because normalizing before the
    panel is complete would compute the expanding history from a subset.

    Nothing here selects a winner.  It produces the map, charges the entire
    grid to the multiple-testing account, and reports which cells -- if any --
    survived.  Deciding what to build from that is a separate act by a person.
    """
    horizons = horizon_availability(horizons_seconds, grid_seconds=grid_seconds)
    available_horizons = horizons["available_seconds"]

    for observation in observations:
        observation.setdefault("transforms", {})
    decomposition: dict[str, Any] = {}
    for feature in features:
        expanding_normalization(observations, feature=feature)
        decomposition[feature] = residualize_cross_section(
            observations, feature=feature, sectors=sectors
        )

    feasibility = horizon_cost_feasibility(
        observations, horizons_seconds=available_horizons, safety_multiple=safety_multiple
    )
    feasible_horizons = [
        horizon
        for horizon in available_horizons
        if feasibility.get(f"{horizon}s", {}).get("feasible") is not False
    ]

    slice_assignments = _slice_assignments(observations, slices=slices)
    declared_cells = [
        CellKey(feature, transform, horizon, kind, value)
        for feature in features
        for transform in transforms
        for horizon in feasible_horizons
        for kind, values in slice_assignments.items()
        for value in values
    ]
    trials = effective_trials if effective_trials is not None else max(1, len(declared_cells))

    cells: list[dict[str, Any]] = []
    performance: dict[str, dict[date, float]] = {}
    for cell in declared_cells:
        rows = _cell_rows(observations, cell=cell)
        measured = measure_cell(
            rows,
            cell=cell,
            safety_multiple=safety_multiple,
            effective_trials=trials,
            minimum_observations=minimum_observations,
            minimum_sessions=minimum_sessions,
        )
        cells.append(measured)
        if measured["verdict"] != VERDICT_INSUFFICIENT:
            performance[cell.key()] = _cell_session_performance(rows, measured)

    adjusted = benjamini_hochberg(
        {cell["cell_key"]: cell.get("rank_ic_p_value") for cell in cells}
    )
    for cell in cells:
        cell["rank_ic_p_value_bh_adjusted"] = adjusted.get(cell["cell_key"])
        # A cell that survives on its own p-value but not after adjusting for
        # the size of the grid it was found in is a selection artefact, and
        # saying so here is cheaper than discovering it in a Paper Lab session.
        if cell["verdict"] == VERDICT_TRADABLE:
            bh = cell["rank_ic_p_value_bh_adjusted"]
            if bh is not None and float(bh) > 0.05:
                cell["verdict"] = VERDICT_UNSTABLE
                cell.setdefault("reasons", []).append(
                    f"Benjamini-Hochberg adjusted p={bh} across {len(cells)} cells in this "
                    "grid exceeds 0.05"
                )

    pbo = probability_of_backtest_overfitting(performance)
    survivors = [cell for cell in cells if cell["verdict"] == VERDICT_TRADABLE]
    overfit = pbo.get("probability") is not None and float(pbo["probability"]) > 0.5
    if overfit:
        for cell in survivors:
            cell["verdict"] = VERDICT_UNSTABLE
            cell.setdefault("reasons", []).append(
                f"probability of backtest overfitting across this grid is {pbo['probability']}; "
                "no cell selected from it can be treated as validated"
            )
        survivors = []

    dependence = {
        feature: cross_sectional_dependence(observations, feature=feature)
        for feature in features
    }

    return {
        "alpha_map_version": ALPHA_MAP_VERSION,
        "observations": len(observations),
        "horizons": horizons,
        "horizon_cost_feasibility": feasibility,
        "feature_decomposition": decomposition,
        "cross_sectional_dependence": dependence,
        "effective_trials": trials,
        "declared_cells": len(declared_cells),
        "cells": sorted(
            cells,
            key=lambda row: (
                -(row.get("extreme_bucket_gross_bps") or 0.0),
                row["cell_key"],
            ),
        ),
        "probability_of_backtest_overfitting": pbo,
        "verdict_counts": _verdict_counts(cells),
        "survivors": [cell["cell_key"] for cell in survivors],
        "strategy_construction_authorized": bool(survivors),
        "kill_summary": _kill_summary(cells),
    }


def _slice_assignments(
    observations: Sequence[dict[str, Any]],
    *,
    slices: Sequence[str],
) -> dict[str, list[str]]:
    assignments: dict[str, list[str]] = {}
    for kind in slices:
        if kind == "all":
            assignments["all"] = ["all"]
        elif kind == "symbol":
            assignments["symbol"] = sorted(
                {str(row["symbol"]).upper() for row in observations}
            )
        elif kind == "time_of_day":
            assignments["time_of_day"] = sorted({str(row["slot"]) for row in observations})
        elif kind == "session_half":
            assignments["session_half"] = ["first", "second"]
        else:
            raise ValueError(f"unsupported slice kind {kind!r}")
    return assignments


def _in_slice(row: dict[str, Any], *, kind: str, value: str) -> bool:
    if kind == "all":
        return True
    if kind == "symbol":
        return str(row["symbol"]).upper() == value
    if kind == "time_of_day":
        return str(row["slot"]) == value
    if kind == "session_half":
        minutes = _finite(row.get("minutes_from_open"))
        if minutes is None:
            return False
        return (minutes < 195) if value == "first" else (minutes >= 195)
    raise ValueError(f"unsupported slice kind {kind!r}")


def _cell_rows(
    observations: Sequence[dict[str, Any]],
    *,
    cell: CellKey,
) -> list[dict[str, Any]]:
    transform_key = f"{cell.feature}::{cell.transform}"
    rows: list[dict[str, Any]] = []
    for observation in observations:
        if not _in_slice(observation, kind=cell.slice_kind, value=cell.slice_value):
            continue
        value = _finite((observation.get("transforms") or {}).get(transform_key))
        outcome = (observation.get("forward") or {}).get(cell.horizon_seconds)
        if value is None or not outcome or not outcome.get("available"):
            continue
        rows.append(
            {
                "value": value,
                "forward": float(outcome["gross_return"]),
                "cost_bps": float(observation.get("cost_bps") or 0.0),
                "symbol": str(observation["symbol"]).upper(),
                "session_date": observation["session_date"],
                "timestamp": observation["timestamp"],
            }
        )
    return rows


def _cell_session_performance(
    rows: Sequence[dict[str, Any]],
    measured: dict[str, Any],
) -> dict[date, float]:
    """Per-session net bps of the cell's own extreme-bucket expression.

    This is what CSCV re-ranks.  It is not a strategy -- there is no threshold
    or sizing in it -- it is the cell's measured response expressed per day so
    that two cells can be compared on the same sessions.
    """
    end = str(measured.get("extreme_bucket_end") or "top")
    sign = int(measured.get("extreme_trade_sign") or 1)
    buckets = int((measured.get("monotonicity") or {}).get("buckets") or DEFAULT_BUCKETS)
    extreme = _extreme_bucket_rows(rows, end=end, buckets=max(2, buckets))
    by_session: dict[date, list[float]] = defaultdict(list)
    for row in extreme:
        by_session[row["session_date"]].append(
            sign * float(row["forward"]) * 10_000 - float(row.get("cost_bps") or 0.0)
        )
    return {session: fmean(values) for session, values in by_session.items()}


def _verdict_counts(cells: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for cell in cells:
        counts[cell["verdict"]] += 1
    return dict(counts)


def _kill_summary(cells: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Why the grid died, aggregated to the level a person can act on.

    The distinction this preserves is the whole point of the layer.  "No
    information at any horizon" says stop working on the feature.  "Information
    below cost" says the feature is real and the problem is the expression --
    a different horizon, a cheaper venue, a more selective event.  A single
    "rejected" verdict would collapse both into the same shrug.
    """
    by_feature: dict[str, dict[str, Any]] = {}
    for cell in cells:
        entry = by_feature.setdefault(
            cell["feature"],
            {"verdicts": defaultdict(int), "best_horizon_seconds": None, "best_gross_bps": None},
        )
        entry["verdicts"][cell["verdict"]] += 1
        gross = cell.get("extreme_bucket_gross_bps")
        if gross is not None and (
            entry["best_gross_bps"] is None or gross > entry["best_gross_bps"]
        ):
            entry["best_gross_bps"] = gross
            entry["best_horizon_seconds"] = cell["horizon_seconds"]
    output: dict[str, Any] = {}
    for feature, entry in by_feature.items():
        verdicts = dict(entry["verdicts"])
        if verdicts.get(VERDICT_TRADABLE):
            recommendation = "construct a strategy against the surviving cells only"
        elif verdicts.get(VERDICT_UNSTABLE):
            recommendation = (
                "an effect appears but does not survive selection or stability checks; "
                "collect more sessions before believing it"
            )
        elif verdicts.get(VERDICT_BELOW_COST):
            recommendation = (
                "the forecast is real and too small to harvest at this cost; "
                "change the expression (horizon, selectivity, venue), not the threshold"
            )
        elif verdicts.get(VERDICT_NO_INFORMATION):
            recommendation = "retire the feature at these horizons"
        else:
            recommendation = "insufficient data to conclude anything"
        output[feature] = {
            "verdicts": verdicts,
            "best_horizon_seconds": entry["best_horizon_seconds"],
            "best_gross_bps": entry["best_gross_bps"],
            "recommendation": recommendation,
        }
    return output


# ---------------------------------------------------------------------------
# Panel construction from stored evidence
# ---------------------------------------------------------------------------

PANEL_FEATURES = (
    "signed_trade_imbalance",
    "signed_trade_count_imbalance",
    "large_trade_share",
    "effective_trade_count",
    "relative_volume",
    "bar_return",
    "close_location",
    "distance_from_session_vwap",
    "effective_spread_bps",
)


def load_alpha_map_panel(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    symbols: Sequence[str],
    signal_timeframe: str,
    grid_timeframe: str,
    start: datetime,
    end: datetime,
    cost_model: dict[str, Any],
) -> dict[str, Any]:
    """Build the observation panel from *frozen* dataset evidence only.

    Every table read here is a ``research_dataset_*`` snapshot bounded by
    ``dataset_id``.  Reading the live tables instead -- which an earlier version
    of this function did -- would have made the whole declaration protocol
    decorative: the split boundaries would come from the frozen manifest while
    the signals and outcomes came from tables that a nightly ingest rewrites,
    so re-running a declaration after a backfill could produce a different
    answer with the same declaration id and the same content hash.  A frozen
    dataset that only freezes the boundaries is not a frozen dataset.

    The signal side is read at ``signal_timeframe`` (where the trade-flow
    features live) and the outcome side at ``grid_timeframe``, which must be
    finer and must have been frozen into the same dataset as an outcome grid.
    Decision time is the signal bar's *close*, because that is the first
    instant its features are knowable.

    Feed and source are not parameters: the snapshot already pinned both when
    it was frozen, and accepting them here would let a caller ask for a feed
    the dataset does not contain and get silence instead of a refusal.

    The returned observations hold a live handle to the frozen outcome grid
    rather than a copy of it, so ``attach_forward_returns`` -- and only it --
    still reads from ``conn``.  That is what keeps the 1m grid from being
    resident all at once; see ``_FrozenOutcomeGrid``.  The consequence for
    callers is that the two functions must be used against the same open
    connection, which every caller already does.
    """
    signal_minutes = timeframe_minutes(signal_timeframe)
    grid_minutes = timeframe_minutes(grid_timeframe)
    if grid_minutes >= signal_minutes:
        raise ValueError(
            f"the measurement grid ({grid_timeframe}) must be finer than the signal "
            f"timeframe ({signal_timeframe}); measuring horizons on the signal's own "
            "grid is the limitation this module exists to remove"
        )
    upper = [str(symbol).upper() for symbol in symbols]

    flow_rows = conn.execute(
        """
        SELECT symbol, timestamp, signed_trade_imbalance, signed_trade_count_imbalance,
               large_trade_share, effective_trade_count, effective_spread_bps,
               total_volume, trade_count, unclassified_share
        FROM research_dataset_trade_flow_features
        WHERE dataset_id = %s AND symbol = ANY(%s) AND timeframe = %s
          AND timestamp >= %s AND timestamp < %s
        ORDER BY symbol, timestamp
        """,
        (dataset_id, upper, signal_timeframe, start, end),
    ).fetchall()
    if not flow_rows:
        raise ValueError(
            f"dataset {dataset_id} has no frozen {signal_timeframe} trade-flow features "
            f"for these {len(upper)} symbols in this phase window. Freeze them into the "
            "dataset before mapping alpha; the alpha map does not read live tables."
        )
    signal_candles = _dataset_candles_by_symbol(
        conn,
        dataset_id=dataset_id,
        symbols=upper,
        timeframe=signal_timeframe,
        start=start,
        end=end,
        include_session_context=True,
    )
    horizon_slack = timedelta(minutes=signal_minutes + 90)
    # Not loaded here.  The outcome grid is the one input whose size is set by
    # the calendar rather than the declaration -- a discovery window is millions
    # of 1m bars -- and panel construction needs only to know which sessions it
    # covers.  The bars themselves are read one symbol at a time when the
    # forward ladder is attached.
    grid = _FrozenOutcomeGrid(
        conn,
        dataset_id=dataset_id,
        symbols=upper,
        timeframe=grid_timeframe,
        start=start,
        end=end + horizon_slack,
    )
    grid_sessions = grid.sessions_with_bars()
    if not grid_sessions:
        raise ValueError(
            f"dataset {dataset_id} contains no frozen {grid_timeframe} candles. Re-freeze "
            f"it with --outcome-timeframes {grid_timeframe}; the alpha map cannot measure "
            f"sub-{signal_timeframe} horizons without a finer frozen grid."
        )

    signal_index: dict[str, dict[datetime, dict[str, Any]]] = defaultdict(dict)
    for symbol, rows in signal_candles.items():
        for row in rows:
            signal_index[symbol][row["timestamp"]] = row

    observations: list[dict[str, Any]] = []
    skipped: dict[str, int] = defaultdict(int)
    for row in flow_rows:
        symbol = str(row["symbol"]).upper()
        timestamp = row["timestamp"]
        candle = signal_index.get(symbol, {}).get(timestamp)
        if candle is None:
            skipped["no_matching_signal_candle"] += 1
            continue
        session = exchange_session_date(timestamp)
        if session not in grid_sessions.get(symbol, frozenset()):
            skipped["no_grid_bars_for_session"] += 1
            continue
        decision = bar_close_timestamp(timestamp, timeframe=signal_timeframe)
        features = _panel_features(dict(row), candle=dict(candle))
        session_close = session_close_timestamp(dict(candle))
        if session_close is None:
            skipped["signal_bar_without_minutes_to_close"] += 1
        observations.append(
            {
                "symbol": symbol,
                "timestamp": decision,
                "signal_bar_timestamp": timestamp,
                "session_date": session,
                "slot": bar_slot(timestamp),
                "minutes_from_open": _finite(candle.get("minutes_from_open")),
                "session_close": session_close,
                "features": features,
                "cost_bps": estimated_round_trip_cost_bps(
                    cost_model, symbol=symbol, timestamp=decision, stressed=True
                ),
                "_grid": grid,
                "_grid_key": (symbol, session),
                "_decision": decision,
                "_session_close": session_close,
            }
        )
    return {
        "observations": observations,
        "skipped": dict(skipped),
        "grid_seconds": grid_minutes * 60,
        "signal_rows": len(flow_rows),
    }


def session_close_timestamp(signal_row: dict[str, Any]) -> datetime | None:
    """When the exchange session containing this signal bar actually ended.

    ``minutes_to_close`` is written by the feature engine as
    ``market_close - bar_timestamp`` in minutes, against the XNYS calendar via
    ``pandas_market_calendars`` -- see
    ``app/services/labs/intraday/session.py``.  It is measured from the bar's
    *open*, which is what ``timestamp`` holds throughout this schema, so the
    close is recovered by adding it back to that same timestamp.

    Deriving the boundary this way rather than from a 16:00 clock is the whole
    point: on an early-close session the calendar returns 13:00 and this
    follows automatically, whereas a hardcoded close would silently admit three
    hours of after-hours bars on exactly the days that are easiest to overlook.
    """
    minutes = _finite(signal_row.get("minutes_to_close"))
    timestamp = signal_row.get("timestamp")
    if minutes is None or not isinstance(timestamp, datetime):
        return None
    return timestamp + timedelta(minutes=minutes)


def attach_forward_returns(
    observations: Sequence[dict[str, Any]],
    *,
    horizons_seconds: Sequence[int],
    grid_seconds: int,
) -> dict[str, Any]:
    """Populate each observation's forward ladder and drop the scratch keys.

    This reads the frozen outcome grid from the connection the panel was built
    against, one symbol at a time.  ``load_alpha_map_panel`` deliberately did
    not load those bars: holding them for the whole panel is what OOM-killed
    the discovery run.

    ``unavailable_by_reason`` is returned alongside the coverage counts because
    the reasons are diagnostic rather than incidental: a run whose rungs are
    mostly ``session_end_before_horizon`` is telling you the horizon is too long
    for where in the session the signal fires, which is a finding about the
    hypothesis and not a data problem.

    The panel is walked grouped by symbol so the outcome grid stays one symbol
    resident.  That is an ordering, not a filter: every observation is still
    measured, each one independently of the others, and the two counters below
    are sums.  So the order this walks in cannot change what it reports -- it
    changes only how many of the frozen 1m bars are alive while it does.
    """
    coverage: dict[str, int] = defaultdict(int)
    reasons: dict[str, int] = defaultdict(int)
    for observation in sorted(observations, key=lambda row: str(row.get("symbol") or "")):
        grid = observation.pop("_grid")
        symbol, session = observation.pop("_grid_key")
        ladder = forward_return_ladder(
            grid.session_bars(symbol, session),
            decision_timestamp=observation.pop("_decision"),
            horizons_seconds=horizons_seconds,
            grid_seconds=grid_seconds,
            session_close_timestamp=observation.pop("_session_close"),
        )
        observation["forward"] = ladder
        for horizon, outcome in ladder.items():
            if outcome.get("available"):
                coverage[f"{horizon}s"] += 1
            else:
                reasons[str(outcome.get("reason") or "unknown")] += 1
    return {
        "available_by_horizon": dict(coverage),
        "unavailable_by_reason": dict(reasons),
    }


# ---------------------------------------------------------------------------
# Frozen candle loading
#
# The outcome grid is the only thing in this module whose size is set by the
# calendar rather than by the declaration: a discovery window over a few dozen
# symbols is millions of 1m bars.  Everything below exists so that number never
# becomes a Python-object count.
# ---------------------------------------------------------------------------

# Rows per round trip from a server-side cursor.  Large enough that the FETCH
# round trips are not the cost, small enough that the resident page is noise.
DATASET_CANDLE_ITERSIZE = 10_000

# One source of truth for the DISTINCT ON, so the streaming loader below and
# `_dataset_candles_by_symbol` cannot drift apart on which row wins when a
# snapshot was not pinned to a single source.
_SIGNAL_CANDLE_QUERY = """
    SELECT DISTINCT ON (c.symbol, c.timestamp)
           c.symbol, c.timestamp, c.source,
           c.open, c.high, c.low, c.close, c.volume,
           f.minutes_from_open, f.minutes_to_close,
           f.session_relative_volume, f.distance_from_session_vwap
    FROM research_dataset_candles c
    LEFT JOIN research_dataset_intraday_features f
      ON f.dataset_id = c.dataset_id
     AND f.symbol = c.symbol
     AND f.timeframe = c.timeframe
     AND f.timestamp = c.timestamp
    WHERE c.dataset_id = %s
      AND c.symbol = ANY(%s)
      AND c.timeframe = %s
      AND c.timestamp >= %s
      AND c.timestamp < %s
    ORDER BY c.symbol, c.timestamp, c.source
"""

_GRID_CANDLE_QUERY = """
    SELECT DISTINCT ON (c.symbol, c.timestamp)
           c.symbol, c.timestamp, c.source,
           c.open, c.high, c.low, c.close, c.volume
    FROM research_dataset_candles c
    WHERE c.dataset_id = %s
      AND c.symbol = ANY(%s)
      AND c.timeframe = %s
      AND c.timestamp >= %s
      AND c.timestamp < %s
    ORDER BY c.symbol, c.timestamp, c.source
"""

# Which (symbol, session) pairs the frozen grid covers at all.  DISTINCT ON is
# deliberately absent: it picks *which* of several source rows survives for a
# (symbol, timestamp), and every one of them carries the same timestamp and so
# maps to the same exchange session date.  The set this builds is therefore
# identical to the one the full loader would produce, at two columns per row
# and without the sort that DISTINCT ON implies.
_GRID_PRESENCE_QUERY = """
    SELECT c.symbol, c.timestamp
    FROM research_dataset_candles c
    WHERE c.dataset_id = %s
      AND c.symbol = ANY(%s)
      AND c.timeframe = %s
      AND c.timestamp >= %s
      AND c.timestamp < %s
"""


def _stream_rows(
    conn: psycopg.Connection,
    query: str,
    params: Sequence[Any],
    *,
    label: str,
    itersize: int = DATASET_CANDLE_ITERSIZE,
) -> Iterator[Any]:
    """Iterate a result set through a server-side cursor.

    ``fetchall()`` materializes a result twice and holds both copies at the
    same instant: libpq's buffer for the whole result, and then one Python
    object per row per column.  On the 1m discovery grid that is roughly 6.5M
    rows, and the process was OOM-killed at 7.9GiB resident before it had
    measured anything.  A named cursor keeps at most ``itersize`` rows on this
    side and lets the caller decide how much of the stream it retains.

    The cursor is declared ``WITH HOLD`` only when the connection is in
    autocommit, where a plain ``DECLARE`` has no transaction to live in.  The
    research CLIs run inside a transaction, so the usual path is the cheap one.
    """
    withhold = bool(getattr(conn, "autocommit", False))
    with conn.cursor(name=f"alpha_map_{label}_{uuid4().hex}", withhold=withhold) as cursor:
        cursor.itersize = itersize
        cursor.execute(query, params)
        yield from cursor


class _GridBar:
    """One outcome-grid bar, holding only what the forward ladder reads.

    The full row as a dict is around a kilobyte once psycopg hands back NUMERIC
    columns as ``Decimal`` and the symbol and source as fresh strings; at 6.5M
    rows that is most of a VPS spent on five fields and change.  ``symbol``,
    ``source`` and ``volume`` are never consulted by ``forward_return_ladder``
    -- the symbol is already the key this bar is filed under -- so they are not
    kept.

    Mapping access is preserved rather than replaced by attributes so the
    ladder itself is untouched; a measurement function is the last place to
    accept a refactor in exchange for memory.
    """

    __slots__ = ("timestamp", "open", "high", "low", "close")

    def __init__(self, row: Any) -> None:
        self.timestamp = row["timestamp"]
        # `_finite` is what the ladder would have applied to these values
        # anyway, so converting on the way in changes no arithmetic: it only
        # moves the Decimal -> float conversion off the hot path and out of the
        # retained set.  A NULL or non-finite price still arrives as None and
        # still lands in `missing_ohlc` / `missing_entry_price`.
        self.open = _finite(row["open"])
        self.high = _finite(row["high"])
        self.low = _finite(row["low"])
        self.close = _finite(row["close"])

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key) from None

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return f"_GridBar({self.timestamp!r}, close={self.close!r})"


class _FrozenOutcomeGrid:
    """The frozen outcome grid, one symbol resident at a time.

    Holding the whole grid is what the panel used to do, and it is what killed
    the process: every observation kept a reference to its session's bars, so
    the last symbol's load could not free the first symbol's.

    Two things make one-symbol residency possible without changing a single
    measured number.  Panel construction does not need the bars at all -- it
    needs to know only *whether* a (symbol, session) has any, which is the
    ``no_grid_bars_for_session`` skip -- so it runs against a two-column
    presence pass.  And the bars themselves are needed only when the ladder is
    attached, which walks the panel grouped by symbol.  A cache of exactly one
    symbol is therefore never a cache miss in the steady state.

    Every read is still bounded by ``dataset_id`` against ``research_dataset_``
    tables, and still collapses the source dimension with the same DISTINCT ON.
    Nothing here loosens what the panel is allowed to see; it changes only how
    much of it is alive at once.
    """

    def __init__(
        self,
        conn: psycopg.Connection,
        *,
        dataset_id: int,
        symbols: Sequence[str],
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> None:
        self._conn = conn
        self._dataset_id = dataset_id
        self._symbols = [str(symbol).upper() for symbol in symbols]
        self._timeframe = timeframe
        self._start = start
        self._end = end
        # Every symbol shares the same minute grid, so the session date of a
        # given timestamp is computed once for the whole run rather than once
        # per row.  Pure function of the timestamp, so this is a speedup and
        # not a semantic change.
        self._session_dates: dict[datetime, date] = {}
        self._resident: str | None = None
        self._sessions: dict[date, list[_GridBar]] = {}

    def _params(self, symbols: Sequence[str]) -> tuple[Any, ...]:
        return (self._dataset_id, list(symbols), self._timeframe, self._start, self._end)

    def _session_date(self, timestamp: datetime) -> date:
        cached = self._session_dates.get(timestamp)
        if cached is None:
            cached = exchange_session_date(timestamp)
            self._session_dates[timestamp] = cached
        return cached

    def sessions_with_bars(self) -> dict[str, set[date]]:
        """Which exchange sessions the frozen grid covers, per symbol."""
        present: dict[str, set[date]] = defaultdict(set)
        for row in _stream_rows(
            self._conn,
            _GRID_PRESENCE_QUERY,
            self._params(self._symbols),
            label="grid_presence",
        ):
            present[str(row["symbol"]).upper()].add(self._session_date(row["timestamp"]))
        return dict(present)

    def session_bars(self, symbol: str, session: date) -> list[_GridBar]:
        """One symbol's bars for one exchange session, in chronological order."""
        symbol = str(symbol).upper()
        if symbol != self._resident:
            self._load(symbol)
        return self._sessions.get(session, [])

    def _load(self, symbol: str) -> None:
        # Dropped before the next symbol is built rather than after, so the two
        # are never resident together.
        self._resident = None
        self._sessions = {}
        grouped: dict[date, list[_GridBar]] = defaultdict(list)
        for row in _stream_rows(
            self._conn,
            _GRID_CANDLE_QUERY,
            self._params([symbol]),
            label="grid",
        ):
            grouped[self._session_date(row["timestamp"])].append(_GridBar(row))
        # Sorted in place: the stream already arrives in timestamp order, and
        # building new lists here would put two copies of the symbol side by
        # side for no gain.
        for bars in grouped.values():
            bars.sort(key=lambda bar: bar.timestamp)
        self._sessions = dict(grouped)
        self._resident = symbol


def _dataset_candles_by_symbol(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    symbols: Sequence[str],
    timeframe: str,
    start: datetime,
    end: datetime,
    include_session_context: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Load frozen candles, optionally joined to frozen session context.

    Signal-timeframe candles need frozen intraday session context because
    minutes_to_close, relative volume and distance from VWAP are signal
    features.

    Outcome-grid candles need only timestamp/OHLCV. Joining millions of 1m
    outcome rows to research_dataset_intraday_features is unnecessary and can
    make panel construction extremely expensive.

    DISTINCT ON still collapses the source dimension for snapshots that were
    not pinned to a single source.

    Rows arrive through a server-side cursor, so the result set is never
    materialized twice.  This still returns every matching row, which is the
    right shape for the signal timeframe -- one bar per 15 or 30 minutes -- and
    the wrong shape for a 1m outcome grid.  For that, panel construction uses
    ``_FrozenOutcomeGrid``, which holds one symbol at a time.
    """
    params = (
        dataset_id,
        [str(symbol).upper() for symbol in symbols],
        timeframe,
        start,
        end,
    )
    query = _SIGNAL_CANDLE_QUERY if include_session_context else _GRID_CANDLE_QUERY

    output: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _stream_rows(conn, query, params, label="candles"):
        record = dict(row)
        output[str(record["symbol"]).upper()].append(record)

    return dict(output)


def _panel_features(flow: dict[str, Any], *, candle: dict[str, Any]) -> dict[str, float | None]:
    open_price = _finite(candle.get("open"))
    high = _finite(candle.get("high"))
    low = _finite(candle.get("low"))
    close = _finite(candle.get("close"))
    bar_return = (close / open_price - 1) if open_price and close and open_price > 0 else None
    close_location = (
        (close - low) / (high - low) if None not in (high, low, close) and high > low else None
    )
    return {
        "signed_trade_imbalance": _finite(flow.get("signed_trade_imbalance")),
        "signed_trade_count_imbalance": _finite(flow.get("signed_trade_count_imbalance")),
        "large_trade_share": _finite(flow.get("large_trade_share")),
        "effective_trade_count": _finite(flow.get("effective_trade_count")),
        "effective_spread_bps": _finite(flow.get("effective_spread_bps")),
        "relative_volume": _finite(candle.get("session_relative_volume")),
        "distance_from_session_vwap": _finite(candle.get("distance_from_session_vwap")),
        "bar_return": bar_return,
        "close_location": close_location,
    }


# ---------------------------------------------------------------------------
# Declaration, execution, persistence
# ---------------------------------------------------------------------------


def declare_alpha_map(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    signal_timeframe: str = "30m",
    grid_timeframe: str = "1m",
    symbols: Sequence[str],
    features: Sequence[str] = PANEL_FEATURES,
    horizons_seconds: Sequence[int] = DEFAULT_HORIZONS_SECONDS,
    transforms: Sequence[str] = TRANSFORMS,
    slices: Sequence[str] = ("all",),
    cost_calibration_id: int | None = None,
    cost_safety_multiple: float = DEFAULT_COST_SAFETY_MULTIPLE,
    phases: Sequence[str] = ("discovery", "validation", "confirmation"),
) -> dict[str, Any]:
    """Freeze the grid before any of it is measured.

    Declaring first is what makes the trial count honest.  A grid chosen after
    seeing which horizons looked promising is a grid of one test reported as a
    grid of one test, when it was really a search over all of them.

    ``phases`` names the windows this declaration is allowed to be measured
    against, and every one of them is preflighted for frozen signal *and*
    outcome-grid coverage before the declaration is written.  Declaring all
    three by default is the strict reading: a declaration is single-use, so a
    phase that cannot be measured should stop the declaration rather than be
    discovered later by the run that spent it.
    """
    if not symbols:
        raise ValueError("an alpha map declaration must name at least one symbol")
    unknown = sorted(set(transforms) - set(TRANSFORMS))
    if unknown:
        raise ValueError(f"unsupported feature transforms: {unknown}")
    if cost_safety_multiple < 1:
        raise ValueError("cost_safety_multiple must be at least 1")
    if timeframe_minutes(grid_timeframe) >= timeframe_minutes(signal_timeframe):
        raise ValueError(
            f"grid_timeframe {grid_timeframe} must be finer than signal_timeframe "
            f"{signal_timeframe}"
        )

    cost_model = load_cost_model(conn, cost_calibration_id)
    splits = get_dataset_splits(conn, dataset_id)
    if splits is None:
        raise ValueError(
            f"dataset {dataset_id} has no nested research splits; an alpha map measured "
            "without them cannot say which of its looks were free"
        )
    normalized_symbols = sorted({str(symbol).upper() for symbol in symbols})
    requested_phases = sorted(set(phases))
    unknown_phases = sorted(set(requested_phases) - {"discovery", "validation", "confirmation"})
    if unknown_phases:
        raise ValueError(f"unsupported phases: {unknown_phases}")
    if not requested_phases:
        raise ValueError("a declaration must name at least one measurable phase")

    windows = phase_windows(splits)
    coverage = dataset_coverage(
        conn,
        dataset_id=dataset_id,
        symbols=normalized_symbols,
        signal_timeframe=signal_timeframe,
        grid_timeframe=grid_timeframe,
    )
    by_phase = {
        phase: dataset_coverage(
            conn,
            dataset_id=dataset_id,
            symbols=normalized_symbols,
            signal_timeframe=signal_timeframe,
            grid_timeframe=grid_timeframe,
            start=windows[phase][0],
            end=windows[phase][1],
        )
        for phase in requested_phases
    }
    # Refusing at declaration rather than at measurement. A declaration is
    # single-use: if the missing outcome grid only surfaced during the run, the
    # declaration would already be spent and the fix would cost a new one.
    #
    # Checked per phase, not dataset-wide. An outcome grid frozen over only the
    # discovery window passes a dataset-wide "does it exist anywhere" test and
    # then yields a confirmation run in which every horizon is unavailable --
    # which reads as a null result rather than as missing data.
    for phase in requested_phases:
        window = windows[phase]
        phase_coverage = by_phase[phase]
        if not phase_coverage["trade_flow_rows"]:
            raise ValueError(
                f"dataset {dataset_id} has no frozen {signal_timeframe} trade-flow "
                f"features inside the {phase} window ({window[0]} to {window[1]}) for "
                "these symbols, so that phase cannot be measured."
            )
        if not phase_coverage["grid_candles"]:
            raise ValueError(
                f"dataset {dataset_id} has no frozen {grid_timeframe} candles inside the "
                f"{phase} window ({window[0]} to {window[1]}). The outcome grid exists "
                "elsewhere in the dataset but not where this phase would measure it; "
                f"re-freeze with --outcome-timeframes {grid_timeframe} covering the whole "
                "declared window."
            )
    cell_keys = declared_cell_keys(
        features=features,
        transforms=transforms,
        horizons_seconds=horizons_seconds,
        slices=slices,
        symbols=normalized_symbols,
        signal_timeframe=signal_timeframe,
    )
    declared_cells = len(cell_keys)
    specification = {
        "protocol_version": ALPHA_MAP_VERSION,
        "signal_timeframe": signal_timeframe,
        "grid_timeframe": grid_timeframe,
        "symbols": normalized_symbols,
        "features": sorted(set(features)),
        "transforms": sorted(set(transforms)),
        "horizons_seconds": sorted({int(item) for item in horizons_seconds}),
        "slices": sorted(set(slices)),
        "cost_model": jsonable(cost_model),
        "cost_safety_multiple": float(cost_safety_multiple),
        # Feed and source are recorded from the frozen dataset rather than
        # accepted from the caller, so the declaration cannot claim a feed the
        # snapshot does not contain.
        "dataset_coverage": coverage,
        "phases": requested_phases,
        "phase_coverage": by_phase,
        "declared_cell_count": declared_cells,
    }
    specification_hash = sha256(
        dumps(specification, sort_keys=True, default=str).encode()
    ).hexdigest()
    existing = conn.execute(
        "SELECT * FROM intraday_alpha_map_declarations WHERE specification_hash = %s",
        (specification_hash,),
    ).fetchone()
    if existing:
        return {**jsonable(dict(existing)), "already_declared": True}

    # Every cell is a test, and the ledger has to know that before the run so
    # the deflated Sharpe is charged for the grid rather than for the winner.
    declare_trials(
        conn,
        purpose="alpha_map",
        timeframe=signal_timeframe,
        factor_keys=cell_keys,
        dataset_id=dataset_id,
        hypothesis=(
            "Pre-strategy forward-return cartography: measure whether each feature "
            "predicts anything, at which horizon, and whether it survives cost."
        ),
        protocol_version=ALPHA_MAP_VERSION,
    )
    row = conn.execute(
        """
        INSERT INTO intraday_alpha_map_declarations(
            dataset_id, signal_timeframe, grid_timeframe, symbols, features,
            horizons_seconds, slices, cost_model, cost_safety_multiple,
            split_boundaries, specification, specification_hash,
            declared_cell_count, protocol_version
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING *
        """,
        (
            dataset_id,
            signal_timeframe,
            grid_timeframe,
            Jsonb(normalized_symbols),
            Jsonb(sorted(set(features))),
            Jsonb(sorted({int(item) for item in horizons_seconds})),
            Jsonb(sorted(set(slices))),
            Jsonb(jsonable(cost_model)),
            cost_safety_multiple,
            Jsonb(jsonable(_split_boundaries(splits))),
            Jsonb(specification),
            specification_hash,
            declared_cells,
            ALPHA_MAP_VERSION,
        ),
    ).fetchone()
    conn.commit()
    return {**jsonable(dict(row)), "already_declared": False}


def dataset_coverage(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    symbols: Sequence[str],
    signal_timeframe: str,
    grid_timeframe: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, Any]:
    """What the frozen dataset actually contains for this measurement.

    Recorded into the declaration so the run is reproducible against a stated
    row count: if a later reader gets different numbers from the same dataset
    id, the dataset was not immutable and this is where that shows up.

    ``start``/``end`` bound the count to one phase window.  Counting over the
    whole dataset is the wrong question for a declaration: an outcome grid that
    covers only the discovery window would satisfy a dataset-wide check and
    then produce a confirmation run with no measurable horizons at all.
    """
    upper = [str(symbol).upper() for symbol in symbols]
    flow = conn.execute(
        """
        SELECT COUNT(*) AS rows, COUNT(DISTINCT symbol) AS symbols,
               MIN(timestamp) AS first_bar, MAX(timestamp) AS last_bar
        FROM research_dataset_trade_flow_features
        WHERE dataset_id = %s AND symbol = ANY(%s) AND timeframe = %s
          AND (%s::timestamptz IS NULL OR timestamp >= %s)
          AND (%s::timestamptz IS NULL OR timestamp < %s)
        """,
        (dataset_id, upper, signal_timeframe, start, start, end, end),
    ).fetchone()
    grid = conn.execute(
        """
        SELECT COUNT(*) AS rows, COUNT(DISTINCT symbol) AS symbols,
               MIN(timestamp) AS first_bar, MAX(timestamp) AS last_bar
        FROM research_dataset_candles
        WHERE dataset_id = %s AND symbol = ANY(%s) AND timeframe = %s
          AND (%s::timestamptz IS NULL OR timestamp >= %s)
          AND (%s::timestamptz IS NULL OR timestamp < %s)
        """,
        (dataset_id, upper, grid_timeframe, start, start, end, end),
    ).fetchone()
    return jsonable(
        {
            "dataset_id": dataset_id,
            "signal_timeframe": signal_timeframe,
            "grid_timeframe": grid_timeframe,
            "window_start": start,
            "window_end": end,
            "trade_flow_rows": int((flow or {}).get("rows") or 0),
            "trade_flow_symbols": int((flow or {}).get("symbols") or 0),
            "trade_flow_first_bar": (flow or {}).get("first_bar"),
            "trade_flow_last_bar": (flow or {}).get("last_bar"),
            "grid_candles": int((grid or {}).get("rows") or 0),
            "grid_symbols": int((grid or {}).get("symbols") or 0),
            "grid_first_bar": (grid or {}).get("first_bar"),
            "grid_last_bar": (grid or {}).get("last_bar"),
        }
    )


def phase_windows(splits: Any) -> dict[str, tuple[datetime, datetime]]:
    """The half-open window each split phase covers."""
    return {
        "discovery": (splits.discovery_start, splits.validation_start),
        "validation": (splits.validation_start, splits.confirmation_start),
        "confirmation": (splits.confirmation_start, splits.confirmation_end),
    }


def declared_cell_keys(
    *,
    features: Sequence[str],
    transforms: Sequence[str],
    horizons_seconds: Sequence[int],
    slices: Sequence[str],
    symbols: Sequence[str],
    signal_timeframe: str,
) -> list[str]:
    """Every cell the declaration commits to measuring.

    This is the list the trial ledger is charged, so it has to enumerate slice
    *values*, not slice kinds.  Slicing by symbol across 40 names is 40 more
    looks at the data, and charging it as one would put the deflated Sharpe
    back to flattering exactly the searches it exists to discount.
    """
    slice_values: list[tuple[str, str]] = []
    for kind in sorted(set(slices)):
        if kind == "all":
            slice_values.append(("all", "all"))
        elif kind == "symbol":
            slice_values.extend(
                ("symbol", symbol) for symbol in sorted({str(item).upper() for item in symbols})
            )
        elif kind == "time_of_day":
            slice_values.extend(
                ("time_of_day", slot) for slot in regular_session_slots(signal_timeframe)
            )
        elif kind == "session_half":
            slice_values.extend([("session_half", "first"), ("session_half", "second")])
        else:
            raise ValueError(f"unsupported slice kind {kind!r}")
    return [
        CellKey(feature, transform, horizon, kind, value).key()
        for feature in sorted(set(features))
        for transform in sorted(set(transforms))
        for horizon in sorted({int(item) for item in horizons_seconds})
        for kind, value in slice_values
    ]


def _split_boundaries(splits: Any) -> dict[str, Any]:
    return {
        "discovery_start": splits.discovery_start,
        "discovery_end": splits.discovery_end,
        "validation_start": splits.validation_start,
        "validation_end": splits.validation_end,
        "confirmation_start": splits.confirmation_start,
        "confirmation_end": splits.confirmation_end,
        "split_version": splits.split_version,
    }


def run_alpha_map(
    conn: psycopg.Connection,
    *,
    declaration_id: int,
    phase: str = "discovery",
) -> dict[str, Any]:
    """Measure a declared grid exactly once, against one phase of the split.

    One declaration, one measurement -- enforced here and by the unique key on
    the runs table.  Re-measuring the same grid after seeing its result is a
    second look at the data, and the honest way to take a second look is a new
    declaration whose cells are charged to the trial ledger again.  ``phase``
    chooses which window that single measurement reads.
    """
    if phase not in {"discovery", "validation", "confirmation"}:
        raise ValueError(f"unsupported phase {phase!r}")
    declaration = conn.execute(
        "SELECT * FROM intraday_alpha_map_declarations WHERE id = %s",
        (declaration_id,),
    ).fetchone()
    if not declaration:
        raise ValueError(f"No alpha map declaration id={declaration_id}.")
    declaration = dict(declaration)
    existing = conn.execute(
        "SELECT id, phase FROM intraday_alpha_map_runs WHERE declaration_id = %s",
        (declaration_id,),
    ).fetchone()
    if existing:
        raise ValueError(
            f"Declaration {declaration_id} was already measured as run "
            f"{existing['id']} in phase {existing['phase']}. Measuring it again would "
            "reuse the same data for a second look; declare a new grid instead."
        )

    specification = dict(declaration["specification"])
    dataset_id = int(declaration["dataset_id"])
    splits = get_dataset_splits(conn, dataset_id)
    if splits is None:
        raise ValueError(f"dataset {dataset_id} lost its research splits")
    declared_phases = [
        str(item) for item in (specification.get("phases") or ["discovery", "validation", "confirmation"])
    ]
    if phase not in declared_phases:
        raise ValueError(
            f"Declaration {declaration_id} was preflighted for {declared_phases}, not "
            f"{phase!r}. Measuring a phase whose frozen coverage was never checked is "
            "how a run comes back empty and gets read as a null result."
        )
    window = phase_windows(splits)[phase]
    record_split_access(
        conn,
        dataset_id=dataset_id,
        phase=phase,
        decision_type="alpha_map_measurement",
        detail={"declaration_id": declaration_id, "protocol_version": ALPHA_MAP_VERSION},
    )

    cost_model = dict(declaration["cost_model"])
    symbols = [str(item).upper() for item in (declaration["symbols"] or [])]
    panel = load_alpha_map_panel(
        conn,
        dataset_id=dataset_id,
        symbols=symbols,
        signal_timeframe=str(declaration["signal_timeframe"]),
        grid_timeframe=str(declaration["grid_timeframe"]),
        start=window[0],
        end=window[1],
        cost_model=cost_model,
    )
    observations = panel["observations"]
    horizons = [int(item) for item in (declaration["horizons_seconds"] or [])]
    coverage = attach_forward_returns(
        observations, horizons_seconds=horizons, grid_seconds=panel["grid_seconds"]
    )

    features = [str(item) for item in (declaration["features"] or [])]
    transforms = [str(item) for item in specification.get("transforms") or TRANSFORMS]
    slices = [str(item) for item in (declaration["slices"] or ["all"])]
    ledger = effective_trials_for_run(
        conn,
        timeframe=str(declaration["signal_timeframe"]),
        factor_keys=declared_cell_keys(
            features=features,
            transforms=transforms,
            horizons_seconds=horizons,
            slices=slices,
            symbols=symbols,
            signal_timeframe=str(declaration["signal_timeframe"]),
        ),
        spec_hash=str(declaration["specification_hash"]),
    )
    results = measure_alpha_map(
        observations,
        features=features,
        horizons_seconds=horizons,
        grid_seconds=panel["grid_seconds"],
        sectors=sector_map(conn, symbols),
        transforms=transforms,
        slices=slices,
        safety_multiple=float(declaration["cost_safety_multiple"]),
        effective_trials=int(ledger["effective_trials"]),
    )
    results["panel"] = {
        "signal_rows": panel["signal_rows"],
        "skipped": panel["skipped"],
        "forward_coverage": coverage["available_by_horizon"],
        "forward_unavailable_by_reason": coverage["unavailable_by_reason"],
    }
    results["trial_ledger"] = ledger
    results["phase"] = phase

    run = conn.execute(
        """
        INSERT INTO intraday_alpha_map_runs(
            declaration_id, dataset_id, phase, observation_count, effective_trials,
            probability_of_backtest_overfitting, cross_sectional_dependence, results,
            survivors, strategy_construction_authorized, protocol_version
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING *
        """,
        (
            declaration_id,
            dataset_id,
            phase,
            len(observations),
            int(ledger["effective_trials"]),
            (results["probability_of_backtest_overfitting"] or {}).get("probability"),
            Jsonb(jsonable(results["cross_sectional_dependence"])),
            Jsonb(jsonable({key: value for key, value in results.items() if key != "cells"})),
            Jsonb(results["survivors"]),
            results["strategy_construction_authorized"],
            ALPHA_MAP_VERSION,
        ),
    ).fetchone()
    run_id = int(run["id"])
    for cell in results["cells"]:
        conn.execute(
            """
            INSERT INTO intraday_alpha_map_cells(
                run_id, cell_key, feature, feature_transform, horizon_seconds,
                slice_kind, slice_value, observations, distinct_sessions,
                distinct_symbols, rank_ic, rank_ic_t_statistic,
                extreme_bucket_gross_bps, long_short_gross_bps,
                estimated_round_trip_cost_bps, required_gross_bps, net_bps,
                monotonicity, verdict, detail
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                run_id,
                cell["cell_key"],
                cell["feature"],
                cell["feature_transform"],
                cell["horizon_seconds"],
                cell["slice_kind"],
                cell["slice_value"],
                cell["observations"],
                cell["distinct_sessions"],
                cell["distinct_symbols"],
                cell.get("rank_ic"),
                cell.get("rank_ic_t_statistic"),
                cell.get("extreme_bucket_gross_bps"),
                cell.get("long_short_gross_bps"),
                (cell.get("cost_hurdle") or {}).get("estimated_round_trip_cost_bps"),
                (cell.get("cost_hurdle") or {}).get("required_gross_bps"),
                cell.get("net_bps"),
                (cell.get("monotonicity") or {}).get("rank_correlation"),
                cell["verdict"],
                Jsonb(jsonable(cell)),
            ),
        )
    conn.commit()
    return {"run_id": run_id, **results}


def alpha_map_report(conn: psycopg.Connection, *, run_id: int) -> dict[str, Any]:
    run = conn.execute(
        "SELECT * FROM intraday_alpha_map_runs WHERE id = %s", (run_id,)
    ).fetchone()
    if not run:
        raise ValueError(f"No alpha map run id={run_id}.")
    cells = conn.execute(
        """
        SELECT * FROM intraday_alpha_map_cells
        WHERE run_id = %s
        ORDER BY extreme_bucket_gross_bps DESC NULLS LAST, cell_key
        """,
        (run_id,),
    ).fetchall()
    return jsonable(
        {
            "run": dict(run),
            "cells": [dict(row) for row in cells],
        }
    )


def feature_horizon_profile(
    conn: psycopg.Connection,
    *,
    feature: str,
    transform: str | None = None,
) -> dict[str, Any]:
    """Every horizon this feature has ever been measured at.

    This is the table the whole layer exists to produce: one feature, one row
    per horizon, and the answer to "where does the information live" readable
    without opening a strategy.
    """
    rows = conn.execute(
        """
        SELECT feature_transform, horizon_seconds, verdict,
               AVG(rank_ic) AS rank_ic,
               AVG(rank_ic_t_statistic) AS rank_ic_t_statistic,
               AVG(extreme_bucket_gross_bps) AS extreme_bucket_gross_bps,
               AVG(estimated_round_trip_cost_bps) AS cost_bps,
               AVG(net_bps) AS net_bps,
               SUM(observations) AS observations,
               COUNT(*) AS cells
        FROM intraday_alpha_map_cells
        WHERE feature = %s AND (%s::text IS NULL OR feature_transform = %s)
        GROUP BY feature_transform, horizon_seconds, verdict
        ORDER BY horizon_seconds, feature_transform
        """,
        (feature, transform, transform),
    ).fetchall()
    return jsonable(
        {
            "feature": feature,
            "transform": transform,
            "profile": [dict(row) for row in rows],
        }
    )


def cleared_cell(
    conn: psycopg.Connection,
    *,
    run_id: int,
    cell_key: str,
) -> dict[str, Any]:
    """The clearance Paper Lab reads before it will run a strategy.

    Raises rather than returning a falsy value, because the caller is deciding
    whether to place orders and a silent None is the wrong shape for that.
    """
    row = conn.execute(
        """
        SELECT c.*, r.strategy_construction_authorized, r.phase
        FROM intraday_alpha_map_cells c
        JOIN intraday_alpha_map_runs r ON r.id = c.run_id
        WHERE c.run_id = %s AND c.cell_key = %s
        """,
        (run_id, cell_key),
    ).fetchone()
    if not row:
        raise ValueError(f"No alpha map cell {cell_key!r} in run {run_id}.")
    record = dict(row)
    if str(record["verdict"]) != VERDICT_TRADABLE:
        raise ValueError(
            f"Alpha map cell {cell_key!r} in run {run_id} is {record['verdict']}, not "
            f"{VERDICT_TRADABLE}. It does not authorize a strategy."
        )
    return jsonable(record)
