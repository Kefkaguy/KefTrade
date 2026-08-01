"""Return-blind calibration of extreme signed trade imbalance.

This module is intentionally unable to read candles, future prices, returns or
P&L.  It estimates only the marginal distribution of a predictor already
materialized by :mod:`intraday_trade_flow`, freezes the eligible rows, and
publishes the threshold that a later hypothesis version must use unchanged.

No campaign, broker, order-submission or UI code belongs here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from hashlib import sha256
from json import dumps
from math import ceil, sqrt
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import psycopg
from psycopg.types.json import Jsonb

from app.services.intraday_trade_flow import TRADE_FLOW_VERSION

CALIBRATION_VERSION = "signed_trade_imbalance_calibration_v1_return_blind"
EXCHANGE = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class CalibrationSpec:
    """The rule frozen before calibration observations are examined."""

    quantile: float = 0.95
    null_quantile: float = 0.99
    minimum_symbol_sessions: int = 1_500
    minimum_sessions: int = 60
    minimum_symbols: int = 40
    minimum_eligible_bars: int = 15_000
    minimum_trade_count: int = 200
    maximum_unclassified_share: float = 0.25
    minimum_effective_trade_count: float = 50.0
    bootstrap_samples: int = 1_000
    null_draws_per_bar: int = 64
    random_seed: int = 20_260_801
    maximum_relative_ci_half_width: float = 0.10
    maximum_chronological_half_difference: float = 0.20
    maximum_bucket_quantile_ratio: float = 1.50
    minimum_bucket_bars: int = 100
    threshold_rounding: float = 0.01

    def frozen(self) -> dict[str, Any]:
        return {**asdict(self), "calculation_version": CALIBRATION_VERSION}

    def specification_hash(self) -> str:
        return sha256(dumps(self.frozen(), sort_keys=True).encode()).hexdigest()


DEFAULT_SPEC = CalibrationSpec()


def _row_value(row: Any, key: str) -> Any:
    return row.get(key) if hasattr(row, "get") else getattr(row, key)


def _session_date(row: Any) -> date:
    supplied = _row_value(row, "session_date")
    if supplied is not None:
        return supplied
    return _row_value(row, "timestamp").astimezone(EXCHANGE).date()


def _time_slot(moment: datetime) -> str:
    return moment.astimezone(EXCHANGE).strftime("%H:%M")


def _round_up(value: float, step: float) -> float:
    return round(ceil((float(value) - 1e-12) / step) * step, 10)


def _weighted_quantile(values: np.ndarray, quantile: float, weights: np.ndarray) -> float:
    if values.size == 0:
        raise ValueError("cannot calculate a quantile from no observations")
    order = np.argsort(values, kind="mergesort")
    sorted_values = values[order]
    sorted_weights = weights[order]
    cumulative = np.cumsum(sorted_weights)
    cutoff = float(quantile) * float(cumulative[-1])
    return float(sorted_values[min(np.searchsorted(cumulative, cutoff, side="left"), values.size - 1)])


def _cluster_balanced_weights(rows: Sequence[dict[str, Any]]) -> np.ndarray:
    """Equal symbol-session weight, divided equally across its eligible bars."""
    counts: dict[tuple[str, date], int] = {}
    for row in rows:
        key = (str(row["symbol"]), row["session_date"])
        counts[key] = counts.get(key, 0) + 1
    return np.asarray(
        [1.0 / counts[(str(row["symbol"]), row["session_date"])] for row in rows],
        dtype=float,
    )


def eligible_rows(
    rows: Sequence[Any], *, spec: CalibrationSpec = DEFAULT_SPEC
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Apply predictor-quality gates; no outcome field is accepted or inspected."""
    eligible: list[dict[str, Any]] = []
    excluded: dict[str, int] = {}

    def reject(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    for source in rows:
        imbalance = _row_value(source, "signed_trade_imbalance")
        trade_count = int(_row_value(source, "trade_count") or 0)
        unclassified = _row_value(source, "unclassified_share")
        total_volume = float(_row_value(source, "total_volume") or 0)
        classified = float(_row_value(source, "classified_volume") or 0)
        size_squared = float(_row_value(source, "trade_size_squared_sum") or 0)
        effective = float(_row_value(source, "effective_trade_count") or 0)
        method = str(_row_value(source, "classification_method") or "")
        if imbalance is None or not -1 <= float(imbalance) <= 1:
            reject("missing_or_invalid_imbalance")
            continue
        if trade_count < spec.minimum_trade_count:
            reject("minimum_trade_count")
            continue
        if unclassified is None or float(unclassified) > spec.maximum_unclassified_share:
            reject("classification_coverage")
            continue
        if total_volume <= 0 or classified <= 0:
            reject("missing_classified_volume")
            continue
        if size_squared <= 0 or effective < spec.minimum_effective_trade_count:
            reject("missing_or_weak_null_moments")
            continue
        if method not in {"tick_rule", "lee_ready", "mixed"}:
            reject("unknown_classifier")
            continue
        timestamp = _row_value(source, "timestamp")
        eligible.append(
            {
                "symbol": str(_row_value(source, "symbol")).upper(),
                "timestamp": timestamp,
                "session_date": _session_date(source),
                "time_slot": _time_slot(timestamp),
                "trade_count": trade_count,
                "total_volume": total_volume,
                "classified_volume": classified,
                "trade_size_squared_sum": size_squared,
                "effective_trade_count": effective,
                "signed_trade_imbalance": float(imbalance),
                "unclassified_share": float(unclassified),
            }
        )
    return eligible, excluded


def _minimum_gates(rows: Sequence[dict[str, Any]], spec: CalibrationSpec) -> dict[str, bool]:
    symbol_sessions = {(row["symbol"], row["session_date"]) for row in rows}
    sessions = {row["session_date"] for row in rows}
    symbols = {row["symbol"] for row in rows}
    return {
        "minimum_1500_symbol_sessions": len(symbol_sessions) >= spec.minimum_symbol_sessions,
        "minimum_60_sessions": len(sessions) >= spec.minimum_sessions,
        "minimum_40_symbols": len(symbols) >= spec.minimum_symbols,
        "minimum_15000_eligible_bars": len(rows) >= spec.minimum_eligible_bars,
    }


def _liquidity_bucket(volume: float, boundaries: tuple[float, float]) -> str:
    if volume <= boundaries[0]:
        return "low"
    if volume <= boundaries[1]:
        return "medium"
    return "high"


def _threshold_for_row(
    row: dict[str, Any], *, mode: str, global_threshold: float,
    bucket_thresholds: dict[str, float], liquidity_boundaries: tuple[float, float]
) -> float:
    if mode == "global":
        return global_threshold
    bucket = _liquidity_bucket(float(row["total_volume"]), liquidity_boundaries)
    return bucket_thresholds[f"{row['time_slot']}|{bucket}"]


def _bootstrap(
    rows: Sequence[dict[str, Any]], *, spec: CalibrationSpec, mode: str,
    global_threshold: float, bucket_thresholds: dict[str, float],
    liquidity_boundaries: tuple[float, float]
) -> dict[str, Any]:
    sessions = sorted({row["session_date"] for row in rows})
    indices = {
        session: np.asarray([i for i, row in enumerate(rows) if row["session_date"] == session])
        for session in sessions
    }
    values = np.asarray([abs(float(row["signed_trade_imbalance"])) for row in rows])
    events = np.asarray(
        [
            value >= _threshold_for_row(
                row,
                mode=mode,
                global_threshold=global_threshold,
                bucket_thresholds=bucket_thresholds,
                liquidity_boundaries=liquidity_boundaries,
            )
            for value, row in zip(values, rows)
        ],
        dtype=float,
    )
    rng = np.random.default_rng(spec.random_seed + 1)
    thresholds: list[float] = []
    event_rates: list[float] = []
    for _ in range(spec.bootstrap_samples):
        selected = rng.choice(sessions, size=len(sessions), replace=True)
        sample_indices = np.concatenate([indices[item] for item in selected])
        sample_rows = [rows[int(index)] for index in sample_indices]
        weights = _cluster_balanced_weights(sample_rows)
        thresholds.append(_weighted_quantile(values[sample_indices], spec.quantile, weights))
        event_rates.append(float(np.average(events[sample_indices], weights=weights)))
    return {
        "samples": spec.bootstrap_samples,
        "resampling_unit": "whole_trading_session",
        "threshold_confidence_interval_95": [
            round(float(np.quantile(thresholds, 0.025)), 8),
            round(float(np.quantile(thresholds, 0.975)), 8),
        ],
        "event_rate_confidence_interval_95": [
            round(float(np.quantile(event_rates, 0.025)), 8),
            round(float(np.quantile(event_rates, 0.975)), 8),
        ],
    }


def calibrate_predictor_distribution(
    source_rows: Sequence[Any], *, spec: CalibrationSpec = DEFAULT_SPEC
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Calibrate without accepting any candle or return input."""
    rows, exclusions = eligible_rows(source_rows, spec=spec)
    gates = _minimum_gates(rows, spec)
    counts = {
        "source_bars": len(source_rows),
        "eligible_bars": len(rows),
        "symbols": len({row["symbol"] for row in rows}),
        "sessions": len({row["session_date"] for row in rows}),
        "symbol_sessions": len({(row["symbol"], row["session_date"]) for row in rows}),
    }
    base = {
        "calculation_version": CALIBRATION_VERSION,
        "return_blind": True,
        "outcome_fields_accessed": [],
        "specification": spec.frozen(),
        "specification_hash": spec.specification_hash(),
        "counts": counts,
        "exclusions": exclusions,
        "minimum_data_gates": gates,
    }
    if not rows or not all(gates.values()):
        return {
            **base,
            "ready_for_declaration": False,
            "refusal_reasons": [key for key, passed in gates.items() if not passed],
        }, rows

    values = np.asarray([abs(row["signed_trade_imbalance"]) for row in rows], dtype=float)
    weights = _cluster_balanced_weights(rows)
    empirical = _weighted_quantile(values, spec.quantile, weights)

    # Under random independent trade signs, Var(sum(sign_i * size_i) / sum(size_i))
    # equals sum(size_i^2) / sum(size_i)^2. With >=50 effective trades, a
    # normal Rademacher approximation is accurate enough for the noise floor
    # and avoids retaining personally reconstructable raw prints.
    null_scales = np.asarray(
        [sqrt(row["trade_size_squared_sum"]) / row["classified_volume"] for row in rows]
    )
    rng = np.random.default_rng(spec.random_seed)
    null_draws = np.abs(
        rng.standard_normal((len(rows), spec.null_draws_per_bar)) * null_scales[:, None]
    ).reshape(-1)
    null_threshold = float(np.quantile(null_draws, spec.null_quantile))
    global_raw = max(empirical, null_threshold)
    global_threshold = _round_up(global_raw, spec.threshold_rounding)

    volumes = np.asarray([row["total_volume"] for row in rows], dtype=float)
    liquidity_boundaries = (
        float(np.quantile(volumes, 1 / 3)),
        float(np.quantile(volumes, 2 / 3)),
    )
    for row in rows:
        row["liquidity_bucket"] = _liquidity_bucket(
            row["total_volume"], liquidity_boundaries
        )

    bucket_quantiles: dict[str, float] = {}
    bucket_thresholds: dict[str, float] = {}
    sparse_buckets: list[str] = []
    for key in sorted({f"{row['time_slot']}|{row['liquidity_bucket']}" for row in rows}):
        subset = [row for row in rows if f"{row['time_slot']}|{row['liquidity_bucket']}" == key]
        if len(subset) < spec.minimum_bucket_bars:
            sparse_buckets.append(key)
            continue
        q95 = _weighted_quantile(
            np.asarray([abs(row["signed_trade_imbalance"]) for row in subset]),
            spec.quantile,
            _cluster_balanced_weights(subset),
        )
        bucket_quantiles[key] = q95
        bucket_thresholds[key] = _round_up(max(q95, null_threshold), spec.threshold_rounding)

    quantile_values = list(bucket_quantiles.values())
    ratio = (
        max(quantile_values) / max(min(quantile_values), 1e-12)
        if quantile_values
        else float("inf")
    )
    mode = (
        "time_liquidity_bucket"
        if ratio > spec.maximum_bucket_quantile_ratio
        else "global"
    )
    expected_buckets = {
        f"{slot}|{bucket}"
        for slot in {row["time_slot"] for row in rows}
        for bucket in ("low", "medium", "high")
    }
    if mode == "time_liquidity_bucket" and expected_buckets - set(bucket_thresholds):
        sparse_buckets = sorted(expected_buckets - set(bucket_thresholds))

    ordered_sessions = sorted({row["session_date"] for row in rows})
    halfway = len(ordered_sessions) // 2
    halves = [
        [row for row in rows if row["session_date"] in set(ordered_sessions[:halfway])],
        [row for row in rows if row["session_date"] in set(ordered_sessions[halfway:])],
    ]
    half_thresholds = [
        _weighted_quantile(
            np.asarray([abs(row["signed_trade_imbalance"]) for row in half]),
            spec.quantile,
            _cluster_balanced_weights(half),
        )
        for half in halves
    ]
    half_difference = abs(half_thresholds[1] - half_thresholds[0]) / max(
        (half_thresholds[0] + half_thresholds[1]) / 2, 1e-12
    )

    bootstrap = _bootstrap(
        rows,
        spec=spec,
        mode=mode,
        global_threshold=global_threshold,
        bucket_thresholds=bucket_thresholds,
        liquidity_boundaries=liquidity_boundaries,
    )
    low, high = bootstrap["threshold_confidence_interval_95"]
    relative_half_width = ((high - low) / 2) / max(empirical, 1e-12)
    lower_event_rate = bootstrap["event_rate_confidence_interval_95"][0]
    stability = {
        "relative_bootstrap_ci_half_width": round(relative_half_width, 8),
        "chronological_half_thresholds": [round(value, 8) for value in half_thresholds],
        "chronological_half_relative_difference": round(half_difference, 8),
        "bucket_quantile_ratio": round(ratio, 8),
        "sparse_buckets": sparse_buckets,
        "gates": {
            "bootstrap_ci_stable": relative_half_width <= spec.maximum_relative_ci_half_width,
            "chronological_halves_stable": half_difference <= spec.maximum_chronological_half_difference,
            "bucket_coverage_complete": mode == "global" or not sparse_buckets,
            "positive_event_rate_lower_bound": lower_event_rate > 0,
        },
    }
    ready = all(stability["gates"].values())
    return {
        **base,
        "ready_for_declaration": ready,
        "refusal_reasons": [key for key, passed in stability["gates"].items() if not passed],
        "threshold": {
            "mode": mode,
            "empirical_absolute_imbalance_q95": round(empirical, 8),
            "random_sign_null_q99": round(null_threshold, 8),
            "global_raw": round(global_raw, 8),
            "global_rounded_up": global_threshold,
            "bucket_thresholds": bucket_thresholds if mode != "global" else {},
            "liquidity_volume_boundaries": [round(item, 4) for item in liquidity_boundaries],
            "rounding": "upward_to_0.01",
        },
        "stability": stability,
        "bootstrap": bootstrap,
        "expected_event_rate_lower_95": lower_event_rate,
        "required_discovery_symbol_sessions": (
            ceil(2_126 / max(lower_event_rate * 13, 1e-12))
            if lower_event_rate > 0
            else None
        ),
        "required_discovery_events": 2_126,
        "null_method": (
            "variance-preserving Rademacher normal approximation using the "
            "classified trade-size second moment; no returns"
        ),
    }, rows


def load_source_rows(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    start: date,
    end: date,
    timeframe: str,
    feed: str,
) -> list[dict[str, Any]]:
    """The only SQL read in calibration: predictor features and QC fields."""
    rows = conn.execute(
        """
        SELECT symbol, timestamp,
               (timestamp AT TIME ZONE 'America/New_York')::date AS session_date,
               trade_count, total_volume, classified_volume,
               trade_size_squared_sum, effective_trade_count,
               signed_trade_imbalance, unclassified_share, classification_method
        FROM intraday_trade_flow_features
        WHERE symbol = ANY(%s)
          AND timeframe = %s AND provider = 'alpaca' AND feed = %s
          AND calculation_version = %s
          AND (timestamp AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s
        ORDER BY timestamp, symbol
        """,
        ([item.upper() for item in symbols], timeframe, feed, TRADE_FLOW_VERSION, start, end),
    ).fetchall()
    return [dict(row) for row in rows]


def _dataset_hash(rows: Sequence[dict[str, Any]]) -> str:
    digest = sha256()
    for row in sorted(rows, key=lambda item: (item["timestamp"], item["symbol"])):
        payload = (
            row["symbol"], row["timestamp"].isoformat(), row["trade_count"],
            row["total_volume"], row["classified_volume"],
            row["trade_size_squared_sum"], row["effective_trade_count"],
            row["signed_trade_imbalance"], row["unclassified_share"],
        )
        digest.update(dumps(payload, default=str, separators=(",", ":")).encode())
    return digest.hexdigest()


def persist_calibration(
    conn: psycopg.Connection,
    *,
    report: dict[str, Any],
    rows: Sequence[dict[str, Any]],
    timeframe: str,
    feed: str,
) -> int:
    if not rows:
        raise ValueError("An empty predictor sample cannot be persisted as a calibration.")
    dataset_hash = _dataset_hash(rows)
    spec_hash = str(report["specification_hash"])
    existing = conn.execute(
        """
        SELECT id FROM intraday_trade_imbalance_calibrations
        WHERE dataset_hash = %s AND specification_hash = %s
        """,
        (dataset_hash, spec_hash),
    ).fetchone()
    if existing:
        return int(existing["id"])
    mode = str((report.get("threshold") or {}).get("mode") or "global")
    global_threshold = (report.get("threshold") or {}).get("global_rounded_up")
    row = conn.execute(
        """
        INSERT INTO intraday_trade_imbalance_calibrations(
            timeframe, provider, feed, window_start, window_end, symbols,
            dataset_hash, specification_hash, threshold_mode,
            calibrated_threshold, ready_for_declaration, report,
            calculation_version
        ) VALUES (%s, 'alpaca', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            timeframe,
            feed,
            min(item["timestamp"] for item in rows),
            max(item["timestamp"] for item in rows),
            Jsonb(sorted({item["symbol"] for item in rows})),
            dataset_hash,
            spec_hash,
            mode,
            global_threshold,
            bool(report["ready_for_declaration"]),
            Jsonb(report),
            CALIBRATION_VERSION,
        ),
    ).fetchone()
    calibration_id = int(row["id"])
    for item in rows:
        conn.execute(
            """
            INSERT INTO intraday_trade_imbalance_calibration_rows(
                calibration_id, symbol, timestamp, session_date, time_slot,
                liquidity_bucket, trade_count, total_volume, classified_volume,
                trade_size_squared_sum, effective_trade_count,
                signed_trade_imbalance, unclassified_share
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                calibration_id, item["symbol"], item["timestamp"], item["session_date"],
                item["time_slot"], item.get("liquidity_bucket") or "unassigned",
                item["trade_count"], item["total_volume"], item["classified_volume"],
                item["trade_size_squared_sum"], item["effective_trade_count"],
                item["signed_trade_imbalance"], item["unclassified_share"],
            ),
        )
    conn.commit()
    return calibration_id


def load_calibration(
    conn: psycopg.Connection, calibration_id: int, *, require_ready: bool = True
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM intraday_trade_imbalance_calibrations WHERE id = %s",
        (calibration_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"No signed-trade-imbalance calibration id={calibration_id}.")
    calibration = dict(row)
    if require_ready and not calibration["ready_for_declaration"]:
        raise ValueError(
            f"Calibration {calibration_id} did not pass its preregistered minimum "
            "data and stability gates; it cannot authorize a v2 hypothesis."
        )
    return calibration
