"""Champion graduation gate (Phase 13.9).

`research_champion_import` produces deduped *research champions*: candidates
that looked good in the exact backtest that found them. This module decides
whether a champion survives outside that backtest, which is the only thing
that turns it into a final elite the portfolio solver may read.

The battery is deliberately the boring, well-documented one: hold out a later
period, re-run on other symbols, re-run on the sibling timeframe, double the
costs, check the drawdown under stress, confirm the trade count is real, and
refuse candidates that merely duplicate something already validated.

Two rules this module never breaks:

* Thresholds are never weakened to make a champion pass. `run_champion_validation`
  accepts threshold *overrides* so a research policy change is explicit and
  recorded, but it never relaxes anything on its own when a champion fails.
* "We could not measure this" is never reported as "this passed". A gate that
  could not be evaluated is `inconclusive`, and a run with any inconclusive
  gate lands in `needs_more_data`, not `validated`.

Split into two halves on purpose: everything above `champion_validation_queue`
is pure (measurements in, gate verdicts out) and unit-testable without a
database; everything below performs the measurements and persists evidence.
"""

from __future__ import annotations

import time
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.services.elite_portfolio_builder import (
    decision_hash,
    parameter_similarity_breakdown,
    pearson_correlation,
)
from app.services.strategy_discovery import jsonable
from app.services.strategy_research import finite_metric, profit_factor_passes

CHAMPION_VALIDATION_PROTOCOL_VERSION = "champion_validation_v1"

VALIDATION_STATES = (
    "pending_validation",
    "validating",
    "validated",
    "failed_validation",
    "needs_more_data",
)

GATE_PASSED = "passed"
GATE_FAILED = "failed"
GATE_INCONCLUSIVE = "inconclusive"

# A profit factor with zero losing trades is reported by the simulator as
# `profit_factor=None, profit_factor_is_infinite=True`. Ratio arithmetic still
# needs a number, so infinity is represented by this sentinel rather than
# silently collapsing to 0.0 (which would read as a catastrophic failure).
INFINITE_PROFIT_FACTOR = 999.0

# Bars `run_backtest` skips at the start of any dataset before it will open a
# position. A holdout slice is extended backwards by this much so the holdout
# period itself is fully tradeable rather than losing its first 50 bars.
SIMULATOR_WARMUP_BARS = 50

# Wall-clock budget for one `run_champion_validation` call. Sized to return
# comfortably inside the proxy and browser timeouts (see
# deploy/production/nginx/keftrade.conf and apps/web/lib/api.ts) so a large
# queue is drained by repeated calls that each report progress, rather than by
# one request that appears to hang and is eventually cut off mid-flight.
DEFAULT_RUN_BUDGET_SECONDS = 240.0

DEFAULT_VALIDATION_THRESHOLDS: dict[str, Any] = {
    # Unseen-period holdout. The split point is NOT chosen here -- it is read
    # from the candidate's own `walk_forward_train_ratio`, because that is what
    # decides which bars the promoted job actually traded. See
    # `_selection_split` for why choosing our own ratio produced a gate that
    # tested on the selection window and passed everything.
    "default_walk_forward_train_ratio": 0.70,
    "minimum_unseen_rows": 300,
    "minimum_selection_rows": 300,
    "minimum_out_of_sample_profit_factor": 1.10,
    "minimum_profit_factor_retention": 0.55,
    "minimum_out_of_sample_trades": 15,
    # Cross-asset
    "cross_symbol_sample": 4,
    "minimum_cross_symbols_tested": 2,
    "minimum_cross_symbols_passed": 2,
    "minimum_cross_symbol_profit_factor": 1.00,
    "minimum_cross_symbol_trades": 10,
    # Regime
    "minimum_regimes_passed": 2,
    "minimum_regime_trades": 3,
    # Stress
    "cost_stress_multiplier": 2.0,
    "minimum_cost_stress_profit_factor": 1.00,
    "maximum_stressed_drawdown": 0.18,
    # Timeframe stability
    "minimum_timeframe_stability_profit_factor": 1.00,
    "minimum_timeframe_stability_trades": 10,
    # Duplication
    "maximum_return_correlation": 0.85,
    "minimum_correlation_observations": 30,
    "maximum_parameter_similarity": 0.90,
}

GATE_LABELS: dict[str, str] = {
    "out_of_sample": "Out-of-sample period",
    "minimum_trades": "Minimum trade count",
    "cross_symbol": "Different symbol",
    "regime_robustness": "Different market regime",
    "cost_stress": "Costs and slippage stress",
    "drawdown_stress": "Drawdown stress",
    "timeframe_stability": "Timeframe stability",
    "correlation_duplication": "Correlation and duplication",
    "parameter_similarity": "Parameter similarity",
}

GATE_ORDER: tuple[str, ...] = (
    "out_of_sample",
    "minimum_trades",
    "cross_symbol",
    "regime_robustness",
    "cost_stress",
    "drawdown_stress",
    "timeframe_stability",
    "correlation_duplication",
    "parameter_similarity",
)


def validation_thresholds(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge caller overrides over the defaults, keeping only known keys.

    Unknown keys are rejected rather than silently ignored so a typo in a
    threshold name can never quietly leave the default gate in place while the
    caller believes they tightened it.
    """
    merged = dict(DEFAULT_VALIDATION_THRESHOLDS)
    for key, value in dict(overrides or {}).items():
        if key not in DEFAULT_VALIDATION_THRESHOLDS:
            raise ValueError(f"unknown champion validation threshold {key!r}")
        merged[key] = value
    return merged


def thresholds_weakened(thresholds: dict[str, Any]) -> list[str]:
    """Threshold keys the caller set looser than the shipped default.

    Reported, never blocked: a deliberate research-policy change is legitimate,
    but it has to be visible in the stored evidence instead of hiding inside a
    request parameter.
    """
    looser_when_lower = {
        "minimum_unseen_rows",
        "minimum_selection_rows",
        "minimum_out_of_sample_profit_factor",
        "minimum_profit_factor_retention",
        "minimum_out_of_sample_trades",
        "minimum_cross_symbols_tested",
        "minimum_cross_symbols_passed",
        "minimum_cross_symbol_profit_factor",
        "minimum_cross_symbol_trades",
        "minimum_regimes_passed",
        "minimum_regime_trades",
        "cost_stress_multiplier",
        "minimum_cost_stress_profit_factor",
        "minimum_correlation_observations",
        "minimum_timeframe_stability_profit_factor",
        "minimum_timeframe_stability_trades",
    }
    looser_when_higher = {
        "maximum_stressed_drawdown",
        "maximum_return_correlation",
        "maximum_parameter_similarity",
    }
    weakened: list[str] = []
    for key, default in DEFAULT_VALIDATION_THRESHOLDS.items():
        value = thresholds.get(key, default)
        if key in looser_when_lower and float(value) < float(default):
            weakened.append(key)
        elif key in looser_when_higher and float(value) > float(default):
            weakened.append(key)
    return sorted(weakened)


def profit_factor_value(metrics: dict[str, Any]) -> float:
    if metrics.get("profit_factor_is_infinite"):
        return INFINITE_PROFIT_FACTOR
    return finite_metric(metrics.get("profit_factor"))


def _measured(measurements: dict[str, Any], key: str) -> dict[str, Any] | None:
    row = measurements.get(key)
    if isinstance(row, dict) and row.get("status") == "measured":
        return row
    return None


def _unavailable_reason(measurements: dict[str, Any], key: str) -> str:
    row = measurements.get(key)
    if not isinstance(row, dict):
        return f"The {key.replace('_', ' ')} measurement was never attempted."
    return str(row.get("reason") or f"The {key.replace('_', ' ')} measurement is unavailable.")


def _gate(gate_id: str, status: str, detail: str, observed: dict[str, Any], required: dict[str, Any]) -> dict[str, Any]:
    return {
        "gate_id": gate_id,
        "label": GATE_LABELS[gate_id],
        "status": status,
        "detail": detail,
        "observed": observed,
        "required": required,
    }


def gate_out_of_sample(measurements: dict[str, Any], thresholds: dict[str, Any]) -> dict[str, Any]:
    """Does the edge survive on bars the promoted job never traded?

    `unseen_period` is the portion of the dataset the promoted job's own
    walk-forward split skipped, so no candidate was ever ranked or promoted on
    it. `selection_period` is the portion it did trade -- the window the
    champion's headline metrics came from, and therefore the window its
    selection is biased toward. Comparing the two is the whole point of the
    gate; measuring on the selection window would be self-confirming.
    """
    required = {
        "minimum_profit_factor": thresholds["minimum_out_of_sample_profit_factor"],
        "minimum_expectancy_per_trade": 0,
        "minimum_profit_factor_retention": thresholds["minimum_profit_factor_retention"],
    }
    unseen = _measured(measurements, "unseen_period")
    selection = _measured(measurements, "selection_period")
    if unseen is None:
        return _gate("out_of_sample", GATE_INCONCLUSIVE, _unavailable_reason(measurements, "unseen_period"), {}, required)
    if selection is None:
        return _gate(
            "out_of_sample",
            GATE_INCONCLUSIVE,
            "The unseen window ran but the selection window did not, so profit-factor "
            f"decay could not be measured. {_unavailable_reason(measurements, 'selection_period')}",
            {"unseen_profit_factor": profit_factor_value(unseen["metrics"])},
            required,
        )

    unseen_metrics = unseen["metrics"]
    selection_metrics = selection["metrics"]
    unseen_pf = profit_factor_value(unseen_metrics)
    selection_pf = profit_factor_value(selection_metrics)
    expectancy = finite_metric(unseen_metrics.get("expectancy_per_trade"))
    # An infinite profit factor (zero losing trades) is a sentinel, not a
    # measurement, so a ratio built from one would be an artifact rather than
    # evidence of decay. Retention is reported as null in that case and the
    # absolute floors below still apply.
    comparable = not (unseen_metrics.get("profit_factor_is_infinite") or selection_metrics.get("profit_factor_is_infinite"))
    retention = round(unseen_pf / selection_pf, 6) if comparable and selection_pf > 0 else None
    observed = {
        "selection_profit_factor": selection_pf,
        "unseen_profit_factor": unseen_pf,
        "profit_factor_retention": retention,
        "unseen_expectancy_per_trade": expectancy,
        "unseen_trades": int(finite_metric(unseen_metrics.get("number_of_trades"))),
        "selection_trades": int(finite_metric(selection_metrics.get("number_of_trades"))),
        "unseen_window": unseen.get("window"),
        "selection_window": selection.get("window"),
    }

    failures: list[str] = []
    if not profit_factor_passes(unseen_metrics, float(thresholds["minimum_out_of_sample_profit_factor"])):
        failures.append(f"profit factor on unseen bars {unseen_pf:.3f} < {float(thresholds['minimum_out_of_sample_profit_factor']):.2f}")
    if expectancy <= 0:
        failures.append(f"expectancy on unseen bars {expectancy:.4f} is not positive")
    if retention is not None and retention < float(thresholds["minimum_profit_factor_retention"]):
        failures.append(
            f"profit factor decayed to {retention:.0%} of the selection window "
            f"(minimum {float(thresholds['minimum_profit_factor_retention']):.0%})"
        )
    if failures:
        return _gate("out_of_sample", GATE_FAILED, "; ".join(failures) + ".", observed, required)
    return _gate(
        "out_of_sample",
        GATE_PASSED,
        f"Held up on bars the search never traded: profit factor {unseen_pf:.3f} on {observed['unseen_trades']} trades, "
        f"{'retention n/a' if retention is None else f'{retention:.0%} of the selection window'}.",
        observed,
        required,
    )


def gate_minimum_trades(measurements: dict[str, Any], thresholds: dict[str, Any]) -> dict[str, Any]:
    """Is the unseen-period result built on enough trades to mean anything?"""
    required = {"minimum_out_of_sample_trades": thresholds["minimum_out_of_sample_trades"]}
    unseen = _measured(measurements, "unseen_period")
    if unseen is None:
        return _gate("minimum_trades", GATE_INCONCLUSIVE, _unavailable_reason(measurements, "unseen_period"), {}, required)
    trades = int(finite_metric(unseen["metrics"].get("number_of_trades")))
    minimum = int(thresholds["minimum_out_of_sample_trades"])
    observed = {"unseen_trades": trades, "unseen_rows": unseen.get("row_count")}
    if trades < minimum:
        return _gate(
            "minimum_trades",
            GATE_FAILED,
            f"Only {trades} trade(s) on unseen bars over {unseen.get('row_count')} bars; "
            f"{minimum} are required before the metrics carry any weight.",
            observed,
            required,
        )
    return _gate("minimum_trades", GATE_PASSED, f"{trades} trades on unseen bars clears the {minimum}-trade floor.", observed, required)


def gate_cross_symbol(measurements: dict[str, Any], thresholds: dict[str, Any]) -> dict[str, Any]:
    """Is this an edge, or a fact about one ticker?"""
    required = {
        "minimum_symbols_tested": thresholds["minimum_cross_symbols_tested"],
        "minimum_symbols_passed": thresholds["minimum_cross_symbols_passed"],
        "minimum_profit_factor": thresholds["minimum_cross_symbol_profit_factor"],
        "minimum_trades": thresholds["minimum_cross_symbol_trades"],
    }
    rows = [row for row in (measurements.get("cross_symbol") or []) if isinstance(row, dict)]
    measured = [row for row in rows if row.get("status") == "measured"]
    minimum_tested = int(thresholds["minimum_cross_symbols_tested"])
    if len(measured) < minimum_tested:
        unavailable = [f"{row.get('symbol')}: {row.get('reason')}" for row in rows if row.get("status") != "measured"]
        return _gate(
            "cross_symbol",
            GATE_INCONCLUSIVE,
            f"Only {len(measured)} of the required {minimum_tested} alternate symbol(s) could be evaluated. "
            + ("Blocked by — " + "; ".join(unavailable[:4]) + "." if unavailable else "No alternate symbols were available in this dataset."),
            {"symbols_tested": len(measured), "unavailable": unavailable[:8]},
            required,
        )

    minimum_pf = float(thresholds["minimum_cross_symbol_profit_factor"])
    minimum_trades = int(thresholds["minimum_cross_symbol_trades"])
    evaluated: list[dict[str, Any]] = []
    for row in measured:
        metrics = row["metrics"]
        trades = int(finite_metric(metrics.get("number_of_trades")))
        expectancy = finite_metric(metrics.get("expectancy_per_trade"))
        passed = profit_factor_passes(metrics, minimum_pf) and expectancy > 0 and trades >= minimum_trades
        evaluated.append(
            {
                "symbol": row.get("symbol"),
                "profit_factor": profit_factor_value(metrics),
                "expectancy_per_trade": expectancy,
                "trades": trades,
                "passed": passed,
            }
        )
    passing = [row for row in evaluated if row["passed"]]
    observed = {"symbols_tested": len(evaluated), "symbols_passed": len(passing), "results": evaluated}
    minimum_passed = int(thresholds["minimum_cross_symbols_passed"])
    if len(passing) < minimum_passed:
        return _gate(
            "cross_symbol",
            GATE_FAILED,
            f"Only {len(passing)} of {len(evaluated)} alternate symbol(s) held up; {minimum_passed} are required. "
            "A single-symbol result is a property of that symbol, not an edge.",
            observed,
            required,
        )
    return _gate(
        "cross_symbol",
        GATE_PASSED,
        f"{len(passing)} of {len(evaluated)} alternate symbol(s) reproduced the edge "
        f"({', '.join(str(row['symbol']) for row in passing)}).",
        observed,
        required,
    )


def gate_regime_robustness(measurements: dict[str, Any], thresholds: dict[str, Any]) -> dict[str, Any]:
    """Did it make money in more than one kind of market?"""
    required = {
        "minimum_regimes_passed": thresholds["minimum_regimes_passed"],
        "minimum_trades_per_regime": thresholds["minimum_regime_trades"],
    }
    buckets = [row for row in (measurements.get("regime_buckets") or []) if isinstance(row, dict)]
    basis = str(measurements.get("regime_basis") or "unavailable")
    minimum_trades = int(thresholds["minimum_regime_trades"])
    minimum_passed = int(thresholds["minimum_regimes_passed"])
    if not buckets:
        return _gate(
            "regime_robustness",
            GATE_INCONCLUSIVE,
            "No regime or calendar buckets could be derived from the full-period run, so regime "
            "behaviour is unmeasured.",
            {"regime_basis": basis},
            required,
        )

    eligible = [row for row in buckets if int(finite_metric((row.get("metrics") or {}).get("number_of_trades"))) >= minimum_trades]
    detailed = [
        {
            "bucket": row.get("bucket"),
            "trades": int(finite_metric((row.get("metrics") or {}).get("number_of_trades"))),
            "expectancy_per_trade": finite_metric((row.get("metrics") or {}).get("expectancy_per_trade")),
            "profit_factor": profit_factor_value(row.get("metrics") or {}),
        }
        for row in buckets
    ]
    observed = {"regime_basis": basis, "buckets": detailed, "buckets_with_enough_trades": len(eligible)}
    if len(eligible) < minimum_passed:
        return _gate(
            "regime_robustness",
            GATE_INCONCLUSIVE,
            f"Only {len(eligible)} bucket(s) on the {basis} basis reached {minimum_trades} trades, so there is not "
            f"enough evidence to judge {minimum_passed} distinct regimes.",
            observed,
            required,
        )
    profitable = [row for row in eligible if finite_metric((row.get("metrics") or {}).get("expectancy_per_trade")) > 0]
    observed["buckets_profitable"] = len(profitable)
    if len(profitable) < minimum_passed:
        return _gate(
            "regime_robustness",
            GATE_FAILED,
            f"Profitable in only {len(profitable)} of {len(eligible)} measured {basis} bucket(s); "
            f"{minimum_passed} are required.",
            observed,
            required,
        )
    return _gate(
        "regime_robustness",
        GATE_PASSED,
        f"Positive expectancy in {len(profitable)} of {len(eligible)} {basis} bucket(s).",
        observed,
        required,
    )


def gate_cost_stress(measurements: dict[str, Any], thresholds: dict[str, Any]) -> dict[str, Any]:
    """Does the edge survive paying more than the backtest assumed?"""
    required = {
        "cost_multiplier": thresholds["cost_stress_multiplier"],
        "minimum_profit_factor": thresholds["minimum_cost_stress_profit_factor"],
        "minimum_expectancy_per_trade": 0,
    }
    stressed = _measured(measurements, "cost_stress")
    if stressed is None:
        return _gate("cost_stress", GATE_INCONCLUSIVE, _unavailable_reason(measurements, "cost_stress"), {}, required)
    baseline = _measured(measurements, "full")
    metrics = stressed["metrics"]
    stressed_pf = profit_factor_value(metrics)
    expectancy = finite_metric(metrics.get("expectancy_per_trade"))
    observed = {
        "stressed_profit_factor": stressed_pf,
        "baseline_profit_factor": profit_factor_value(baseline["metrics"]) if baseline else None,
        "stressed_expectancy_per_trade": expectancy,
        "stressed_trades": int(finite_metric(metrics.get("number_of_trades"))),
        "applied_costs": stressed.get("parameter_overrides"),
    }
    minimum_pf = float(thresholds["minimum_cost_stress_profit_factor"])
    failures: list[str] = []
    if not profit_factor_passes(metrics, minimum_pf):
        failures.append(f"profit factor falls to {stressed_pf:.3f} (< {minimum_pf:.2f})")
    if expectancy <= 0:
        failures.append(f"expectancy falls to {expectancy:.4f}")
    multiplier = float(thresholds["cost_stress_multiplier"])
    if failures:
        return _gate(
            "cost_stress",
            GATE_FAILED,
            f"At {multiplier:g}× fees and slippage the edge disappears: " + "; ".join(failures) + ".",
            observed,
            required,
        )
    return _gate(
        "cost_stress",
        GATE_PASSED,
        f"Still profitable at {multiplier:g}× fees and slippage (profit factor {stressed_pf:.3f}).",
        observed,
        required,
    )


def gate_drawdown_stress(measurements: dict[str, Any], thresholds: dict[str, Any]) -> dict[str, Any]:
    """What is the worst drawdown across every window and stress we ran?"""
    required = {"maximum_drawdown": thresholds["maximum_stressed_drawdown"]}
    observations: list[dict[str, Any]] = []
    for key in ("full", "selection_period", "unseen_period", "cost_stress", "timeframe_stability"):
        row = _measured(measurements, key)
        if row is not None:
            observations.append({"run": key, "max_drawdown": finite_metric(row["metrics"].get("max_drawdown"))})
    for row in measurements.get("cross_symbol") or []:
        if isinstance(row, dict) and row.get("status") == "measured":
            observations.append(
                {"run": f"cross_symbol:{row.get('symbol')}", "max_drawdown": finite_metric(row["metrics"].get("max_drawdown"))}
            )
    if not observations:
        return _gate(
            "drawdown_stress",
            GATE_INCONCLUSIVE,
            "No run completed, so no drawdown could be observed.",
            {},
            required,
        )
    worst = max(observations, key=lambda row: row["max_drawdown"])
    maximum = float(thresholds["maximum_stressed_drawdown"])
    observed = {"worst_run": worst["run"], "worst_max_drawdown": worst["max_drawdown"], "observations": observations}
    if worst["max_drawdown"] > maximum:
        return _gate(
            "drawdown_stress",
            GATE_FAILED,
            f"Worst drawdown {worst['max_drawdown']:.2%} on the {worst['run']} run exceeds the {maximum:.2%} stress limit.",
            observed,
            required,
        )
    return _gate(
        "drawdown_stress",
        GATE_PASSED,
        f"Worst drawdown across {len(observations)} run(s) was {worst['max_drawdown']:.2%}, inside the {maximum:.2%} limit.",
        observed,
        required,
    )


def gate_timeframe_stability(measurements: dict[str, Any], thresholds: dict[str, Any]) -> dict[str, Any]:
    """Is the result a property of the strategy or of one bar size?"""
    required = {
        "minimum_profit_factor": thresholds["minimum_timeframe_stability_profit_factor"],
        "minimum_trades": thresholds["minimum_timeframe_stability_trades"],
        "minimum_expectancy_per_trade": 0,
    }
    sibling = _measured(measurements, "timeframe_stability")
    if sibling is None:
        return _gate("timeframe_stability", GATE_INCONCLUSIVE, _unavailable_reason(measurements, "timeframe_stability"), {}, required)
    metrics = sibling["metrics"]
    pf = profit_factor_value(metrics)
    trades = int(finite_metric(metrics.get("number_of_trades")))
    expectancy = finite_metric(metrics.get("expectancy_per_trade"))
    observed = {
        "sibling_timeframe": sibling.get("timeframe"),
        "sibling_profit_factor": pf,
        "sibling_expectancy_per_trade": expectancy,
        "sibling_trades": trades,
    }
    minimum_pf = float(thresholds["minimum_timeframe_stability_profit_factor"])
    minimum_trades = int(thresholds["minimum_timeframe_stability_trades"])
    if trades < minimum_trades:
        return _gate(
            "timeframe_stability",
            GATE_INCONCLUSIVE,
            f"The {sibling.get('timeframe')} run produced only {trades} trade(s); {minimum_trades} are needed "
            "before the comparison says anything.",
            observed,
            required,
        )
    if not profit_factor_passes(metrics, minimum_pf) or expectancy <= 0:
        return _gate(
            "timeframe_stability",
            GATE_FAILED,
            f"The same strategy on {sibling.get('timeframe')} returns profit factor {pf:.3f} and expectancy "
            f"{expectancy:.4f}: the result does not survive a change of bar size.",
            observed,
            required,
        )
    return _gate(
        "timeframe_stability",
        GATE_PASSED,
        f"Holds on {sibling.get('timeframe')} too (profit factor {pf:.3f} on {trades} trades).",
        observed,
        required,
    )


def gate_correlation_duplication(measurements: dict[str, Any], thresholds: dict[str, Any]) -> dict[str, Any]:
    """Would this add anything to the elites that already exist?"""
    required = {
        "maximum_absolute_correlation": thresholds["maximum_return_correlation"],
        "minimum_observations": thresholds["minimum_correlation_observations"],
    }
    peers = [row for row in (measurements.get("peer_correlations") or []) if isinstance(row, dict)]
    if not peers:
        return _gate(
            "correlation_duplication",
            GATE_PASSED,
            "No validated elite exists yet to duplicate.",
            {"peers_compared": 0},
            required,
        )
    minimum_observations = int(thresholds["minimum_correlation_observations"])
    usable = [
        row
        for row in peers
        if row.get("coefficient") is not None and int(row.get("observations") or 0) >= minimum_observations
    ]
    if not usable:
        return _gate(
            "correlation_duplication",
            GATE_INCONCLUSIVE,
            f"None of the {len(peers)} validated elite(s) share {minimum_observations} overlapping daily observations "
            "with this champion, so overlap is unmeasured.",
            {"peers_compared": len(peers), "peers_with_enough_overlap": 0},
            required,
        )
    worst = max(usable, key=lambda row: abs(float(row["coefficient"])))
    maximum = float(thresholds["maximum_return_correlation"])
    observed = {
        "peers_compared": len(peers),
        "peers_with_enough_overlap": len(usable),
        "maximum_absolute_correlation": round(abs(float(worst["coefficient"])), 6),
        "most_correlated_peer": worst.get("peer_key"),
        "observations": int(worst.get("observations") or 0),
    }
    if abs(float(worst["coefficient"])) > maximum:
        return _gate(
            "correlation_duplication",
            GATE_FAILED,
            f"Daily returns correlate {float(worst['coefficient']):.3f} with existing elite {worst.get('peer_key')} "
            f"over {worst.get('observations')} observations, above the {maximum:.2f} limit: this is a duplicate, "
            "not a new source of return.",
            observed,
            required,
        )
    return _gate(
        "correlation_duplication",
        GATE_PASSED,
        f"Highest overlap with an existing elite is {abs(float(worst['coefficient'])):.3f}, inside the {maximum:.2f} limit.",
        observed,
        required,
    )


def gate_parameter_similarity(measurements: dict[str, Any], thresholds: dict[str, Any]) -> dict[str, Any]:
    """Is this a re-parameterisation of an elite that already occupies this slot?

    Compared only against elites in the *same* (symbol, timeframe, family) slot.
    Identical parameters on a different symbol are cross-asset confirmation, not
    duplication. The portfolio solver's global `PARAMETER_SIMILARITY` hard rule
    still applies at construction time and is untouched by this gate, so nothing
    here lets two near-identical candidates into one portfolio.
    """
    required = {"maximum_similarity": thresholds["maximum_parameter_similarity"]}
    peers = [row for row in (measurements.get("peer_parameter_similarity") or []) if isinstance(row, dict)]
    if not peers:
        return _gate(
            "parameter_similarity",
            GATE_PASSED,
            "No validated elite occupies this symbol/timeframe/family slot.",
            {"peers_compared": 0},
            required,
        )
    worst = max(peers, key=lambda row: float(row.get("similarity") or 0))
    similarity = float(worst.get("similarity") or 0)
    maximum = float(thresholds["maximum_parameter_similarity"])
    observed = {
        "peers_compared": len(peers),
        "maximum_similarity": round(similarity, 6),
        "most_similar_peer": worst.get("peer_key"),
    }
    if similarity > maximum:
        return _gate(
            "parameter_similarity",
            GATE_FAILED,
            f"Parameters are {similarity:.2%} identical to existing elite {worst.get('peer_key')} in the same slot, "
            f"above the {maximum:.0%} limit.",
            observed,
            required,
        )
    return _gate(
        "parameter_similarity",
        GATE_PASSED,
        f"Closest same-slot elite is {similarity:.2%} similar, inside the {maximum:.0%} limit.",
        observed,
        required,
    )


GATE_EVALUATORS = {
    "out_of_sample": gate_out_of_sample,
    "minimum_trades": gate_minimum_trades,
    "cross_symbol": gate_cross_symbol,
    "regime_robustness": gate_regime_robustness,
    "cost_stress": gate_cost_stress,
    "drawdown_stress": gate_drawdown_stress,
    "timeframe_stability": gate_timeframe_stability,
    "correlation_duplication": gate_correlation_duplication,
    "parameter_similarity": gate_parameter_similarity,
}


def evaluate_gates(measurements: dict[str, Any], thresholds: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    resolved = validation_thresholds(thresholds)
    return [GATE_EVALUATORS[gate_id](measurements, resolved) for gate_id in GATE_ORDER]


def classify_validation(gates: list[dict[str, Any]]) -> tuple[str, str]:
    """Turn gate verdicts into a champion state.

    Any failure demotes the whole run; an unmeasurable gate is never treated as
    a pass, so `needs_more_data` (not `validated`) is the outcome when evidence
    is missing.
    """
    failed = [gate for gate in gates if gate["status"] == GATE_FAILED]
    inconclusive = [gate for gate in gates if gate["status"] == GATE_INCONCLUSIVE]
    if failed:
        return "failed_validation", "Failed " + ", ".join(gate["label"].lower() for gate in failed) + "."
    if inconclusive:
        return "needs_more_data", "Could not measure " + ", ".join(gate["label"].lower() for gate in inconclusive) + "."
    return "validated", f"Passed all {len(gates)} graduation gates."


def gate_counts(gates: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "passed": sum(1 for gate in gates if gate["status"] == GATE_PASSED),
        "failed": sum(1 for gate in gates if gate["status"] == GATE_FAILED),
        "inconclusive": sum(1 for gate in gates if gate["status"] == GATE_INCONCLUSIVE),
    }


# ---------------------------------------------------------------------------
# Measurement + persistence
# ---------------------------------------------------------------------------

_SWING_TIMEFRAMES = ("1h", "4h")


class ChampionValidationError(RuntimeError):
    """A champion could not be measured at all (bad payload, missing job)."""


def _selection_split(candidate: dict[str, Any], thresholds: dict[str, Any]) -> float:
    """Row fraction dividing bars the promoted job never traded from bars it did.

    `run_backtest` does not merely score the walk-forward split -- it refuses to
    open a position before it, opening trades only from
    `len(rows) * walk_forward_train_ratio` onward. Every promoted job's headline
    metrics therefore come from the *tail* of its dataset, and that tail is the
    window its promotion was selected on. The head is the only part of the
    dataset no candidate was ever ranked on.

    Choosing our own holdout ratio here instead of reading the candidate's own
    made this gate test on the tail -- the selection window -- which is
    self-confirming and passed every champion it saw.
    """
    ratio = (candidate.get("parameters") or {}).get("walk_forward_train_ratio")
    try:
        parsed = float(ratio)
    except (TypeError, ValueError):
        parsed = float(thresholds["default_walk_forward_train_ratio"])
    if not 0.0 < parsed < 1.0:
        # 0 or 1 means the job traded everything (or nothing was skipped), so
        # there is no untouched head to test on. Fall back to the documented
        # default rather than producing an empty unseen window; the row-count
        # floors below still reject a split that cannot be measured.
        parsed = float(thresholds["default_walk_forward_train_ratio"])
    return max(0.0, min(1.0, parsed))


def _candidate_kind(candidate: dict[str, Any]) -> str:
    from app.services.labs.intraday.cross_sectional_dataset import is_cross_sectional_candidate
    from app.services.labs.intraday.families.registry import is_intraday_lab_candidate

    if is_cross_sectional_candidate(candidate):
        return "cross_sectional"
    if is_intraday_lab_candidate(candidate):
        return "intraday"
    return "swing"


def _sibling_timeframes(kind: str, timeframe: str) -> list[str]:
    from app.services.labs.intraday.dataset import SUPPORTED_INTRADAY_TIMEFRAMES

    family = SUPPORTED_INTRADAY_TIMEFRAMES if kind in {"intraday", "cross_sectional"} else _SWING_TIMEFRAMES
    return [value for value in family if value != timeframe]


def _load_manifest(conn: psycopg.Connection, dataset_id: int | None) -> dict[str, Any] | None:
    if dataset_id is None:
        return None
    row = conn.execute(
        "SELECT id, assets, timeframes FROM research_dataset_manifests WHERE id = %s",
        (dataset_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": int(row["id"]),
        "assets": [str(value).upper() for value in (row["assets"] or [])],
        "timeframes": [str(value) for value in (row["timeframes"] or [])],
    }


def _live_rows_available(conn: psycopg.Connection, kind: str, symbol: str, timeframe: str) -> int:
    table = "intraday_features" if kind in {"intraday", "cross_sectional"} else "features"
    row = conn.execute(
        f"SELECT COUNT(*) AS rows FROM {table} WHERE symbol = %s AND timeframe = %s",  # noqa: S608 - table name is a literal from a two-value map
        (symbol, timeframe),
    ).fetchone()
    return int((row or {}).get("rows") or 0)


def _resolve_source(
    conn: psycopg.Connection,
    *,
    kind: str,
    symbol: str,
    timeframe: str,
    dataset_id: int | None,
    manifest: dict[str, Any] | None,
    require_frozen: bool,
    minimum_rows: int,
) -> tuple[int | None, str] | None:
    """Where to read `symbol`/`timeframe` from, or None when it is unavailable.

    The frozen snapshot is always preferred. Live tables are a fallback for the
    robustness probes only (other symbols, sibling timeframe) and every
    measurement records which source it used, so a snapshot result and a live
    result are never confused for one another.
    """
    if manifest and symbol.upper() in manifest["assets"] and timeframe in manifest["timeframes"]:
        return manifest["id"], "snapshot"
    if kind == "cross_sectional":
        # The ranking universe only exists inside a snapshot manifest.
        return None
    if require_frozen:
        return None
    if _live_rows_available(conn, kind, symbol, timeframe) >= minimum_rows:
        return None, "live"
    return None


def _load_dataset(
    conn: psycopg.Connection,
    *,
    kind: str,
    symbol: str,
    timeframe: str,
    dataset_id: int | None,
    lookback_bars: int,
    cache: dict[Any, Any],
) -> dict[str, Any]:
    key = (kind, dataset_id, symbol, timeframe, lookback_bars)
    if key in cache:
        return cache[key]
    if kind == "cross_sectional":
        from app.services.labs.intraday.cross_sectional_dataset import load_cross_sectional_intraday_dataset

        if dataset_id is None:
            raise ChampionValidationError("cross-sectional candidates require a frozen dataset snapshot")
        dataset = load_cross_sectional_intraday_dataset(conn, symbol, timeframe, dataset_id=dataset_id, lookback_bars=lookback_bars)
    elif kind == "intraday":
        from app.services.labs.intraday.dataset import load_intraday_backtest_dataset

        dataset = load_intraday_backtest_dataset(conn, symbol, timeframe, dataset_id=dataset_id)
    else:
        from app.services.research_campaigns import load_campaign_dataset

        dataset = load_campaign_dataset(conn, symbol, timeframe, False, dataset_id=dataset_id)
    while len(cache) >= 6:
        cache.pop(next(iter(cache)))
    cache[key] = dataset
    return dataset


def _validation_candidate(candidate_payload: dict[str, Any], *, kind: str, timeframe: str, overrides: dict[str, Any]) -> Any:
    """Rebuild the stored candidate with validation-run parameter overrides.

    `walk_forward_train_ratio` is pinned to 0 for every validation run so the
    in-sample and out-of-sample slices are executed identically and their
    profit factors are directly comparable; the holdout split is done here, by
    slicing rows, rather than by the simulator's own internal split.

    `frequency_screen_min_opportunities` is disabled because it is calibrated
    for a full dataset and would short-circuit a deliberately shortened window
    before any trade is simulated. The trade-count floor is enforced directly
    by `gate_minimum_trades` on the measured result instead, so nothing is
    loosened by turning it off here.
    """
    from dataclasses import replace as dataclass_replace

    from app.services.research_campaigns import apply_timeframe_scaling, candidate_from_payload

    candidate = candidate_from_payload(candidate_payload)
    parameters = {
        **candidate.parameters,
        "walk_forward_train_ratio": 0.0,
        "frequency_screen_min_opportunities": 0,
    }
    if kind in {"intraday", "cross_sectional"}:
        from app.services.labs.intraday.feature_engine_v2 import DEFAULT_CONFIG

        parameters["timeframe"] = timeframe
        parameters["recent_candle_window_bars"] = int(
            candidate.parameters.get("recent_candle_window_bars") or DEFAULT_CONFIG.lookback_bars
        )
        candidate = dataclass_replace(candidate, parameters={**parameters, **overrides})
        return candidate
    candidate = dataclass_replace(candidate, parameters={**parameters, **overrides})
    return apply_timeframe_scaling(candidate, timeframe)


def _simulate(
    conn: psycopg.Connection,
    *,
    kind: str,
    candidate_payload: dict[str, Any],
    symbol: str,
    timeframe: str,
    dataset_id: int | None,
    data_source: str,
    cache: dict[Any, Any],
    window: tuple[float, float] | None = None,
    parameter_overrides: dict[str, Any] | None = None,
    keep_series: bool = False,
    minimum_rows: int = 0,
) -> dict[str, Any]:
    """Run one measurement, returning a `measured` or `unavailable` record.

    Never raises for a data problem: an unusable dataset becomes an
    `unavailable` measurement with the real reason attached, which downstream
    becomes an inconclusive gate rather than a silent pass or a lost run.
    """
    from app.services.backtester import combine_candles_features
    from app.services.strategy_discovery import evaluate_candidate

    label_window = {"start_fraction": window[0], "end_fraction": window[1]} if window else None
    try:
        lookback_bars = int((candidate_payload.get("parameters") or {}).get("cross_sectional_lookback_bars", 8))
        dataset = _load_dataset(
            conn,
            kind=kind,
            symbol=symbol,
            timeframe=timeframe,
            dataset_id=dataset_id,
            lookback_bars=lookback_bars,
            cache=cache,
        )
        rows = combine_candles_features(dataset["candles"], dataset["features"])
        total_rows = len(rows)
        if window is None:
            start_index, end_index = 0, total_rows
        else:
            start_index = max(0, int(total_rows * float(window[0])) - SIMULATOR_WARMUP_BARS)
            end_index = min(total_rows, int(total_rows * float(window[1])))
        sliced = rows[start_index:end_index]
        if len(sliced) < max(minimum_rows, SIMULATOR_WARMUP_BARS + 1):
            return {
                "status": "unavailable",
                "symbol": symbol,
                "timeframe": timeframe,
                "window": label_window,
                "reason": (
                    f"{symbol} {timeframe} yields {len(sliced)} usable bar(s) in this window; "
                    f"at least {max(minimum_rows, SIMULATOR_WARMUP_BARS + 1)} are required."
                ),
            }

        session_end_index = None
        if dataset.get("session_end_index") is not None:
            from app.services.labs.intraday.dataset import build_session_end_index

            session_end_index = build_session_end_index(sliced)

        candidate = _validation_candidate(
            candidate_payload,
            kind=kind,
            timeframe=timeframe,
            overrides=dict(parameter_overrides or {}),
        )
        result = evaluate_candidate(
            candidate,
            [row["candle"] for row in sliced],
            [row["feature"] for row in sliced],
            dataset.get("context_by_time") or {},
            market_arrays=None,
            session_end_index=session_end_index,
            persist_bar_series=keep_series,
        )
    except Exception as error:  # noqa: BLE001 - any loader/simulator failure is evidence, not a crash
        return {
            "status": "unavailable",
            "symbol": symbol,
            "timeframe": timeframe,
            "window": label_window,
            "reason": f"{type(error).__name__}: {error}",
        }

    metrics = dict(result.get("metrics") or {})
    measurement: dict[str, Any] = {
        "status": "measured",
        "symbol": symbol,
        "timeframe": timeframe,
        "data_source": data_source,
        "dataset_id": dataset_id,
        "window": label_window,
        "row_count": len(sliced),
        "first_timestamp": _timestamp(sliced[0]["candle"].get("timestamp")),
        "last_timestamp": _timestamp(sliced[-1]["candle"].get("timestamp")),
        "metrics": {key: value for key, value in metrics.items() if key != "walk_forward"},
        "parameter_overrides": dict(parameter_overrides or {}),
    }
    if keep_series:
        measurement["_result"] = {
            "strategy_returns": dict(result.get("strategy_returns") or {}),
            "trades": list(result.get("trades") or []),
        }
        measurement["_regime_analysis"] = dict(result.get("regime_analysis") or {})
    return measurement


def _timestamp(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)


def _regime_buckets(regime_analysis: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Pick the most informative bucketing available for the regime gate.

    Real trend/volatility regimes when the dataset carries them (swing
    campaigns), otherwise calendar years, which every run produces. The basis
    is returned alongside the buckets so the stored verdict says which one was
    actually used rather than implying a regime label that never existed.
    """
    for key, basis in (("by_market_regime", "market_regime"), ("by_volatility_regime", "volatility_regime")):
        rows = [row for row in (regime_analysis.get(key) or []) if str(row.get("regime") or "unknown") != "unknown"]
        if len(rows) >= 2:
            return basis, [{"bucket": str(row.get("regime")), "metrics": dict(row.get("metrics") or {})} for row in rows]
    years = regime_analysis.get("by_year") or []
    if years:
        return "calendar_year", [{"bucket": str(row.get("year")), "metrics": dict(row.get("metrics") or {})} for row in years]
    return "unavailable", []


def _cross_symbol_pool(
    conn: psycopg.Connection,
    *,
    manifest: dict[str, Any] | None,
    symbol: str,
    campaign_id: int | None,
    timeframe: str,
    limit: int,
) -> list[str]:
    """Deterministic alternate-symbol list: manifest assets first, then the
    campaign's own universe. Sorted so the same champion always draws the same
    comparison set and a re-run is reproducible."""
    pool: list[str] = []
    if manifest:
        pool = [value for value in manifest["assets"] if value != symbol.upper()]
    if len(pool) < limit and campaign_id is not None:
        rows = conn.execute(
            """
            SELECT DISTINCT UPPER(symbol) AS symbol
            FROM research_campaign_jobs
            WHERE campaign_id = %s AND timeframe = %s AND UPPER(symbol) <> %s
            ORDER BY 1
            """,
            (campaign_id, timeframe, symbol.upper()),
        ).fetchall()
        for row in rows:
            value = str(row["symbol"])
            if value not in pool:
                pool.append(value)
    return sorted(set(pool))[:limit] if pool else []


def _peer_evidence(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Already-final elites, read through the same pipeline the portfolio
    solver uses, so the duplication gate and the solver see identical evidence."""
    from app.services.elite_portfolio_repository import load_elite_candidate_variants

    peers: list[dict[str, Any]] = []
    for variant in load_elite_candidate_variants(conn):
        peers.append(
            {
                "peer_key": variant["candidate_key"],
                "symbol": variant["symbol"],
                "timeframe": variant["timeframe"],
                "family_id": variant["family_id"],
                "parameters": dict(variant.get("parameters") or {}),
                "strategy_returns": dict(variant.get("strategy_returns") or {}),
            }
        )
    return peers


def _duplication_measurements(
    *,
    champion: dict[str, Any],
    champion_returns: dict[str, float],
    peers: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    correlations: list[dict[str, Any]] = []
    similarities: list[dict[str, Any]] = []
    for peer in peers:
        coefficient, observations, _ = pearson_correlation(champion_returns, peer["strategy_returns"])
        correlations.append(
            {
                "peer_key": peer["peer_key"],
                "coefficient": None if coefficient is None else round(float(coefficient), 6),
                "observations": observations,
            }
        )
        same_slot = (
            peer["symbol"] == champion["symbol"]
            and peer["timeframe"] == champion["timeframe"]
            and peer["family_id"] == champion["family_id"]
        )
        if same_slot:
            breakdown = parameter_similarity_breakdown(champion["parameters"], peer["parameters"])
            similarities.append(
                {
                    "peer_key": peer["peer_key"],
                    "similarity": breakdown["overall_similarity"],
                    "compared_parameter_count": breakdown["compared_parameter_count"],
                }
            )
    return correlations, similarities


def measure_champion(
    conn: psycopg.Connection,
    champion: dict[str, Any],
    *,
    thresholds: dict[str, Any],
    peers: list[dict[str, Any]],
    cache: dict[Any, Any],
    require_frozen: bool = False,
) -> dict[str, Any]:
    """Run every measurement the gates need for one champion."""
    from app.services.elite_portfolio_repository import aligned_daily_evidence

    candidate_payload = dict(champion["candidate"] or {})
    if not candidate_payload.get("candidate_id"):
        raise ChampionValidationError(f"champion {champion['candidate_id']} has no usable candidate payload")

    kind = _candidate_kind(candidate_payload)
    symbol = str(champion["symbol"]).upper()
    timeframe = str(champion["timeframe"])
    dataset_id = int(champion["dataset_id"]) if champion.get("dataset_id") is not None else None
    manifest = _load_manifest(conn, dataset_id)
    minimum_rows = int(thresholds["minimum_unseen_rows"]) + int(thresholds["minimum_selection_rows"])
    split = _selection_split(candidate_payload, thresholds)

    measurements: dict[str, Any] = {
        "protocol_version": CHAMPION_VALIDATION_PROTOCOL_VERSION,
        "candidate_kind": kind,
        "symbol": symbol,
        "timeframe": timeframe,
        "dataset_id": dataset_id,
        "selection_split": split,
        "cross_symbol": [],
        "backtests_executed": 0,
    }

    def record(key: str, value: dict[str, Any]) -> dict[str, Any]:
        measurements["backtests_executed"] = int(measurements["backtests_executed"]) + 1
        measurements[key] = value
        return value

    common = {
        "kind": kind,
        "candidate_payload": candidate_payload,
        "symbol": symbol,
        "timeframe": timeframe,
        "dataset_id": dataset_id,
        "data_source": "snapshot" if dataset_id is not None else "live",
        "cache": cache,
    }

    full = record("full", _simulate(conn, **common, keep_series=True, minimum_rows=minimum_rows))
    # rows[:split] is what the promoted job's walk-forward split skipped -- no
    # candidate was ever ranked on it. rows[split:] is what it traded, and
    # therefore what its promotion was selected on. See `_selection_split`.
    record(
        "unseen_period",
        _simulate(conn, **common, window=(0.0, split), minimum_rows=int(thresholds["minimum_unseen_rows"])),
    )
    record(
        "selection_period",
        _simulate(conn, **common, window=(split, 1.0), minimum_rows=int(thresholds["minimum_selection_rows"])),
    )

    multiplier = float(thresholds["cost_stress_multiplier"])
    base_parameters = dict(candidate_payload.get("parameters") or {})
    base_fee = float(base_parameters.get("fee_rate") or 0)
    base_slippage = float(base_parameters.get("slippage_rate") or 0)
    if base_fee <= 0 and base_slippage <= 0:
        # Multiplying zero costs would re-run the identical backtest and report
        # it as "survived the stress". An unmeasurable stress test is reported
        # as unmeasurable.
        measurements["cost_stress"] = {
            "status": "unavailable",
            "symbol": symbol,
            "timeframe": timeframe,
            "reason": (
                "This candidate was backtested with zero fees and zero slippage, so multiplying them "
                "changes nothing. Re-run the research job with realistic costs before the stress test "
                "can mean anything."
            ),
        }
    else:
        record(
            "cost_stress",
            _simulate(
                conn,
                **common,
                parameter_overrides={"fee_rate": base_fee * multiplier, "slippage_rate": base_slippage * multiplier},
            ),
        )

    for peer_symbol in _cross_symbol_pool(
        conn,
        manifest=manifest,
        symbol=symbol,
        campaign_id=champion.get("campaign_id"),
        timeframe=timeframe,
        limit=int(thresholds["cross_symbol_sample"]),
    ):
        source = _resolve_source(
            conn,
            kind=kind,
            symbol=peer_symbol,
            timeframe=timeframe,
            dataset_id=dataset_id,
            manifest=manifest,
            require_frozen=require_frozen,
            minimum_rows=minimum_rows,
        )
        if source is None:
            measurements["cross_symbol"].append(
                {
                    "status": "unavailable",
                    "symbol": peer_symbol,
                    "timeframe": timeframe,
                    "reason": f"No {'frozen ' if require_frozen else ''}{timeframe} dataset is available for {peer_symbol}.",
                }
            )
            continue
        peer_dataset_id, peer_source = source
        measurements["backtests_executed"] = int(measurements["backtests_executed"]) + 1
        measurements["cross_symbol"].append(
            _simulate(
                conn,
                kind=kind,
                candidate_payload=candidate_payload,
                symbol=peer_symbol,
                timeframe=timeframe,
                dataset_id=peer_dataset_id,
                data_source=peer_source,
                cache=cache,
            )
        )

    sibling_options = _sibling_timeframes(kind, timeframe)
    sibling_measurement: dict[str, Any] | None = None
    for sibling in sibling_options:
        source = _resolve_source(
            conn,
            kind=kind,
            symbol=symbol,
            timeframe=sibling,
            dataset_id=dataset_id,
            manifest=manifest,
            require_frozen=require_frozen,
            minimum_rows=minimum_rows,
        )
        if source is None:
            sibling_measurement = sibling_measurement or {
                "status": "unavailable",
                "symbol": symbol,
                "timeframe": sibling,
                "reason": (
                    f"No {sibling} dataset is available for {symbol}. Snapshot both timeframes "
                    "before this stability check can run."
                ),
            }
            continue
        sibling_dataset_id, sibling_source = source
        measurements["backtests_executed"] = int(measurements["backtests_executed"]) + 1
        sibling_measurement = _simulate(
            conn,
            kind=kind,
            candidate_payload=candidate_payload,
            symbol=symbol,
            timeframe=sibling,
            dataset_id=sibling_dataset_id,
            data_source=sibling_source,
            cache=cache,
        )
        break
    measurements["timeframe_stability"] = sibling_measurement or {
        "status": "unavailable",
        "symbol": symbol,
        "timeframe": None,
        "reason": f"No sibling timeframe exists for {timeframe}.",
    }

    regime_analysis = full.pop("_regime_analysis", {}) if isinstance(full, dict) else {}
    basis, buckets = _regime_buckets(regime_analysis)
    measurements["regime_basis"] = basis
    measurements["regime_buckets"] = buckets

    champion_result = full.pop("_result", {}) if isinstance(full, dict) else {}
    champion_returns, _ = aligned_daily_evidence(champion_result) if champion_result else ({}, {})
    correlations, similarities = _duplication_measurements(
        champion={
            "symbol": symbol,
            "timeframe": timeframe,
            "family_id": str(champion.get("family_id") or ""),
            "parameters": base_parameters,
        },
        champion_returns=champion_returns,
        peers=peers,
    )
    measurements["peer_correlations"] = correlations
    measurements["peer_parameter_similarity"] = similarities
    measurements["champion_daily_observations"] = len(champion_returns)
    measurements["_champion_returns"] = champion_returns
    return measurements


CHAMPION_QUEUE_SQL = """
    SELECT
        e.id,
        e.candidate_id,
        e.campaign_id,
        e.family_id,
        e.research_score,
        e.profit_factor,
        e.expectancy,
        e.max_drawdown,
        e.trade_count,
        e.promotion_state,
        e.validation_state,
        e.validation_state_reason,
        e.validated_at,
        e.last_validation_run_id,
        e.strategy_direction,
        j.id AS research_job_id,
        UPPER(j.symbol) AS symbol,
        j.timeframe AS timeframe,
        j.candidate AS candidate,
        j.dataset_id AS dataset_id,
        COALESCE(j.strategy_family, e.family_id) AS strategy_family
    FROM elite_research_candidates e
    JOIN LATERAL (
        SELECT id, symbol, timeframe, candidate, dataset_id, strategy_family
        FROM research_campaign_jobs
        WHERE campaign_id = e.campaign_id
          AND candidate_id = e.candidate_id
          AND status IN ('completed', 'promoted')
          AND simulation_only = TRUE
        ORDER BY (status = 'promoted') DESC, validation_score DESC, id DESC
        LIMIT 1
    ) j ON TRUE
    WHERE e.simulation_only = TRUE
      AND e.promotion_state = 'research_champion'
"""


def champion_validation_queue(conn: psycopg.Connection, *, limit: int = 25) -> dict[str, Any]:
    """What is waiting for validation, and what did the last runs conclude."""
    bounded = max(1, min(int(limit), 200))
    counts = conn.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE promotion_state = 'research_champion') AS research_champions,
            COUNT(*) FILTER (WHERE promotion_state = 'research_champion' AND validation_state = 'pending_validation') AS pending_validation,
            COUNT(*) FILTER (WHERE promotion_state = 'research_champion' AND validation_state = 'validating') AS validating,
            COUNT(*) FILTER (WHERE promotion_state = 'research_champion' AND validation_state = 'failed_validation') AS failed_validation,
            COUNT(*) FILTER (WHERE promotion_state = 'research_champion' AND validation_state = 'needs_more_data') AS needs_more_data,
            COUNT(*) FILTER (WHERE promotion_state = 'elite') AS final_elites,
            COUNT(*) FILTER (WHERE promotion_state = 'elite' AND validation_state = 'validated') AS graduated_elites
        FROM elite_research_candidates
        WHERE simulation_only = TRUE
        """
    ).fetchone()
    rows = conn.execute(
        CHAMPION_QUEUE_SQL
        + """
          AND e.validation_state IN ('pending_validation', 'needs_more_data', 'failed_validation', 'validating')
        ORDER BY
            CASE e.validation_state
                WHEN 'pending_validation' THEN 0
                WHEN 'needs_more_data' THEN 1
                WHEN 'validating' THEN 2
                ELSE 3
            END,
            e.research_score DESC,
            e.id ASC
        LIMIT %s
        """,
        (bounded,),
    ).fetchall()
    queue = [
        {
            "elite_candidate_id": int(row["id"]),
            "candidate_id": row["candidate_id"],
            "campaign_id": row["campaign_id"],
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "family_id": row["strategy_family"],
            "research_score": float(row["research_score"] or 0),
            "profit_factor": float(row["profit_factor"] or 0),
            "expectancy": float(row["expectancy"] or 0),
            "max_drawdown": float(row["max_drawdown"] or 0),
            "trade_count": int(row["trade_count"] or 0),
            "validation_state": row["validation_state"],
            "validation_state_reason": row["validation_state_reason"],
            "dataset_id": row["dataset_id"],
        }
        for row in rows
    ]
    summary = {key: int((counts or {}).get(key) or 0) for key in (
        "research_champions",
        "pending_validation",
        "validating",
        "failed_validation",
        "needs_more_data",
        "final_elites",
        "graduated_elites",
    )}
    return {
        "protocol_version": CHAMPION_VALIDATION_PROTOCOL_VERSION,
        "gates": [{"gate_id": gate_id, "label": GATE_LABELS[gate_id]} for gate_id in GATE_ORDER],
        "thresholds": DEFAULT_VALIDATION_THRESHOLDS,
        "queue": queue,
        "simulation_only": True,
        **summary,
    }


def champion_validation_diagnostics(conn: psycopg.Connection, *, limit: int = 25) -> dict[str, Any]:
    """Why champions are failing, grouped the way the next research decision needs.

    Phase 5 of the graduation plan is "stop expanding dead families, keep
    expanding the near-passes". That decision needs failures grouped by gate,
    family, symbol, and timeframe rather than a flat list of rejected rows.
    """
    bounded = max(1, min(int(limit), 100))
    by_gate = conn.execute(
        """
        SELECT g.gate_id, g.label, g.status, COUNT(*) AS candidates
        FROM elite_champion_validation_gates g
        JOIN elite_research_candidates e ON e.last_validation_run_id = g.run_id
        GROUP BY g.gate_id, g.label, g.status
        ORDER BY g.gate_id, g.status
        """
    ).fetchall()
    by_group = conn.execute(
        """
        SELECT
            r.family_id,
            r.symbol,
            r.timeframe,
            COUNT(*) FILTER (WHERE r.status = 'validated') AS validated,
            COUNT(*) FILTER (WHERE r.status = 'failed_validation') AS failed_validation,
            COUNT(*) FILTER (WHERE r.status = 'needs_more_data') AS needs_more_data,
            COUNT(*) AS runs
        FROM elite_champion_validation_runs r
        JOIN elite_research_candidates e
          ON e.last_validation_run_id = r.id
        GROUP BY r.family_id, r.symbol, r.timeframe
        ORDER BY validated DESC, runs DESC
        LIMIT %s
        """,
        (bounded,),
    ).fetchall()
    recent = conn.execute(
        """
        SELECT id, elite_candidate_id, candidate_id, symbol, timeframe, family_id, status,
               state_reason, gates_passed, gates_failed, gates_inconclusive, backtests_executed,
               completed_at, runtime_ms
        FROM elite_champion_validation_runs
        ORDER BY id DESC
        LIMIT %s
        """,
        (bounded,),
    ).fetchall()
    return {
        "protocol_version": CHAMPION_VALIDATION_PROTOCOL_VERSION,
        "by_gate": [dict(row) for row in by_gate],
        "by_group": [dict(row) for row in by_group],
        "recent_runs": [jsonable(dict(row)) for row in recent],
        "simulation_only": True,
    }


def champion_validation_run(conn: psycopg.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM elite_champion_validation_runs WHERE id = %s",
        (int(run_id),),
    ).fetchone()
    if not row:
        raise ChampionValidationError(f"no champion validation run {run_id}")
    gates = conn.execute(
        "SELECT gate_id, label, status, detail, observed, required FROM elite_champion_validation_gates WHERE run_id = %s ORDER BY id",
        (int(run_id),),
    ).fetchall()
    return jsonable({**dict(row), "gate_results": [dict(gate) for gate in gates]})


def run_champion_validation(
    conn: psycopg.Connection,
    *,
    limit: int = 5,
    elite_candidate_ids: list[int] | None = None,
    threshold_overrides: dict[str, Any] | None = None,
    revalidate: bool = False,
    require_frozen: bool = False,
    max_runtime_seconds: float = DEFAULT_RUN_BUDGET_SECONDS,
) -> dict[str, Any]:
    """Validate the next champions in the queue and graduate the survivors.

    A champion that passes every gate becomes `promotion_state = 'elite'`,
    which is the only thing that makes it visible to the portfolio solver.
    Everything else keeps its champion state and carries the reason it did not
    graduate, so re-running after more data arrives is the natural next step.

    Bounded by `max_runtime_seconds` as well as `limit`. Each champion costs a
    full battery of backtests, so a large queue cannot finish inside one HTTP
    request; rather than hold a connection open until something upstream times
    out and the caller sees a hang with nothing to show for it, the run stops
    at the budget and reports `remaining` so the caller can simply call again.
    Every champion commits its own verdict, so stopping early never loses work.
    """
    # The queue query below is already bounded to champions in an eligible
    # validation_state, so a caller can request "the whole queue" via a large
    # limit without needing to know its exact size up front.
    bounded = max(1, min(int(limit), 2000))
    thresholds = validation_thresholds(threshold_overrides)
    weakened = thresholds_weakened(thresholds)
    if weakened:
        raise ValueError(
            "champion validation thresholds may not be weakened: "
            + ", ".join(weakened)
            + ". Tighten them or change the shipped defaults deliberately."
        )

    states = (
        ["pending_validation", "needs_more_data", "failed_validation", "validating"]
        if revalidate
        else ["pending_validation", "needs_more_data"]
    )
    if elite_candidate_ids:
        rows = conn.execute(
            CHAMPION_QUEUE_SQL + " AND e.id = ANY(%s) ORDER BY e.research_score DESC, e.id ASC LIMIT %s",
            ([int(value) for value in elite_candidate_ids], bounded),
        ).fetchall()
    else:
        rows = conn.execute(
            CHAMPION_QUEUE_SQL
            + " AND e.validation_state = ANY(%s) ORDER BY e.research_score DESC, e.id ASC LIMIT %s",
            (states, bounded),
        ).fetchall()

    peers = _peer_evidence(conn)
    cache: dict[Any, Any] = {}
    outcomes: list[dict[str, Any]] = []
    graduated = 0
    budget = max(0.0, float(max_runtime_seconds))
    batch_started = time.perf_counter()
    budget_exhausted = False

    for raw in rows:
        # Checked before starting a champion, never mid-champion: a partially
        # measured champion has no verdict to record, so abandoning one would
        # only waste the backtests already run.
        if outcomes and budget and (time.perf_counter() - batch_started) >= budget:
            budget_exhausted = True
            break
        champion = dict(raw)
        started = time.perf_counter()
        conn.execute(
            """
            UPDATE elite_research_candidates
            SET validation_state = 'validating',
                validation_state_reason = 'Champion validation in progress.',
                validation_protocol_version = %s,
                validation_started_at = NOW()
            WHERE id = %s
            """,
            (CHAMPION_VALIDATION_PROTOCOL_VERSION, champion["id"]),
        )
        conn.commit()
        try:
            measurements = measure_champion(
                conn,
                champion,
                thresholds=thresholds,
                peers=peers,
                cache=cache,
                require_frozen=require_frozen,
            )
            champion_returns = measurements.pop("_champion_returns", {})
            gates = [GATE_EVALUATORS[gate_id](measurements, thresholds) for gate_id in GATE_ORDER]
            status, reason = classify_validation(gates)
        except Exception as error:  # noqa: BLE001 - a broken champion must not abort the batch
            conn.rollback()
            _record_error(conn, champion, thresholds, error)
            outcomes.append(
                {
                    "elite_candidate_id": int(champion["id"]),
                    "candidate_id": champion["candidate_id"],
                    "symbol": champion["symbol"],
                    "timeframe": champion["timeframe"],
                    "status": "error",
                    "reason": f"{type(error).__name__}: {error}",
                }
            )
            continue

        counts = gate_counts(gates)
        runtime_ms = int((time.perf_counter() - started) * 1000)
        run_id = _persist_run(
            conn,
            champion=champion,
            status=status,
            reason=reason,
            thresholds=thresholds,
            measurements=measurements,
            gates=gates,
            counts=counts,
            runtime_ms=runtime_ms,
        )
        promoted = status == "validated"
        conn.execute(
            """
            UPDATE elite_research_candidates
            SET validation_state = %s,
                validation_state_reason = %s,
                validation_protocol_version = %s,
                last_validation_run_id = %s,
                validated_at = CASE WHEN %s THEN NOW() ELSE validated_at END,
                promotion_state = CASE WHEN %s THEN 'elite' ELSE promotion_state END,
                candidate_level = CASE WHEN %s THEN 'cluster_elite' ELSE candidate_level END,
                promotion_rule_version = %s,
                demotion_reason = CASE WHEN %s THEN NULL ELSE %s END,
                reevaluated_at = NOW()
            WHERE id = %s
            """,
            (
                status,
                reason,
                CHAMPION_VALIDATION_PROTOCOL_VERSION,
                run_id,
                promoted,
                promoted,
                promoted,
                CHAMPION_VALIDATION_PROTOCOL_VERSION,
                promoted,
                reason,
                champion["id"],
            ),
        )
        conn.commit()

        if promoted:
            graduated += 1
            # A champion that just graduated becomes a peer immediately, so two
            # near-identical champions in the same batch cannot both graduate.
            peers.append(
                {
                    "peer_key": f"{champion['candidate_id']}|{champion['symbol']}|{champion['timeframe']}",
                    "symbol": champion["symbol"],
                    "timeframe": champion["timeframe"],
                    "family_id": str(champion.get("strategy_family") or champion.get("family_id") or ""),
                    "parameters": dict((champion["candidate"] or {}).get("parameters") or {}),
                    "strategy_returns": champion_returns,
                }
            )

        outcomes.append(
            {
                "elite_candidate_id": int(champion["id"]),
                "candidate_id": champion["candidate_id"],
                "symbol": champion["symbol"],
                "timeframe": champion["timeframe"],
                "family_id": champion["strategy_family"],
                "run_id": run_id,
                "status": status,
                "reason": reason,
                "gates_passed": counts["passed"],
                "gates_failed": counts["failed"],
                "gates_inconclusive": counts["inconclusive"],
                "backtests_executed": int(measurements.get("backtests_executed") or 0),
                "runtime_ms": runtime_ms,
                "failed_gates": [gate["gate_id"] for gate in gates if gate["status"] == GATE_FAILED],
                "inconclusive_gates": [gate["gate_id"] for gate in gates if gate["status"] == GATE_INCONCLUSIVE],
            }
        )

    status = champion_validation_queue(conn)
    return {
        "protocol_version": CHAMPION_VALIDATION_PROTOCOL_VERSION,
        "examined": len(outcomes),
        "validated": graduated,
        "failed_validation": sum(1 for row in outcomes if row["status"] == "failed_validation"),
        "needs_more_data": sum(1 for row in outcomes if row["status"] == "needs_more_data"),
        "errors": sum(1 for row in outcomes if row["status"] == "error"),
        "thresholds": thresholds,
        "thresholds_weakened": False,
        "outcomes": outcomes,
        # True when the run stopped on its time budget with champions still
        # queued. The caller is expected to call again; nothing is lost.
        "budget_exhausted": budget_exhausted,
        "remaining": int(status.get("pending_validation") or 0),
        "runtime_seconds": round(time.perf_counter() - batch_started, 3),
        "status": status,
        "simulation_only": True,
    }


def _persist_run(
    conn: psycopg.Connection,
    *,
    champion: dict[str, Any],
    status: str,
    reason: str,
    thresholds: dict[str, Any],
    measurements: dict[str, Any],
    gates: list[dict[str, Any]],
    counts: dict[str, int],
    runtime_ms: int,
) -> int:
    stored_measurements = jsonable({key: value for key, value in measurements.items() if not key.startswith("_")})
    evidence_hash = decision_hash(
        {
            "protocol_version": CHAMPION_VALIDATION_PROTOCOL_VERSION,
            "candidate_id": champion["candidate_id"],
            "campaign_id": champion["campaign_id"],
            "symbol": champion["symbol"],
            "timeframe": champion["timeframe"],
            "thresholds": thresholds,
            "measurements": stored_measurements,
            "gates": gates,
        }
    )
    row = conn.execute(
        """
        INSERT INTO elite_champion_validation_runs(
            elite_candidate_id, campaign_id, candidate_id, research_job_id, dataset_id,
            symbol, timeframe, family_id, protocol_version, status, state_reason,
            thresholds, measurements, gates, gates_passed, gates_failed, gates_inconclusive,
            backtests_executed, thresholds_weakened, evidence_hash, completed_at, runtime_ms,
            simulation_only
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, FALSE, %s, NOW(), %s,
            TRUE
        )
        RETURNING id
        """,
        (
            champion["id"],
            champion["campaign_id"],
            champion["candidate_id"],
            champion["research_job_id"],
            champion["dataset_id"],
            champion["symbol"],
            champion["timeframe"],
            champion["strategy_family"] or champion["family_id"],
            CHAMPION_VALIDATION_PROTOCOL_VERSION,
            status,
            reason,
            Jsonb(jsonable(thresholds)),
            Jsonb(stored_measurements),
            Jsonb(jsonable(gates)),
            counts["passed"],
            counts["failed"],
            counts["inconclusive"],
            int(measurements.get("backtests_executed") or 0),
            evidence_hash,
            runtime_ms,
        ),
    ).fetchone()
    run_id = int(row["id"])
    for gate in gates:
        conn.execute(
            """
            INSERT INTO elite_champion_validation_gates(
                run_id, elite_candidate_id, gate_id, label, status, detail, observed, required
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, gate_id) DO NOTHING
            """,
            (
                run_id,
                champion["id"],
                gate["gate_id"],
                gate["label"],
                gate["status"],
                gate["detail"],
                Jsonb(jsonable(gate["observed"])),
                Jsonb(jsonable(gate["required"])),
            ),
        )
    return run_id


def _record_error(conn: psycopg.Connection, champion: dict[str, Any], thresholds: dict[str, Any], error: Exception) -> None:
    """Store the failure and hand the champion back to the queue.

    An execution error is not a verdict: the champion returns to
    `needs_more_data` rather than `failed_validation`, so a broken loader can
    never masquerade as evidence that a strategy is bad.
    """
    message = f"{type(error).__name__}: {error}"
    conn.execute(
        """
        INSERT INTO elite_champion_validation_runs(
            elite_candidate_id, campaign_id, candidate_id, research_job_id, dataset_id,
            symbol, timeframe, family_id, protocol_version, status, state_reason,
            thresholds, evidence_hash, error, completed_at, simulation_only
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, 'error', %s,
            %s, %s, %s, NOW(), TRUE
        )
        """,
        (
            champion["id"],
            champion["campaign_id"],
            champion["candidate_id"],
            champion["research_job_id"],
            champion["dataset_id"],
            champion["symbol"],
            champion["timeframe"],
            champion["strategy_family"] or champion["family_id"],
            CHAMPION_VALIDATION_PROTOCOL_VERSION,
            message[:500],
            Jsonb(jsonable(thresholds)),
            decision_hash({"candidate_id": champion["candidate_id"], "error": message}),
            message[:2000],
        ),
    )
    conn.execute(
        """
        UPDATE elite_research_candidates
        SET validation_state = 'needs_more_data',
            validation_state_reason = %s,
            validation_protocol_version = %s,
            reevaluated_at = NOW()
        WHERE id = %s
        """,
        (f"Validation could not run: {message[:400]}", CHAMPION_VALIDATION_PROTOCOL_VERSION, champion["id"]),
    )
    conn.commit()
