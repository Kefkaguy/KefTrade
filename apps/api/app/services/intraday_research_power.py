"""Power, regime and stability reporting for intraday factor evidence.

A factor that fails is not evidence of absence until the sample was large
enough to have found the effect had it been there, and a factor that passes is
not evidence of presence until it survives being cut by time, by symbol and by
market state.  Every diagnostic therefore reports what it would have taken to
detect the effect, where the sample was concentrated, and how the estimate
moved between discovery and validation.

The module contains no research verdict, no campaign code, and no UI code.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from math import sqrt
from statistics import fmean, pstdev
from typing import Any, Sequence

from app.services.intraday_session_calendar import (
    closing_bar,
    opening_bar,
    ordered_regular_sessions,
)

RESEARCH_POWER_VERSION = "intraday_research_power_v1"

# Two-sided 5% test at 80% power.
Z_ALPHA_TWO_SIDED = 1.959964
Z_POWER = 0.841621
MINIMUM_SUBPERIOD_SESSIONS = 20
TRAILING_VOLATILITY_SESSIONS = 20


def required_sessions_for_power(
    *,
    effect_bps: float | None,
    session_dispersion_bps: float | None,
    power_z: float = Z_POWER,
    alpha_z: float = Z_ALPHA_TWO_SIDED,
) -> int | None:
    """Sessions needed to detect ``effect_bps`` at 80% power, two-sided 5%."""
    if not effect_bps or not session_dispersion_bps or session_dispersion_bps <= 0:
        return None
    return int(
        round(((alpha_z + power_z) * session_dispersion_bps / abs(effect_bps)) ** 2 + 0.5)
    )


def _session_means(outcomes: Sequence[dict[str, Any]]) -> dict[date, float]:
    grouped: dict[date, list[float]] = defaultdict(list)
    for row in outcomes:
        grouped[row["session_date"]].append(float(row["value"]))
    return {key: fmean(values) for key, values in grouped.items()}


def _subperiod_statistics(
    session_means: dict[date, float],
    *,
    label_of: Any,
) -> dict[str, Any]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for session_date, value in session_means.items():
        grouped[label_of(session_date)].append(value)
    output: dict[str, Any] = {}
    for label in sorted(grouped):
        values = grouped[label]
        deviation = pstdev(values) if len(values) > 1 else 0.0
        mean = fmean(values)
        output[label] = {
            "sessions": len(values),
            "mean_return_bps": _round(mean * 10_000),
            "t_statistic": (
                _round(mean / (deviation / sqrt(len(values))))
                if deviation > 0 and len(values) > 1
                else None
            ),
            "positive": mean > 0,
            # A three-session quarter says nothing either way; the flag keeps
            # a thin slice from being read as instability or as support.
            "sufficient_sessions": len(values) >= MINIMUM_SUBPERIOD_SESSIONS,
        }
    return output


def _stability(subperiods: dict[str, Any]) -> dict[str, Any]:
    scored = [
        result for result in subperiods.values() if result["sufficient_sessions"]
    ]
    positive = sum(1 for result in scored if result["positive"])
    return {
        "scored_subperiods": len(scored),
        "positive_subperiods": positive,
        "positive_share": _round(positive / len(scored)) if scored else None,
        # Two thirds of usable subperiods pointing the same way is the bar for
        # calling an effect stable rather than driven by one good stretch.
        "stable": bool(scored) and positive / len(scored) >= 2 / 3,
    }


def benchmark_session_context(
    candles_by_symbol: dict[str, Sequence[dict[str, Any]]],
    *,
    timeframe: str,
    benchmark_symbol: str = "SPY",
) -> dict[date, dict[str, Any]]:
    """Per-session benchmark return and trailing realized volatility.

    Volatility uses a trailing window so the regime label attached to a
    session never depends on what happened after it.
    """
    returns: list[tuple[date, float]] = []
    for session_date, session in ordered_regular_sessions(
        candles_by_symbol.get(benchmark_symbol, []),
        timeframe=timeframe,
    ):
        first_bar = opening_bar(session, timeframe=timeframe)
        last_bar = closing_bar(session, timeframe=timeframe)
        if first_bar is None or last_bar is None:
            continue
        open_price = float(first_bar["open"])
        if open_price <= 0:
            continue
        returns.append((session_date, (float(last_bar["close"]) - open_price) / open_price))

    context: dict[date, dict[str, Any]] = {}
    for index, (session_date, value) in enumerate(returns):
        window = [item for _, item in returns[max(0, index - TRAILING_VOLATILITY_SESSIONS) : index]]
        context[session_date] = {
            "benchmark_return": value,
            "trailing_volatility": pstdev(window) if len(window) > 1 else None,
        }
    return context


def _regime_statistics(
    session_means: dict[date, float],
    context: dict[date, dict[str, Any]],
) -> dict[str, Any]:
    volatilities = [
        item["trailing_volatility"]
        for session_date, item in context.items()
        if session_date in session_means and item["trailing_volatility"] is not None
    ]
    median_volatility = (
        sorted(volatilities)[len(volatilities) // 2] if volatilities else None
    )

    def label(session_date: date) -> str | None:
        item = context.get(session_date)
        if item is None:
            return None
        return "market_up" if item["benchmark_return"] >= 0 else "market_down"

    def volatility_label(session_date: date) -> str | None:
        item = context.get(session_date)
        if item is None or item["trailing_volatility"] is None or median_volatility is None:
            return None
        return (
            "high_volatility"
            if item["trailing_volatility"] > median_volatility
            else "low_volatility"
        )

    direction = _subperiod_statistics(
        {key: value for key, value in session_means.items() if label(key)},
        label_of=lambda key: label(key) or "unclassified",
    )
    volatility = _subperiod_statistics(
        {key: value for key, value in session_means.items() if volatility_label(key)},
        label_of=lambda key: volatility_label(key) or "unclassified",
    )
    classified = sum(result["sessions"] for result in direction.values())
    return {
        "benchmark_sessions_matched": classified,
        "benchmark_coverage": (
            _round(classified / len(session_means)) if session_means else None
        ),
        "median_trailing_volatility": _round(median_volatility),
        "market_direction": direction,
        "volatility": volatility,
        "direction_stability": _stability(direction),
        "volatility_stability": _stability(volatility),
        # A factor that only works in one market direction is a conditional
        # claim, and is reported as such rather than as a general edge.
        "regime_independent": (
            _stability(direction)["stable"] and _stability(volatility)["stable"]
        ),
    }


def concentration_report(
    outcomes: Sequence[dict[str, Any]],
    *,
    sector_by_symbol: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Symbol and sector concentration of the evidence."""
    sectors = {
        str(key).upper(): str(value)
        for key, value in (sector_by_symbol or {}).items()
        if value
    }
    symbol_counts: Counter[str] = Counter(
        str(row["symbol"]).upper() for row in outcomes
    )
    sector_counts: Counter[str] = Counter(
        sectors.get(str(row["symbol"]).upper(), "unknown") for row in outcomes
    )
    total = max(1, len(outcomes))
    known = total - sector_counts.get("unknown", 0)
    return {
        "observations": len(outcomes),
        "distinct_symbols": len(symbol_counts),
        "max_symbol_share": _round(max(symbol_counts.values(), default=0) / total),
        "symbol_hhi": _round(sum((value / total) ** 2 for value in symbol_counts.values())),
        "largest_symbol": symbol_counts.most_common(1)[0][0] if symbol_counts else None,
        "distinct_sectors": len([key for key in sector_counts if key != "unknown"]),
        "max_sector_share": _round(
            max(
                (value for key, value in sector_counts.items() if key != "unknown"),
                default=0,
            )
            / total
        ),
        "sector_hhi": _round(
            sum(
                (value / known) ** 2
                for key, value in sector_counts.items()
                if key != "unknown"
            )
        )
        if known
        else None,
        "largest_sector": next(
            (
                key
                for key, _ in sector_counts.most_common()
                if key != "unknown"
            ),
            None,
        ),
        # Sector concentration computed over partial coverage is not a
        # measurement of the portfolio, so the coverage travels with it.
        "sector_coverage": _round(known / total),
    }


def effect_size_drift(
    discovery_metrics: dict[str, Any] | None,
    validation_metrics: dict[str, Any] | None,
) -> dict[str, Any]:
    """How far the estimate moved from the sample that found it."""
    discovery = (discovery_metrics or {}).get("gross_directional_edge_bps")
    validation = (validation_metrics or {}).get("gross_directional_edge_bps")
    if discovery is None or validation is None:
        return {
            "discovery_edge_bps": discovery,
            "validation_edge_bps": validation,
            "status": "not_comparable",
        }
    retained = validation / discovery if discovery else None
    return {
        "discovery_edge_bps": _round(discovery),
        "validation_edge_bps": _round(validation),
        "drift_bps": _round(validation - discovery),
        "retained_fraction": _round(retained),
        "sign_flipped": bool(discovery * validation < 0),
        # McLean and Pontiff find published predictors keep roughly half their
        # in-sample size out of sample; losing more than that is a warning,
        # not a disqualification, and is reported rather than gated on.
        "decay_beyond_published_norm": bool(
            retained is not None and retained < 0.5
        ),
        "status": "measured",
    }


def _meets_predeclared_power(
    *,
    observations: int,
    sessions: int,
    required_event_count: int | None,
    required_sessions: int | None,
    fallback_sessions: int | None,
) -> bool:
    """Did the sample reach the size the hypothesis declared it would need?

    When no requirement was predeclared there is nothing to hold the result
    to, and the observed-effect estimate is the only thing left -- but it is
    reported as a fallback rather than treated as the standard.
    """
    if required_event_count is None and required_sessions is None:
        return bool(fallback_sessions is not None and sessions >= fallback_sessions)
    if required_event_count is not None and observations < required_event_count:
        return False
    if required_sessions is not None and sessions < required_sessions:
        return False
    return True


def power_and_stability_report(
    observations: Sequence[dict[str, Any]],
    *,
    evidence_quality: dict[str, Any],
    net_evidence_quality: dict[str, Any] | None = None,
    benchmark_context: dict[date, dict[str, Any]] | None = None,
    sector_by_symbol: dict[str, str] | None = None,
    discovery_metrics: dict[str, Any] | None = None,
    validation_metrics: dict[str, Any] | None = None,
    trials_recorded: int | None = None,
    required_event_count: int | None = None,
    required_sessions: int | None = None,
) -> dict[str, Any]:
    """Assemble every diagnostic the confirmation protocol requires."""
    outcomes = [
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
            "symbol": row["symbol"],
        }
        for row in observations
        if row.get("score") is not None and row.get("target_return") is not None
    ]
    session_means = _session_means(outcomes)
    session_values = [session_means[key] for key in sorted(session_means)]
    dispersion_bps = (
        pstdev(session_values) * 10_000 if len(session_values) > 1 else None
    )
    observed_bps = evidence_quality.get("mean_return_bps")
    net_bps = (net_evidence_quality or {}).get("mean_return_bps")
    observations_per_session = (
        len(outcomes) / len(session_means) if session_means else None
    )

    def sessions_for(effect: float | None) -> int | None:
        return required_sessions_for_power(
            effect_bps=effect, session_dispersion_bps=dispersion_bps
        )

    required_gross = sessions_for(observed_bps)
    required_net = sessions_for(net_bps)
    quarters = _subperiod_statistics(
        session_means, label_of=lambda key: f"{key.year}Q{(key.month - 1) // 3 + 1}"
    )
    years = _subperiod_statistics(session_means, label_of=lambda key: str(key.year))

    return {
        "power_version": RESEARCH_POWER_VERSION,
        "power": {
            "observed_sessions": len(session_means),
            "observed_observations": len(outcomes),
            "observations_per_session": _round(observations_per_session),
            "session_dispersion_bps": _round(dispersion_bps),
            "observed_edge_bps": _round(observed_bps),
            "net_edge_bps": _round(net_bps),
            # Descriptive only. Sizing a requirement from the effect that was
            # measured is circular: when the true effect is near zero the
            # requirement diverges, so a real null would always look
            # underpowered and could never be retired.
            "sessions_required_for_the_observed_effect": required_gross,
            "sessions_required_for_the_observed_net_effect": required_net,
            "minimum_detectable_effect_bps": evidence_quality.get(
                "minimum_detectable_effect_bps_80pct_power"
            ),
            # The predeclared requirement, fixed before any result existed.
            "required_event_count": required_event_count,
            "required_sessions": required_sessions,
            "meets_required_event_count": (
                required_event_count is None or len(outcomes) >= required_event_count
            ),
            "meets_required_sessions": (
                required_sessions is None or len(session_means) >= required_sessions
            ),
            "adequately_powered": _meets_predeclared_power(
                observations=len(outcomes),
                sessions=len(session_means),
                required_event_count=required_event_count,
                required_sessions=required_sessions,
                fallback_sessions=required_gross,
            ),
            # A null is interpretable when the sample reached the size the
            # hypothesis declared it would need. Anything else lets a failed
            # idea live forever behind "not enough data".
            "null_result_is_interpretable": _meets_predeclared_power(
                observations=len(outcomes),
                sessions=len(session_means),
                required_event_count=required_event_count,
                required_sessions=required_sessions,
                fallback_sessions=required_gross,
            ),
        },
        "subperiods": {
            "quarterly": quarters,
            "annual": years,
            "quarterly_stability": _stability(quarters),
            "annual_stability": _stability(years),
        },
        "concentration": concentration_report(
            outcomes, sector_by_symbol=sector_by_symbol
        ),
        "regimes": (
            _regime_statistics(session_means, benchmark_context)
            if benchmark_context
            else {"status": "no_benchmark_context_supplied"}
        ),
        "effect_size_drift": effect_size_drift(discovery_metrics, validation_metrics),
        "bootstrap_confidence_interval_bps": evidence_quality.get("block_bootstrap", {}).get(
            "confidence_interval_95"
        ),
        "net_bootstrap_confidence_interval_bps": (
            (net_evidence_quality or {}).get("block_bootstrap", {}).get(
                "confidence_interval_95"
            )
        ),
        "trials": {
            "effective_trials_applied": evidence_quality.get("effective_trials"),
            "trials_recorded_in_ledger": trials_recorded,
            # Correcting for fewer trials than were actually run understates
            # the selection problem, so the mismatch is surfaced explicitly.
            "ledger_covers_applied_trials": (
                trials_recorded is None
                or trials_recorded <= int(evidence_quality.get("effective_trials") or 0)
            ),
        },
    }


def _round(value: float | None) -> float | None:
    return round(float(value), 6) if value is not None else None
