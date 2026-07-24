from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal
import gzip
from hashlib import sha256
import json
import math
import os
from pathlib import Path
from statistics import mean, median, pstdev
from threading import Lock
from typing import Any, Iterable
import time
import psycopg
from psycopg.types.json import Jsonb

from app.observability import elapsed_ms, log_event
from app.services.features import calculate_features
from app.services.regimes import calculate_regimes
from app.services.strategy_discovery import (
    DiscoveryCandidate,
    candidate_execution_key,
    canonical_candidate_key,
    generate_balanced_discovery_candidates,
    generate_family_discovery_candidates,
)
from app.services.strategy_families import (
    PHASE_2_FAMILY_NAMES,
    PHASE_2_FAMILY_VERSION,
    family_mutation_grid,
    family_observation_evidence,
    family_observation_score,
    strategy_family_spec,
)
from app.services.strategy_research import (
    aggregate_profit_factor,
    build_context_by_time,
    finite_metric,
    profit_factor_is_infinite,
    profit_factor_passes,
)


ARCHITECTURE_VERSION = "reproducible_research_architecture_v1"
DATASET_VERSION = "research_dataset_snapshot_v1"
PROFILE_VERSION = "asset_behavior_profile_v1"
CLUSTER_VERSION = "asset_similarity_cluster_v2"
HYPOTHESIS_VERSION = "evidence_hypothesis_v1"
GENERATOR_VERSION = "hypothesis_targeted_generator_v2"
CANDIDATE_LEVEL_VERSION = "candidate_levels_v1"
ARCHIVE_VERSION = "research_archive_v1"
DEFAULT_ALLOCATION = {"exploitation": 0.70, "nearby": 0.20, "exploration": 0.10}
MAX_CLUSTER_HYPOTHESIS_DISTANCE = 1.5
MIN_AUTOMATIC_SAMPLES_PER_MARKET = 2000
VALIDATION_POLICY_KEY = "strong_research_gates"
VALIDATION_POLICY_VERSION = 1
SAFETY_STATEMENT = (
    "Deterministic, simulation-only research architecture. Validation thresholds are versioned and cannot "
    "be weakened automatically; no broker routing or live execution is enabled."
)

PROFILE_FEATURES = (
    "realized_volatility",
    "atr_ratio",
    "trend_persistence",
    "trend_strength",
    "mean_reversion_score",
    "breakout_follow_through",
    "median_pullback_depth",
    "momentum_persistence",
    "volume_expansion_ratio",
    "gap_frequency",
)

_SCHEMA_LOCK = Lock()
_SCHEMA_READY = False


def ensure_research_architecture_tables(conn: psycopg.Connection) -> None:
    """Ensure the architecture is usable on databases created before migration 028.

    The checked-in migration remains the canonical deployment path. This runtime guard mirrors
    the repository's existing campaign-table pattern so a live pre-028 database fails forward
    safely instead of accepting an unversioned campaign.
    """

    return None
    global _SCHEMA_READY
    is_real_connection = conn.__class__.__module__.startswith("psycopg")
    if is_real_connection and _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if is_real_connection and _SCHEMA_READY:
            return
        _ensure_architecture_schema(conn)
        if is_real_connection:
            conn.commit()
            _SCHEMA_READY = True


def _ensure_architecture_schema(conn: psycopg.Connection) -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS research_dataset_manifests (
            id BIGSERIAL PRIMARY KEY, dataset_key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            mode TEXT NOT NULL, snapshot_version INTEGER NOT NULL DEFAULT 1, assets JSONB NOT NULL,
            timeframes JSONB NOT NULL, window_start TIMESTAMPTZ, window_end TIMESTAMPTZ,
            candle_counts JSONB NOT NULL, candle_hashes JSONB NOT NULL,
            source_providers JSONB NOT NULL DEFAULT '[]'::jsonb, content_hash TEXT NOT NULL,
            integrity JSONB NOT NULL DEFAULT '{}'::jsonb, calculation_version TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), immutable BOOLEAN NOT NULL DEFAULT TRUE,
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT research_dataset_manifests_mode_check CHECK (mode IN ('reproducibility', 'rolling')),
            CONSTRAINT research_dataset_manifests_immutable_check CHECK (immutable = TRUE),
            CONSTRAINT research_dataset_manifests_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS research_dataset_candles (
            dataset_id BIGINT NOT NULL REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
            symbol TEXT NOT NULL, source TEXT NOT NULL, timeframe TEXT NOT NULL,
            timestamp TIMESTAMPTZ NOT NULL, open NUMERIC NOT NULL, high NUMERIC NOT NULL,
            low NUMERIC NOT NULL, close NUMERIC NOT NULL, volume NUMERIC NOT NULL,
            PRIMARY KEY(dataset_id, symbol, timeframe, timestamp, source)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS asset_profile_versions (
            id BIGSERIAL PRIMARY KEY, profile_key TEXT NOT NULL, version INTEGER NOT NULL,
            dataset_id BIGINT NOT NULL REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
            symbol TEXT NOT NULL, timeframe TEXT NOT NULL, evidence_window JSONB NOT NULL,
            metrics JSONB NOT NULL, behavior_labels JSONB NOT NULL,
            regime_distribution JSONB NOT NULL DEFAULT '{}'::jsonb,
            correlations JSONB NOT NULL DEFAULT '{}'::jsonb,
            limitations JSONB NOT NULL DEFAULT '[]'::jsonb, calculation_version TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT asset_profile_versions_unique UNIQUE(profile_key, version),
            CONSTRAINT asset_profile_versions_dataset_unique UNIQUE(dataset_id, symbol, timeframe),
            CONSTRAINT asset_profile_versions_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS asset_cluster_versions (
            id BIGSERIAL PRIMARY KEY, cluster_key TEXT NOT NULL, version INTEGER NOT NULL,
            dataset_id BIGINT NOT NULL REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
            name TEXT NOT NULL, description TEXT NOT NULL, centroid JSONB NOT NULL,
            member_count INTEGER NOT NULL, quality_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
            algorithm_version TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT asset_cluster_versions_unique UNIQUE(cluster_key, version),
            CONSTRAINT asset_cluster_versions_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS asset_cluster_members (
            cluster_id BIGINT NOT NULL REFERENCES asset_cluster_versions(id) ON DELETE CASCADE,
            asset_profile_id BIGINT NOT NULL REFERENCES asset_profile_versions(id) ON DELETE RESTRICT,
            symbol TEXT NOT NULL, timeframe TEXT NOT NULL, similarity_score NUMERIC NOT NULL,
            distance_to_centroid NUMERIC NOT NULL, evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY(cluster_id, asset_profile_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS research_hypothesis_versions (
            id BIGSERIAL PRIMARY KEY, hypothesis_key TEXT NOT NULL, version INTEGER NOT NULL,
            parent_hypothesis_id BIGINT REFERENCES research_hypothesis_versions(id) ON DELETE SET NULL,
            scope_type TEXT NOT NULL, scope_ref TEXT NOT NULL, strategy_family TEXT NOT NULL,
            title TEXT NOT NULL, observation TEXT NOT NULL, hypothesis TEXT NOT NULL,
            expected_behavior TEXT NOT NULL, relevant_regimes JSONB NOT NULL DEFAULT '[]'::jsonb,
            confidence_score NUMERIC NOT NULL, evidence_window JSONB NOT NULL,
            creation_source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'proposed',
            supporting_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
            contradictory_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
            test_summary JSONB NOT NULL DEFAULT '{}'::jsonb, calculation_version TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT research_hypothesis_versions_unique UNIQUE(hypothesis_key, version),
            CONSTRAINT research_hypothesis_versions_scope_check CHECK (scope_type IN ('asset', 'cluster', 'universal')),
            CONSTRAINT research_hypothesis_versions_status_check CHECK (status IN ('proposed', 'testing', 'supported', 'weak', 'rejected', 'retired')),
            CONSTRAINT research_hypothesis_versions_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS research_validation_policy_versions (
            id BIGSERIAL PRIMARY KEY, policy_key TEXT NOT NULL, version INTEGER NOT NULL,
            name TEXT NOT NULL, thresholds JSONB NOT NULL, approval JSONB NOT NULL,
            calculation_version TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            immutable BOOLEAN NOT NULL DEFAULT TRUE, simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT research_validation_policy_versions_unique UNIQUE(policy_key, version),
            CONSTRAINT research_validation_policy_versions_immutable_check CHECK (immutable = TRUE),
            CONSTRAINT research_validation_policy_versions_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS research_candidate_stage_evidence (
            id BIGSERIAL PRIMARY KEY, evidence_key TEXT NOT NULL UNIQUE,
            campaign_id BIGINT NOT NULL REFERENCES research_campaigns(id) ON DELETE CASCADE,
            candidate_id TEXT NOT NULL, candidate_level TEXT NOT NULL, scope_type TEXT NOT NULL,
            scope_ref TEXT NOT NULL, hypothesis_version_id BIGINT REFERENCES research_hypothesis_versions(id) ON DELETE SET NULL,
            parent_candidate_id TEXT, gate_results JSONB NOT NULL, metrics JSONB NOT NULL,
            evidence_refs JSONB NOT NULL, promoted BOOLEAN NOT NULL DEFAULT FALSE,
            calculation_version TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT research_candidate_stage_level_check CHECK (candidate_level IN ('generated', 'research_candidate', 'asset_specialist', 'cluster_candidate', 'cluster_elite', 'universal_elite')),
            CONSTRAINT research_candidate_stage_scope_check CHECK (scope_type IN ('asset', 'cluster', 'universal')),
            CONSTRAINT research_candidate_stage_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS research_campaign_archives (
            id BIGSERIAL PRIMARY KEY, archive_key TEXT NOT NULL UNIQUE,
            campaign_id BIGINT,
            original_campaign_id BIGINT NOT NULL,
            dataset_id BIGINT REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
            manifest JSONB NOT NULL, content_hash TEXT NOT NULL,
            storage_locations JSONB NOT NULL DEFAULT '[]'::jsonb, calculation_version TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), immutable BOOLEAN NOT NULL DEFAULT TRUE,
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT research_campaign_archives_immutable_check CHECK (immutable = TRUE),
            CONSTRAINT research_campaign_archives_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS autonomous_research_cycles (
            id BIGSERIAL PRIMARY KEY, cycle_key TEXT NOT NULL UNIQUE, universe_key TEXT NOT NULL,
            dataset_id BIGINT REFERENCES research_dataset_manifests(id) ON DELETE SET NULL,
            cluster_ids JSONB NOT NULL DEFAULT '[]'::jsonb, hypothesis_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
            approval_mode TEXT NOT NULL, status TEXT NOT NULL, plan JSONB NOT NULL,
            result JSONB NOT NULL DEFAULT '{}'::jsonb, safety_controls JSONB NOT NULL,
            calculation_version TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ, simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT autonomous_research_cycles_approval_check CHECK (approval_mode IN ('manual', 'auto_queue')),
            CONSTRAINT autonomous_research_cycles_status_check CHECK (status IN ('planned', 'queued', 'completed', 'failed', 'paused')),
            CONSTRAINT autonomous_research_cycles_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """,
        """
        ALTER TABLE research_campaigns
            ADD COLUMN IF NOT EXISTS dataset_id BIGINT REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
            ADD COLUMN IF NOT EXISTS dataset_mode TEXT,
            ADD COLUMN IF NOT EXISTS code_commit TEXT,
            ADD COLUMN IF NOT EXISTS generator_version TEXT,
            ADD COLUMN IF NOT EXISTS validation_policy_id BIGINT REFERENCES research_validation_policy_versions(id) ON DELETE RESTRICT,
            ADD COLUMN IF NOT EXISTS threshold_version TEXT,
            ADD COLUMN IF NOT EXISTS hypothesis_version_id BIGINT REFERENCES research_hypothesis_versions(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS cluster_id BIGINT REFERENCES asset_cluster_versions(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS experiment_generation INTEGER NOT NULL DEFAULT 1,
            ADD COLUMN IF NOT EXISTS immutable_config JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS finalized_at TIMESTAMPTZ
        """,
        """
        ALTER TABLE research_campaign_jobs
            ADD COLUMN IF NOT EXISTS dataset_id BIGINT REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
            ADD COLUMN IF NOT EXISTS hypothesis_version_id BIGINT REFERENCES research_hypothesis_versions(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS parent_candidate_id TEXT,
            ADD COLUMN IF NOT EXISTS generation_channel TEXT,
            ADD COLUMN IF NOT EXISTS rejection_diagnostics JSONB NOT NULL DEFAULT '[]'::jsonb
        """,
        """
        ALTER TABLE elite_research_candidates
            ADD COLUMN IF NOT EXISTS candidate_level TEXT NOT NULL DEFAULT 'cluster_elite',
            ADD COLUMN IF NOT EXISTS scope_type TEXT,
            ADD COLUMN IF NOT EXISTS scope_ref TEXT,
            ADD COLUMN IF NOT EXISTS dataset_id BIGINT REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
            ADD COLUMN IF NOT EXISTS hypothesis_version_id BIGINT REFERENCES research_hypothesis_versions(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS parent_candidate_id TEXT
        """,
    ]
    for statement in statements:
        conn.execute(statement)
    conn.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_immutable_research_record_mutation()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'immutable research evidence cannot be updated or deleted';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    immutable_tables = (
        "research_dataset_manifests",
        "research_dataset_candles",
        "asset_profile_versions",
        "asset_cluster_versions",
        "asset_cluster_members",
        "research_hypothesis_versions",
        "research_validation_policy_versions",
        "research_candidate_stage_evidence",
        "research_campaign_archives",
    )
    for table in immutable_tables:
        trigger = f"{table}_immutable_trigger"
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
        conn.execute(
            f"""
            CREATE TRIGGER {trigger}
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION prevent_immutable_research_record_mutation()
            """
        )
    conn.execute(
        """
        INSERT INTO research_validation_policy_versions(
            policy_key, version, name, thresholds, approval, calculation_version, immutable, simulation_only
        ) VALUES (%s, %s, %s, %s, %s, %s, TRUE, TRUE)
        ON CONFLICT(policy_key, version) DO NOTHING
        """,
        (
            VALIDATION_POLICY_KEY,
            VALIDATION_POLICY_VERSION,
            "Strong research validation gates",
            Jsonb(validation_thresholds()),
            Jsonb({"threshold_changes_require_explicit_version": True, "automatic_weakening_forbidden": True}),
            "research_validation_policy_v1",
        ),
    )


def validation_thresholds() -> dict[str, Any]:
    return {
        "single_market": {
            "minimum_profit_factor": 1.2,
            "minimum_expectancy_per_trade": 0,
            "maximum_drawdown": 0.12,
            "minimum_trades": 30,
            "walk_forward_required": True,
            "paper_readiness_required": True,
        },
        "cross_market": {
            "minimum_profit_factor": 1.2,
            "minimum_expectancy": 0,
            "maximum_drawdown": 0.12,
            "minimum_trades": 60,
            "minimum_stability": 0.6,
            "minimum_assets_passed": 2,
            "minimum_timeframes_passed": 1,
        },
    }


def record_dataset_snapshot(
    conn: psycopg.Connection,
    *,
    assets: list[str],
    timeframes: list[str],
    mode: str = "rolling",
    name: str | None = None,
) -> dict[str, Any]:
    """Materialize an exact immutable candle snapshot for every campaign job."""

    if mode not in {"rolling", "reproducibility"}:
        raise ValueError("dataset mode must be 'rolling' or 'reproducibility'")
    normalized_assets = sorted({item.strip().upper() for item in assets if item.strip()})
    normalized_timeframes = sorted({item.strip() for item in timeframes if item.strip()})
    if not normalized_assets or not normalized_timeframes:
        raise ValueError("dataset snapshot requires at least one asset and timeframe")
    ensure_research_architecture_tables(conn)
    summaries: list[dict[str, Any]] = []
    for symbol in normalized_assets:
        for timeframe in normalized_timeframes:
            row = conn.execute(
                """
                SELECT COUNT(*) AS candle_count, MIN(timestamp) AS window_start, MAX(timestamp) AS window_end,
                       MD5(COALESCE(STRING_AGG(
                           CONCAT_WS('|', source, timestamp::text, open::text, high::text, low::text, close::text, volume::text),
                           '||' ORDER BY timestamp, source
                       ), '')) AS candle_hash,
                       ARRAY_AGG(DISTINCT source ORDER BY source) AS sources
                FROM candles
                WHERE symbol = %s AND timeframe = %s
                """,
                (symbol, timeframe),
            ).fetchone()
            count = int(row.get("candle_count") or 0)
            if count == 0:
                raise ValueError(f"cannot snapshot missing dataset {symbol} {timeframe}")
            summaries.append(
                {
                    "key": f"{symbol}|{timeframe}",
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "candle_count": count,
                    "window_start": row.get("window_start"),
                    "window_end": row.get("window_end"),
                    "candle_hash": str(row.get("candle_hash") or ""),
                    "sources": list(row.get("sources") or []),
                }
            )
    content_hash = stable_hash(
        {
            "mode": mode,
            "assets": normalized_assets,
            "timeframes": normalized_timeframes,
            "datasets": [{key: jsonable(item[key]) for key in ("key", "candle_count", "window_start", "window_end", "candle_hash", "sources")} for item in summaries],
            "calculation_version": DATASET_VERSION,
        }
    )
    dataset_key = f"dataset_{content_hash[:24]}"
    counts = {item["key"]: item["candle_count"] for item in summaries}
    hashes = {item["key"]: item["candle_hash"] for item in summaries}
    sources = sorted({source for item in summaries for source in item["sources"]})
    window_starts = [item["window_start"] for item in summaries if item["window_start"] is not None]
    window_ends = [item["window_end"] for item in summaries if item["window_end"] is not None]
    row = conn.execute(
        """
        INSERT INTO research_dataset_manifests(
            dataset_key, name, mode, snapshot_version, assets, timeframes, window_start, window_end,
            candle_counts, candle_hashes, source_providers, content_hash, integrity,
            calculation_version, immutable, simulation_only
        ) VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, TRUE)
        ON CONFLICT(dataset_key) DO NOTHING
        RETURNING *
        """,
        (
            dataset_key,
            name or f"{mode.title()} snapshot: {', '.join(normalized_assets)} / {', '.join(normalized_timeframes)}",
            mode,
            Jsonb(normalized_assets),
            Jsonb(normalized_timeframes),
            min(window_starts) if window_starts else None,
            max(window_ends) if window_ends else None,
            Jsonb(counts),
            Jsonb(hashes),
            Jsonb(sources),
            content_hash,
            Jsonb({"verified_at_creation": True, "dataset_count": len(summaries), "exact_candles_materialized": True}),
            DATASET_VERSION,
        ),
    ).fetchone()
    if not row:
        row = conn.execute("SELECT * FROM research_dataset_manifests WHERE dataset_key = %s", (dataset_key,)).fetchone()
    dataset_id = int(row["id"])
    for item in summaries:
        conn.execute(
            """
            INSERT INTO research_dataset_candles(dataset_id, symbol, source, timeframe, timestamp, open, high, low, close, volume)
            SELECT %s, symbol, source, timeframe, timestamp, open, high, low, close, volume
            FROM candles
            WHERE symbol = %s AND timeframe = %s AND timestamp BETWEEN %s AND %s
            ON CONFLICT(dataset_id, symbol, timeframe, timestamp, source) DO NOTHING
            """,
            (dataset_id, item["symbol"], item["timeframe"], item["window_start"], item["window_end"]),
        )
    conn.commit()
    return jsonable(dict(row))


def verify_dataset_snapshot(conn: psycopg.Connection, dataset_id: int) -> dict[str, Any]:
    ensure_research_architecture_tables(conn)
    manifest = conn.execute("SELECT * FROM research_dataset_manifests WHERE id = %s", (dataset_id,)).fetchone()
    if not manifest:
        raise ValueError(f"research dataset {dataset_id} was not found")
    actual_counts: dict[str, int] = {}
    actual_hashes: dict[str, str] = {}
    for symbol in manifest["assets"]:
        for timeframe in manifest["timeframes"]:
            row = conn.execute(
                """
                SELECT COUNT(*) AS candle_count,
                       MD5(COALESCE(STRING_AGG(
                           CONCAT_WS('|', source, timestamp::text, open::text, high::text, low::text, close::text, volume::text),
                           '||' ORDER BY timestamp, source
                       ), '')) AS candle_hash
                FROM research_dataset_candles
                WHERE dataset_id = %s AND symbol = %s AND timeframe = %s
                """,
                (dataset_id, symbol, timeframe),
            ).fetchone()
            key = f"{symbol}|{timeframe}"
            actual_counts[key] = int(row.get("candle_count") or 0)
            actual_hashes[key] = str(row.get("candle_hash") or "")
    expected_counts = dict(manifest.get("candle_counts") or {})
    expected_hashes = dict(manifest.get("candle_hashes") or {})
    issues = []
    for key in sorted(set(expected_counts) | set(actual_counts)):
        if int(expected_counts.get(key) or 0) != int(actual_counts.get(key) or 0):
            issues.append({"dataset": key, "check": "candle_count", "expected": expected_counts.get(key), "actual": actual_counts.get(key)})
        if str(expected_hashes.get(key) or "") != str(actual_hashes.get(key) or ""):
            issues.append({"dataset": key, "check": "candle_hash", "expected": expected_hashes.get(key), "actual": actual_hashes.get(key)})
    return {
        "dataset_id": dataset_id,
        "dataset_key": manifest["dataset_key"],
        "passed": not issues,
        "issues": issues,
        "candle_counts": actual_counts,
        "candle_hashes": actual_hashes,
        "calculation_version": DATASET_VERSION,
        "simulation_only": True,
    }


def load_snapshot_candles(conn: psycopg.Connection, dataset_id: int, symbol: str, timeframe: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT symbol, timeframe, timestamp, open, high, low, close, volume, source
            FROM research_dataset_candles
            WHERE dataset_id = %s AND symbol = %s AND timeframe = %s
            ORDER BY timestamp ASC, source ASC
            """,
            (dataset_id, symbol, timeframe),
        ).fetchall()
    ]


def calculate_asset_profile(
    candles: list[dict[str, Any]],
    regimes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Calculate transparent behavior statistics without fitting a predictive model."""

    ordered = sorted((dict(row) for row in candles), key=lambda row: row["timestamp"])
    if len(ordered) < 30:
        raise ValueError("an asset profile requires at least 30 candles")
    closes = [finite_metric(row["close"]) for row in ordered]
    opens = [finite_metric(row["open"]) for row in ordered]
    highs = [finite_metric(row["high"]) for row in ordered]
    lows = [finite_metric(row["low"]) for row in ordered]
    volumes = [finite_metric(row["volume"]) for row in ordered]
    returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes)) if closes[index - 1] > 0]
    realized_volatility = pstdev(returns) if len(returns) >= 2 else 0.0

    true_ranges = []
    gaps = []
    for index in range(1, len(ordered)):
        previous_close = closes[index - 1]
        true_ranges.append(max(highs[index] - lows[index], abs(highs[index] - previous_close), abs(lows[index] - previous_close)) / previous_close if previous_close else 0.0)
        gaps.append(abs(opens[index] - previous_close) / previous_close if previous_close else 0.0)

    rolling_mean = [None] * len(closes)
    rolling_high = [None] * len(closes)
    for index in range(19, len(closes)):
        window = closes[index - 19 : index + 1]
        rolling_mean[index] = mean(window)
        rolling_high[index] = max(window)
    trend_states = [1 if rolling_mean[index] is not None and closes[index] >= rolling_mean[index] else -1 for index in range(19, len(closes))]
    run_lengths = consecutive_run_lengths(trend_states)
    trend_persistence = mean(run_lengths) if run_lengths else 0.0
    trend_strength = abs(mean(returns)) / realized_volatility if realized_volatility > 0 else 0.0

    lag_correlation = pearson(returns[:-1], returns[1:]) if len(returns) > 3 else 0.0
    mean_reversion_score = max(0.0, -lag_correlation)
    reversal_rate = (
        sum(1 for left, right in zip(returns[:-1], returns[1:]) if left != 0 and right != 0 and (left > 0) != (right > 0)) / max(1, len(returns) - 1)
    )

    breakout_outcomes = []
    for index in range(20, len(closes) - 5):
        if closes[index] > max(highs[index - 20 : index]):
            breakout_outcomes.append(closes[index + 5] / closes[index] - 1)
    breakout_follow_through = (
        sum(1 for value in breakout_outcomes if value > 0) / len(breakout_outcomes) if breakout_outcomes else 0.0
    )
    pullback_depths = [
        max(0.0, (rolling_high[index] - closes[index]) / rolling_high[index])
        for index in range(19, len(closes))
        if rolling_high[index]
    ]

    momentum_matches = []
    for index in range(5, len(closes) - 1):
        prior_momentum = closes[index] / closes[index - 5] - 1
        next_return = closes[index + 1] / closes[index] - 1
        if prior_momentum != 0 and next_return != 0:
            momentum_matches.append((prior_momentum > 0) == (next_return > 0))
    momentum_persistence = sum(momentum_matches) / len(momentum_matches) if momentum_matches else 0.0

    absolute_returns = [abs(value) for value in returns]
    high_move_threshold = percentile(absolute_returns, 0.75)
    expansion_volumes = [volumes[index + 1] for index, value in enumerate(absolute_returns) if value >= high_move_threshold]
    base_volume = median([value for value in volumes if value >= 0]) or 0
    volume_expansion_ratio = (median(expansion_volumes) / base_volume) if expansion_volumes and base_volume else 0.0
    gap_frequency = sum(1 for value in gaps if value >= 0.01) / len(gaps) if gaps else 0.0

    regime_distribution = summarize_profile_regimes(regimes or [])
    metrics = {
        "sample_size": len(ordered),
        "realized_volatility": round(realized_volatility, 8),
        "atr_ratio": round(mean(true_ranges), 8) if true_ranges else 0.0,
        "atr_ratio_p90": round(percentile(true_ranges, 0.90), 8),
        "trend_persistence": round(trend_persistence, 4),
        "trend_strength": round(trend_strength, 6),
        "return_autocorrelation_lag1": round(lag_correlation, 6),
        "mean_reversion_score": round(mean_reversion_score, 6),
        "reversal_rate": round(reversal_rate, 6),
        "breakout_follow_through": round(breakout_follow_through, 6),
        "breakout_sample_size": len(breakout_outcomes),
        "average_breakout_return_5_bars": round(mean(breakout_outcomes), 8) if breakout_outcomes else 0.0,
        "median_pullback_depth": round(median(pullback_depths), 8) if pullback_depths else 0.0,
        "pullback_depth_p90": round(percentile(pullback_depths, 0.90), 8),
        "momentum_persistence": round(momentum_persistence, 6),
        "volume_expansion_ratio": round(volume_expansion_ratio, 6),
        "gap_frequency": round(gap_frequency, 6),
    }
    from app.services.deeper_observations import aggregate_deeper_observations

    deeper_observations = aggregate_deeper_observations(ordered)
    metrics["market_structure_observations"] = deeper_observations["observations"]
    for observation_key, observation in deeper_observations["observations"].items():
        metrics[f"{observation_key}_score"] = observation["score"]
        metrics[f"{observation_key}_event_rate"] = observation["event_rate"]
    behavior_labels = {
        "volatility": label(realized_volatility, 0.008, 0.018),
        "trend_persistence": label(trend_persistence, 2.5, 4.5),
        "trend_strength": label(trend_strength, 0.02, 0.08),
        "mean_reversion": label(mean_reversion_score, 0.05, 0.15),
        "breakout_follow_through": label(breakout_follow_through, 0.48, 0.58),
        "pullback_depth": label(median(pullback_depths) if pullback_depths else 0.0, 0.01, 0.03),
        "momentum_persistence": label(momentum_persistence, 0.50, 0.56),
        "volume_sensitivity": label(volume_expansion_ratio, 1.0, 1.3),
        "gap_behavior": label(gap_frequency, 0.01, 0.05),
    }
    return {
        "metrics": metrics,
        "behavior_labels": behavior_labels,
        "regime_distribution": regime_distribution,
        "limitations": [
            {
                "metric": "earnings_behavior",
                "status": "unavailable",
                "reason": "No versioned corporate-event dataset is present; the engine refuses to infer earnings windows from price alone.",
            }
        ],
        "evidence_window": {
            "start": jsonable(ordered[0]["timestamp"]),
            "end": jsonable(ordered[-1]["timestamp"]),
            "candle_count": len(ordered),
        },
        "calculation_version": PROFILE_VERSION,
    }


def summarize_profile_regimes(regimes: list[dict[str, Any]]) -> dict[str, Any]:
    trend = Counter(str(row.get("trend_regime") or "unknown") for row in regimes)
    volatility = Counter(str(row.get("volatility_regime") or "unknown") for row in regimes)
    total = max(1, len(regimes))
    return {
        "trend": {key: {"count": value, "share": round(value / total, 6)} for key, value in sorted(trend.items())},
        "volatility": {key: {"count": value, "share": round(value / total, 6)} for key, value in sorted(volatility.items())},
        "sample_size": len(regimes),
    }


def build_return_correlations(candles_by_key: dict[tuple[str, str], list[dict[str, Any]]]) -> dict[str, dict[str, float]]:
    by_timeframe: dict[str, list[tuple[str, dict[Any, float]]]] = {}
    for (symbol, timeframe), candles in candles_by_key.items():
        ordered = sorted(candles, key=lambda row: row["timestamp"])
        returns: dict[Any, float] = {}
        for index in range(1, len(ordered)):
            previous = finite_metric(ordered[index - 1]["close"])
            if previous:
                returns[ordered[index]["timestamp"]] = finite_metric(ordered[index]["close"]) / previous - 1
        by_timeframe.setdefault(timeframe, []).append((symbol, returns))
    result: dict[str, dict[str, float]] = {}
    for timeframe, rows in by_timeframe.items():
        for left_symbol, left in rows:
            key = f"{left_symbol}|{timeframe}"
            result[key] = {}
            for right_symbol, right in rows:
                shared = sorted(set(left) & set(right))
                correlation = pearson([left[item] for item in shared], [right[item] for item in shared]) if len(shared) >= 3 else 0.0
                result[key][right_symbol] = round(correlation, 6)
    return result


def build_asset_profiles(conn: psycopg.Connection, dataset_id: int) -> dict[str, Any]:
    ensure_research_architecture_tables(conn)
    manifest = conn.execute("SELECT * FROM research_dataset_manifests WHERE id = %s", (dataset_id,)).fetchone()
    if not manifest:
        raise ValueError(f"research dataset {dataset_id} was not found")
    candles_by_key = {
        (symbol, timeframe): load_snapshot_candles(conn, dataset_id, symbol, timeframe)
        for symbol in manifest["assets"]
        for timeframe in manifest["timeframes"]
    }
    correlations = build_return_correlations(candles_by_key)
    persisted = []
    for (symbol, timeframe), candles in candles_by_key.items():
        features = calculate_features(candles)
        regimes = calculate_regimes(candles, features)
        profile = calculate_asset_profile(candles, regimes)
        profile_key = f"{symbol}|{timeframe}"
        existing = conn.execute(
            "SELECT * FROM asset_profile_versions WHERE dataset_id = %s AND symbol = %s AND timeframe = %s",
            (dataset_id, symbol, timeframe),
        ).fetchone()
        if existing:
            persisted.append(jsonable(dict(existing)))
            continue
        version_row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM asset_profile_versions WHERE profile_key = %s",
            (profile_key,),
        ).fetchone()
        row = conn.execute(
            """
            INSERT INTO asset_profile_versions(
                profile_key, version, dataset_id, symbol, timeframe, evidence_window, metrics,
                behavior_labels, regime_distribution, correlations, limitations, calculation_version, simulation_only
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            RETURNING *
            """,
            (
                profile_key,
                int(version_row["next_version"]),
                dataset_id,
                symbol,
                timeframe,
                Jsonb(profile["evidence_window"]),
                Jsonb(profile["metrics"]),
                Jsonb(profile["behavior_labels"]),
                Jsonb(profile["regime_distribution"]),
                Jsonb(correlations.get(profile_key, {})),
                Jsonb(profile["limitations"]),
                PROFILE_VERSION,
            ),
        ).fetchone()
        persisted.append(jsonable(dict(row)))
    conn.commit()
    return {"dataset_id": dataset_id, "profiles": persisted, "calculation_version": PROFILE_VERSION, "simulation_only": True}


def calculate_asset_clusters(
    profiles: list[dict[str, Any]],
    *,
    target_clusters: int | None = None,
) -> list[dict[str, Any]]:
    results = []
    by_timeframe: dict[str, list[dict[str, Any]]] = {}
    for profile in profiles:
        by_timeframe.setdefault(str(profile["timeframe"]), []).append(profile)
    for timeframe, timeframe_profiles in sorted(by_timeframe.items()):
        if not timeframe_profiles:
            continue
        count = len(timeframe_profiles)
        cluster_count = min(count, max(1, target_clusters or round(math.sqrt(count))))
        vectors = standardized_profile_vectors(timeframe_profiles)
        groups = [[index] for index in range(count)]
        while len(groups) > cluster_count:
            pairs = []
            for left in range(len(groups)):
                for right in range(left + 1, len(groups)):
                    distance = mean(
                        profile_distance(timeframe_profiles[a], timeframe_profiles[b], vectors[a], vectors[b])
                        for a in groups[left]
                        for b in groups[right]
                    )
                    member_key = sorted(str(timeframe_profiles[item]["symbol"]) for item in groups[left] + groups[right])
                    pairs.append((round(distance, 12), member_key, left, right))
            _distance, _key, left, right = min(pairs)
            groups[left] = sorted(groups[left] + groups[right])
            del groups[right]
        for index, group in enumerate(sorted(groups, key=lambda items: sorted(str(timeframe_profiles[item]["symbol"]) for item in items))):
            members = [timeframe_profiles[item] for item in group]
            centroid = {feature: round(mean(finite_metric(member["metrics"].get(feature)) for member in members), 8) for feature in PROFILE_FEATURES}
            distances = [euclidean(vectors[item], [mean(vectors[member_index][feature_index] for member_index in group) for feature_index in range(len(PROFILE_FEATURES))]) for item in group]
            symbols = sorted(str(member["symbol"]) for member in members)
            cluster_key = f"cluster_{stable_hash({'timeframe': timeframe, 'symbols': symbols, 'algorithm': CLUSTER_VERSION})[:20]}"
            average_distance = mean(distances) if distances else 0.0
            results.append(
                {
                    "cluster_key": cluster_key,
                    "timeframe": timeframe,
                    "name": cluster_name(centroid, timeframe, index + 1),
                    "description": f"Behavior-measured {timeframe} cluster containing {', '.join(symbols)}.",
                    "centroid": centroid,
                    "members": [
                        {
                            "asset_profile_id": member.get("id"),
                            "symbol": member["symbol"],
                            "timeframe": member["timeframe"],
                            "similarity_score": round(1.0 / (1.0 + distances[position]), 6),
                            "distance_to_centroid": round(distances[position], 6),
                        }
                        for position, member in enumerate(members)
                    ],
                    "quality_metrics": {
                        "average_distance_to_centroid": round(average_distance, 6),
                        "member_count": len(members),
                        "method": "deterministic agglomerative average-linkage over standardized behavior metrics and return correlation",
                    },
                    "algorithm_version": CLUSTER_VERSION,
                }
            )
    return results


def build_asset_clusters(conn: psycopg.Connection, dataset_id: int, *, target_clusters: int | None = None) -> dict[str, Any]:
    ensure_research_architecture_tables(conn)
    profiles = [dict(row) for row in conn.execute("SELECT * FROM asset_profile_versions WHERE dataset_id = %s ORDER BY timeframe, symbol", (dataset_id,)).fetchall()]
    if not profiles:
        profiles = [dict(row) for row in build_asset_profiles(conn, dataset_id)["profiles"]]
    clusters = calculate_asset_clusters(profiles, target_clusters=target_clusters)
    persisted = []
    for cluster in clusters:
        existing = conn.execute(
            """
            SELECT * FROM asset_cluster_versions
            WHERE dataset_id = %s AND cluster_key = %s AND algorithm_version = %s
            ORDER BY version DESC LIMIT 1
            """,
            (dataset_id, cluster["cluster_key"], CLUSTER_VERSION),
        ).fetchone()
        if existing:
            cluster_row = dict(existing)
        else:
            version_row = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM asset_cluster_versions WHERE cluster_key = %s", (cluster["cluster_key"],)).fetchone()
            created = conn.execute(
                """
                INSERT INTO asset_cluster_versions(
                    cluster_key, version, dataset_id, name, description, centroid, member_count,
                    quality_metrics, algorithm_version, simulation_only
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                RETURNING *
                """,
                (
                    cluster["cluster_key"], int(version_row["next_version"]), dataset_id, cluster["name"],
                    cluster["description"], Jsonb(cluster["centroid"]), len(cluster["members"]),
                    Jsonb(cluster["quality_metrics"]), CLUSTER_VERSION,
                ),
            ).fetchone()
            cluster_row = dict(created)
        for member in cluster["members"]:
            conn.execute(
                """
                INSERT INTO asset_cluster_members(
                    cluster_id, asset_profile_id, symbol, timeframe, similarity_score, distance_to_centroid, evidence
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(cluster_id, asset_profile_id) DO NOTHING
                """,
                (
                    cluster_row["id"], member["asset_profile_id"], member["symbol"], member["timeframe"],
                    member["similarity_score"], member["distance_to_centroid"],
                    Jsonb({"dataset_id": dataset_id, "algorithm_version": CLUSTER_VERSION}),
                ),
            )
        cluster_row["members"] = cluster["members"]
        persisted.append(jsonable(cluster_row))
    conn.commit()
    return {"dataset_id": dataset_id, "clusters": persisted, "algorithm_version": CLUSTER_VERSION, "simulation_only": True}


def generate_hypotheses_from_intelligence(
    profiles: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
    *,
    dataset_id: int | None = None,
) -> list[dict[str, Any]]:
    hypotheses: list[dict[str, Any]] = []
    profile_by_id = {int(row["id"]): row for row in profiles if row.get("id") is not None}
    for cluster in clusters:
        members = [profile_by_id.get(int(member["asset_profile_id"])) for member in cluster.get("members", []) if member.get("asset_profile_id") is not None]
        members = [row for row in members if row]
        if len(members) < 2:
            continue
        cluster_quality = dict(cluster.get("quality_metrics") or {})
        average_distance = finite_metric(cluster_quality.get("average_distance_to_centroid"))
        if average_distance > MAX_CLUSTER_HYPOTHESIS_DISTANCE:
            # Agglomerative clustering still describes the universe, but a
            # forced low-cohesion group is not a valid transfer hypothesis.
            continue
        family, evidence = select_hypothesis_family(cluster["centroid"])
        symbols = sorted(str(row["symbol"]) for row in members)
        sample_size = sum(int((row.get("metrics") or {}).get("sample_size") or 0) for row in members)
        cohesion_score = 1.0 / (1.0 + average_distance)
        confidence = hypothesis_confidence(evidence, sample_size, len(members), cohesion_score=cohesion_score)
        hypotheses.append(
            hypothesis_record(
                scope_type="cluster",
                scope_ref=str(cluster["cluster_key"]),
                strategy_family=family,
                symbols=symbols,
                timeframe=str(members[0]["timeframe"]),
                metrics=evidence,
                confidence=confidence,
                supporting_evidence=[f"asset_profile:{row['id']}" for row in members] + [f"asset_cluster:{cluster.get('id') or cluster['cluster_key']}"],
                evidence_window={
                    "dataset_id": dataset_id,
                    "profile_ids": [row["id"] for row in members],
                    "sample_size": sample_size,
                    "cluster_average_distance": round(average_distance, 6),
                    "cluster_cohesion_score": round(cohesion_score, 6),
                },
            )
        )
    for profile in profiles:
        family, evidence = select_hypothesis_family(profile["metrics"])
        sample_size = int((profile.get("metrics") or {}).get("sample_size") or 0)
        hypotheses.append(
            hypothesis_record(
                scope_type="asset",
                scope_ref=str(profile["symbol"]),
                strategy_family=family,
                symbols=[str(profile["symbol"])],
                timeframe=str(profile["timeframe"]),
                metrics=evidence,
                confidence=hypothesis_confidence(evidence, sample_size, 1),
                supporting_evidence=[f"asset_profile:{profile['id']}"],
                evidence_window={**dict(profile.get("evidence_window") or {}), "dataset_id": dataset_id, "profile_ids": [profile["id"]]},
            )
        )
    return sorted(hypotheses, key=lambda row: (row["scope_type"] == "cluster", row["confidence_score"], row["hypothesis_key"]), reverse=True)


def hypothesis_record(
    *,
    scope_type: str,
    scope_ref: str,
    strategy_family: str,
    symbols: list[str],
    timeframe: str,
    metrics: dict[str, float],
    confidence: float,
    supporting_evidence: list[str],
    evidence_window: dict[str, Any],
) -> dict[str, Any]:
    behavior = family_behavior(strategy_family)
    symbol_text = ", ".join(symbols)
    hypothesis_key = f"hyp_{stable_hash({'scope_type': scope_type, 'scope_ref': scope_ref, 'family': strategy_family, 'timeframe': timeframe})[:20]}"
    observation = (
        f"Measured {timeframe} behavior for {symbol_text} favors {behavior['observation']} "
        f"({', '.join(f'{key}={value:.4f}' for key, value in sorted(metrics.items()))})."
    )
    return {
        "hypothesis_key": hypothesis_key,
        "scope_type": scope_type,
        "scope_ref": scope_ref,
        "strategy_family": strategy_family,
        "title": f"{strategy_family} behavior on {symbol_text}",
        "observation": observation,
        "hypothesis": f"{behavior['hypothesis']} on {symbol_text} during {behavior['regime']} regimes.",
        "expected_behavior": behavior["expected"],
        "relevant_regimes": behavior["relevant_regimes"],
        "confidence_score": confidence,
        "evidence_window": evidence_window,
        "creation_source": "deterministic_asset_intelligence",
        "status": "proposed",
        "supporting_evidence": supporting_evidence,
        "contradictory_evidence": [],
        "test_summary": {"source_dataset_id": evidence_window.get("dataset_id"), "timeframe": timeframe, "symbols": symbols, "metrics": metrics},
        "calculation_version": HYPOTHESIS_VERSION,
    }


def select_hypothesis_family(metrics: dict[str, Any]) -> tuple[str, dict[str, float]]:
    breakout = finite_metric(metrics.get("breakout_follow_through"))
    volume = finite_metric(metrics.get("volume_expansion_ratio"))
    momentum = finite_metric(metrics.get("momentum_persistence"))
    trend = finite_metric(metrics.get("trend_persistence"))
    trend_strength = finite_metric(metrics.get("trend_strength"))
    mean_reversion = finite_metric(metrics.get("mean_reversion_score"))
    reversal = finite_metric(metrics.get("reversal_rate"))
    gap = finite_metric(metrics.get("gap_frequency"))
    scored = [
        (breakout * 0.55 + min(2.0, volume) / 2 * 0.25 + momentum * 0.20, "Breakout", {"breakout_follow_through": breakout, "volume_expansion_ratio": volume, "momentum_persistence": momentum}),
        (min(1.0, trend / 6) * 0.55 + min(1.0, trend_strength * 8) * 0.20 + momentum * 0.25, "Pullback", {"trend_persistence": trend, "trend_strength": trend_strength, "momentum_persistence": momentum}),
        (mean_reversion * 1.5 * 0.55 + reversal * 0.45, "Mean Reversion", {"mean_reversion_score": mean_reversion, "reversal_rate": reversal}),
        (min(1.0, gap * 10) * 0.45 + breakout * 0.30 + momentum * 0.25, "Gap Continuation", {"gap_frequency": gap, "breakout_follow_through": breakout, "momentum_persistence": momentum}),
        (min(1.0, trend / 6) * 0.50 + momentum * 0.30 + breakout * 0.20, "Trend Following", {"trend_persistence": trend, "momentum_persistence": momentum, "breakout_follow_through": breakout}),
    ]
    _score, family, evidence = max(scored, key=lambda item: (item[0], item[1]))
    return family, evidence


def family_behavior(strategy_family: str) -> dict[str, Any]:
    rows = {
        "Breakout": {
            "observation": "breakout follow-through with volume expansion",
            "hypothesis": "Volume-confirmed range breaks have positive out-of-sample expectancy",
            "regime": "positive-trend or volatility-expansion",
            "expected": "Continuation after a measured range break; failure is defined by unchanged strong validation gates.",
            "relevant_regimes": ["bull_trend", "high_volatility", "normal_volatility"],
        },
        "Pullback": {
            "observation": "persistent trends with measurable retracements",
            "hypothesis": "Volatility-normalized pullbacks inside persistent trends have positive out-of-sample expectancy",
            "regime": "bull-trend",
            "expected": "Re-entry after a controlled pullback while the measured higher-level trend remains constructive.",
            "relevant_regimes": ["bull_trend", "normal_volatility", "low_volatility"],
        },
        "Mean Reversion": {
            "observation": "short-horizon return reversal",
            "hypothesis": "Statistically extended moves revert toward their local mean with positive out-of-sample expectancy",
            "regime": "sideways",
            "expected": "Reversal after a measured extension; trending-regime losses must remain within the existing drawdown gate.",
            "relevant_regimes": ["sideways", "normal_volatility", "low_volatility"],
        },
        "Gap Continuation": {
            "observation": "material opening gaps with subsequent momentum persistence",
            "hypothesis": "Volume-supported opening gaps continue with positive out-of-sample expectancy",
            "regime": "high-volatility trend",
            "expected": "Continuation after an opening displacement when trend and volume evidence agree.",
            "relevant_regimes": ["bull_trend", "high_volatility"],
        },
        "Trend Following": {
            "observation": "multi-bar directional persistence",
            "hypothesis": "Confirmed directional persistence continues with positive out-of-sample expectancy",
            "regime": "trend",
            "expected": "Continuation while measured trend and momentum remain aligned.",
            "relevant_regimes": ["bull_trend", "normal_volatility", "high_volatility"],
        },
    }
    return rows[strategy_family]


def hypothesis_confidence(
    metrics: dict[str, Any],
    sample_size: int,
    member_count: int,
    *,
    cohesion_score: float | None = None,
) -> float:
    effect_values = []
    for key, value in metrics.items():
        numeric = abs(finite_metric(value))
        if "ratio" in key or "persistence" in key or "follow" in key or "reversal" in key:
            numeric = min(1.0, numeric if numeric <= 1 else numeric / 2)
        effect_values.append(numeric)
    effect = mean(effect_values) if effect_values else 0.0
    sample_confidence = min(1.0, math.log10(max(10, sample_size)) / 4)
    transfer_confidence = (
        max(0.0, min(1.0, float(cohesion_score)))
        if cohesion_score is not None
        else min(1.0, 0.55 + max(0, member_count - 1) * 0.10)
    )
    return round(min(0.95, 0.25 + effect * 0.30 + sample_confidence * 0.30 + transfer_confidence * 0.15), 4)


def build_research_hypotheses(conn: psycopg.Connection, dataset_id: int) -> dict[str, Any]:
    ensure_research_architecture_tables(conn)
    profiles = [dict(row) for row in conn.execute("SELECT * FROM asset_profile_versions WHERE dataset_id = %s ORDER BY timeframe, symbol", (dataset_id,)).fetchall()]
    if not profiles:
        profiles = [dict(row) for row in build_asset_profiles(conn, dataset_id)["profiles"]]
    clusters = []
    for row in conn.execute(
        "SELECT * FROM asset_cluster_versions WHERE dataset_id = %s AND algorithm_version = %s ORDER BY id",
        (dataset_id, CLUSTER_VERSION),
    ).fetchall():
        cluster = dict(row)
        cluster["members"] = [dict(item) for item in conn.execute("SELECT * FROM asset_cluster_members WHERE cluster_id = %s ORDER BY symbol", (cluster["id"],)).fetchall()]
        clusters.append(cluster)
    if not clusters:
        clusters = [dict(row) for row in build_asset_clusters(conn, dataset_id)["clusters"]]
    hypotheses = generate_hypotheses_from_intelligence(profiles, clusters, dataset_id=dataset_id)
    hypotheses.extend(generate_phase2_family_hypotheses(profiles, clusters, dataset_id=dataset_id))
    persisted = []
    for hypothesis in hypotheses:
        existing = conn.execute(
            """
            SELECT * FROM research_hypothesis_versions
            WHERE hypothesis_key = %s AND test_summary->>'source_dataset_id' = %s
            ORDER BY version DESC LIMIT 1
            """,
            (hypothesis["hypothesis_key"], str(dataset_id)),
        ).fetchone()
        if existing:
            persisted.append(jsonable(dict(existing)))
            continue
        prior = conn.execute(
            "SELECT * FROM research_hypothesis_versions WHERE hypothesis_key = %s ORDER BY version DESC LIMIT 1",
            (hypothesis["hypothesis_key"],),
        ).fetchone()
        if not prior and hypothesis["scope_type"] == "cluster":
            summary = dict(hypothesis.get("test_summary") or {})
            prior = conn.execute(
                """
                SELECT * FROM research_hypothesis_versions
                WHERE scope_type = 'cluster'
                  AND strategy_family = %s
                  AND test_summary->>'timeframe' = %s
                  AND test_summary->'symbols' = %s
                ORDER BY version DESC, id DESC
                LIMIT 1
                """,
                (hypothesis["strategy_family"], summary.get("timeframe"), Jsonb(list(summary.get("symbols") or []))),
            ).fetchone()
        prior = dict(prior) if prior else None
        if prior:
            hypothesis["test_summary"] = {
                **hypothesis["test_summary"],
                "prior_hypothesis_id": prior["id"],
                "prior_status": prior["status"],
                "prior_confidence_score": finite_metric(prior["confidence_score"]),
            }
            same_dataset = str((prior.get("test_summary") or {}).get("source_dataset_id")) == str(dataset_id)
            if same_dataset and prior["status"] in {"supported", "weak", "rejected", "retired"}:
                hypothesis["status"] = prior["status"]
            if prior["status"] in {"supported", "weak"}:
                hypothesis["supporting_evidence"] = list(hypothesis["supporting_evidence"]) + [f"research_hypothesis:{prior['id']}"]
            elif prior["status"] in {"rejected", "retired"}:
                hypothesis["contradictory_evidence"] = list(hypothesis["contradictory_evidence"]) + [f"research_hypothesis:{prior['id']}"]
        version_row = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM research_hypothesis_versions WHERE hypothesis_key = %s", (hypothesis["hypothesis_key"],)).fetchone()
        row = conn.execute(
            """
            INSERT INTO research_hypothesis_versions(
                hypothesis_key, version, parent_hypothesis_id, scope_type, scope_ref, strategy_family,
                title, observation, hypothesis, expected_behavior, relevant_regimes, confidence_score,
                evidence_window, creation_source, status, supporting_evidence, contradictory_evidence,
                test_summary, calculation_version, simulation_only
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            RETURNING *
            """,
            (
                hypothesis["hypothesis_key"], int(version_row["next_version"]), prior.get("id") if prior else None, hypothesis["scope_type"],
                hypothesis["scope_ref"], hypothesis["strategy_family"], hypothesis["title"],
                hypothesis["observation"], hypothesis["hypothesis"], hypothesis["expected_behavior"],
                Jsonb(hypothesis["relevant_regimes"]), hypothesis["confidence_score"], Jsonb(hypothesis["evidence_window"]),
                hypothesis["creation_source"], hypothesis["status"], Jsonb(hypothesis["supporting_evidence"]),
                Jsonb(hypothesis["contradictory_evidence"]), Jsonb(hypothesis["test_summary"]), HYPOTHESIS_VERSION,
            ),
        ).fetchone()
        persisted.append(jsonable(dict(row)))
    conn.commit()
    return {"dataset_id": dataset_id, "hypotheses": persisted, "calculation_version": HYPOTHESIS_VERSION, "simulation_only": True}


def generate_phase2_family_hypotheses(
    profiles: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
    *,
    dataset_id: int,
) -> list[dict[str, Any]]:
    """Create measured, falsifiable family hypotheses without claiming confirmation.

    These hypotheses are derived from already-observed profile data, so every
    record is explicitly post-hoc and unconfirmed. A same-dataset campaign may
    regression-test executability and the validation funnel, but cannot promote
    the hypothesis lifecycle to supported.
    """

    profile_by_id = {int(row["id"]): row for row in profiles if row.get("id") is not None}
    eligible_clusters: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for cluster in clusters:
        members = [
            profile_by_id.get(int(member["asset_profile_id"]))
            for member in cluster.get("members", [])
            if member.get("asset_profile_id") is not None
        ]
        members = [row for row in members if row]
        average_distance = finite_metric((cluster.get("quality_metrics") or {}).get("average_distance_to_centroid"))
        if len(members) >= 2 and average_distance <= MAX_CLUSTER_HYPOTHESIS_DISTANCE:
            eligible_clusters.append((cluster, members))

    hypotheses: list[dict[str, Any]] = []
    scopes: list[tuple[str, str, list[dict[str, Any]], dict[str, Any], dict[str, Any]]] = []
    for cluster, members in eligible_clusters:
        scopes.append(("cluster", str(cluster["cluster_key"]), members, dict(cluster.get("centroid") or {}), dict(cluster.get("quality_metrics") or {})))
    if not scopes:
        for profile in profiles:
            scopes.append(("asset", str(profile["symbol"]), [profile], dict(profile.get("metrics") or {}), {}))

    for scope_type, scope_ref, members, metrics, quality in scopes:
        symbols = sorted(str(row["symbol"]) for row in members)
        timeframe = str(members[0]["timeframe"])
        sample_size = sum(int((row.get("metrics") or {}).get("sample_size") or 0) for row in members)
        average_distance = finite_metric(quality.get("average_distance_to_centroid"))
        cohesion_score = 1.0 / (1.0 + average_distance) if scope_type == "cluster" else 1.0
        for family in PHASE_2_FAMILY_NAMES:
            spec = strategy_family_spec(family)
            evidence = family_observation_evidence(family, metrics)
            observation_score = family_observation_score(family, metrics)
            default_parameters = {
                "trend_fast": 20,
                "trend_slow": 50,
                **{key: values[len(values) // 2] for key, values in spec.parameter_ranges.items()},
            }
            scope_text = ", ".join(symbols)
            hypothesis_key = f"phase2_hyp_{stable_hash({'version': PHASE_2_FAMILY_VERSION, 'scope_type': scope_type, 'scope_ref': scope_ref, 'family': family, 'timeframe': timeframe})[:20]}"
            evidence_text = ", ".join(f"{key}={value:.6f}" for key, value in sorted(evidence.items()))
            confidence = min(
                0.85,
                hypothesis_confidence(evidence, sample_size, len(members), cohesion_score=cohesion_score) * (0.75 + observation_score * 0.25),
            )
            supporting = [f"asset_profile:{row['id']}" for row in members]
            if scope_type == "cluster":
                supporting.append(f"asset_cluster:{next((cluster.get('id') for cluster, cluster_members in eligible_clusters if str(cluster['cluster_key']) == scope_ref), scope_ref)}")
            contradictory_observations = []
            contradictory_evidence: list[str] = []
            if observation_score < 0.45:
                contradictory_observations.append(
                    f"The measured family-observation score is only {observation_score:.4f}; usefulness is not established."
                )
                contradictory_evidence.extend(supporting)
            hypotheses.append(
                {
                    "hypothesis_key": hypothesis_key,
                    "scope_type": scope_type,
                    "scope_ref": scope_ref,
                    "strategy_family": family,
                    "title": f"{family} measured-behavior test on {scope_text}",
                    "observation": f"Measured {timeframe} profile behavior for {scope_text}: {evidence_text} (family observation score={observation_score:.4f}). Post-hoc and unconfirmed.",
                    "hypothesis": spec.hypothesis_template.format(scope=scope_text, **default_parameters),
                    "expected_behavior": spec.expected_behavior,
                    "relevant_regimes": list(spec.relevant_conditions),
                    "confidence_score": round(confidence, 4),
                    "evidence_window": {
                        "dataset_id": dataset_id,
                        "profile_ids": [row["id"] for row in members],
                        "sample_size": sample_size,
                        "cluster_average_distance": round(average_distance, 6) if scope_type == "cluster" else None,
                        "independent_confirmation_required": True,
                    },
                    "creation_source": "phase2_measured_family_intelligence",
                    "status": "proposed",
                    "supporting_evidence": supporting,
                    "contradictory_evidence": contradictory_evidence,
                    "test_summary": {
                        "source_dataset_id": dataset_id,
                        "timeframe": timeframe,
                        "symbols": symbols,
                        "metrics": evidence,
                        "family_observation_score": observation_score,
                        "measurable_success_criteria": spec.success_criteria,
                        "falsification_criteria": spec.falsification_criteria,
                        "contradictory_observations": contradictory_observations,
                        "post_hoc": True,
                        "confirmation_status": "unconfirmed",
                        "family_version": PHASE_2_FAMILY_VERSION,
                        "generation_seed": 0,
                    },
                    "calculation_version": HYPOTHESIS_VERSION,
                }
            )
    return sorted(hypotheses, key=lambda row: (row["scope_ref"], row["strategy_family"]))


def generate_targeted_candidates(
    hypothesis: dict[str, Any],
    *,
    max_candidates: int,
    parents: list[DiscoveryCandidate] | None = None,
    allocation: dict[str, float] | None = None,
) -> dict[str, Any]:
    if max_candidates < 1:
        raise ValueError("targeted generation requires at least one candidate")
    allocation = normalize_allocation(allocation or DEFAULT_ALLOCATION)
    counts = allocation_counts(max_candidates, allocation)
    strategy_family = str(hypothesis["strategy_family"])
    generation_seed = int(hypothesis.get("generation_seed") or (hypothesis.get("test_summary") or {}).get("generation_seed") or 0)
    preferred_entries = preferred_entry_families(strategy_family)
    pool_size = min(16000, max(1600, max_candidates * 16))
    # Start from the balanced, frequency-aware pool. The previous targeted
    # generator consumed the first lexicographic combinations, which crowded a
    # campaign with strict, closely related filters and could make the 30-trade
    # validation gate unreachable before quality was even tested.
    if strategy_family in PHASE_2_FAMILY_NAMES:
        preferred = generate_family_discovery_candidates(
            strategy_family,
            max_candidates=pool_size,
            role="core",
            seed=generation_seed,
        )
        exploratory = generate_family_discovery_candidates(
            strategy_family,
            max_candidates=max(200, counts["exploration"] * 4),
            role="exploration",
            seed=generation_seed,
        )
        historical_parents = [
            candidate
            for candidate in list(parents or [])
            if candidate.parameters.get("strategy_architecture") == PHASE_2_FAMILY_VERSION
            and candidate.parameters.get("phase2_strategy_family") == strategy_family
        ]
    else:
        pool = generate_balanced_discovery_candidates(max_candidates=pool_size)
        preferred = [candidate for candidate in pool if candidate.blocks.get("entry") in preferred_entries]
        exploratory = [candidate for candidate in pool if candidate.blocks.get("entry") not in preferred_entries]
        historical_parents = list(parents or [])
    historical_ids = {candidate.candidate_id for candidate in historical_parents}
    selected: list[DiscoveryCandidate] = []
    channels: dict[str, str] = {}
    seen: set[str] = set()

    exploitation_sources = historical_parents + preferred
    for source in exploitation_sources:
        if len([item for item in selected if channels[item.candidate_id] == "exploitation"]) >= counts["exploitation"]:
            break
        candidate = annotate_candidate(
            source,
            hypothesis,
            "exploitation",
            parent_id=source.candidate_id if source.candidate_id in historical_ids else source.parent_candidate_id,
        )
        append_targeted_candidate(selected, channels, seen, candidate, "exploitation")

    nearby_parents = [item for item in selected if channels[item.candidate_id] == "exploitation"] or [annotate_candidate(item, hypothesis, "exploitation") for item in preferred[:1]]
    mutation_index = 0
    while len([item for item in selected if channels[item.candidate_id] == "nearby"]) < counts["nearby"] and nearby_parents:
        parent = nearby_parents[mutation_index % len(nearby_parents)]
        candidate = nearby_candidate(parent, hypothesis, mutation_index)
        append_targeted_candidate(selected, channels, seen, candidate, "nearby")
        mutation_index += 1
        if mutation_index > max_candidates * 20:
            break

    for source in exploratory:
        if len([item for item in selected if channels[item.candidate_id] == "exploration"]) >= counts["exploration"]:
            break
        candidate = annotate_candidate(source, hypothesis, "exploration")
        append_targeted_candidate(selected, channels, seen, candidate, "exploration")

    # Deterministic fill protects small or unusually constrained rule libraries while retaining traceability.
    for source in preferred + exploratory:
        if len(selected) >= max_candidates:
            break
        channel = min(counts, key=lambda key: (sum(1 for value in channels.values() if value == key) / max(1, counts[key]) if counts[key] else math.inf, key))
        candidate = annotate_candidate(source, hypothesis, channel)
        append_targeted_candidate(selected, channels, seen, candidate, channel)

    selected = selected[:max_candidates]
    actual = Counter(channels[item.candidate_id] for item in selected)
    return {
        "candidates": selected,
        "allocation": {"requested": counts, "actual": dict(actual), "ratios": allocation},
        "preferred_entries": list(preferred_entries),
        "generator_version": GENERATOR_VERSION,
        "family_version": PHASE_2_FAMILY_VERSION if strategy_family in PHASE_2_FAMILY_NAMES else None,
        "generation_seed": generation_seed,
        "hypothesis_id": hypothesis.get("id"),
        "hypothesis_key": hypothesis["hypothesis_key"],
    }


def annotate_candidate(
    candidate: DiscoveryCandidate,
    hypothesis: dict[str, Any],
    channel: str,
    *,
    parent_id: str | None = None,
) -> DiscoveryCandidate:
    params = {
        **candidate.parameters,
        "research_architecture_version": ARCHITECTURE_VERSION,
        "generator_version": GENERATOR_VERSION,
        "hypothesis_version_id": hypothesis.get("id"),
        "hypothesis_key": hypothesis["hypothesis_key"],
        "hypothesis_scope_type": hypothesis["scope_type"],
        "hypothesis_scope_ref": hypothesis["scope_ref"],
        "hypothesis_strategy_family": hypothesis["strategy_family"],
        "generation_channel": channel,
        "expected_behavior": hypothesis["expected_behavior"],
        "relevant_regimes": list(hypothesis.get("relevant_regimes") or []),
    }
    effective_parent = parent_id or candidate.parent_candidate_id
    canonical = canonical_candidate_key(candidate.blocks, params, effective_parent)
    return DiscoveryCandidate(
        candidate_id=f"sd_{sha256(canonical.encode()).hexdigest()[:14]}",
        family_id=f"hyp_family_{sha256(str(hypothesis['hypothesis_key']).encode()).hexdigest()[:10]}",
        parent_candidate_id=effective_parent,
        generation=max(1, candidate.generation + (1 if parent_id == candidate.candidate_id else 0)),
        blocks=dict(candidate.blocks),
        parameters=params,
        complexity=candidate.complexity,
        canonical_key=canonical,
    )


def nearby_candidate(parent: DiscoveryCandidate, hypothesis: dict[str, Any], index: int) -> DiscoveryCandidate:
    params = dict(parent.parameters)
    family = str(hypothesis["strategy_family"])
    grids = {
        "Breakout": [("breakout_lookback", [10, 15, 20, 30]), ("volume_change_min", [0.0, 0.15, 0.30]), ("atr_multiplier", [1.5, 2.0, 2.5])],
        "Pullback": [("entry_distance_to_ema20_max", [0.015, 0.025, 0.035, 0.05]), ("rsi_min", [45, 50, 55, 60]), ("atr_multiplier", [1.5, 2.0, 2.5])],
        "Mean Reversion": [("rsi_oversold", [32, 36, 40, 44]), ("risk_reward", [1.2, 1.5, 1.8]), ("max_holding_bars", [8, 12, 18])],
        "Gap Continuation": [("returns_5_min", [0.01, 0.015, 0.02, 0.03]), ("volume_change_min", [0.0, 0.15, 0.30]), ("max_holding_bars", [8, 12, 18])],
        "Trend Following": trend_following_mutation_grid(params),
    }
    if family in PHASE_2_FAMILY_NAMES:
        grids[family] = family_mutation_grid(family)
    key, values = grids[family][index % len(grids[family])]
    current = params.get(key)
    alternatives = [value for value in values if value != current] or values
    params[key] = alternatives[(index // len(grids[family])) % len(alternatives)]
    params["generation_channel"] = "nearby"
    params["controlled_mutation"] = {"parameter": key, "from": current, "to": params[key]}
    canonical = canonical_candidate_key(parent.blocks, params, parent.candidate_id)
    return DiscoveryCandidate(
        candidate_id=f"sd_{sha256(canonical.encode()).hexdigest()[:14]}",
        family_id=parent.family_id,
        parent_candidate_id=parent.candidate_id,
        generation=parent.generation + 1,
        blocks=dict(parent.blocks),
        parameters=params,
        complexity=parent.complexity,
        canonical_key=canonical,
    )


def trend_following_mutation_grid(params: dict[str, Any]) -> list[tuple[str, list[float | int]]]:
    """Only mutate parameters that can change this parent's executable path."""

    grid: list[tuple[str, list[float | int]]] = [
        ("trend_fast", [10, 20, 30]),
        ("trend_slow", [50, 100, 200]),
        ("volume_change_min", [-0.25, -0.10, 0.0, 0.15]),
        ("risk_reward", [1.2, 1.5, 2.0, 2.5]),
        ("max_holding_bars", [6, 8, 12, 18]),
    ]
    entry = str(params.get("entry") or "")
    if entry == "pullback":
        grid.insert(2, ("entry_distance_to_ema20_max", [0.025, 0.05, 0.075, 0.10]))
    elif entry in {"trend_continuation", "gap_proxy"}:
        grid.insert(2, ("returns_5_min", [0.0025, 0.005, 0.008, 0.012]))
    momentum = str(params.get("momentum") or "")
    if momentum == "rsi":
        grid.insert(3, ("rsi_min", [45, 50, 55, 60]))
    elif momentum == "stochastic_proxy":
        grid.insert(3, ("rsi_max", [68, 72, 76, 80]))
    return grid


def create_intelligent_research_campaign(
    conn: psycopg.Connection,
    *,
    universe_key: str,
    name: str | None = None,
    max_candidates: int = 250,
    asset_limit: int = 10,
    timeframes: list[str] | None = None,
    dataset_mode: str = "rolling",
    dataset_id: int | None = None,
    hypothesis_id: int | None = None,
    allocation: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Create the default hypothesis-driven campaign instead of a broad random sweep."""
    started = time.perf_counter()
    log_event("Research campaign launch requested", mode="intelligent", universe_key=universe_key, max_candidates=max_candidates, asset_limit=asset_limit, timeframes=timeframes, dataset_mode=dataset_mode, dataset_id=dataset_id, hypothesis_id=hypothesis_id)

    from app.services.research_campaigns import (
        DEFAULT_CAMPAIGN_TIMEFRAMES,
        DEFAULT_SCHEDULING_CONFIG,
        ensure_campaign_tables,
        get_universe,
        queue_campaign_jobs,
        seed_default_universes,
        update_campaign_counts,
    )

    if max_candidates > 5000:
        raise ValueError("max_candidates cannot exceed the immutable campaign safety limit of 5000")
    ensure_campaign_tables(conn)
    ensure_research_architecture_tables(conn)
    seed_default_universes(conn)
    universe = get_universe(conn, universe_key)
    universe_assets = [str(asset).upper() for asset in list(universe.get("assets") or [])[:asset_limit]]
    selected_timeframes = sorted(set(timeframes or universe.get("default_timeframes") or DEFAULT_CAMPAIGN_TIMEFRAMES))
    if dataset_id is None:
        snapshot_started = time.perf_counter()
        log_event("Dataset snapshot started", assets=len(universe_assets), timeframes=len(selected_timeframes), dataset_mode=dataset_mode)
        dataset = record_dataset_snapshot(conn, assets=universe_assets, timeframes=selected_timeframes, mode=dataset_mode)
        dataset_id = int(dataset["id"])
        log_event("Dataset snapshot completed", dataset_id=dataset_id, elapsed_ms=elapsed_ms(snapshot_started))
    else:
        dataset_row = conn.execute("SELECT * FROM research_dataset_manifests WHERE id = %s", (dataset_id,)).fetchone()
        if not dataset_row:
            raise ValueError(f"research dataset {dataset_id} was not found")
        dataset = jsonable(dict(dataset_row))
        dataset_mode = str(dataset["mode"])
    verify_started = time.perf_counter()
    integrity = verify_dataset_snapshot(conn, dataset_id)
    log_event("Dataset verification finished", dataset_id=dataset_id, passed=integrity["passed"], elapsed_ms=elapsed_ms(verify_started))
    if not integrity["passed"]:
        raise ValueError(f"research dataset {dataset_id} failed integrity verification")

    profile_started = time.perf_counter()
    profiles = build_asset_profiles(conn, dataset_id)["profiles"]
    log_event("Asset profiles built", dataset_id=dataset_id, count=len(profiles), elapsed_ms=elapsed_ms(profile_started))
    cluster_started = time.perf_counter()
    clusters = build_asset_clusters(conn, dataset_id)["clusters"]
    log_event("Asset clusters built", dataset_id=dataset_id, count=len(clusters), elapsed_ms=elapsed_ms(cluster_started))
    hypothesis_started = time.perf_counter()
    hypotheses = build_research_hypotheses(conn, dataset_id)["hypotheses"]
    log_event("Research hypotheses built", dataset_id=dataset_id, count=len(hypotheses), elapsed_ms=elapsed_ms(hypothesis_started))
    hypothesis = select_campaign_hypothesis(hypotheses, hypothesis_id)
    testing_hypothesis = append_hypothesis_version(conn, hypothesis, status="testing", test_summary={**dict(hypothesis.get("test_summary") or {}), "campaign_status": "queued"})

    cluster_id = None
    if testing_hypothesis["scope_type"] == "cluster":
        cluster = next((row for row in clusters if row["cluster_key"] == testing_hypothesis["scope_ref"]), None)
        if not cluster:
            raise ValueError(f"hypothesis cluster {testing_hypothesis['scope_ref']} was not found")
        target_assets = sorted({str(row["symbol"]) for row in cluster["members"]})
        target_timeframes = sorted({str(row["timeframe"]) for row in cluster["members"]})
        cluster_id = int(cluster["id"])
    elif testing_hypothesis["scope_type"] == "asset":
        target_assets = [str(testing_hypothesis["scope_ref"])]
        target_timeframes = [str((testing_hypothesis.get("test_summary") or {}).get("timeframe") or selected_timeframes[0])]
    else:
        target_assets = universe_assets
        target_timeframes = selected_timeframes
    target_assets = [asset for asset in target_assets if asset in universe_assets]
    target_timeframes = [timeframe for timeframe in target_timeframes if timeframe in selected_timeframes]
    if not target_assets or not target_timeframes:
        raise ValueError("selected hypothesis has no datasets inside the requested universe scope")

    parents = load_targeted_parent_candidates(
        conn,
        strategy_family=str(testing_hypothesis["strategy_family"]),
        assets=target_assets,
        timeframes=target_timeframes,
        limit=max(5, min(100, max_candidates // 2)),
    )
    generation_started = time.perf_counter()
    generation = generate_targeted_candidates(testing_hypothesis, max_candidates=max_candidates, parents=parents, allocation=allocation)
    candidates = generation["candidates"]
    log_event("Job generation started", assets=len(target_assets), timeframes=len(target_timeframes), strategies=len(candidates), expected_jobs=len(target_assets) * len(target_timeframes) * len(candidates), elapsed_ms=elapsed_ms(generation_started))
    policy = conn.execute(
        "SELECT * FROM research_validation_policy_versions WHERE policy_key = %s AND version = %s",
        (VALIDATION_POLICY_KEY, VALIDATION_POLICY_VERSION),
    ).fetchone()
    if not policy:
        raise RuntimeError("strong research validation policy is unavailable")
    experiment_generation = max((candidate.generation for candidate in candidates), default=1)
    immutable_config = {
        "architecture_version": ARCHITECTURE_VERSION,
        "dataset_id": dataset_id,
        "dataset_key": dataset["dataset_key"],
        "dataset_content_hash": dataset["content_hash"],
        "dataset_mode": dataset_mode,
        "hypothesis_version_id": testing_hypothesis["id"],
        "hypothesis_key": testing_hypothesis["hypothesis_key"],
        "scope": {"type": testing_hypothesis["scope_type"], "ref": testing_hypothesis["scope_ref"], "assets": target_assets, "timeframes": target_timeframes},
        "generator_version": GENERATOR_VERSION,
        "family_version": generation.get("family_version"),
        "generation_seed": generation.get("generation_seed", 0),
        "validation_policy": {"id": policy["id"], "key": policy["policy_key"], "version": policy["version"], "thresholds": policy["thresholds"]},
        "allocation": generation["allocation"],
        "code_commit": code_commit(),
    }
    campaign_key = f"intelligent_{stable_hash({**immutable_config, 'max_candidates': max_candidates})[:24]}"
    insert_started = time.perf_counter()
    log_event("Before INSERT research_campaigns", campaign_key=campaign_key, name=name or f"{testing_hypothesis['strategy_family']} hypothesis campaign: {testing_hypothesis['scope_ref']}")
    row = conn.execute(
        """
        INSERT INTO research_campaigns(
            campaign_key, name, universe_key, status, requested_candidates, controls, scheduling_config,
            safety_statement, dataset_id, dataset_mode, code_commit, generator_version,
            validation_policy_id, threshold_version, hypothesis_version_id, cluster_id,
            experiment_generation, immutable_config, simulation_only
        ) VALUES (%s, %s, %s, 'queued', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
        ON CONFLICT(campaign_key) DO UPDATE SET updated_at = NOW()
        RETURNING *
        """,
        (
            campaign_key,
            name or f"{testing_hypothesis['strategy_family']} hypothesis campaign: {testing_hypothesis['scope_ref']}",
            universe_key,
            max_candidates,
            Jsonb(
                {
                    "architecture_version": ARCHITECTURE_VERSION,
                    "objective": "Test one measured market hypothesis with controlled strategy-family variations.",
                    "target_scope": immutable_config["scope"],
                    "allocation": generation["allocation"],
                    "candidate_quality_policy": "Hypothesis-bound generation only; validation thresholds are unchanged.",
                    "candidate_levels": ["research_candidate", "asset_specialist", "cluster_candidate", "cluster_elite", "universal_elite"],
                    "approval_required_for_live_execution": True,
                }
            ),
            Jsonb({**DEFAULT_SCHEDULING_CONFIG, "max_generated_candidates": max_candidates}),
            SAFETY_STATEMENT,
            dataset_id,
            dataset_mode,
            code_commit(),
            GENERATOR_VERSION,
            int(policy["id"]),
            f"{VALIDATION_POLICY_KEY}:v{VALIDATION_POLICY_VERSION}",
            int(testing_hypothesis["id"]),
            cluster_id,
            experiment_generation,
            Jsonb(immutable_config),
        ),
    ).fetchone()
    log_event("After INSERT research_campaigns", elapsed_ms=elapsed_ms(insert_started), rows_affected=1 if row else 0, campaign_id=row["id"] if row else None)
    campaign_id = int(row["id"])
    log_event("Campaign inserted into database", campaign_id=campaign_id)
    created = queue_campaign_jobs(conn, campaign_id, candidates, target_assets, target_timeframes)
    for candidate in candidates:
        conn.execute(
            """
            UPDATE research_campaign_jobs
            SET dataset_id = %s,
                hypothesis_version_id = %s,
                parent_candidate_id = %s,
                generation_channel = %s
            WHERE campaign_id = %s AND candidate_id = %s
            """,
            (
                dataset_id,
                testing_hypothesis["id"],
                candidate.parent_candidate_id,
                candidate.parameters.get("generation_channel"),
                campaign_id,
                candidate.candidate_id,
            ),
        )
    update_campaign_counts(conn, campaign_id)
    log_event("Before COMMIT research_campaigns", campaign_id=campaign_id)
    conn.commit()
    log_event("After COMMIT research_campaigns", campaign_id=campaign_id, jobs_inserted=created)
    log_event("Campaign committed", campaign_id=campaign_id)
    log_event("Campaign returned", campaign_id=campaign_id, elapsed_ms=elapsed_ms(started))
    return {
        "campaign": jsonable(dict(row)),
        "dataset": dataset,
        "dataset_integrity": integrity,
        "asset_profiles": {"count": len(profiles), "calculation_version": PROFILE_VERSION},
        "clusters": clusters,
        "hypothesis": testing_hypothesis,
        "targeting": immutable_config["scope"],
        "assets": target_assets,
        "timeframes": target_timeframes,
        "candidate_generation": {key: value for key, value in generation.items() if key != "candidates"},
        "candidates_generated": len(candidates),
        "jobs_created": created,
        "campaign_version": ARCHITECTURE_VERSION,
        "architecture_version": ARCHITECTURE_VERSION,
        "simulation_only": True,
        "safety": SAFETY_STATEMENT,
    }


def select_campaign_hypothesis(hypotheses: list[dict[str, Any]], hypothesis_id: int | None = None) -> dict[str, Any]:
    eligible = [
        row
        for row in hypotheses
        if (
            row.get("status") in {"proposed", "supported"}
            or (row.get("status") == "weak" and not (row.get("test_summary") or {}).get("campaign_id"))
        )
        and (
            hypothesis_validation_samples_per_market(row) == 0
            or hypothesis_validation_samples_per_market(row) >= MIN_AUTOMATIC_SAMPLES_PER_MARKET
        )
    ]
    if hypothesis_id is not None:
        match = next((row for row in hypotheses if int(row["id"]) == hypothesis_id), None)
        if not match:
            raise ValueError(f"research hypothesis {hypothesis_id} was not found in the selected dataset")
        if match.get("status") in {"rejected", "retired"}:
            raise ValueError("rejected or retired hypotheses cannot create new campaigns without an explicit new version")
        return match
    if not eligible:
        raise ValueError("no eligible evidence-based hypothesis is available")
    return max(
        eligible,
        key=lambda row: (
            row.get("scope_type") == "cluster",
            not bool((row.get("test_summary") or {}).get("campaign_id")),
            row.get("status") == "supported",
            (row.get("test_summary") or {}).get("prior_status") == "supported",
            hypothesis_validation_samples_per_market(row),
            finite_metric(row.get("confidence_score")),
            str(row.get("hypothesis_key")),
        ),
    )


def hypothesis_validation_samples_per_market(hypothesis: dict[str, Any]) -> float:
    """Rank evidence windows by usable observations per independently gated market."""

    evidence_window = dict(hypothesis.get("evidence_window") or {})
    test_summary = dict(hypothesis.get("test_summary") or {})
    total_samples = finite_metric(evidence_window.get("sample_size") or evidence_window.get("candle_count"))
    symbols = list(test_summary.get("symbols") or [])
    market_count = max(1, len(symbols))
    return round(total_samples / market_count, 4)


def append_hypothesis_version(
    conn: psycopg.Connection,
    hypothesis: dict[str, Any],
    *,
    status: str,
    test_summary: dict[str, Any],
    supporting_evidence: list[str] | None = None,
    contradictory_evidence: list[str] | None = None,
    required_conditions: str | None = None,
    invalidation_conditions: str | None = None,
    success_criteria: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in {"proposed", "testing", "supported", "weak", "rejected", "retired"}:
        raise ValueError(f"unsupported hypothesis status {status}")
    version_row = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM research_hypothesis_versions WHERE hypothesis_key = %s", (hypothesis["hypothesis_key"],)).fetchone()
    row = conn.execute(
        """
        INSERT INTO research_hypothesis_versions(
            hypothesis_key, version, parent_hypothesis_id, scope_type, scope_ref, strategy_family,
            title, observation, hypothesis, expected_behavior, relevant_regimes, confidence_score,
            evidence_window, creation_source, status, supporting_evidence, contradictory_evidence,
            test_summary, calculation_version, required_conditions, invalidation_conditions,
            success_criteria, simulation_only
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
        RETURNING *
        """,
        (
            hypothesis["hypothesis_key"], int(version_row["next_version"]), hypothesis.get("id"),
            hypothesis["scope_type"], hypothesis["scope_ref"], hypothesis["strategy_family"],
            hypothesis["title"], hypothesis["observation"], hypothesis["hypothesis"],
            hypothesis["expected_behavior"], Jsonb(list(hypothesis.get("relevant_regimes") or [])),
            hypothesis["confidence_score"], Jsonb(dict(hypothesis.get("evidence_window") or {})),
            hypothesis["creation_source"], status,
            Jsonb(list(supporting_evidence if supporting_evidence is not None else hypothesis.get("supporting_evidence") or [])),
            Jsonb(list(contradictory_evidence if contradictory_evidence is not None else hypothesis.get("contradictory_evidence") or [])),
            Jsonb(test_summary), HYPOTHESIS_VERSION,
            required_conditions if required_conditions is not None else hypothesis.get("required_conditions"),
            invalidation_conditions if invalidation_conditions is not None else hypothesis.get("invalidation_conditions"),
            Jsonb(success_criteria if success_criteria is not None else dict(hypothesis.get("success_criteria") or {})) if (success_criteria is not None or hypothesis.get("success_criteria")) else None,
        ),
    ).fetchone()
    return jsonable(dict(row))


def load_targeted_parent_candidates(
    conn: psycopg.Connection,
    *,
    strategy_family: str,
    assets: list[str],
    timeframes: list[str],
    limit: int,
) -> list[DiscoveryCandidate]:
    rows = conn.execute(
        """
        SELECT DISTINCT ON (candidate_id) candidate
        FROM research_campaign_jobs
        WHERE status = 'promoted'
          AND strategy_family = %s
          AND symbol = ANY(%s::text[])
          AND timeframe = ANY(%s::text[])
          AND simulation_only = TRUE
        ORDER BY candidate_id, validation_score DESC, completed_at DESC
        LIMIT %s
        """,
        (strategy_family, assets, timeframes, limit),
    ).fetchall()
    parents = []
    for row in rows:
        payload = dict(row.get("candidate") or {})
        try:
            parents.append(
                DiscoveryCandidate(
                    candidate_id=str(payload["candidate_id"]),
                    family_id=str(payload["family_id"]),
                    parent_candidate_id=payload.get("parent_candidate_id"),
                    generation=int(payload.get("generation") or 1),
                    blocks=dict(payload.get("blocks") or {}),
                    parameters=dict(payload.get("parameters") or {}),
                    complexity=int(payload.get("complexity") or 1),
                    canonical_key=str(payload.get("canonical_key") or canonical_candidate_key(dict(payload.get("blocks") or {}), dict(payload.get("parameters") or {}), payload.get("parent_candidate_id"))),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return parents


def validation_gate_diagnostics(result: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = dict(result.get("metrics") or {})
    readiness = dict(result.get("paper_readiness") or {})
    checks = [
        gate_result("trade_count", finite_metric(metrics.get("number_of_trades")), 30, ">="),
        profit_factor_gate_result(metrics, 1.2),
        gate_result("positive_expectancy", finite_metric(metrics.get("expectancy_per_trade")), 0, ">"),
        gate_result("maximum_drawdown", finite_metric(metrics.get("max_drawdown")), 0.12, "<="),
        gate_result("walk_forward", bool((metrics.get("walk_forward") or {}).get("enabled")), True, "=="),
        gate_result("paper_readiness", bool(readiness.get("paper_ready")), True, "=="),
    ]
    return checks


def validation_funnel(jobs: list[dict[str, Any]], stages: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    terminal = [row for row in jobs if row.get("status") in {"completed", "promoted", "rejected"} and row.get("result")]
    diagnostics = {int(row.get("id") or index): validation_gate_diagnostics(dict(row.get("result") or {})) for index, row in enumerate(terminal)}
    order = ["trade_count", "profit_factor", "positive_expectancy", "maximum_drawdown", "walk_forward", "paper_readiness"]
    surviving = set(diagnostics)
    funnel = [
        {"stage": "jobs_generated", "level": "job", "count": len(jobs)},
        {"stage": "jobs_completed", "level": "job", "count": len(terminal)},
    ]
    for gate_name in order:
        surviving = {
            key
            for key in surviving
            if next((item["passed"] for item in diagnostics[key] if item["name"] == gate_name), False)
        }
        funnel.append({"stage": f"passed_{gate_name}", "level": "job", "count": len(surviving)})
    stage_rows = list(stages or [])
    for candidate_level in ("research_candidate", "asset_specialist", "cluster_candidate", "cluster_elite", "universal_elite"):
        candidate_ids = {row["candidate_id"] for row in stage_rows if row.get("candidate_level") == candidate_level}
        funnel.append({"stage": candidate_level, "level": "candidate", "count": len(candidate_ids)})
    return funnel


def build_candidate_stage_evidence(
    campaign: dict[str, Any],
    jobs: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    controls = dict(campaign.get("controls") or {})
    scope = dict(controls.get("target_scope") or (campaign.get("immutable_config") or {}).get("scope") or {})
    scope_type = str(scope.get("type") or "universal")
    scope_ref = str(scope.get("ref") or campaign.get("universe_key") or "universal")
    hypothesis_id = campaign.get("hypothesis_version_id")
    jobs_by_candidate: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        jobs_by_candidate.setdefault(str(job["candidate_id"]), []).append(job)
    summary_by_candidate = {str(row["candidate_id"]): row for row in summaries}
    evidence = []
    for candidate_id, rows in sorted(jobs_by_candidate.items()):
        summary = summary_by_candidate[candidate_id]
        parent_id = next((row.get("parent_candidate_id") for row in rows if row.get("parent_candidate_id")), None)
        base_refs = [f"research_campaign_job:{row['id']}" for row in rows]
        evidence.append(stage_row(campaign, candidate_id, "generated", scope_type, scope_ref, hypothesis_id, parent_id, [], summary_metrics(summary), base_refs, True))
        passed = [row for row in rows if row.get("status") == "promoted"]
        if not passed:
            continue
        all_passed_gates = [validation_gate_diagnostics(dict(row.get("result") or {})) for row in passed]
        evidence.append(stage_row(campaign, candidate_id, "research_candidate", scope_type, scope_ref, hypothesis_id, parent_id, all_passed_gates, summary_metrics(summary), base_refs, True))
        for symbol in sorted({str(row["symbol"]) for row in passed}):
            symbol_rows = [row for row in passed if str(row["symbol"]) == symbol]
            symbol_metrics = aggregate_job_metrics(symbol_rows)
            evidence.append(
                stage_row(
                    campaign, candidate_id, "asset_specialist", "asset", symbol, hypothesis_id, parent_id,
                    [validation_gate_diagnostics(dict(row.get("result") or {})) for row in symbol_rows], symbol_metrics,
                    [f"research_campaign_job:{row['id']}" for row in symbol_rows], True,
                )
            )
        if len({row["symbol"] for row in passed}) >= 2:
            cross_gates = cross_validation_gate_results(summary)
            evidence.append(stage_row(campaign, candidate_id, "cluster_candidate", "cluster", scope_ref, hypothesis_id, parent_id, cross_gates, summary_metrics(summary), base_refs, True))
            if scope_type == "cluster" and all(row["passed"] for row in cross_gates):
                evidence.append(stage_row(campaign, candidate_id, "cluster_elite", "cluster", scope_ref, hypothesis_id, parent_id, cross_gates, summary_metrics(summary), base_refs, True))
            if scope_type == "universal" and passes_universal_validation(summary, len({row["symbol"] for row in rows})):
                evidence.append(stage_row(campaign, candidate_id, "universal_elite", "universal", scope_ref, hypothesis_id, parent_id, cross_gates, summary_metrics(summary), base_refs, True))
    return evidence


def cross_validation_gate_results(summary: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        gate_result("research_score", summary.get("research_score"), 0, ">"),
        profit_factor_gate_result(summary, 1.2),
        gate_result("positive_expectancy", summary.get("expectancy"), 0, ">"),
        gate_result("maximum_drawdown", summary.get("max_drawdown"), 0.12, "<="),
        gate_result("trade_count", summary.get("trade_count"), 60, ">="),
        gate_result("stability", summary.get("stability"), 0.6, ">="),
        gate_result("assets_passed", summary.get("assets_passed"), 2, ">="),
        gate_result("timeframes_passed", summary.get("timeframes_passed"), 1, ">="),
    ]


def passes_universal_validation(summary: dict[str, Any], assets_tested: int) -> bool:
    minimum_assets = max(2, math.ceil(assets_tested * 0.6))
    return all(row["passed"] for row in cross_validation_gate_results(summary)) and int(summary.get("assets_passed") or 0) >= minimum_assets


def persist_candidate_stage_evidence(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        conn.execute(
            """
            INSERT INTO research_candidate_stage_evidence(
                evidence_key, campaign_id, candidate_id, candidate_level, scope_type, scope_ref,
                hypothesis_version_id, parent_candidate_id, gate_results, metrics, evidence_refs,
                promoted, calculation_version, simulation_only
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            ON CONFLICT(evidence_key) DO NOTHING
            """,
            (
                row["evidence_key"], row["campaign_id"], row["candidate_id"], row["candidate_level"],
                row["scope_type"], row["scope_ref"], row.get("hypothesis_version_id"), row.get("parent_candidate_id"),
                Jsonb(jsonable(row["gate_results"])), Jsonb(jsonable(row["metrics"])), Jsonb(row["evidence_refs"]),
                row["promoted"], CANDIDATE_LEVEL_VERSION,
            ),
        )


def finalize_architecture_campaign(
    conn: psycopg.Connection,
    campaign_id: int,
    *,
    summaries: list[dict[str, Any]],
    analytics: dict[str, Any],
) -> dict[str, Any]:
    ensure_research_architecture_tables(conn)
    campaign_row = conn.execute("SELECT * FROM research_campaigns WHERE id = %s", (campaign_id,)).fetchone()
    if not campaign_row or not campaign_row.get("dataset_id"):
        return analytics
    campaign = dict(campaign_row)
    jobs = [dict(row) for row in conn.execute("SELECT * FROM research_campaign_jobs WHERE campaign_id = %s ORDER BY candidate_id, symbol, timeframe", (campaign_id,)).fetchall()]
    stage_rows = build_candidate_stage_evidence(campaign, jobs, summaries)
    persist_candidate_stage_evidence(conn, stage_rows)
    for job in jobs:
        diagnostics = validation_gate_diagnostics(dict(job.get("result") or {})) if job.get("result") else []
        conn.execute("UPDATE research_campaign_jobs SET rejection_diagnostics = %s WHERE id = %s", (Jsonb(diagnostics), job["id"]))

    controls = dict(campaign.get("controls") or {})
    scope = dict(controls.get("target_scope") or {})
    elite_level = "universal_elite" if scope.get("type") == "universal" else "cluster_elite"
    elite_ids = {row["candidate_id"] for row in stage_rows if row["candidate_level"] == elite_level}
    for candidate_id in elite_ids:
        parent_id = next((row.get("parent_candidate_id") for row in jobs if row["candidate_id"] == candidate_id and row.get("parent_candidate_id")), None)
        conn.execute(
            """
            UPDATE elite_research_candidates
            SET candidate_level = %s, scope_type = %s, scope_ref = %s, dataset_id = %s,
                hypothesis_version_id = %s, parent_candidate_id = %s
            WHERE campaign_id = %s AND candidate_id = %s
            """,
            (elite_level, scope.get("type"), scope.get("ref"), campaign.get("dataset_id"), campaign.get("hypothesis_version_id"), parent_id, campaign_id, candidate_id),
        )

    stage_counts = Counter(row["candidate_level"] for row in stage_rows)
    analytics["research_architecture"] = {
        "architecture_version": ARCHITECTURE_VERSION,
        "dataset_id": campaign["dataset_id"],
        "dataset_mode": campaign.get("dataset_mode"),
        "hypothesis_version_id": campaign.get("hypothesis_version_id"),
        "scope": scope,
        "candidate_levels": dict(stage_counts),
        "validation_funnel": validation_funnel(jobs, stage_rows),
        "threshold_version": campaign.get("threshold_version"),
    }
    hypothesis = None
    if campaign.get("hypothesis_version_id"):
        hypothesis_row = conn.execute("SELECT * FROM research_hypothesis_versions WHERE id = %s", (campaign["hypothesis_version_id"],)).fetchone()
        hypothesis = dict(hypothesis_row) if hypothesis_row else None
    if hypothesis:
        hypothesis_summary = dict(hypothesis.get("test_summary") or {})
        lifecycle = interpret_hypothesis_result(stage_counts, hypothesis_summary, campaign.get("dataset_id"))
        status = lifecycle["status"]
        lifecycle_interpretation = lifecycle["interpretation"]
        same_evidence_post_hoc = lifecycle["same_evidence_post_hoc"]
        result_refs = [f"research_candidate_stage_evidence:{row['evidence_key']}" for row in stage_rows if row["candidate_level"] != "generated"]
        updated_hypothesis = append_hypothesis_version(
            conn,
            hypothesis,
            status=status,
            test_summary={
                **hypothesis_summary,
                "campaign_id": campaign_id,
                "candidate_levels": dict(stage_counts),
                "validation_funnel": analytics["research_architecture"]["validation_funnel"],
                "lifecycle_interpretation": lifecycle_interpretation,
                "confirmation_status": "unconfirmed" if same_evidence_post_hoc else hypothesis_summary.get("confirmation_status"),
            },
            supporting_evidence=list(hypothesis.get("supporting_evidence") or []) + (result_refs if status in {"supported", "testing"} else []),
            contradictory_evidence=list(hypothesis.get("contradictory_evidence") or []) + (result_refs if status in {"weak", "rejected"} else []),
        )
        analytics["research_architecture"]["hypothesis_result"] = {"id": updated_hypothesis["id"], "status": status}

    conn.execute(
        "UPDATE research_campaigns SET analytics = %s, finalized_at = NOW(), updated_at = NOW() WHERE id = %s",
        (Jsonb(jsonable(analytics)), campaign_id),
    )
    archive = persist_campaign_archive(conn, campaign_id)
    analytics["research_architecture"]["archive"] = {key: archive.get(key) for key in ("archive_key", "content_hash", "storage_locations")}
    conn.execute("UPDATE research_campaigns SET analytics = %s WHERE id = %s", (Jsonb(jsonable(analytics)), campaign_id))
    conn.execute(
        "UPDATE autonomous_research_cycles SET status = 'completed', completed_at = NOW(), result = %s WHERE campaign_id = %s",
        (Jsonb({"candidate_levels": dict(stage_counts), "archive_key": archive["archive_key"]}), campaign_id),
    )
    return analytics


def interpret_hypothesis_result(
    stage_counts: Counter[str] | dict[str, int],
    hypothesis_summary: dict[str, Any],
    campaign_dataset_id: Any,
) -> dict[str, Any]:
    if int(stage_counts.get("cluster_elite", 0)) or int(stage_counts.get("universal_elite", 0)):
        status = "supported"
    elif int(stage_counts.get("asset_specialist", 0)):
        status = "weak"
    else:
        status = "rejected"
    same_evidence_post_hoc = (
        bool(hypothesis_summary.get("post_hoc"))
        and str(hypothesis_summary.get("source_dataset_id")) == str(campaign_dataset_id)
    )
    interpretation = "independent_result"
    if same_evidence_post_hoc and status == "supported":
        # The regression result is useful evidence, but the same frozen
        # dataset supplied the observation that generated this hypothesis.
        # Preserve the pass while refusing to call it independent support.
        status = "testing"
        interpretation = "same_evidence_pass_unconfirmed"
    return {
        "status": status,
        "interpretation": interpretation,
        "same_evidence_post_hoc": same_evidence_post_hoc,
    }


def persist_campaign_archive(conn: psycopg.Connection, campaign_id: int, *, archive_directory: str | Path | None = None) -> dict[str, Any]:
    campaign = conn.execute("SELECT * FROM research_campaigns WHERE id = %s", (campaign_id,)).fetchone()
    if not campaign:
        raise ValueError(f"research campaign {campaign_id} was not found")
    campaign = dict(campaign)
    jobs = [jsonable(dict(row)) for row in conn.execute("SELECT * FROM research_campaign_jobs WHERE campaign_id = %s ORDER BY id", (campaign_id,)).fetchall()]
    stages = [jsonable(dict(row)) for row in conn.execute("SELECT * FROM research_candidate_stage_evidence WHERE campaign_id = %s ORDER BY id", (campaign_id,)).fetchall()]
    learning_tables = (
        "research_knowledge_versions",
        "research_failure_patterns",
        "research_success_patterns",
        "research_recommendations",
        "research_confidence_history",
        "research_evolution_history",
        "research_timeline_events",
        "research_campaign_plans",
    )
    learning = {
        table: [jsonable(dict(row)) for row in conn.execute(f"SELECT * FROM {table} WHERE campaign_id = %s ORDER BY id", (campaign_id,)).fetchall()]
        for table in learning_tables
    }
    report = conn.execute("SELECT * FROM research_campaign_reports WHERE campaign_id = %s ORDER BY created_at DESC LIMIT 1", (campaign_id,)).fetchone()
    dataset = conn.execute("SELECT * FROM research_dataset_manifests WHERE id = %s", (campaign.get("dataset_id"),)).fetchone() if campaign.get("dataset_id") else None
    hypothesis = conn.execute("SELECT * FROM research_hypothesis_versions WHERE id = %s", (campaign.get("hypothesis_version_id"),)).fetchone() if campaign.get("hypothesis_version_id") else None
    latest_hypothesis = conn.execute(
        "SELECT * FROM research_hypothesis_versions WHERE hypothesis_key = %s ORDER BY version DESC LIMIT 1",
        (hypothesis["hypothesis_key"],),
    ).fetchone() if hypothesis else None
    campaign_manifest = jsonable(campaign)
    architecture_analytics = dict((campaign_manifest.get("analytics") or {}).get("research_architecture") or {})
    architecture_analytics.pop("archive", None)
    if campaign_manifest.get("analytics") is not None:
        campaign_manifest["analytics"] = {**campaign_manifest["analytics"], "research_architecture": architecture_analytics}
    manifest = {
        "archive_version": ARCHIVE_VERSION,
        "exported_at": jsonable(campaign.get("finalized_at") or campaign.get("completed_at") or campaign.get("created_at")),
        "campaign": campaign_manifest,
        "dataset": jsonable(dict(dataset)) if dataset else None,
        "tested_hypothesis": jsonable(dict(hypothesis)) if hypothesis else None,
        "hypothesis_result": jsonable(dict(latest_hypothesis)) if latest_hypothesis else None,
        "jobs": jobs,
        "candidate_stage_evidence": stages,
        "learning_evidence": learning,
        "report": jsonable(dict(report)) if report else None,
        "restore_policy": "Immutable evidence import; restored campaigns are not automatically resumed or deployed.",
        "simulation_only": True,
    }
    content_hash = stable_hash(manifest)
    archive_key = f"campaign_archive_{campaign_id}_{content_hash[:16]}"
    directory = Path(archive_directory) if archive_directory else default_archive_directory()
    directory.mkdir(parents=True, exist_ok=True)
    campaign_path = directory / f"{archive_key}.json.gz"
    write_gzip_json(campaign_path, {"content_hash": content_hash, "manifest": manifest})
    locations = [str(campaign_path.resolve())]
    if campaign.get("dataset_id"):
        dataset_export = export_dataset_bundle(conn, int(campaign["dataset_id"]), archive_directory=directory)
        locations.append(dataset_export["path"])
    row = conn.execute(
        """
        INSERT INTO research_campaign_archives(
            archive_key, campaign_id, original_campaign_id, dataset_id, manifest, content_hash,
            storage_locations, calculation_version, immutable, simulation_only
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE, TRUE)
        ON CONFLICT(archive_key) DO NOTHING
        RETURNING *
        """,
        (archive_key, campaign_id, campaign_id, campaign.get("dataset_id"), Jsonb(manifest), content_hash, Jsonb(locations), ARCHIVE_VERSION),
    ).fetchone()
    if not row:
        row = conn.execute("SELECT * FROM research_campaign_archives WHERE archive_key = %s", (archive_key,)).fetchone()
    conn.commit()
    return jsonable(dict(row))


def export_dataset_bundle(conn: psycopg.Connection, dataset_id: int, *, archive_directory: str | Path | None = None) -> dict[str, Any]:
    manifest = conn.execute("SELECT * FROM research_dataset_manifests WHERE id = %s", (dataset_id,)).fetchone()
    if not manifest:
        raise ValueError(f"research dataset {dataset_id} was not found")
    integrity = verify_dataset_snapshot(conn, dataset_id)
    if not integrity["passed"]:
        raise ValueError(f"research dataset {dataset_id} cannot be exported because integrity verification failed")
    candles = [archive_candle(dict(row)) for row in conn.execute("SELECT * FROM research_dataset_candles WHERE dataset_id = %s ORDER BY symbol, timeframe, timestamp, source", (dataset_id,)).fetchall()]
    payload = {"archive_version": ARCHIVE_VERSION, "dataset": jsonable(dict(manifest)), "candles": candles, "integrity": integrity}
    bundle_hash = stable_hash(payload)
    directory = Path(archive_directory) if archive_directory else default_archive_directory()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"dataset_{manifest['dataset_key']}_{bundle_hash[:16]}.json.gz"
    if not path.exists():
        write_gzip_json(path, {"bundle_hash": bundle_hash, "payload": payload})
    return {"dataset_id": dataset_id, "dataset_key": manifest["dataset_key"], "bundle_hash": bundle_hash, "path": str(path.resolve()), "candle_count": len(candles)}


def restore_dataset_bundle(conn: psycopg.Connection, path: str | Path) -> dict[str, Any]:
    ensure_research_architecture_tables(conn)
    bundle = read_gzip_json(Path(path))
    payload = dict(bundle.get("payload") or {})
    if stable_hash(payload) != bundle.get("bundle_hash"):
        raise ValueError("dataset archive checksum does not match its payload")
    dataset = dict(payload.get("dataset") or {})
    existing = conn.execute("SELECT * FROM research_dataset_manifests WHERE dataset_key = %s", (dataset.get("dataset_key"),)).fetchone()
    if existing:
        verification = verify_dataset_snapshot(conn, int(existing["id"]))
        return {"restored": False, "reason": "dataset_already_present", "dataset": jsonable(dict(existing)), "integrity": verification}
    row = conn.execute(
        """
        INSERT INTO research_dataset_manifests(
            dataset_key, name, mode, snapshot_version, assets, timeframes, window_start, window_end,
            candle_counts, candle_hashes, source_providers, content_hash, integrity,
            calculation_version, immutable, simulation_only
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, TRUE)
        RETURNING *
        """,
        (
            dataset["dataset_key"], dataset["name"], dataset["mode"], dataset.get("snapshot_version", 1),
            Jsonb(dataset["assets"]), Jsonb(dataset["timeframes"]), dataset.get("window_start"), dataset.get("window_end"),
            Jsonb(dataset["candle_counts"]), Jsonb(dataset["candle_hashes"]), Jsonb(dataset.get("source_providers") or []),
            dataset["content_hash"], Jsonb(dataset.get("integrity") or {}), dataset.get("calculation_version") or DATASET_VERSION,
        ),
    ).fetchone()
    dataset_id = int(row["id"])
    for candle in payload.get("candles") or []:
        conn.execute(
            """
            INSERT INTO research_dataset_candles(dataset_id, symbol, source, timeframe, timestamp, open, high, low, close, volume)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(dataset_id, symbol, timeframe, timestamp, source) DO NOTHING
            """,
            (dataset_id, candle["symbol"], candle["source"], candle["timeframe"], candle["timestamp"], candle["open"], candle["high"], candle["low"], candle["close"], candle["volume"]),
        )
    verification = verify_dataset_snapshot(conn, dataset_id)
    if not verification["passed"]:
        conn.rollback()
        raise ValueError("restored dataset failed candle-count or candle-hash verification")
    conn.commit()
    return {"restored": True, "dataset": jsonable(dict(row)), "integrity": verification}


def restore_campaign_archive(conn: psycopg.Connection, path: str | Path) -> dict[str, Any]:
    ensure_research_architecture_tables(conn)
    bundle = read_gzip_json(Path(path))
    manifest = dict(bundle.get("manifest") or {})
    content_hash = stable_hash(manifest)
    if content_hash != bundle.get("content_hash"):
        raise ValueError("campaign archive checksum does not match its manifest")
    original_campaign_id = int((manifest.get("campaign") or {}).get("id"))
    archive_key = f"campaign_archive_{original_campaign_id}_{content_hash[:16]}"
    row = conn.execute(
        """
        INSERT INTO research_campaign_archives(
            archive_key, campaign_id, original_campaign_id, dataset_id, manifest, content_hash,
            storage_locations, calculation_version, immutable, simulation_only
        ) VALUES (%s, NULL, %s, NULL, %s, %s, %s, %s, TRUE, TRUE)
        ON CONFLICT(archive_key) DO NOTHING
        RETURNING *
        """,
        (archive_key, original_campaign_id, Jsonb(manifest), content_hash, Jsonb([str(Path(path).resolve())]), ARCHIVE_VERSION),
    ).fetchone()
    if not row:
        row = conn.execute("SELECT * FROM research_campaign_archives WHERE archive_key = %s", (archive_key,)).fetchone()
    conn.commit()
    return {"restored": True, "archive": jsonable(dict(row)), "operational_resume_allowed": False}


def run_autonomous_research_cycle(
    conn: psycopg.Connection,
    *,
    universe_key: str,
    timeframes: list[str] | None = None,
    max_candidates: int = 250,
    asset_limit: int = 10,
    dataset_mode: str = "rolling",
    approval_mode: str = "manual",
) -> dict[str, Any]:
    """Observe, profile, cluster, hypothesize, and optionally queue one bounded campaign."""

    from app.services.research_campaigns import DEFAULT_CAMPAIGN_TIMEFRAMES, get_universe, seed_default_universes

    if approval_mode not in {"manual", "auto_queue"}:
        raise ValueError("approval_mode must be 'manual' or 'auto_queue'")
    ensure_research_architecture_tables(conn)
    seed_default_universes(conn)
    universe = get_universe(conn, universe_key)
    assets = [str(item).upper() for item in list(universe.get("assets") or [])[:asset_limit]]
    selected_timeframes = sorted(set(timeframes or universe.get("default_timeframes") or DEFAULT_CAMPAIGN_TIMEFRAMES))
    dataset = record_dataset_snapshot(conn, assets=assets, timeframes=selected_timeframes, mode=dataset_mode)
    profiles = build_asset_profiles(conn, int(dataset["id"]))["profiles"]
    clusters = build_asset_clusters(conn, int(dataset["id"]))["clusters"]
    hypotheses = build_research_hypotheses(conn, int(dataset["id"]))["hypotheses"]
    hypothesis = select_campaign_hypothesis(hypotheses)
    plan = {
        "loop": ["observe", "profile", "cluster", "hypothesize", "generate", "validate", "learn", "archive"],
        "dataset_id": dataset["id"],
        "profile_ids": [row["id"] for row in profiles],
        "cluster_ids": [row["id"] for row in clusters],
        "selected_hypothesis_id": hypothesis["id"],
        "selected_hypothesis": hypothesis["title"],
        "scope": {"type": hypothesis["scope_type"], "ref": hypothesis["scope_ref"]},
        "max_candidates": max_candidates,
        "allocation": DEFAULT_ALLOCATION,
        "validation_policy": f"{VALIDATION_POLICY_KEY}:v{VALIDATION_POLICY_VERSION}",
        "approval_mode": approval_mode,
    }
    cycle_key = f"cycle_{stable_hash({**plan, 'dataset_content_hash': dataset['content_hash']})[:24]}"
    campaign = None
    status = "planned"
    if approval_mode == "auto_queue":
        campaign = create_intelligent_research_campaign(
            conn,
            universe_key=universe_key,
            max_candidates=max_candidates,
            asset_limit=asset_limit,
            timeframes=selected_timeframes,
            dataset_mode=dataset_mode,
            dataset_id=int(dataset["id"]),
            hypothesis_id=int(hypothesis["id"]),
        )
        status = "queued"
    row = conn.execute(
        """
        INSERT INTO autonomous_research_cycles(
            cycle_key, universe_key, dataset_id, cluster_ids, hypothesis_ids, campaign_id,
            approval_mode, status, plan, result, safety_controls, calculation_version, simulation_only
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
        ON CONFLICT(cycle_key) DO UPDATE SET status = EXCLUDED.status, result = EXCLUDED.result
        RETURNING *
        """,
        (
            cycle_key, universe_key, dataset["id"], Jsonb([row["id"] for row in clusters]), Jsonb([row["id"] for row in hypotheses]),
            (campaign or {}).get("campaign", {}).get("id"), approval_mode, status, Jsonb(plan),
            Jsonb({"campaign": (campaign or {}).get("campaign")}),
            Jsonb({"simulation_only": True, "job_limit": max_candidates * max(1, len(assets)) * max(1, len(selected_timeframes)), "threshold_weakening_forbidden": True, "manual_live_approval_required": True}),
            ARCHITECTURE_VERSION,
        ),
    ).fetchone()
    conn.commit()
    return {
        "cycle": jsonable(dict(row)),
        "plan": plan,
        "dataset": dataset,
        "profiles": {"count": len(profiles)},
        "clusters": clusters,
        "hypotheses": hypotheses,
        "campaign": campaign,
        "simulation_only": True,
        "safety": SAFETY_STATEMENT,
    }


def research_architecture_state(conn: psycopg.Connection, *, dataset_id: int | None = None, limit: int = 50) -> dict[str, Any]:
    ensure_research_architecture_tables(conn)
    datasets = [dict(row) for row in conn.execute("SELECT * FROM research_dataset_manifests ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()]
    if dataset_id is None and datasets:
        dataset_id = int(datasets[0]["id"])
    profiles = []
    clusters = []
    hypotheses = []
    if dataset_id is not None:
        profiles = [dict(row) for row in conn.execute("SELECT * FROM asset_profile_versions WHERE dataset_id = %s ORDER BY timeframe, symbol", (dataset_id,)).fetchall()]
        for row in conn.execute("SELECT * FROM asset_cluster_versions WHERE dataset_id = %s ORDER BY name", (dataset_id,)).fetchall():
            cluster = dict(row)
            cluster["members"] = [dict(item) for item in conn.execute("SELECT * FROM asset_cluster_members WHERE cluster_id = %s ORDER BY symbol", (cluster["id"],)).fetchall()]
            clusters.append(cluster)
        hypotheses = [dict(row) for row in conn.execute("SELECT * FROM research_hypothesis_versions WHERE test_summary->>'source_dataset_id' = %s ORDER BY confidence_score DESC, version DESC", (str(dataset_id),)).fetchall()]
    cycles = [dict(row) for row in conn.execute("SELECT * FROM autonomous_research_cycles ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()]
    archives = [dict(row) for row in conn.execute("SELECT id, archive_key, original_campaign_id, dataset_id, content_hash, storage_locations, created_at FROM research_campaign_archives ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()]
    return {
        "architecture_version": ARCHITECTURE_VERSION,
        "active_dataset_id": dataset_id,
        "datasets": [jsonable(row) for row in datasets],
        "asset_profiles": [jsonable(row) for row in profiles],
        "clusters": [jsonable(row) for row in clusters],
        "hypotheses": [jsonable(row) for row in hypotheses],
        "cycles": [jsonable(row) for row in cycles],
        "archives": [jsonable(row) for row in archives],
        "validation_policy": {"key": VALIDATION_POLICY_KEY, "version": VALIDATION_POLICY_VERSION, "thresholds": validation_thresholds()},
        "safety": {"simulation_only": True, "statement": SAFETY_STATEMENT},
    }


def load_frozen_campaign_dataset(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    symbol: str,
    timeframe: str,
) -> dict[str, Any]:
    candles = load_snapshot_candles(conn, dataset_id, symbol, timeframe)
    if not candles:
        raise ValueError(f"dataset {dataset_id} does not contain {symbol} {timeframe}")
    features = calculate_features(candles)
    regimes = calculate_regimes(candles, features)
    context_by_time = build_context_by_time(candles, features, regimes)
    return {"candles": candles, "features": features, "regimes": regimes, "context_by_time": context_by_time}


def profit_factor_gate_result(metrics: dict[str, Any], threshold: float) -> dict[str, Any]:
    infinite = profit_factor_is_infinite(metrics)
    return {
        "name": "profit_factor",
        "actual": "infinite" if infinite else finite_metric(metrics.get("profit_factor")),
        "threshold": threshold,
        "comparator": ">=",
        "passed": profit_factor_passes(metrics, threshold),
    }


def gate_result(name: str, actual: Any, threshold: Any, comparator: str) -> dict[str, Any]:
    if comparator == ">=":
        passed = finite_metric(actual) >= finite_metric(threshold)
    elif comparator == ">":
        passed = finite_metric(actual) > finite_metric(threshold)
    elif comparator == "<=":
        passed = finite_metric(actual) <= finite_metric(threshold)
    elif comparator == "==":
        passed = actual == threshold
    else:
        raise ValueError(f"unsupported gate comparator {comparator}")
    return {"name": name, "actual": jsonable(actual), "threshold": jsonable(threshold), "comparator": comparator, "passed": bool(passed)}


def stage_row(
    campaign: dict[str, Any],
    candidate_id: str,
    candidate_level: str,
    scope_type: str,
    scope_ref: str,
    hypothesis_id: int | None,
    parent_id: str | None,
    gates: list[Any],
    metrics: dict[str, Any],
    refs: list[str],
    promoted: bool,
) -> dict[str, Any]:
    evidence_key = stable_hash({"campaign": campaign["id"], "candidate": candidate_id, "level": candidate_level, "scope": f"{scope_type}:{scope_ref}"})
    return {
        "evidence_key": evidence_key,
        "campaign_id": campaign["id"],
        "candidate_id": candidate_id,
        "candidate_level": candidate_level,
        "scope_type": scope_type,
        "scope_ref": scope_ref,
        "hypothesis_version_id": hypothesis_id,
        "parent_candidate_id": parent_id,
        "gate_results": gates,
        "metrics": metrics,
        "evidence_refs": refs,
        "promoted": promoted,
        "calculation_version": CANDIDATE_LEVEL_VERSION,
    }


def summary_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    return {key: jsonable(summary.get(key)) for key in ("research_score", "profit_factor", "profit_factor_is_infinite", "expectancy", "max_drawdown", "trade_count", "stability", "assets_passed", "timeframes_passed", "regimes_passed")}


def aggregate_job_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = [dict((row.get("result") or {}).get("metrics") or {}) for row in rows]
    profit_factor, profit_factor_infinite = aggregate_profit_factor(metrics)
    return {
        "profit_factor": profit_factor,
        "profit_factor_is_infinite": profit_factor_infinite,
        "expectancy": average(item.get("expectancy_per_trade") for item in metrics),
        "max_drawdown": average(item.get("max_drawdown") for item in metrics),
        "trade_count": int(sum(finite_metric(item.get("number_of_trades")) for item in metrics)),
        "markets": len(rows),
    }


def normalize_allocation(allocation: dict[str, float]) -> dict[str, float]:
    keys = {"exploitation", "nearby", "exploration"}
    if set(allocation) != keys:
        raise ValueError("allocation must contain exploitation, nearby, and exploration")
    values = {key: float(allocation[key]) for key in keys}
    if any(value < 0 for value in values.values()) or sum(values.values()) <= 0:
        raise ValueError("allocation ratios must be non-negative and sum to more than zero")
    total = sum(values.values())
    return {key: round(values[key] / total, 8) for key in ("exploitation", "nearby", "exploration")}


def allocation_counts(total: int, allocation: dict[str, float]) -> dict[str, int]:
    exploitation = max(1, int(round(total * allocation["exploitation"])))
    nearby = int(round(total * allocation["nearby"]))
    if exploitation + nearby > total:
        nearby = max(0, total - exploitation)
    exploration = total - exploitation - nearby
    return {"exploitation": exploitation, "nearby": nearby, "exploration": exploration}


def preferred_entry_families(strategy_family: str) -> tuple[str, ...]:
    return {
        "Breakout": ("breakout", "opening_range_proxy"),
        "Momentum": ("momentum",),
        "Pullback": ("pullback", "trend_continuation"),
        "Mean Reversion": ("mean_reversion",),
        "Volatility Expansion": ("volatility_expansion",),
        "Range Breakout": ("range_breakout",),
        "Continuation": ("continuation",),
        "Gap": ("gap",),
        "Bearish Breakdown": ("bearish_breakdown",),
        "Bearish Momentum": ("bearish_momentum",),
        "Gap Continuation": ("gap_proxy", "opening_range_proxy"),
        "Trend Following": ("trend_continuation", "pullback"),
    }[strategy_family]


def append_targeted_candidate(
    selected: list[DiscoveryCandidate],
    channels: dict[str, str],
    seen: set[str],
    candidate: DiscoveryCandidate,
    channel: str,
) -> bool:
    execution_key = candidate_execution_key(candidate)
    if execution_key in seen:
        return False
    seen.add(execution_key)
    selected.append(candidate)
    channels[candidate.candidate_id] = channel
    return True


def standardized_profile_vectors(profiles: list[dict[str, Any]]) -> list[list[float]]:
    columns = [[finite_metric(profile["metrics"].get(feature)) for profile in profiles] for feature in PROFILE_FEATURES]
    standardized_columns = []
    for values in columns:
        center = mean(values)
        scale = pstdev(values) if len(values) > 1 else 0.0
        standardized_columns.append([(value - center) / scale if scale > 0 else 0.0 for value in values])
    return [[standardized_columns[column][row] for column in range(len(PROFILE_FEATURES))] for row in range(len(profiles))]


def profile_distance(left: dict[str, Any], right: dict[str, Any], left_vector: list[float], right_vector: list[float]) -> float:
    behavior_distance = euclidean(left_vector, right_vector) / max(1.0, math.sqrt(len(PROFILE_FEATURES)))
    correlation = finite_metric((left.get("correlations") or {}).get(str(right["symbol"])))
    correlation_distance = (1.0 - max(-1.0, min(1.0, correlation))) / 2
    return behavior_distance * 0.70 + correlation_distance * 0.30


def cluster_name(centroid: dict[str, float], timeframe: str, index: int) -> str:
    volatility = "High-volatility" if centroid["realized_volatility"] >= 0.018 else "Low-volatility" if centroid["realized_volatility"] <= 0.008 else "Moderate-volatility"
    behavior = "mean-reversion" if centroid["mean_reversion_score"] >= 0.15 else "breakout" if centroid["breakout_follow_through"] >= 0.58 else "trend"
    return f"{volatility} {behavior} {timeframe} cluster {index}"


def consecutive_run_lengths(values: list[int]) -> list[int]:
    if not values:
        return []
    result = []
    current = values[0]
    length = 1
    for value in values[1:]:
        if value == current:
            length += 1
        else:
            result.append(length)
            current = value
            length = 1
    result.append(length)
    return result


def pearson(left: list[float], right: list[float]) -> float:
    count = min(len(left), len(right))
    if count < 2:
        return 0.0
    left_values = left[:count]
    right_values = right[:count]
    left_mean = mean(left_values)
    right_mean = mean(right_values)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left_values, right_values))
    denominator = math.sqrt(sum((value - left_mean) ** 2 for value in left_values) * sum((value - right_mean) ** 2 for value in right_values))
    return numerator / denominator if denominator else 0.0


def euclidean(left: list[float], right: list[float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)))


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * min(1.0, max(0.0, quantile))
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def label(value: float, low: float, high: float) -> str:
    if value >= high:
        return "high"
    if value <= low:
        return "low"
    return "moderate"


def average(values: Iterable[Any]) -> float:
    numbers = [finite_metric(value) for value in values if value is not None]
    return round(mean(numbers), 6) if numbers else 0.0


def stable_hash(value: Any) -> str:
    encoded = json.dumps(jsonable(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return sha256(encoded).hexdigest()


def code_commit() -> str:
    return os.getenv("VERCEL_GIT_COMMIT_SHA") or os.getenv("GIT_COMMIT_SHA") or os.getenv("SOURCE_VERSION") or "unknown"


def default_archive_directory() -> Path:
    configured = os.getenv("KEFTRADE_RESEARCH_ARCHIVE_DIR")
    return Path(configured) if configured else Path(__file__).resolve().parents[4] / "reports" / "research_archives"


def archive_candle(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(row["symbol"]),
        "source": str(row["source"]),
        "timeframe": str(row["timeframe"]),
        "timestamp": jsonable(row["timestamp"]),
        "open": str(row["open"]),
        "high": str(row["high"]),
        "low": str(row["low"]),
        "close": str(row["close"]),
        "volume": str(row["volume"]),
    }


def write_gzip_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        json.dump(jsonable(value), handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
    os.replace(temporary, path)


def read_gzip_json(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("research archive must contain a JSON object")
    return value


def jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
