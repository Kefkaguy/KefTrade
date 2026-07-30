"""Positive and negative controls that certify the measurement instrument.

A null result means nothing until the pipeline has been shown to report a
signal that is really there and to stay silent when one is not.  This module
supplies both halves:

* a synthetic market carrying a factor of known sign, strength and holding
  horizon, which the real measurement path must recover;
* placebos built from real observations -- shuffled session dates, flipped
  signs, permuted symbols, resampled noise -- every one of which must fail.

It also replicates the published first-to-last-half-hour calculation gross of
costs using the paper's own definition, so that a weak result on recent data
can be attributed to decay rather than to a broken calculation.

The module contains no campaign, broker, order-submission, or UI code.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from math import sqrt
from random import Random
from statistics import fmean, pstdev
from typing import Any, Callable, Sequence

from app.services.intraday_research_integrity import clustered_outcome_statistics
from app.services.intraday_session_calendar import (
    NEW_YORK,
    closing_bar,
    opening_bar,
    ordered_regular_sessions,
    regular_session_slots,
)

RESEARCH_CONTROLS_VERSION = "intraday_research_controls_v1"

# Gao, Han, Li and Zhou, "Market intraday momentum" (JFE 2018), SPY
# 1993-2013: the last half hour is predicted by the first.  Recorded as
# provenance for the replication below, never as a pass/fail threshold on our
# own sample.
PUBLISHED_INTRADAY_MOMENTUM = {
    "study": "Market intraday momentum",
    "reference": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2440866",
    "instrument": "SPY",
    "published_sample_start": "1993-01-01",
    "published_sample_end": "2013-12-31",
    "definition": (
        "r1 = first half-hour return measured from the previous session's "
        "close to the 09:30-10:00 close; r13 = last half-hour return measured "
        "close-to-close from 15:30 to 16:00. Timing strategy: long when "
        "r1 > 0, short when r1 < 0, held over the last half hour only."
    ),
}


# ---------------------------------------------------------------------------
# Synthetic market with an injected factor
# ---------------------------------------------------------------------------


def synthetic_intraday_candles(
    *,
    symbols: Sequence[str],
    sessions: int = 260,
    timeframe: str = "30m",
    injected_effect_bps: float = 0.0,
    injected_sign: int = 1,
    horizon_bars: int = 1,
    noise_bps: float = 25.0,
    gap_probability: float = 0.18,
    gap_size: float = 0.006,
    gap_effect_bps: float = 0.0,
    gap_acceptance_probability: float = 0.5,
    seed: int = 7,
    start: date = date(2024, 1, 2),
) -> dict[str, list[dict[str, Any]]]:
    """Build candles carrying factors of known sign, strength and horizon.

    Two independent relationships can be planted:

    * ``injected_effect_bps`` -- the continuous claim.  The sign of the
      opening half-hour return is repeated, at that strength, in the closing
      half hour, ``horizon_bars`` from the end of the session.
    * ``gap_effect_bps`` -- the directional-event claim.  On a gap session the
      bar after the open either accepts or absorbs the gap, and the following
      bar pays that strength in the direction the acceptance/absorption
      hypothesis predicts.

    With both at zero the generator produces a market containing no factor at
    all, which is the negative control.
    """
    slots = regular_session_slots(timeframe)
    rng = Random(seed)
    direction = 1 if injected_sign >= 0 else -1
    effect = injected_effect_bps / 10_000 * direction
    gap_effect = gap_effect_bps / 10_000 * direction
    noise = noise_bps / 10_000
    entry_index = 2

    candles: dict[str, list[dict[str, Any]]] = {}
    for symbol_index, symbol in enumerate(symbols):
        rows: list[dict[str, Any]] = []
        price = 100.0 + symbol_index
        session_day = start
        produced = 0
        while produced < sessions:
            if session_day.weekday() >= 5:
                session_day += timedelta(days=1)
                continue
            previous_close = price
            gap = (
                rng.choice((1.0, -1.0)) * gap_size * (1 + rng.uniform(0, 0.5))
                if rng.random() < gap_probability and len(slots) > entry_index
                else 0.0
            )
            accepted = rng.random() < gap_acceptance_probability
            # Acceptance leaves the gap standing, absorption gives most of it
            # back; the two are separated by the builder's fill thresholds.
            fill_fraction = rng.uniform(0.0, 0.15) if accepted else rng.uniform(0.6, 0.9)
            gap_payoff = (
                (1 if gap > 0 else -1) * gap_effect * (1 if accepted else -1)
                if gap
                else 0.0
            )
            opening_return = rng.gauss(0, noise * 4)
            closing_return = effect * (1 if (gap or opening_return) > 0 else -1) + rng.gauss(
                0, noise
            )

            session_open = previous_close * (1 + gap) if gap else previous_close
            for bar_index, slot in enumerate(slots):
                hour, minute = (int(part) for part in slot.split(":"))
                timestamp = datetime(
                    session_day.year,
                    session_day.month,
                    session_day.day,
                    hour,
                    minute,
                    tzinfo=NEW_YORK,
                ).astimezone(UTC)
                open_price = session_open if bar_index == 0 else price
                if bar_index == 0:
                    close_price = open_price * (1 + opening_return)
                elif gap and bar_index == 1:
                    # Land the decision bar exactly on the intended fill.
                    close_price = session_open - fill_fraction * (session_open - previous_close)
                elif gap and bar_index == entry_index:
                    close_price = open_price * (1 + gap_payoff + rng.gauss(0, noise))
                elif bar_index == len(slots) - horizon_bars:
                    close_price = open_price * (1 + closing_return)
                else:
                    close_price = open_price * (1 + rng.gauss(0, noise))
                relative_volume = (
                    2.0 + rng.uniform(0, 0.5)
                    if gap and bar_index == 1
                    else 1.0 + rng.uniform(0, 1.4)
                )
                rows.append(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "timestamp": timestamp,
                        "open": open_price,
                        "high": max(open_price, close_price) * 1.0005,
                        "low": min(open_price, close_price) * 0.9995,
                        "close": close_price,
                        "volume": 100_000.0 * (1 + rng.uniform(0, 1)),
                        "session_vwap": (open_price + close_price) / 2,
                        "session_relative_volume": relative_volume,
                    }
                )
                price = close_price
            produced += 1
            session_day += timedelta(days=1)
        candles[symbol] = rows
    return candles


def _directional_outcomes(observations: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "value": (
                1.0
                if float(row["score"]) > 0
                else -1.0
                if float(row["score"]) < 0
                else 0.0
            )
            * float(row["target_return"]),
            "session_date": row["session_date"],
            "timestamp": row["timestamp"],
            "symbol": row["symbol"],
        }
        for row in observations
        if row.get("score") is not None and row.get("target_return") is not None
    ]


def measure_observations(
    observations: Sequence[dict[str, Any]],
    *,
    effective_trials: int = 1,
) -> dict[str, Any]:
    """Direction-adjusted, session-clustered summary of a raw observation set."""
    outcomes = _directional_outcomes(observations)
    if not outcomes:
        return {
            "observations": 0,
            "mean_return_bps": None,
            "day_clustered_t_statistic": None,
            "detected": False,
            "detected_either_sign": False,
        }
    evidence = clustered_outcome_statistics(
        outcomes,
        effective_trials=effective_trials,
        require_symbol_diversification=len({row["symbol"] for row in outcomes}) > 1,
    )
    clustered_t = evidence["day_clustered_t_statistic"]
    return {
        "observations": len(outcomes),
        "distinct_sessions": evidence["distinct_sessions"],
        "mean_return_bps": evidence["mean_return_bps"],
        "day_clustered_t_statistic": clustered_t,
        "block_bootstrap": evidence["block_bootstrap"],
        # `detected` is the research bar -- only a profitable edge counts.
        # `detected_either_sign` is the instrument bar: it asks whether the
        # pipeline can see a relationship at all, whichever way it points.
        "detected": bool(clustered_t is not None and clustered_t >= 3.0),
        "detected_either_sign": bool(clustered_t is not None and abs(clustered_t) >= 3.0),
    }


def positive_control(
    builder: Callable[..., list[dict[str, Any]]],
    *,
    timeframe: str = "30m",
    symbols: Sequence[str] = ("SPY", "QQQ"),
    sessions: int = 260,
    injected_effect_bps: float = 12.0,
    injected_sign: int = 1,
    label: str = "continuous",
    seed: int = 7,
    **generator_kwargs: Any,
) -> dict[str, Any]:
    """The pipeline must recover a factor that was deliberately planted."""
    candles = synthetic_intraday_candles(
        symbols=symbols,
        sessions=sessions,
        timeframe=timeframe,
        injected_effect_bps=injected_effect_bps,
        injected_sign=injected_sign,
        seed=seed,
        **generator_kwargs,
    )
    observations = builder(candles, timeframe=timeframe)
    measured = measure_observations(observations)
    expected_sign = 1 if injected_sign >= 0 else -1
    measured_bps = measured.get("mean_return_bps")
    recovered_sign = (
        0 if measured_bps is None else 1 if measured_bps > 0 else -1
    )
    return {
        "control": "positive",
        "label": label,
        "injected_effect_bps": injected_effect_bps,
        "injected_sign": expected_sign,
        "measured": measured,
        "recovered_sign_matches": recovered_sign == expected_sign,
        "passed": bool(
            measured["detected_either_sign"] and recovered_sign == expected_sign
        ),
    }


# ---------------------------------------------------------------------------
# Placebos
# ---------------------------------------------------------------------------


def shuffle_session_dates(
    observations: Sequence[dict[str, Any]],
    *,
    seed: int = 11,
) -> list[dict[str, Any]]:
    """Break the score/target pairing while keeping both marginal samples."""
    rng = Random(seed)
    targets = [row["target_return"] for row in observations]
    rng.shuffle(targets)
    return [
        {**row, "target_return": target}
        for row, target in zip(observations, targets)
    ]


def flip_signal_directions(
    observations: Sequence[dict[str, Any]],
    *,
    seed: int = 13,
) -> list[dict[str, Any]]:
    """Randomly reverse the claimed direction of each signal."""
    rng = Random(seed)
    return [
        {**row, "score": float(row["score"]) * rng.choice((-1.0, 1.0))}
        for row in observations
    ]


def permute_symbols(
    observations: Sequence[dict[str, Any]],
    *,
    seed: int = 17,
) -> list[dict[str, Any]]:
    """Attach each score to a *different* symbol's outcome at the same instant.

    A plain shuffle is not strong enough here: across a two-name cross-section
    it leaves the true pairing intact half the time, so half the real signal
    survives and the placebo passes when it should not.  A cyclic rotation by a
    non-zero offset is a derangement, so no observation keeps its own target.
    """
    rng = Random(seed)
    by_timestamp: dict[Any, list[dict[str, Any]]] = {}
    for row in observations:
        by_timestamp.setdefault(row["timestamp"], []).append(row)
    output: list[dict[str, Any]] = []
    for rows in by_timestamp.values():
        if len(rows) < 2:
            # Nothing to swap with; a single-name instant carries no
            # cross-sectional pairing to break.
            continue
        targets = [row["target_return"] for row in rows]
        offset = rng.randrange(1, len(rows))
        rotated = targets[offset:] + targets[:offset]
        output.extend(
            {**row, "target_return": target} for row, target in zip(rows, rotated)
        )
    return output


def resample_pure_noise(
    observations: Sequence[dict[str, Any]],
    *,
    seed: int = 19,
) -> list[dict[str, Any]]:
    """Replace targets with noise of the same scale and no relationship."""
    rng = Random(seed)
    values = [float(row["target_return"]) for row in observations]
    scale = pstdev(values) if len(values) > 1 else 0.0
    return [
        {**row, "target_return": rng.gauss(0, scale)}
        for row in observations
    ]


PLACEBOS: dict[str, Callable[..., list[dict[str, Any]]]] = {
    "shuffled_session_dates": shuffle_session_dates,
    "flipped_signal_directions": flip_signal_directions,
    "permuted_symbols": permute_symbols,
    "resampled_pure_noise": resample_pure_noise,
}


def negative_controls(
    observations: Sequence[dict[str, Any]],
    *,
    repetitions: int = 5,
    effective_trials: int = 1,
) -> dict[str, Any]:
    """Every placebo must fail to clear the confirmation bar."""
    results: dict[str, Any] = {}
    for name, placebo in PLACEBOS.items():
        draws = [
            measure_observations(
                placebo(observations, seed=1_000 + index),
                effective_trials=effective_trials,
            )
            for index in range(max(1, repetitions))
        ]
        detections = sum(1 for draw in draws if draw["detected_either_sign"])
        results[name] = {
            "repetitions": len(draws),
            "false_detections": detections,
            "max_absolute_t_statistic": max(
                (abs(draw["day_clustered_t_statistic"] or 0.0) for draw in draws),
                default=None,
            ),
            "passed": detections == 0,
        }
    return {
        "control": "negative",
        "placebos": results,
        "placebos_failing": sorted(
            name for name, result in results.items() if not result["passed"]
        ),
        "passed": all(result["passed"] for result in results.values()),
    }


# ---------------------------------------------------------------------------
# Published-definition replication
# ---------------------------------------------------------------------------


def published_intraday_momentum_replication(
    candles_by_symbol: dict[str, Sequence[dict[str, Any]]],
    *,
    timeframe: str = "30m",
    symbol: str = "SPY",
    newey_west_lags: int = 5,
) -> dict[str, Any]:
    """Reproduce the paper's own calculation, gross of costs.

    Uses the published close-to-close definitions rather than the executable
    open-to-close target: the point is to establish whether the calculation
    reproduces at all, so it must be the calculation the paper performed.
    """
    slots = regular_session_slots(timeframe)
    pairs: list[tuple[float, float, date]] = []
    previous_close: float | None = None
    for session_date, session in ordered_regular_sessions(
        candles_by_symbol.get(symbol, []),
        timeframe=timeframe,
    ):
        first_bar = opening_bar(session, timeframe=timeframe)
        last_bar = closing_bar(session, timeframe=timeframe)
        if first_bar is None or last_bar is None or len(session) < 3:
            previous_close = float(last_bar["close"]) if last_bar else None
            continue
        penultimate = session[-2]
        penultimate_close = float(penultimate["close"])
        if previous_close and previous_close > 0 and penultimate_close > 0:
            r_first = (float(first_bar["close"]) - previous_close) / previous_close
            r_last = (float(last_bar["close"]) - penultimate_close) / penultimate_close
            pairs.append((r_first, r_last, session_date))
        previous_close = float(last_bar["close"])

    if len(pairs) < 30:
        return {
            "controls_version": RESEARCH_CONTROLS_VERSION,
            "published": PUBLISHED_INTRADAY_MOMENTUM,
            "symbol": symbol,
            "status": "insufficient_sessions",
            "sessions": len(pairs),
        }

    first = [row[0] for row in pairs]
    last = [row[1] for row in pairs]
    regression = _ordinary_least_squares(first, last, newey_west_lags=newey_west_lags)
    timing = [
        (1.0 if r_first > 0 else -1.0 if r_first < 0 else 0.0) * r_last
        for r_first, r_last, _ in pairs
    ]
    timing_mean = fmean(timing)
    timing_deviation = pstdev(timing) if len(timing) > 1 else 0.0
    timing_t = (
        timing_mean / (timing_deviation / sqrt(len(timing)))
        if timing_deviation > 0
        else None
    )
    return {
        "controls_version": RESEARCH_CONTROLS_VERSION,
        "published": PUBLISHED_INTRADAY_MOMENTUM,
        "symbol": symbol,
        "status": "measured",
        "sessions": len(pairs),
        "sample_start": str(pairs[0][2]),
        "sample_end": str(pairs[-1][2]),
        "predictive_regression": regression,
        "timing_strategy_gross": {
            "mean_return_bps": round(timing_mean * 10_000, 6),
            "t_statistic": round(timing_t, 6) if timing_t is not None else None,
            "observations": len(timing),
            "hit_rate": round(sum(value > 0 for value in timing) / len(timing), 6),
            "gross_of_costs": True,
        },
        # The published sign is positive.  A negative slope on recent data is
        # a decay finding; a slope that cannot be computed is a defect.
        "reproduces_published_sign": bool(
            regression["slope"] is not None and regression["slope"] > 0
        ),
    }


def _ordinary_least_squares(
    predictor: Sequence[float],
    outcome: Sequence[float],
    *,
    newey_west_lags: int = 5,
) -> dict[str, Any]:
    """Slope with plain and Newey-West heteroskedasticity/autocorrelation t-stats."""
    count = len(predictor)
    if count < 3:
        return {"slope": None, "intercept": None, "t_statistic": None}
    mean_x = fmean(predictor)
    mean_y = fmean(outcome)
    variance = sum((value - mean_x) ** 2 for value in predictor)
    if variance <= 0:
        return {"slope": None, "intercept": None, "t_statistic": None}
    slope = sum(
        (x - mean_x) * (y - mean_y) for x, y in zip(predictor, outcome)
    ) / variance
    intercept = mean_y - slope * mean_x
    residuals = [
        y - (intercept + slope * x) for x, y in zip(predictor, outcome)
    ]
    degrees = count - 2
    residual_variance = sum(value**2 for value in residuals) / degrees
    standard_error = sqrt(residual_variance / variance)

    scores = [(x - mean_x) * residual for x, residual in zip(predictor, residuals)]
    long_run = sum(value**2 for value in scores)
    for lag in range(1, min(newey_west_lags, count - 1) + 1):
        weight = 1 - lag / (newey_west_lags + 1)
        covariance = sum(
            scores[index] * scores[index - lag] for index in range(lag, count)
        )
        long_run += 2 * weight * covariance
    newey_west_error = sqrt(max(long_run, 0.0)) / variance if variance > 0 else None

    return {
        "slope": round(slope, 8),
        "intercept": round(intercept, 8),
        "observations": count,
        "r_squared": round(
            1 - sum(value**2 for value in residuals)
            / sum((y - mean_y) ** 2 for y in outcome),
            6,
        )
        if sum((y - mean_y) ** 2 for y in outcome) > 0
        else None,
        "standard_error": round(standard_error, 8),
        "t_statistic": round(slope / standard_error, 6) if standard_error > 0 else None,
        "newey_west_lags": newey_west_lags,
        "newey_west_t_statistic": (
            round(slope / newey_west_error, 6)
            if newey_west_error not in (None, 0)
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Certification
# ---------------------------------------------------------------------------


def certify_measurement_instrument(
    specs: dict[str, Any],
    *,
    timeframe: str = "30m",
    reference_factor_key: str = "first_to_last_half_hour_market_momentum",
    event_factor_key: str = "overnight_gap_acceptance_absorption",
    candles_by_symbol: dict[str, Sequence[dict[str, Any]]] | None = None,
    sessions: int = 260,
    injected_effect_bps: float = 12.0,
    event_effect_bps: float = 30.0,
    seed: int = 7,
) -> dict[str, Any]:
    """Run every control and return a single certification verdict.

    ``candles_by_symbol`` is the real frozen snapshot, used only for the
    published replication and for placebos built from real observations.  The
    positive control always runs on synthetic data, because it is the only
    case where the true answer is known.
    """
    spec = specs[reference_factor_key]
    positive = positive_control(
        spec.builder,
        timeframe=timeframe,
        sessions=sessions,
        injected_effect_bps=injected_effect_bps,
        injected_sign=1,
        label=f"{spec.factor_type}:{reference_factor_key}",
        seed=seed,
    )
    inverted = positive_control(
        spec.builder,
        timeframe=timeframe,
        sessions=sessions,
        injected_effect_bps=injected_effect_bps,
        injected_sign=-1,
        label=f"{spec.factor_type}:{reference_factor_key}:inverted",
        seed=seed + 1,
    )
    # The bounded gap experiment is a directional-event family, so the
    # instrument must be certified on that measurement path too -- the
    # continuous path passing says nothing about event conditioning.
    event_spec = specs[event_factor_key]
    event_symbols = ("SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMD", "META", "AMZN")
    event_positive = positive_control(
        event_spec.builder,
        timeframe=timeframe,
        symbols=event_symbols,
        sessions=sessions,
        injected_effect_bps=0.0,
        injected_sign=1,
        label=f"{event_spec.factor_type}:{event_factor_key}",
        seed=seed + 3,
        gap_effect_bps=event_effect_bps,
        gap_probability=0.25,
    )
    null_market = synthetic_intraday_candles(
        symbols=("SPY", "QQQ"),
        sessions=sessions,
        timeframe=timeframe,
        injected_effect_bps=0.0,
        gap_effect_bps=0.0,
        seed=seed + 2,
    )
    null_observations = spec.builder(null_market, timeframe=timeframe)
    null_measured = measure_observations(null_observations)
    null_event_market = synthetic_intraday_candles(
        symbols=event_symbols,
        sessions=sessions,
        timeframe=timeframe,
        injected_effect_bps=0.0,
        gap_effect_bps=0.0,
        gap_probability=0.25,
        seed=seed + 4,
    )
    null_event_measured = measure_observations(
        event_spec.builder(null_event_market, timeframe=timeframe)
    )

    placebo_source = null_observations
    replication: dict[str, Any] | None = None
    if candles_by_symbol:
        real_observations = spec.builder(candles_by_symbol, timeframe=timeframe)
        if real_observations:
            placebo_source = real_observations
        replication = published_intraday_momentum_replication(
            candles_by_symbol, timeframe=timeframe
        )
    placebos = negative_controls(placebo_source)

    checks = {
        "recovers_injected_continuous_factor": positive["passed"],
        "recovers_inverted_continuous_factor": inverted["passed"],
        "recovers_injected_directional_event_factor": event_positive["passed"],
        "reports_nothing_on_a_factorless_market": not null_measured["detected_either_sign"],
        "reports_nothing_on_factorless_events": not null_event_measured[
            "detected_either_sign"
        ],
        "every_placebo_fails": placebos["passed"],
    }
    return {
        "controls_version": RESEARCH_CONTROLS_VERSION,
        "timeframe": timeframe,
        "reference_factor_key": reference_factor_key,
        "event_factor_key": event_factor_key,
        "positive_control": positive,
        "inverted_positive_control": inverted,
        "directional_event_positive_control": event_positive,
        "null_market_control": {
            "control": "null_market",
            "measured": null_measured,
            "passed": not null_measured["detected_either_sign"],
        },
        "null_event_market_control": {
            "control": "null_event_market",
            "measured": null_event_measured,
            "passed": not null_event_measured["detected_either_sign"],
        },
        "negative_controls": placebos,
        "published_replication": replication,
        "checks": checks,
        "certified": all(checks.values()),
    }
