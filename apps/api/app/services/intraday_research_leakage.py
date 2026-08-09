"""Mechanical look-ahead proofs for intraday factor builders.

Reading a builder and concluding it does not peek is not evidence.  This
module proves it by experiment: perturb every bar from a cut point onward and
re-derive the factor.  A score that was supposed to be knowable before the cut
must come back bit-identical; a target that claims to span bars after the cut
must come back different.  The first catches look-ahead, the second catches
the opposite defect -- a target wired to stale bars, which produces a factor
that cannot predict anything no matter how real the effect is.

The module contains no research verdict, no campaign code, and no UI code.
"""

from __future__ import annotations

from datetime import datetime
from random import Random
from typing import Any, Sequence

from app.services.intraday_session_calendar import is_regular_session_bar

LEAKAGE_HARNESS_VERSION = "intraday_research_leakage_v1"

_PRICE_FIELDS = ("open", "high", "low", "close")
_PERTURBED_FEATURE_FIELDS = (
    "session_vwap",
    "distance_from_session_vwap",
    "session_relative_volume",
    "order_flow_imbalance",
    "normalized_order_flow_imbalance",
)
# Large enough that no float comparison confuses a real leak with rounding,
# small enough that threshold-conditioned factors still produce events.
_JITTER = 0.02
TIMING_FIELDS = (
    "signal_bar_timestamp",
    "decision_timestamp",
    "entry_bar_timestamp",
    "exit_bar_timestamp",
)


def perturb_future_candles(
    candles_by_symbol: dict[str, Sequence[dict[str, Any]]],
    *,
    cut: datetime,
    seed: int = 20260730,
) -> dict[str, list[dict[str, Any]]]:
    """Randomly re-draw every bar at or after ``cut``.

    Each OHLC field is jittered independently so that *returns* change, not
    just price levels: a single multiplicative shift would leave every
    return-based score identical and the whole experiment would prove nothing.
    """
    rng = Random(seed)
    perturbed: dict[str, list[dict[str, Any]]] = {}
    for symbol, rows in candles_by_symbol.items():
        output: list[dict[str, Any]] = []
        for row in sorted(rows, key=lambda item: item["timestamp"]):
            if row["timestamp"] < cut:
                output.append(dict(row))
                continue
            copy = dict(row)
            for field in _PRICE_FIELDS:
                if copy.get(field) is not None:
                    copy[field] = float(copy[field]) * (1 + rng.uniform(-_JITTER, _JITTER))
            high = max(float(copy[field]) for field in _PRICE_FIELDS if copy.get(field) is not None)
            low = min(float(copy[field]) for field in _PRICE_FIELDS if copy.get(field) is not None)
            copy["high"] = high
            copy["low"] = low
            if copy.get("volume") is not None:
                copy["volume"] = float(copy["volume"]) * (1 + rng.uniform(0.5, 1.5))
            for field in _PERTURBED_FEATURE_FIELDS:
                if copy.get(field) is not None:
                    copy[field] = float(copy[field]) * (1 + rng.uniform(-_JITTER, _JITTER))
            output.append(copy)
        perturbed[symbol] = output
    return perturbed


def _key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("factor_key"),
        row.get("symbol"),
        row.get("session_date"),
        row.get("entry_bar_timestamp") or row.get("timestamp"),
    )


def timing_assertions(
    observations: Sequence[dict[str, Any]],
    *,
    timeframe: str,
) -> dict[str, Any]:
    """Check the timing contract every observation must satisfy."""
    missing_fields = 0
    decision_after_entry = 0
    entry_after_exit = 0
    extended_hours_bars = 0
    for row in observations:
        if any(row.get(field) is None for field in TIMING_FIELDS):
            missing_fields += 1
            continue
        if row["decision_timestamp"] > row["entry_bar_timestamp"]:
            decision_after_entry += 1
        if row["entry_bar_timestamp"] > row["exit_bar_timestamp"]:
            entry_after_exit += 1
        for field in ("signal_bar_timestamp", "entry_bar_timestamp", "exit_bar_timestamp"):
            if not is_regular_session_bar(row[field], timeframe=timeframe):
                extended_hours_bars += 1
    checks = {
        "timing_fields_present": missing_fields == 0,
        "decision_precedes_entry": decision_after_entry == 0,
        "entry_precedes_exit": entry_after_exit == 0,
        "regular_session_bars_only": extended_hours_bars == 0,
    }
    return {
        "observations": len(observations),
        "observations_missing_timing_fields": missing_fields,
        "observations_deciding_after_entry": decision_after_entry,
        "observations_exiting_before_entry": entry_after_exit,
        "observations_touching_extended_hours": extended_hours_bars,
        "checks": checks,
        "passed": all(checks.values()),
    }


def future_perturbation_report(
    builder: Any,
    candles_by_symbol: dict[str, Sequence[dict[str, Any]]],
    *,
    timeframe: str,
    cut: datetime,
    seed: int = 20260730,
    **builder_kwargs: Any,
) -> dict[str, Any]:
    """Re-derive a factor over perturbed future bars and compare."""
    baseline = builder(candles_by_symbol, timeframe=timeframe, **builder_kwargs)
    perturbed_candles = perturb_future_candles(candles_by_symbol, cut=cut, seed=seed)
    perturbed = builder(perturbed_candles, timeframe=timeframe, **builder_kwargs)
    perturbed_by_key = {_key(row): row for row in perturbed}

    comparable_scores = 0
    leaked_scores = 0
    leaked_examples: list[dict[str, Any]] = []
    future_targets = 0
    responsive_targets = 0
    unmatched = 0
    for row in baseline:
        match = perturbed_by_key.get(_key(row))
        if match is None:
            unmatched += 1
            continue
        decision = row.get("decision_timestamp")
        if decision is not None and decision <= cut:
            comparable_scores += 1
            if float(row["score"]) != float(match["score"]):
                leaked_scores += 1
                if len(leaked_examples) < 5:
                    leaked_examples.append(
                        {
                            "symbol": row.get("symbol"),
                            "session_date": str(row.get("session_date")),
                            "decision_timestamp": str(decision),
                            "baseline_score": float(row["score"]),
                            "perturbed_score": float(match["score"]),
                        }
                    )
        entry = row.get("entry_bar_timestamp")
        if entry is not None and entry >= cut:
            future_targets += 1
            if float(row["target_return"]) != float(match["target_return"]):
                responsive_targets += 1

    target_responsiveness = (
        responsive_targets / future_targets if future_targets else None
    )
    checks = {
        # Any score that was knowable before the cut must be untouched by
        # data the researcher could not have had.
        "no_score_reads_the_future": leaked_scores == 0,
        # Absence of a leak is only meaningful if the perturbation reached the
        # data at all.  A target that ignores the perturbed bars is itself a
        # defect, so an unresponsive target fails rather than passing quietly.
        "targets_respond_to_future_bars": (
            target_responsiveness is not None and target_responsiveness >= 0.99
        ),
    }
    return {
        "cut": str(cut),
        "baseline_observations": len(baseline),
        "perturbed_observations": len(perturbed),
        "unmatched_observations": unmatched,
        "scores_comparable_before_cut": comparable_scores,
        "scores_changed_by_future_data": leaked_scores,
        "leak_examples": leaked_examples,
        "targets_spanning_the_cut": future_targets,
        "target_responsiveness": (
            round(target_responsiveness, 6) if target_responsiveness is not None else None
        ),
        "checks": checks,
        "passed": all(checks.values()),
    }


def _cut_points(
    candles_by_symbol: dict[str, Sequence[dict[str, Any]]],
    *,
    fractions: Sequence[float],
) -> list[datetime]:
    timestamps = sorted(
        {row["timestamp"] for rows in candles_by_symbol.values() for row in rows}
    )
    if len(timestamps) < 10:
        return []
    return [timestamps[int(len(timestamps) * fraction)] for fraction in fractions]


def audit_factor_leakage(
    specs: dict[str, Any],
    candles_by_symbol: dict[str, Sequence[dict[str, Any]]],
    *,
    timeframe: str,
    factor_keys: Sequence[str],
    cut_fractions: Sequence[float] = (0.25, 0.5, 0.75),
    cut_points: Sequence[datetime] | None = None,
    seed: int = 20260730,
    **builder_kwargs: Any,
) -> dict[str, Any]:
    """Run the timing contract and perturbation experiment for every factor."""
    cuts = list(cut_points) if cut_points is not None else _cut_points(
        candles_by_symbol, fractions=cut_fractions
    )
    factors: dict[str, Any] = {}
    for key in factor_keys:
        spec = specs[key]
        if timeframe not in spec.supported_timeframes:
            factors[key] = {"status": "unsupported_timeframe"}
            continue
        observations = spec.builder(
            candles_by_symbol, timeframe=timeframe, **builder_kwargs
        )
        if not observations:
            # An empty factor is not proof of safety, and must not be recorded
            # as a pass.
            factors[key] = {
                "status": "not_exercised",
                "detail": "The builder produced no observation on this dataset.",
                "passed": False,
            }
            continue
        timing = timing_assertions(observations, timeframe=timeframe)
        perturbations = [
            future_perturbation_report(
                spec.builder,
                candles_by_symbol,
                timeframe=timeframe,
                cut=cut,
                seed=seed,
                **builder_kwargs,
            )
            for cut in cuts
        ]
        exercised = [
            report for report in perturbations if report["scores_comparable_before_cut"] > 0
        ]
        factors[key] = {
            "status": "audited",
            "factor_type": spec.factor_type,
            "timing": timing,
            "perturbations": perturbations,
            "passed": (
                timing["passed"]
                and bool(exercised)
                and all(report["passed"] for report in exercised)
            ),
        }
    return {
        "harness_version": LEAKAGE_HARNESS_VERSION,
        "timeframe": timeframe,
        "cut_points": [str(cut) for cut in cuts],
        "factors": factors,
        "factors_failing": sorted(
            key for key, result in factors.items() if not result.get("passed", False)
        ),
        "passed": all(result.get("passed", False) for result in factors.values()),
    }
