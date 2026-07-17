from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import mean, median
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.services.research_architecture import (
    HYPOTHESIS_VERSION,
    ensure_research_architecture_tables,
    jsonable,
    load_snapshot_candles,
    stable_hash,
)
from app.services.strategy_research import finite_metric


PHASE_5_OBSERVATION_VERSION = "deeper_market_observations_v1"


@dataclass(frozen=True)
class ObservationDefinition:
    key: str
    label: str
    expected_range: str
    strategy_family: str
    definition: str
    hypothesis: str
    expected_behavior: str
    falsification: str
    relevant_regimes: tuple[str, ...]


OBSERVATION_DEFINITIONS: tuple[ObservationDefinition, ...] = (
    ObservationDefinition(
        "trend_maturity",
        "Trend maturity",
        "0.0 to 1.0",
        "Pullback",
        "Bars in an aligned EMA20/EMA50 trend, normalized by a capped trend-age window.",
        "Mature but non-exhausted trends create higher-quality pullback continuation opportunities.",
        "A controlled retracement resumes in the direction of the established trend.",
        "Falsified if mature-trend pullbacks fail unchanged trade-count, expectancy, drawdown, or walk-forward gates.",
        ("bull_trend", "normal_volatility", "low_volatility"),
    ),
    ObservationDefinition(
        "trend_acceleration",
        "Trend acceleration",
        "0.0 to 1.0",
        "Momentum",
        "Recent 5-bar return improvement versus the prior 5-bar return, scaled by recent volatility.",
        "Positive trend acceleration identifies momentum entries with better continuation odds.",
        "Acceleration persists after entry rather than immediately reverting.",
        "Falsified if acceleration-filtered candidates fail unchanged economic or stability gates.",
        ("bull_trend", "normal_volatility", "high_volatility"),
    ),
    ObservationDefinition(
        "volatility_contraction",
        "Volatility contraction",
        "0.0 to 1.0",
        "Range Breakout",
        "Recent median true range below the prior baseline true range without using future bars.",
        "Contractions define cleaner range-breakout staging areas.",
        "A narrow range resolves directionally after sufficient compression.",
        "Falsified if contraction breakouts do not survive unchanged validation gates.",
        ("sideways", "low_volatility", "normal_volatility"),
    ),
    ObservationDefinition(
        "volatility_expansion",
        "Volatility expansion",
        "0.0 to 1.0",
        "Volatility Expansion",
        "Current true range expansion versus the prior rolling median range.",
        "Directional volatility expansion with participation produces continuation opportunities.",
        "Expansion follows through without breaching existing drawdown and regime-stability gates.",
        "Falsified if expansion entries reverse or collapse under unchanged validation gates.",
        ("high_volatility", "bull_trend", "normal_volatility"),
    ),
    ObservationDefinition(
        "breakout_quality",
        "Breakout quality",
        "0.0 to 1.0",
        "Breakout",
        "Break distance beyond the prior high, close location, and volume participation measured at the bar.",
        "High-quality breakouts are more likely to continue than boundary touches.",
        "Price follows through after a clean, participated range break.",
        "Falsified if quality-filtered breakouts fail unchanged expectancy or stability gates.",
        ("bull_trend", "high_volatility", "normal_volatility"),
    ),
    ObservationDefinition(
        "pullback_quality",
        "Pullback quality",
        "0.0 to 1.0",
        "Pullback",
        "Retracement depth inside an EMA-aligned trend plus reclaim confirmation.",
        "Bounded pullbacks inside aligned trends create better continuation entries.",
        "The trend resumes after a measurable retracement.",
        "Falsified if bounded pullbacks fail unchanged validation gates or only work on source evidence.",
        ("bull_trend", "normal_volatility", "low_volatility"),
    ),
    ObservationDefinition(
        "momentum_persistence",
        "Momentum persistence",
        "0.0 to 1.0",
        "Continuation",
        "Share of same-direction closes inside the recent lookback, weighted by 5-bar return direction.",
        "Persistent momentum after a short pause has measurable continuation value.",
        "Directional persistence survives a bounded pause and resumes.",
        "Falsified if persistence-filtered candidates fail unchanged gates.",
        ("bull_trend", "normal_volatility"),
    ),
    ObservationDefinition(
        "exhaustion",
        "Exhaustion",
        "0.0 to 1.0",
        "Mean Reversion",
        "Overextension from EMA20 combined with RSI extremes and decelerating momentum.",
        "Exhausted extensions are more likely to mean-revert than continue.",
        "An overextended move reverts toward its local mean.",
        "Falsified if exhaustion entries fail unchanged economic or drawdown gates.",
        ("sideways", "high_volatility", "normal_volatility"),
    ),
    ObservationDefinition(
        "liquidity_expansion",
        "Liquidity expansion",
        "0.0 to 1.0",
        "Volatility Expansion",
        "Current volume and dollar volume expansion versus prior rolling medians.",
        "Participation expansion improves reliability of directional signals.",
        "Signals with liquidity expansion show better follow-through than thin moves.",
        "Falsified if liquidity-filtered signals fail unchanged validation gates.",
        ("bull_trend", "high_volatility", "normal_volatility"),
    ),
    ObservationDefinition(
        "false_breakout",
        "False breakout",
        "0.0 to 1.0",
        "Mean Reversion",
        "A prior boundary break that closes back inside the range using only current and previous bars.",
        "Failed breaks create measurable reversal opportunities.",
        "A failed boundary break reverts toward the prior range.",
        "Falsified if false-breakout reversals fail unchanged gates.",
        ("sideways", "high_volatility"),
    ),
    ObservationDefinition(
        "structural_shift",
        "Structural market shift",
        "0.0 to 1.0",
        "Volatility Expansion",
        "Recent trend and volatility state diverges materially from the preceding baseline.",
        "Structural shifts identify when old local behavior should not be extrapolated blindly.",
        "A new volatility/trend state produces distinct strategy performance.",
        "Falsified if shift-aware candidates do not improve robustness under unchanged validation gates.",
        ("high_volatility", "normal_volatility", "bull_trend", "sideways"),
    ),
)


def calculate_deeper_observation_series(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted((dict(row) for row in candles), key=lambda row: row["timestamp"])
    if len(ordered) < 60:
        return []
    closes = [finite_metric(row["close"]) for row in ordered]
    highs = [finite_metric(row["high"]) for row in ordered]
    lows = [finite_metric(row["low"]) for row in ordered]
    volumes = [finite_metric(row["volume"]) for row in ordered]
    tr = true_ranges(ordered)
    ema20 = ema_series(closes, 20)
    ema50 = ema_series(closes, 50)
    rsi14 = rsi_series(closes, 14)
    rows = []
    trend_age = 0
    for index in range(len(ordered)):
        if index < 59:
            continue
        aligned = ema20[index] is not None and ema50[index] is not None and closes[index] >= ema20[index] >= ema50[index]
        trend_age = trend_age + 1 if aligned else 0
        prior_20_high = max(highs[index - 20 : index])
        prior_20_low = min(lows[index - 20 : index])
        prior_range_width = max(0.0, prior_20_high - prior_20_low)
        close_location = (closes[index] - lows[index]) / (highs[index] - lows[index]) if highs[index] > lows[index] else 0.5
        recent_tr = median(tr[index - 4 : index + 1])
        baseline_tr = median(tr[index - 24 : index - 4])
        volume_base = median(volumes[index - 20 : index]) or 0.0
        dollar_base = median([volumes[i] * closes[i] for i in range(index - 20, index)]) or 0.0
        current_dollar = volumes[index] * closes[index]
        return_5 = closes[index] / closes[index - 5] - 1 if closes[index - 5] else 0.0
        prior_return_5 = closes[index - 5] / closes[index - 10] - 1 if closes[index - 10] else 0.0
        volatility = median([abs(closes[i] / closes[i - 1] - 1) for i in range(index - 19, index + 1) if closes[i - 1]]) or 0.000001
        same_direction = sum(1 for i in range(index - 9, index + 1) if closes[i] > closes[i - 1])
        persistence = same_direction / 10 if return_5 >= 0 else (10 - same_direction) / 10
        distance_ema20 = (closes[index] - ema20[index]) / ema20[index] if ema20[index] else 0.0
        prior_break_high = highs[index - 1] > max(highs[index - 21 : index - 1])
        prior_break_low = lows[index - 1] < min(lows[index - 21 : index - 1])
        false_break = (prior_break_high and closes[index] < prior_20_high) or (prior_break_low and closes[index] > prior_20_low)
        recent_vol = median([abs(closes[i] / closes[i - 1] - 1) for i in range(index - 9, index + 1) if closes[i - 1]])
        base_vol = median([abs(closes[i] / closes[i - 1] - 1) for i in range(index - 39, index - 9) if closes[i - 1]]) or 0.000001
        recent_slope = closes[index] / closes[index - 10] - 1 if closes[index - 10] else 0.0
        base_slope = closes[index - 10] / closes[index - 40] - 1 if closes[index - 40] else 0.0
        scores = {
            "trend_maturity": clamp(trend_age / 80),
            "trend_acceleration": clamp((return_5 - prior_return_5) / (volatility * 8)),
            "volatility_contraction": clamp(1 - (recent_tr / baseline_tr if baseline_tr else 1)),
            "volatility_expansion": clamp(((tr[index] / baseline_tr) - 1) / 1.5 if baseline_tr else 0.0),
            "breakout_quality": clamp((closes[index] / prior_20_high - 1) * 80) * 0.45 + clamp((close_location - 0.55) / 0.35) * 0.25 + clamp((volumes[index] / volume_base - 1) / 1.5 if volume_base else 0.0) * 0.30,
            "pullback_quality": clamp(1 - abs(((max(highs[index - 20 : index + 1]) - closes[index]) / max(highs[index - 20 : index + 1]) if max(highs[index - 20 : index + 1]) else 0.0) - 0.03) / 0.04) if aligned and closes[index] > closes[index - 1] else 0.0,
            "momentum_persistence": clamp(persistence),
            "exhaustion": clamp(abs(distance_ema20) / 0.08) * 0.45 + clamp((abs((rsi14[index] or 50) - 50) - 20) / 30) * 0.35 + clamp((prior_return_5 - return_5) / (volatility * 8)) * 0.20,
            "liquidity_expansion": clamp((volumes[index] / volume_base - 1) / 1.5 if volume_base else 0.0) * 0.55 + clamp((current_dollar / dollar_base - 1) / 1.5 if dollar_base else 0.0) * 0.45,
            "false_breakout": 1.0 if false_break else 0.0,
            "structural_shift": clamp(abs(recent_vol / base_vol - 1) / 1.5) * 0.55 + clamp(abs(recent_slope - base_slope) / max(0.01, abs(base_slope) + 0.01)) * 0.45,
        }
        rows.append(
            {
                "timestamp": jsonable(ordered[index]["timestamp"]),
                "symbol": ordered[index].get("symbol"),
                "timeframe": ordered[index].get("timeframe"),
                **{key: round(float(value), 6) for key, value in scores.items()},
            }
        )
    return rows


def aggregate_deeper_observations(candles: list[dict[str, Any]]) -> dict[str, Any]:
    series = calculate_deeper_observation_series(candles)
    aggregates: dict[str, Any] = {}
    for definition in OBSERVATION_DEFINITIONS:
        values = [finite_metric(row.get(definition.key)) for row in series]
        aggregates[definition.key] = {
            "score": round(mean(values), 6) if values else 0.0,
            "event_rate": round(sum(1 for value in values if value >= 0.60) / len(values), 6) if values else 0.0,
            "p90": round(percentile(values, 0.90), 6) if values else 0.0,
            "sample_size": len(values),
            "definition": definition.definition,
            "expected_range": definition.expected_range,
            "version": PHASE_5_OBSERVATION_VERSION,
        }
    return {
        "calculation_version": PHASE_5_OBSERVATION_VERSION,
        "series_sample_size": len(series),
        "observations": aggregates,
        "definitions": {definition.key: definition.__dict__ for definition in OBSERVATION_DEFINITIONS},
        "leakage_controls": {
            "uses_future_bars": False,
            "decision_point": "each observation uses only candles at or before its timestamp",
            "minimum_history_bars": 60,
        },
    }


def create_deeper_observation_hypotheses(
    conn: psycopg.Connection,
    *,
    dataset_id: int = 1,
    max_hypotheses: int | None = None,
) -> dict[str, Any]:
    ensure_research_architecture_tables(conn)
    manifest = conn.execute("SELECT * FROM research_dataset_manifests WHERE id = %s", (dataset_id,)).fetchone()
    if not manifest:
        raise ValueError(f"research dataset {dataset_id} was not found")
    observations_by_market = []
    for symbol in manifest["assets"]:
        for timeframe in manifest["timeframes"]:
            candles = load_snapshot_candles(conn, dataset_id, symbol, timeframe)
            bundle = aggregate_deeper_observations(candles)
            observations_by_market.append({"symbol": symbol, "timeframe": timeframe, **bundle})
    dataset_observations = aggregate_dataset_observations(observations_by_market)
    hypothesis_payloads = build_phase5_hypotheses(dict(manifest), dataset_observations, max_hypotheses=max_hypotheses or len(OBSERVATION_DEFINITIONS))
    stored = []
    for hypothesis in hypothesis_payloads:
        existing = conn.execute(
            """
            SELECT * FROM research_hypothesis_versions
            WHERE hypothesis_key = %s
              AND test_summary->>'source_dataset_id' = %s
              AND test_summary->>'phase' = '5'
            ORDER BY version DESC, id DESC
            LIMIT 1
            """,
            (hypothesis["hypothesis_key"], str(dataset_id)),
        ).fetchone()
        if existing:
            stored.append(jsonable(dict(existing)))
            continue
        version_row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM research_hypothesis_versions WHERE hypothesis_key = %s",
            (hypothesis["hypothesis_key"],),
        ).fetchone()
        row = conn.execute(
            """
            INSERT INTO research_hypothesis_versions(
                hypothesis_key, version, parent_hypothesis_id, scope_type, scope_ref, strategy_family,
                title, observation, hypothesis, expected_behavior, relevant_regimes, confidence_score,
                evidence_window, creation_source, status, supporting_evidence, contradictory_evidence,
                test_summary, calculation_version, simulation_only
            ) VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'proposed', %s, %s, %s, %s, TRUE)
            RETURNING *
            """,
            (
                hypothesis["hypothesis_key"],
                int(version_row["next_version"]),
                hypothesis["scope_type"],
                hypothesis["scope_ref"],
                hypothesis["strategy_family"],
                hypothesis["title"],
                hypothesis["observation"],
                hypothesis["hypothesis"],
                hypothesis["expected_behavior"],
                Jsonb(hypothesis["relevant_regimes"]),
                hypothesis["confidence_score"],
                Jsonb(hypothesis["evidence_window"]),
                hypothesis["creation_source"],
                Jsonb(hypothesis["supporting_evidence"]),
                Jsonb(hypothesis["contradictory_evidence"]),
                Jsonb(hypothesis["test_summary"]),
                HYPOTHESIS_VERSION,
            ),
        ).fetchone()
        stored.append(jsonable(dict(row)))
    conn.commit()
    return {
        "dataset_id": dataset_id,
        "phase": 5,
        "calculation_version": PHASE_5_OBSERVATION_VERSION,
        "stored_hypothesis_ids": [row["id"] for row in stored],
        "hypotheses": stored,
        "observations": dataset_observations,
        "simulation_only": True,
        "phase6_started": False,
    }


def aggregate_dataset_observations(markets: list[dict[str, Any]]) -> dict[str, Any]:
    result = {
        "markets": [{"symbol": row["symbol"], "timeframe": row["timeframe"], "series_sample_size": row["series_sample_size"], "observations": row["observations"]} for row in markets],
        "observations": {},
        "market_count": len(markets),
        "calculation_version": PHASE_5_OBSERVATION_VERSION,
    }
    for definition in OBSERVATION_DEFINITIONS:
        weighted_scores = []
        event_rates = []
        samples = 0
        for market in markets:
            row = market["observations"][definition.key]
            sample_size = int(row["sample_size"])
            samples += sample_size
            weighted_scores.extend([finite_metric(row["score"])] * max(1, sample_size))
            event_rates.append(finite_metric(row["event_rate"]))
        result["observations"][definition.key] = {
            "score": round(mean(weighted_scores), 6) if weighted_scores else 0.0,
            "event_rate": round(mean(event_rates), 6) if event_rates else 0.0,
            "sample_size": samples,
            "definition": definition.definition,
            "expected_range": definition.expected_range,
            "strategy_family": definition.strategy_family,
        }
    return result


def build_phase5_hypotheses(manifest: dict[str, Any], observations: dict[str, Any], *, max_hypotheses: int) -> list[dict[str, Any]]:
    dataset_id = int(manifest["id"])
    scope_ref = str(manifest["dataset_key"])
    assets = list(manifest.get("assets") or [])
    timeframes = list(manifest.get("timeframes") or [])
    ranked = sorted(
        OBSERVATION_DEFINITIONS,
        key=lambda definition: (
            observations["observations"][definition.key]["event_rate"],
            observations["observations"][definition.key]["score"],
            definition.key,
        ),
        reverse=True,
    )[:max_hypotheses]
    rows = []
    for definition in ranked:
        measured = observations["observations"][definition.key]
        support = measured["score"]
        event_rate = measured["event_rate"]
        confidence = round(min(0.82, 0.35 + support * 0.30 + event_rate * 0.20 + min(0.15, math.log10(max(10, measured["sample_size"])) / 40)), 4)
        contradictory = []
        contradictory_notes = []
        if support < 0.20 or event_rate < 0.05:
            contradictory.append(f"research_dataset:{dataset_id}")
            contradictory_notes.append("The observation is measurable but sparse or weak on this preserved dataset.")
        hypothesis_key = f"phase5_obs_{stable_hash({'dataset_scope': scope_ref, 'observation': definition.key, 'version': PHASE_5_OBSERVATION_VERSION})[:20]}"
        rows.append(
            {
                "hypothesis_key": hypothesis_key,
                "scope_type": "universal",
                "scope_ref": scope_ref,
                "strategy_family": definition.strategy_family,
                "title": f"{definition.label} market-structure hypothesis",
                "observation": (
                    f"{definition.label} is measured deterministically on frozen dataset {dataset_id}: "
                    f"score={support:.6f}, event_rate={event_rate:.6f}, sample_size={measured['sample_size']}. "
                    "Post-hoc and unconfirmed."
                ),
                "hypothesis": definition.hypothesis,
                "expected_behavior": definition.expected_behavior,
                "relevant_regimes": list(definition.relevant_regimes),
                "confidence_score": confidence,
                "evidence_window": {
                    "dataset_id": dataset_id,
                    "dataset_key": scope_ref,
                    "assets": assets,
                    "timeframes": timeframes,
                    "sample_size": measured["sample_size"],
                    "observation_key": definition.key,
                    "independent_confirmation_required": True,
                },
                "creation_source": "phase5_deeper_observations",
                "supporting_evidence": [f"research_dataset:{dataset_id}"],
                "contradictory_evidence": contradictory,
                "test_summary": {
                    "phase": 5,
                    "source_dataset_id": dataset_id,
                    "post_hoc": True,
                    "confirmation_status": "unconfirmed",
                    "observation_version": PHASE_5_OBSERVATION_VERSION,
                    "observation_key": definition.key,
                    "definition": definition.definition,
                    "expected_range": definition.expected_range,
                    "measured": measured,
                    "contradictory_observations": contradictory_notes,
                    "falsification_criteria": definition.falsification,
                    "candidate_generation_contract": "standard generate_targeted_candidates using existing strategy_family",
                    "validation_policy": "unchanged strong_research_gates:v1",
                    "leakage_control": "causal rolling windows only; no future bars used",
                    "generation_seed": 0,
                },
            }
        )
    return rows


def true_ranges(candles: list[dict[str, Any]]) -> list[float]:
    ranges = [0.0]
    for index in range(1, len(candles)):
        high = finite_metric(candles[index]["high"])
        low = finite_metric(candles[index]["low"])
        prev = finite_metric(candles[index - 1]["close"])
        ranges.append(max(high - low, abs(high - prev), abs(low - prev)) / prev if prev else 0.0)
    return ranges


def ema_series(values: list[float], period: int) -> list[float | None]:
    rows: list[float | None] = [None] * len(values)
    if len(values) < period:
        return rows
    current = mean(values[:period])
    rows[period - 1] = current
    multiplier = 2 / (period + 1)
    for index in range(period, len(values)):
        current = (values[index] - current) * multiplier + current
        rows[index] = current
    return rows


def rsi_series(values: list[float], period: int) -> list[float | None]:
    rows: list[float | None] = [None] * len(values)
    for index in range(period, len(values)):
        deltas = [values[i] - values[i - 1] for i in range(index - period + 1, index + 1)]
        gains = [max(0.0, value) for value in deltas]
        losses = [abs(min(0.0, value)) for value in deltas]
        avg_gain = mean(gains)
        avg_loss = mean(losses)
        if avg_loss == 0:
            rows[index] = 100.0 if avg_gain > 0 else 50.0
        else:
            rs = avg_gain / avg_loss
            rows[index] = 100 - (100 / (1 + rs))
    return rows


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * quantile))))
    return ordered[position]


def clamp(value: float) -> float:
    if value != value or value in {float("inf"), float("-inf")}:
        return 0.0
    return max(0.0, min(1.0, float(value)))
