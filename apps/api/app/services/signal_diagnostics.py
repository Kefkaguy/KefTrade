"""The cheap test that belongs before an expensive one.

A campaign is currently the *first* test an idea gets: 2,000 jobs of full
trading simulation -- entries, stops, targets, sizing, costs, gates -- that
return a single P&L number conflating all of them. When it fails you cannot
tell which part failed, so the only available move is to run it again. That is
how one family accumulated 2,233 validation batteries and zero elites.

This module answers the prior question, at a fraction of the cost: **does the
signal predict anything at all?** It runs the family's own `decide()` -- the
identical function the campaign trades, so this cannot measure something
different from what gets simulated -- collects the bars where it fires, and
measures the forward return. No stops, no targets, no position sizing, no
exits. If a signal has no predictive content, no execution improvement rescues
it, and there is nothing for a campaign to find.

**Why not a rank IC.** For a continuously-ranked signal (the cross-sectional
families) rank IC is the right statistic, and
`cross_sectional_portfolio.spearman` already computes it. But most families
here emit a sparse gate -- "setup" on a few percent of bars, "avoid"
otherwise. A Spearman correlation over a vector that is mostly ties is
dominated by the ties and says nothing. The correct analogue for an event-style
signal is the mean forward return conditional on firing, which is what this
measures.

**Three things that make the number honest:**

  * *No lookahead.* The signal is read at bar i and the return is measured
    from bar i+1's open, the same convention `run_backtest` fills at.
  * *Excess over drift, not raw return.* A long-only signal on a rising market
    shows a positive edge with no skill whatsoever. Every figure here is net
    of the unconditional forward return over the same horizon, so what is
    reported is what the *timing* added.
  * *Measured against cost.* An edge of 3bps is not a finding when the round
    trip costs 30bps. The verdict compares the two directly, because that
    comparison -- not statistical significance alone -- is what decides
    whether a campaign is worth running.
"""

from __future__ import annotations

from math import sqrt
from statistics import fmean, pstdev
from typing import Any, Callable, Sequence

import psycopg
from psycopg.types.json import Jsonb

SIGNAL_DIAGNOSTICS_VERSION = "signal_diagnostics_v1"

# Horizons in bars. The horizon where edge peaks is itself the finding: it is
# the natural holding period, and it is the input the cost/horizon arithmetic
# needs.
DEFAULT_HORIZONS: tuple[int, ...] = (1, 2, 4, 8, 16, 32)

# Below this many firings the mean is not a measurement.
MINIMUM_SIGNALS_FOR_A_VERDICT = 50

# Testing several horizons and keeping the best inflates significance, so the
# bar is set above the conventional 2.0 rather than at it.
MINIMUM_T_STATISTIC = 3.0

# Warm-up bars before the first signal is honored, matching `run_backtest`'s
# own `i = max(start_index, 50)` so indicators are comparably warm.
WARMUP_BARS = 50

# A job still unclaimed after this long means no worker is consuming the
# queue. Generous enough to tolerate a worker busy with a previous job on its
# poll interval, short enough that a missing worker is reported in seconds
# rather than after a ten-minute client timeout.
UNCLAIMED_JOB_STALL_SECONDS = 45


def _forward_returns(rows: Sequence[dict[str, Any]], horizon: int) -> list[float | None]:
    """Return from bar i+1's open to bar i+1+horizon's open, for every i.

    Indexed by the SIGNAL bar, not the entry bar, so a caller that has a
    signal at i can read `forward[i]` directly and cannot accidentally shift
    it the wrong way.
    """
    forward: list[float | None] = [None] * len(rows)
    for index in range(len(rows) - horizon - 1):
        entry = float(rows[index + 1]["candle"]["open"])
        exit_price = float(rows[index + 1 + horizon]["candle"]["open"])
        forward[index] = (exit_price - entry) / entry if entry > 0 else None
    return forward


def measure_signal_edge(
    rows: Sequence[dict[str, Any]],
    decide: Callable[..., Any],
    params: dict[str, Any],
    *,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    warmup: int = WARMUP_BARS,
    recent_window_bars: int = 0,
    signal_start_timestamp: Any | None = None,
) -> dict[str, Any]:
    """Signal edge per horizon, with no trading simulation of any kind."""
    from app.services.strategy import reset_strategy_state

    reset_strategy_state(decide)
    candle_rows = [row["candle"] for row in rows]

    signals: list[tuple[int, int]] = []  # (bar index, +1 long / -1 short)
    eligible_bars = 0
    for index in range(warmup, len(rows)):
        row = rows[index]
        start = max(0, index + 1 - recent_window_bars) if recent_window_bars else 0
        decision = decide(row["candle"], row["feature"], candle_rows[start : index + 1], params)
        if (
            signal_start_timestamp is not None
            and row["candle"]["timestamp"] <= signal_start_timestamp
        ):
            continue
        eligible_bars += 1
        if getattr(decision, "signal", None) != "setup":
            continue
        direction = 1 if str(getattr(decision, "direction", "long")) == "long" else -1
        signals.append((index, direction))

    by_horizon: list[dict[str, Any]] = []
    for horizon in horizons:
        forward = _forward_returns(rows, horizon)
        measurable = [
            value
            for index, value in enumerate(forward)
            if value is not None
            and (
                signal_start_timestamp is None
                or rows[index]["candle"]["timestamp"] > signal_start_timestamp
            )
        ]
        if not measurable:
            continue
        unconditional = fmean(measurable)

        # Excess is paired per signal: what the timing added over simply being
        # in the market on that side for the same number of bars.
        excess = [
            direction * (forward[index] - unconditional)
            for index, direction in signals
            if forward[index] is not None
        ]
        raw = [
            direction * forward[index]
            for index, direction in signals
            if forward[index] is not None
        ]
        if not excess:
            continue

        mean_excess = fmean(excess)
        deviation = pstdev(excess) if len(excess) > 1 else 0.0
        t_statistic = mean_excess / (deviation / sqrt(len(excess))) if deviation > 0 else None
        by_horizon.append(
            {
                "horizon_bars": horizon,
                "signals": len(excess),
                "raw_edge_bps": round(fmean(raw) * 10_000, 4),
                "unconditional_drift_bps": round(unconditional * 10_000, 4),
                "excess_edge_bps": round(mean_excess * 10_000, 4),
                "t_statistic": round(t_statistic, 4) if t_statistic is not None else None,
                "hit_rate": round(sum(1 for value in excess if value > 0) / len(excess), 4),
            }
        )

    return {
        "signal_count": len(signals),
        "bars_evaluated": eligible_bars,
        "signal_rate": round(len(signals) / max(1, eligible_bars), 6),
        "long_signals": sum(1 for _, direction in signals if direction > 0),
        "short_signals": sum(1 for _, direction in signals if direction < 0),
        "by_horizon": by_horizon,
    }


def round_trip_cost_bps(params: dict[str, Any] | None = None) -> float:
    """Round-trip cost in basis points of notional, from the live parameters."""
    from app.services.labs.intraday.families.v2.base import BASE_V2_PARAMETERS

    source = params or BASE_V2_PARAMETERS
    fee = float(source.get("fee_rate", BASE_V2_PARAMETERS["fee_rate"]))
    slippage = float(source.get("slippage_rate", BASE_V2_PARAMETERS["slippage_rate"]))
    return round(2 * (fee + slippage) * 10_000, 4)


def summarize_edge(measurement: dict[str, Any], *, cost_bps: float) -> dict[str, Any]:
    """Pick the decisive horizon and reach a verdict against cost.

    The best horizon is chosen by t-statistic rather than by raw edge: the
    largest edge in a sweep is frequently the noisiest one, and picking it is
    how a horizon sweep turns into a selection bias.
    """
    horizons = [row for row in measurement["by_horizon"] if row["t_statistic"] is not None]
    if measurement["signal_count"] < MINIMUM_SIGNALS_FOR_A_VERDICT:
        return {
            "verdict": "insufficient_signals",
            "detail": (
                f"{measurement['signal_count']} firings is below the {MINIMUM_SIGNALS_FOR_A_VERDICT} "
                "needed to measure a mean."
            ),
            "best_horizon_bars": None,
            "excess_edge_bps": None,
            "t_statistic": None,
            "clears_cost": False,
        }
    if not horizons:
        return {
            "verdict": "not_measurable",
            "detail": "No horizon produced a variance to test against.",
            "best_horizon_bars": None,
            "excess_edge_bps": None,
            "t_statistic": None,
            "clears_cost": False,
        }

    best = max(horizons, key=lambda row: row["t_statistic"])
    significant = best["t_statistic"] >= MINIMUM_T_STATISTIC
    clears_cost = best["excess_edge_bps"] > cost_bps

    if not significant:
        verdict = "no_signal"
        detail = (
            f"Best horizon {best['horizon_bars']} bars gives {best['excess_edge_bps']:.2f}bps excess at "
            f"t={best['t_statistic']:.2f} — indistinguishable from timing luck. A campaign would spend "
            "full compute to confirm this."
        )
    elif not clears_cost:
        verdict = "signal_below_cost"
        detail = (
            f"Real but uneconomic: {best['excess_edge_bps']:.2f}bps excess at t={best['t_statistic']:.2f} "
            f"against a {cost_bps:.2f}bps round trip. Widen the stop or lengthen the hold before running "
            "a campaign; the signal is not the problem."
        )
    else:
        verdict = "predictive"
        detail = (
            f"{best['excess_edge_bps']:.2f}bps excess over drift at {best['horizon_bars']} bars "
            f"(t={best['t_statistic']:.2f}), clearing a {cost_bps:.2f}bps round trip. Worth a campaign."
        )

    return {
        "verdict": verdict,
        "detail": detail,
        "best_horizon_bars": best["horizon_bars"],
        "excess_edge_bps": best["excess_edge_bps"],
        "t_statistic": best["t_statistic"],
        "hit_rate": best["hit_rate"],
        "clears_cost": clears_cost,
        "statistically_significant": significant,
        "cost_bps": cost_bps,
        "edge_to_cost_ratio": (
            round(best["excess_edge_bps"] / cost_bps, 4) if cost_bps > 0 else None
        ),
    }


# ---------------------------------------------------------------------------
# Running it against a family
# ---------------------------------------------------------------------------

def family_signal_diagnostics(
    conn: psycopg.Connection,
    *,
    architecture: str,
    timeframe: str,
    dataset_id: int,
    symbols: Sequence[str],
    max_variants: int = 3,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    dataset_cache: dict[Any, Any] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    minimum_timestamp_exclusive: Any | None = None,
    cost_bps_override: float | None = None,
) -> dict[str, Any]:
    """Measure one family's signal across symbols, pooling its firings.

    Signals are pooled across symbols before the statistic is taken, so the
    result describes the family rather than its luckiest instrument -- the
    one-symbol dependence that has dominated this pipeline's results.

    `dataset_cache` is keyed by `(is_cross_sectional, symbol, timeframe,
    dataset_id, lookback_bars)`. The candle query behind `_load_dataset` is a
    function of that key alone -- it does not depend on which family or which
    parameter variant is asking -- so without a cache shared across the whole
    sweep, the same symbol's candles get re-fetched once per (family, variant)
    pair: with defaults, up to 12 families x 3 variants = 36x the necessary
    reads per symbol. Pass one dict in from the caller and reuse it across
    every family in a sweep; a fresh `{}` still deduplicates within one family.
    """
    from dataclasses import replace as dataclass_replace

    from app.services.labs.intraday.cross_sectional_dataset import is_cross_sectional_candidate
    from app.services.labs.intraday.families.registry import FAMILY_REGISTRY
    from app.services.strategy_discovery import make_strategy_definition

    definition = FAMILY_REGISTRY.get(architecture)
    if definition is None:
        raise ValueError(f"unknown family {architecture!r}")

    candidates = definition.candidate_generator(max_candidates=max_variants)
    if not candidates:
        raise ValueError(f"family {architecture!r} generated no candidates")

    cache: dict[Any, Any] = {} if dataset_cache is None else dataset_cache
    cost_bps = (
        float(cost_bps_override)
        if cost_bps_override is not None
        else round_trip_cost_bps()
    )
    variants: list[dict[str, Any]] = []
    total_work_units = len(candidates) * len(symbols)
    completed_work_units = 0
    for variant_index, candidate in enumerate(candidates, start=1):
        pooled_rows: list[dict[str, Any]] = []
        per_symbol: list[dict[str, Any]] = []
        for symbol_index, symbol in enumerate(symbols, start=1):
            if progress_callback:
                progress_callback(
                    {
                        "total": total_work_units,
                        "completed": completed_work_units,
                        "variant": variant_index,
                        "variants": len(candidates),
                        "symbol": symbol,
                        "symbol_index": symbol_index,
                        "symbols": len(symbols),
                        "candidate_id": candidate.candidate_id,
                    }
                )
            try:
                dataset = _load_dataset_cached(conn, candidate, symbol, timeframe, dataset_id, cache)
            except Exception as error:  # noqa: BLE001 - one unreadable symbol must not sink the family
                per_symbol.append({"symbol": symbol, "error": str(error)})
                completed_work_units += 1
                continue
            rows = dataset["rows"]
            if minimum_timestamp_exclusive is not None:
                context_bars = max(
                    WARMUP_BARS,
                    int(candidate.parameters.get("recent_candle_window_bars") or 0),
                    max(horizons, default=1) + 1,
                )
                rows = _rows_with_forward_context(
                    rows,
                    minimum_timestamp_exclusive=minimum_timestamp_exclusive,
                    context_bars=context_bars,
                )
            if len(rows) <= WARMUP_BARS + max(horizons) + 1:
                per_symbol.append({"symbol": symbol, "error": "not enough bars"})
                completed_work_units += 1
                continue

            scoped = dataclass_replace(
                candidate,
                parameters={**candidate.parameters, "timeframe": timeframe},
            )
            strategy = make_strategy_definition(scoped)
            measurement = measure_signal_edge(
                rows,
                strategy.decide,
                strategy.parameters,
                horizons=horizons,
                recent_window_bars=int(strategy.parameters.get("recent_candle_window_bars") or 0),
                signal_start_timestamp=minimum_timestamp_exclusive,
            )
            per_symbol.append(
                {
                    "symbol": symbol,
                    "signals": measurement["signal_count"],
                    "summary": summarize_edge(measurement, cost_bps=cost_bps),
                }
            )
            pooled_rows.append(
                {
                    "rows": rows,
                    "decide": strategy.decide,
                    "params": strategy.parameters,
                    "signal_start_timestamp": minimum_timestamp_exclusive,
                }
            )
            completed_work_units += 1

        pooled = _pool_measurements(pooled_rows, horizons=horizons)
        variants.append(
            {
                "candidate_id": candidate.candidate_id,
                "parameters": {
                    key: str(value)
                    for key, value in sorted(candidate.parameters.items())
                    if key not in {"strategy_architecture", "strategy_engine_version", "feature_engine_version"}
                },
                "measurement": pooled,
                "summary": summarize_edge(pooled, cost_bps=cost_bps),
                "by_symbol": per_symbol,
            }
        )

    if progress_callback:
        progress_callback(
            {
                "total": total_work_units,
                "completed": completed_work_units,
                "variant": len(candidates),
                "variants": len(candidates),
                "symbol": str(symbols[-1]) if symbols else "",
                "symbol_index": len(symbols),
                "symbols": len(symbols),
                "candidate_id": candidates[-1].candidate_id,
            }
        )

    scored = [row for row in variants if row["summary"]["t_statistic"] is not None]
    best = max(scored, key=lambda row: row["summary"]["t_statistic"]) if scored else None
    return {
        "architecture": architecture,
        "family_name": definition.name,
        "timeframe": timeframe,
        "dataset_id": dataset_id,
        "symbols": list(symbols),
        "diagnostics_version": SIGNAL_DIAGNOSTICS_VERSION,
        "round_trip_cost_bps": cost_bps,
        "minimum_timestamp_exclusive": minimum_timestamp_exclusive,
        "is_cross_sectional": is_cross_sectional_candidate({"parameters": {"strategy_architecture": architecture}}),
        "variants_measured": len(variants),
        "best_variant": best,
        "summary": best["summary"] if best else {
            "verdict": "insufficient_signals",
            "detail": "No variant produced a measurable signal.",
            "best_horizon_bars": None,
            "excess_edge_bps": None,
            "t_statistic": None,
            "clears_cost": False,
        },
        "variants": variants,
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def persist_signal_diagnostics(conn: psycopg.Connection, report: dict[str, Any]) -> dict[str, Any]:
    """Write one family's verdict. No DDL: this runs inside the worker's loop,
    and a `CREATE TABLE IF NOT EXISTS` here would hold an ACCESS EXCLUSIVE
    lock on the results table for the rest of the worker's transaction,
    blocking every UI read of stored verdicts."""
    from app.services.research_architecture import jsonable

    summary = report["summary"]
    best = report.get("best_variant") or {}
    measurement = best.get("measurement") or {}
    row = conn.execute(
        """
        INSERT INTO research_signal_diagnostics(
            architecture, family_name, timeframe, dataset_id, verdict, detail,
            best_horizon_bars, excess_edge_bps, t_statistic, round_trip_cost_bps,
            clears_cost, signal_count, symbols, horizons, report, calculation_version
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (architecture, timeframe, dataset_id) DO UPDATE SET
            verdict = EXCLUDED.verdict,
            detail = EXCLUDED.detail,
            best_horizon_bars = EXCLUDED.best_horizon_bars,
            excess_edge_bps = EXCLUDED.excess_edge_bps,
            t_statistic = EXCLUDED.t_statistic,
            round_trip_cost_bps = EXCLUDED.round_trip_cost_bps,
            clears_cost = EXCLUDED.clears_cost,
            signal_count = EXCLUDED.signal_count,
            symbols = EXCLUDED.symbols,
            horizons = EXCLUDED.horizons,
            report = EXCLUDED.report,
            created_at = NOW()
        RETURNING *
        """,
        (
            report["architecture"],
            report.get("family_name"),
            report["timeframe"],
            report["dataset_id"],
            summary["verdict"],
            summary["detail"],
            summary.get("best_horizon_bars"),
            summary.get("excess_edge_bps"),
            summary.get("t_statistic"),
            report["round_trip_cost_bps"],
            bool(summary.get("clears_cost")),
            int(measurement.get("signal_count") or 0),
            Jsonb(list(report.get("symbols") or [])),
            Jsonb(measurement.get("by_horizon") or []),
            Jsonb(jsonable({key: value for key, value in report.items() if key != "variants"})),
            SIGNAL_DIAGNOSTICS_VERSION,
        ),
    ).fetchone()
    return dict(row)


def _rows_with_forward_context(
    rows: Sequence[dict[str, Any]],
    *,
    minimum_timestamp_exclusive: Any,
    context_bars: int,
) -> list[dict[str, Any]]:
    """Retain only forward observations plus bounded pre-cutoff feature warm-up."""
    first_forward = next(
        (
            index
            for index, row in enumerate(rows)
            if row["candle"]["timestamp"] > minimum_timestamp_exclusive
        ),
        len(rows),
    )
    start = max(0, first_forward - max(0, context_bars))
    return list(rows[start:])


def list_signal_diagnostics(
    conn: psycopg.Connection, *, dataset_id: int | None = None, timeframe: str | None = None
) -> list[dict[str, Any]]:
    """Stored verdicts, so the UI never triggers a recompute to render a row.

    Pure read, no schema work -- see `get_signal_diagnostics_job` for why DDL
    on a request path is what caused the 502s."""
    clauses = []
    params: list[Any] = []
    if dataset_id is not None:
        clauses.append("dataset_id = %s")
        params.append(dataset_id)
    if timeframe is not None:
        clauses.append("timeframe = %s")
        params.append(timeframe)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM research_signal_diagnostics {where} ORDER BY t_statistic DESC NULLS LAST",
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def run_signal_diagnostics(
    conn: psycopg.Connection,
    *,
    timeframe: str,
    dataset_id: int | None = None,
    symbols: Sequence[str] | None = None,
    architectures: Sequence[str] | None = None,
    max_variants: int = 3,
    max_symbols: int = 4,
    persist: bool = True,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    work_progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Measure every active family's signal on one timeframe.

    Symbol count is capped by default: this is a go/no-go filter, and pooling
    four symbols already separates "predicts nothing" from "predicts
    something" far more cheaply than a 2,000-job campaign does. Raise it when
    a family is close to the line and the answer needs to be tighter.
    """
    from app.services.labs.intraday.campaign_plan import active_family_definitions

    if dataset_id is None:
        row = conn.execute(
            """
            SELECT id FROM research_dataset_manifests
            WHERE dataset_kind = 'intraday'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        if not row:
            raise ValueError("no intraday dataset snapshot exists to measure against")
        dataset_id = int(row["id"])

    if symbols is None:
        manifest = conn.execute(
            "SELECT assets FROM research_dataset_manifests WHERE id = %s", (dataset_id,)
        ).fetchone()
        if not manifest:
            raise ValueError(f"no dataset manifest for dataset_id={dataset_id}")
        symbols = [str(asset).upper() for asset in (manifest["assets"] or [])]
    selected_symbols = list(symbols)[:max_symbols]
    if not selected_symbols:
        raise ValueError("no symbols available to measure")

    if architectures is None:
        families = active_family_definitions()
    else:
        from app.services.labs.intraday.families.registry import FAMILY_REGISTRY

        families = [
            {
                "architecture": architecture,
                "name": FAMILY_REGISTRY[architecture].name,
                "status": FAMILY_REGISTRY[architecture].status,
                "supported_timeframes": list(FAMILY_REGISTRY[architecture].supported_timeframes),
            }
            for architecture in architectures
            if architecture in FAMILY_REGISTRY
        ]
    measurable_families = [family for family in families if timeframe in family["supported_timeframes"]]
    total_families = len(measurable_families)
    completed_families = 0
    if progress_callback:
        progress_callback({"total": total_families, "completed": 0, "current": None})

    # Shared across every family in this sweep: the candle query behind
    # _load_dataset depends only on (symbol, timeframe, dataset_id[,
    # lookback_bars]), which is identical for most families here, so one
    # cache dict turns up to families*variants reads per symbol into one.
    dataset_cache: dict[Any, Any] = {}
    reports: list[dict[str, Any]] = []
    for family in measurable_families:
        if progress_callback:
            progress_callback(
                {
                    "total": total_families,
                    "completed": completed_families,
                    "current": family["name"],
                }
            )
        try:
            report = family_signal_diagnostics(
                conn,
                architecture=family["architecture"],
                timeframe=timeframe,
                dataset_id=dataset_id,
                symbols=selected_symbols,
                max_variants=max_variants,
                dataset_cache=dataset_cache,
                progress_callback=work_progress_callback,
            )
        except Exception as error:  # noqa: BLE001 - one broken family must not sink the sweep
            reports.append(
                {
                    "architecture": family["architecture"],
                    "family_name": family["name"],
                    "timeframe": timeframe,
                    "dataset_id": dataset_id,
                    "error": str(error),
                    "summary": {"verdict": "not_measurable", "detail": str(error)},
                }
            )
            completed_families += 1
            if progress_callback:
                progress_callback(
                    {
                        "total": total_families,
                        "completed": completed_families,
                        "current": None if completed_families >= total_families else family["name"],
                    }
                )
            continue
        if persist:
            # Commit per family rather than once at the end. A single
            # transaction spanning the whole sweep would hold row locks on the
            # results table for minutes, blocking every UI read of stored
            # verdicts -- and it would discard every completed family if a
            # later one failed. Committing here also means the UI sees
            # verdicts appear progressively instead of all at once.
            persist_signal_diagnostics(conn, report)
            conn.commit()
        reports.append(report)
        completed_families += 1
        if progress_callback:
            progress_callback(
                {
                    "total": total_families,
                    "completed": completed_families,
                    "current": None if completed_families >= total_families else family["name"],
                }
            )

    verdicts: dict[str, int] = {}
    for report in reports:
        verdict = str(report["summary"]["verdict"])
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
    predictive = [row["architecture"] for row in reports if row["summary"]["verdict"] == "predictive"]
    below_cost = [row["architecture"] for row in reports if row["summary"]["verdict"] == "signal_below_cost"]

    return {
        "diagnostics_version": SIGNAL_DIAGNOSTICS_VERSION,
        "timeframe": timeframe,
        "dataset_id": dataset_id,
        "symbols": selected_symbols,
        "families_measured": len(reports),
        "verdict_counts": dict(sorted(verdicts.items())),
        "predictive_families": predictive,
        "signal_below_cost_families": below_cost,
        "round_trip_cost_bps": round_trip_cost_bps(),
        "recommendation": _sweep_recommendation(predictive, below_cost, len(reports)),
        "families": [
            {key: value for key, value in report.items() if key != "variants"} for report in reports
        ],
    }


def _sweep_recommendation(predictive: list[str], below_cost: list[str], measured: int) -> str:
    if predictive:
        return (
            f"{len(predictive)} famil{'y' if len(predictive) == 1 else 'ies'} show predictive content that "
            "clears costs. A campaign on these is worth its compute; the rest are not."
        )
    if below_cost:
        return (
            f"No family clears costs, but {len(below_cost)} show a real signal too small to pay for the "
            "round trip. Widen the stop or lengthen the hold before running another campaign -- the "
            "signal is not the problem, the cost structure is."
        )
    if measured:
        return (
            "No family shows predictive content beyond timing luck. A campaign would spend full compute "
            "to confirm that. Change the hypothesis, the universe, or the horizon before running one."
        )
    return "Nothing was measurable on this timeframe."


# ---------------------------------------------------------------------------
# Background job queue
#
# The measurement above reads bars for every (family, variant, symbol) and
# runs decide() over every one of them -- seconds to low minutes of real work.
# Running that inline in an API request holds the request's DB transaction
# open for the whole duration and, on a single-worker process, starves the
# async event loop of GIL time long enough that unrelated endpoints start
# timing out. So the request path only ever enqueues a row; a separate worker
# process (app/workers/signal_diagnostics_runner.py) claims and runs it on its
# own short-lived connection, exactly the pattern campaign_runner.py already
# uses for campaign execution.
# ---------------------------------------------------------------------------

def update_signal_diagnostics_job_progress(
    conn: psycopg.Connection,
    job_id: int,
    *,
    total: int,
    completed: int,
    current: str | None,
) -> None:
    """Persist worker progress so the polling UI can show useful movement."""
    conn.execute(
        """
        UPDATE research_signal_diagnostics_jobs
        SET progress_total = %s,
            progress_completed = %s,
            progress_current = %s
        WHERE id = %s
        """,
        (total, completed, current, job_id),
    )
    conn.commit()


def enqueue_signal_diagnostics_job(
    conn: psycopg.Connection,
    *,
    timeframe: str,
    dataset_id: int | None = None,
    architectures: Sequence[str] | None = None,
    max_variants: int = 3,
    max_symbols: int = 4,
) -> dict[str, Any]:
    """Queue a measurement and return immediately. Does not run anything.

    This is the entire body of what the API request should do: one INSERT,
    one commit, no candle reads, no decide() calls, and no schema work -- the
    request returns in milliseconds regardless of how long the actual
    measurement takes.
    """
    row = conn.execute(
        """
        INSERT INTO research_signal_diagnostics_jobs(
            timeframe, dataset_id, architectures, max_variants, max_symbols
        )
        VALUES (%s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            timeframe,
            dataset_id,
            Jsonb(list(architectures)) if architectures is not None else None,
            max_variants,
            max_symbols,
        ),
    ).fetchone()
    conn.commit()
    return dict(row)


def get_signal_diagnostics_job(conn: psycopg.Connection, job_id: int) -> dict[str, Any] | None:
    """Pure read. Deliberately does NO schema work.

    This is polled every couple of seconds while a measurement runs. It used
    to call `ensure_signal_diagnostics_jobs_table` first, and `CREATE TABLE IF
    NOT EXISTS` takes an ACCESS EXCLUSIVE lock even when the table already
    exists -- so every poll queued behind any other transaction touching the
    table, including the worker's own claim. That serialisation is what turned
    a millisecond SELECT into a 5.8s request and then an nginx 502. Schema is
    owned by migrations 057/058 and bootstrapped once at startup.
    """
    row = conn.execute(
        "SELECT * FROM research_signal_diagnostics_jobs WHERE id = %s", (job_id,)
    ).fetchone()
    return dict(row) if row else None


def signal_diagnostics_queue_health(conn: psycopg.Connection) -> dict[str, Any]:
    """Whether anything is actually consuming the queue.

    A job that never leaves `queued` does not mean the measurement is slow --
    it means no worker process claimed it. Without this, the UI polls a job
    that will never move and reports a ten-minute timeout, which reads like a
    hung measurement rather than a missing process. `worker_running` is
    inferred from evidence rather than assumed: a job has been claimed
    recently, or none is waiting long enough to be suspicious.
    """
    row = conn.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE status = 'queued') AS queued,
            COUNT(*) FILTER (WHERE status = 'running') AS running,
            EXTRACT(EPOCH FROM (NOW() - MIN(created_at) FILTER (WHERE status = 'queued'))) AS oldest_queued_seconds,
            MAX(started_at) AS last_started_at
        FROM research_signal_diagnostics_jobs
        """
    ).fetchone()
    queued = int((row or {}).get("queued") or 0)
    running = int((row or {}).get("running") or 0)
    oldest = (row or {}).get("oldest_queued_seconds")
    oldest_seconds = float(oldest) if oldest is not None else None
    # A queued job older than this with nothing running is the signal that no
    # worker is consuming the queue at all.
    stalled = bool(queued and running == 0 and (oldest_seconds or 0) > UNCLAIMED_JOB_STALL_SECONDS)
    return {
        "queued": queued,
        "running": running,
        "oldest_queued_seconds": round(oldest_seconds, 1) if oldest_seconds is not None else None,
        "last_started_at": (row or {}).get("last_started_at"),
        "worker_appears_stopped": stalled,
        "detail": (
            "No worker has claimed a queued job. Start app/workers/signal_diagnostics_runner.py -- "
            "queued measurements will not run until it does."
            if stalled
            else "Queue is being consumed normally."
        ),
    }


def claim_next_signal_diagnostics_job(conn: psycopg.Connection) -> dict[str, Any] | None:
    """Atomically claim the oldest queued job, or None if the queue is empty.

    `FOR UPDATE SKIP LOCKED` is the same claiming pattern the campaign job
    queue uses, so two worker processes can run concurrently without ever
    claiming the same row. No schema work here either: DDL would take an
    ACCESS EXCLUSIVE lock that blocks every concurrent status poll.
    """
    row = conn.execute(
        """
        UPDATE research_signal_diagnostics_jobs
        SET status = 'running', started_at = NOW()
        WHERE id = (
            SELECT id FROM research_signal_diagnostics_jobs
            WHERE status = 'queued'
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING *
        """
    ).fetchone()
    conn.commit()
    return dict(row) if row else None


def run_claimed_signal_diagnostics_job(conn: psycopg.Connection, job: dict[str, Any]) -> dict[str, Any]:
    """Run one already-claimed job to completion and record the outcome.

    Failures are caught and stored rather than raised, so one bad job (an
    unreadable dataset, a missing symbol) cannot crash the worker loop -- the
    same principle `run_signal_diagnostics` already applies per-family, one
    level up.
    """
    try:
        result = run_signal_diagnostics(
            conn,
            timeframe=job["timeframe"],
            dataset_id=job["dataset_id"],
            architectures=job["architectures"],
            max_variants=job["max_variants"],
            max_symbols=job["max_symbols"],
            progress_callback=lambda progress: update_signal_diagnostics_job_progress(
                conn,
                int(job["id"]),
                total=int(progress["total"]),
                completed=int(progress["completed"]),
                current=progress["current"],
            ),
        )
    except Exception as error:  # noqa: BLE001 - store the failure, do not crash the worker
        row = conn.execute(
            """
            UPDATE research_signal_diagnostics_jobs
            SET status = 'failed', error = %s, progress_current = NULL, completed_at = NOW()
            WHERE id = %s
            RETURNING *
            """,
            (str(error), job["id"]),
        ).fetchone()
        conn.commit()
        return dict(row)

    row = conn.execute(
        """
        UPDATE research_signal_diagnostics_jobs
        SET status = 'completed',
            result = %s,
            progress_completed = GREATEST(progress_completed, progress_total),
            progress_current = NULL,
            completed_at = NOW()
        WHERE id = %s
        RETURNING *
        """,
        (Jsonb(result), job["id"]),
    ).fetchone()
    conn.commit()
    return dict(row)


def run_one_signal_diagnostics_job(conn: psycopg.Connection) -> dict[str, Any] | None:
    """Claim and run one job. Returns None if the queue was empty."""
    job = claim_next_signal_diagnostics_job(conn)
    if job is None:
        return None
    return run_claimed_signal_diagnostics_job(conn, job)


def _load_dataset(conn, candidate, symbol: str, timeframe: str, dataset_id: int) -> dict[str, Any]:
    """The same dataset the campaign would run this candidate against.

    Cross-sectional families need their peer-derived percentile feature, so
    dispatch mirrors `run_campaign_job` rather than re-deciding it here.

    Uncached: prefer `_load_dataset_cached` for anything that loops over
    multiple families or variants, which is every real caller in this module.
    """
    from app.services.labs.intraday.cross_sectional_dataset import (
        is_cross_sectional_candidate,
        load_cross_sectional_intraday_dataset,
    )
    from app.services.labs.intraday.dataset import load_intraday_backtest_dataset

    payload = {"parameters": dict(candidate.parameters)}
    if is_cross_sectional_candidate(payload):
        return load_cross_sectional_intraday_dataset(
            conn,
            symbol,
            timeframe,
            dataset_id=dataset_id,
            lookback_bars=int(candidate.parameters.get("cross_sectional_lookback_bars", 8)),
        )
    return load_intraday_backtest_dataset(conn, symbol, timeframe, dataset_id=dataset_id)


def _load_dataset_cached(
    conn, candidate, symbol: str, timeframe: str, dataset_id: int, cache: dict[Any, Any]
) -> dict[str, Any]:
    """`_load_dataset`, deduplicated across the whole sweep.

    The cache key is exactly the inputs that determine the query result: a
    plain family's dataset depends only on (symbol, timeframe, dataset_id),
    and a cross-sectional family's additionally on lookback_bars. It does NOT
    depend on architecture or on any other candidate parameter, which is why
    the same key -- and therefore the same cached dataset -- is correctly
    shared across every family and every variant that resolves to it.
    """
    from app.services.labs.intraday.cross_sectional_dataset import is_cross_sectional_candidate

    payload = {"parameters": dict(candidate.parameters)}
    is_cross_sectional = is_cross_sectional_candidate(payload)
    lookback_bars = int(candidate.parameters.get("cross_sectional_lookback_bars", 8)) if is_cross_sectional else None
    key = (is_cross_sectional, symbol, timeframe, dataset_id, lookback_bars)
    if key not in cache:
        cache[key] = _load_dataset(conn, candidate, symbol, timeframe, dataset_id)
    return cache[key]


def _pool_measurements(
    datasets: list[dict[str, Any]], *, horizons: Sequence[int]
) -> dict[str, Any]:
    """Re-measure across every symbol's bars as one sample.

    Pooling the raw firings rather than averaging per-symbol summaries keeps
    the t-statistic honest: a symbol contributing four signals should not
    weigh the same as one contributing four hundred.
    """
    from app.services.strategy import reset_strategy_state

    all_excess: dict[int, list[float]] = {horizon: [] for horizon in horizons}
    all_raw: dict[int, list[float]] = {horizon: [] for horizon in horizons}
    drift: dict[int, list[float]] = {horizon: [] for horizon in horizons}
    total_signals = 0
    total_bars = 0
    long_signals = 0
    short_signals = 0

    for entry in datasets:
        rows = entry["rows"]
        decide = entry["decide"]
        params = entry["params"]
        signal_start_timestamp = entry.get("signal_start_timestamp")
        reset_strategy_state(decide)
        candle_rows = [row["candle"] for row in rows]
        recent_window = int(params.get("recent_candle_window_bars") or 0)

        signals: list[tuple[int, int]] = []
        eligible_bars = 0
        for index in range(WARMUP_BARS, len(rows)):
            row = rows[index]
            start = max(0, index + 1 - recent_window) if recent_window else 0
            decision = decide(row["candle"], row["feature"], candle_rows[start : index + 1], params)
            if (
                signal_start_timestamp is not None
                and row["candle"]["timestamp"] <= signal_start_timestamp
            ):
                continue
            eligible_bars += 1
            if getattr(decision, "signal", None) != "setup":
                continue
            direction = 1 if str(getattr(decision, "direction", "long")) == "long" else -1
            signals.append((index, direction))

        total_signals += len(signals)
        total_bars += eligible_bars
        long_signals += sum(1 for _, direction in signals if direction > 0)
        short_signals += sum(1 for _, direction in signals if direction < 0)

        for horizon in horizons:
            forward = _forward_returns(rows, horizon)
            measurable = [
                value
                for index, value in enumerate(forward)
                if value is not None
                and (
                    signal_start_timestamp is None
                    or rows[index]["candle"]["timestamp"] > signal_start_timestamp
                )
            ]
            if not measurable:
                continue
            unconditional = fmean(measurable)
            drift[horizon].append(unconditional)
            for index, direction in signals:
                if forward[index] is None:
                    continue
                all_excess[horizon].append(direction * (forward[index] - unconditional))
                all_raw[horizon].append(direction * forward[index])

    by_horizon: list[dict[str, Any]] = []
    for horizon in horizons:
        excess = all_excess[horizon]
        if not excess:
            continue
        mean_excess = fmean(excess)
        deviation = pstdev(excess) if len(excess) > 1 else 0.0
        t_statistic = mean_excess / (deviation / sqrt(len(excess))) if deviation > 0 else None
        by_horizon.append(
            {
                "horizon_bars": horizon,
                "signals": len(excess),
                "raw_edge_bps": round(fmean(all_raw[horizon]) * 10_000, 4),
                "unconditional_drift_bps": round(fmean(drift[horizon]) * 10_000, 4) if drift[horizon] else 0.0,
                "excess_edge_bps": round(mean_excess * 10_000, 4),
                "t_statistic": round(t_statistic, 4) if t_statistic is not None else None,
                "hit_rate": round(sum(1 for value in excess if value > 0) / len(excess), 4),
            }
        )

    return {
        "signal_count": total_signals,
        "bars_evaluated": total_bars,
        "signal_rate": round(total_signals / max(1, total_bars), 6),
        "long_signals": long_signals,
        "short_signals": short_signals,
        "by_horizon": by_horizon,
    }
