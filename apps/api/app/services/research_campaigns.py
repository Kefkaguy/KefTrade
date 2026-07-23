from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import UTC, date, datetime, time as dt_time, timedelta
from decimal import Decimal
from hashlib import sha256
import os
import socket
from threading import Lock, Thread
import time
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.observability import elapsed_ms, log_event, log_exception
from app.settings import settings
from app.services.evidence_alerts import create_evidence_alert
from app.services.backtester import build_market_arrays, combine_candles_features
from app.services.features import load_candles
from app.services.regimes import load_regimes, sync_market_regimes
from app.services.research_command_center_cache import clear_command_center_cache
from app.services.research_learning import learn_from_completed_campaign, research_generation_guidance, score_candidate_for_guidance
from app.services.strategy_discovery import (
    SAFETY_STATEMENT,
    DiscoveryCandidate,
    candidate_execution_key,
    canonical_candidate_key,
    evaluate_candidate,
    generate_balanced_discovery_candidates,
    generate_discovery_candidates,
    jsonable,
)
from app.services.strategy_research import (
    aggregate_profit_factor,
    build_context_by_time,
    finite_metric,
    profit_factor_passes,
    validation_profit_factor,
)


CAMPAIGN_VERSION = "large_scale_research_campaign_v1"
QUALITY_FIRST_CAMPAIGN_VERSION = "phase_9_7_quality_first_campaign_v1"
TRANSFER_SAMPLE_SIZE_CAMPAIGN_VERSION = "phase_9_8_transferability_sample_size_v1"
OVERFIT_REGIME_ROBUSTNESS_CAMPAIGN_VERSION = "phase_9_9_overfit_regime_robustness_v1"
SINGLE_ASSET_GENERALIZATION_CAMPAIGN_VERSION = "phase_9_10_single_asset_generalization_v1"
STRATEGY_REDESIGN_CAMPAIGN_VERSION = "phase_9_11_strategy_redesign_v1"
VOLATILITY_ADAPTIVE_RELATIVE_STRENGTH_CAMPAIGN_VERSION = "phase_9_12_volatility_adaptive_relative_strength_v1"
DEFAULT_CAMPAIGN_TIMEFRAMES = ("1h", "4h", "1d")
WORKER_VERSION = "campaign_worker_v1"
DEFAULT_BATCH_SIZE = 1000
MIN_CAMPAIGN_CANDLES = 120
MIN_CAMPAIGN_FEATURES = 80
DEFAULT_SCHEDULING_CONFIG: dict[str, Any] = {
    "mode": "manual",
    "batch_size": 25,
    "max_jobs_per_cycle": 50,
    "max_concurrent_workers": 1,
    "retry_limit": 3,
    "retry_backoff_seconds": 300,
    "worker_lease_seconds": 900,
    "execution_window_utc": None,
    "daily_experiment_budget": 250,
    "max_concurrent_backtests": 1,
    "max_concurrent_data_requests": 2,
    "provider_rate_limits": {},
    "global_daily_job_limit": 1000,
    "job_timeout_seconds": 1200,
    "max_generated_candidates": 5000,
    "max_database_queue_depth": 100000,
    "target_workers": 0,
}
DEFAULT_FORWARD_THRESHOLDS: dict[str, Any] = {
    "minimum_active_paper_days": 5,
    "minimum_closed_trades": 5,
    "minimum_paper_profit_factor": 1.1,
    "minimum_paper_expectancy": 0,
    "maximum_paper_drawdown": 0.15,
    "maximum_execution_error_rate": 0.05,
    "maximum_stale_data_block_rate": 0.20,
}
_CAMPAIGN_SCHEMA_LOCK = Lock()
_CAMPAIGN_SCHEMA_READY = False
_UNIVERSE_SCHEMA_LOCK = Lock()
_UNIVERSE_SCHEMA_READY = False
_PARALLEL_POOLS_LOCK = Lock()
_PARALLEL_POOLS: dict[int, dict[str, Any]] = {}
DRIFT_THRESHOLDS: dict[str, dict[str, float]] = {
    "profit_factor": {"warning": 0.30, "severe": 0.55},
    "expectancy": {"warning": 0.35, "severe": 0.65},
    "win_rate": {"warning": 0.20, "severe": 0.35},
    "drawdown": {"warning": 0.50, "severe": 1.00},
    "trade_frequency": {"warning": 0.50, "severe": 0.80},
    "average_holding_period": {"warning": 0.50, "severe": 0.80},
    "slippage": {"warning": 0.50, "severe": 1.00},
}

DEFAULT_UNIVERSES: tuple[dict[str, Any], ...] = (
    {
        "universe_key": "research_core_ten",
        "name": "Research Core Ten",
        "description": "Ten liquid technology leaders and broad-market ETFs for measured profiling and cluster-specific research.",
        "assets": ["TSLA", "NVDA", "AAPL", "MSFT", "AMD", "META", "GOOGL", "AMZN", "SPY", "QQQ"],
        "default_timeframes": ["1h", "4h"],
        "metadata": {"asset_class": "equity_and_etf", "source": "reproducible_research_architecture_v1", "purpose": "asset_intelligence"},
    },
    {
        "universe_key": "sp500_leaders",
        "name": "S&P 500 Leaders",
        "description": "Configurable large-cap equity research universe seeded with liquid S&P 500 leaders.",
        "assets": ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AVGO", "TSLA", "JPM", "LLY"],
        "default_timeframes": ["1h", "4h", "1d"],
        "metadata": {"asset_class": "equity", "source": CAMPAIGN_VERSION},
    },
    {
        "universe_key": "nasdaq100_leaders",
        "name": "Nasdaq 100 Leaders",
        "description": "Configurable growth and technology equity universe.",
        "assets": ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AVGO", "COST", "NFLX", "AMD"],
        "default_timeframes": ["1h", "4h", "1d"],
        "metadata": {"asset_class": "equity", "source": CAMPAIGN_VERSION},
    },
    {
        "universe_key": "major_etfs",
        "name": "Major ETFs",
        "description": "Configurable ETF universe for broad market and sector validation.",
        "assets": ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "TLT", "GLD", "VNQ"],
        "default_timeframes": ["1h", "4h", "1d"],
        "metadata": {"asset_class": "etf", "source": CAMPAIGN_VERSION},
    },
    {
        "universe_key": "large_cap_stocks",
        "name": "Large-Cap Stocks",
        "description": "Configurable liquid large-cap stock universe.",
        "assets": ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "BRK.B", "JPM", "V", "UNH"],
        "default_timeframes": ["1h", "4h", "1d"],
        "metadata": {"asset_class": "equity", "source": CAMPAIGN_VERSION},
    },
    {
        "universe_key": "crypto_pairs",
        "name": "Crypto Pairs",
        "description": "Configurable crypto research universe retained for development and cross-market checks.",
        "assets": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT"],
        "default_timeframes": ["1h", "4h"],
        "metadata": {"asset_class": "crypto", "source": CAMPAIGN_VERSION},
    },
)


def _ensure_campaign_tables(conn: psycopg.Connection) -> None:
    return None
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_universes (
            id BIGSERIAL PRIMARY KEY,
            universe_key TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            assets JSONB NOT NULL,
            default_timeframes JSONB NOT NULL DEFAULT '[]'::jsonb,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT research_universes_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_campaigns (
            id BIGSERIAL PRIMARY KEY,
            campaign_key TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            universe_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            requested_candidates INTEGER NOT NULL,
            queued_jobs INTEGER NOT NULL DEFAULT 0,
            completed_jobs INTEGER NOT NULL DEFAULT 0,
            failed_jobs INTEGER NOT NULL DEFAULT 0,
            rejected_candidates INTEGER NOT NULL DEFAULT 0,
            promoted_candidates INTEGER NOT NULL DEFAULT 0,
            analytics JSONB NOT NULL DEFAULT '{}'::jsonb,
            controls JSONB NOT NULL DEFAULT '{}'::jsonb,
            safety_statement TEXT NOT NULL,
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            canceled_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT research_campaigns_status_check CHECK (status IN ('queued', 'running', 'paused', 'completed', 'canceled', 'failed')),
            CONSTRAINT research_campaigns_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """
    )
    conn.execute(
        """
        ALTER TABLE research_campaigns
            ADD COLUMN IF NOT EXISTS scheduling_config JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS last_scheduler_cycle_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS next_scheduler_cycle_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS daily_jobs_executed INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS daily_budget_date DATE,
            ADD COLUMN IF NOT EXISTS target_workers INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS execution_status TEXT NOT NULL DEFAULT 'idle',
            ADD COLUMN IF NOT EXISTS execution_updated_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS dataset_id BIGINT,
            ADD COLUMN IF NOT EXISTS dataset_mode TEXT,
            ADD COLUMN IF NOT EXISTS generator_version TEXT
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_campaign_jobs (
            id BIGSERIAL PRIMARY KEY,
            campaign_id BIGINT NOT NULL REFERENCES research_campaigns(id) ON DELETE CASCADE,
            job_key TEXT NOT NULL UNIQUE,
            candidate_id TEXT NOT NULL,
            family_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            candidate JSONB NOT NULL,
            result JSONB NOT NULL DEFAULT '{}'::jsonb,
            validation_score DOUBLE PRECISION NOT NULL DEFAULT 0,
            consistency_score DOUBLE PRECISION NOT NULL DEFAULT 0,
            failure_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
            attempts INTEGER NOT NULL DEFAULT 0,
            latest_error TEXT,
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT research_campaign_jobs_status_check CHECK (status IN ('queued', 'running', 'completed', 'rejected', 'promoted', 'failed', 'canceled', 'blocked_data', 'deferred_rate_limit', 'retrying')),
            CONSTRAINT research_campaign_jobs_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """
    )
    conn.execute(
        """
        ALTER TABLE research_campaign_jobs
            ADD COLUMN IF NOT EXISTS worker_id TEXT,
            ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS failure_classification TEXT,
            ADD COLUMN IF NOT EXISTS deferred_until TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS blocked_reason TEXT,
            ADD COLUMN IF NOT EXISTS execution_runtime_ms INTEGER,
            ADD COLUMN IF NOT EXISTS execution_profile JSONB NOT NULL DEFAULT '{}'::jsonb
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS elite_research_candidates (
            id BIGSERIAL PRIMARY KEY,
            campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
            candidate_id TEXT NOT NULL,
            family_id TEXT NOT NULL,
            strategy_name TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            research_score DOUBLE PRECISION NOT NULL,
            profit_factor DOUBLE PRECISION NOT NULL DEFAULT 0,
            expectancy DOUBLE PRECISION NOT NULL DEFAULT 0,
            max_drawdown DOUBLE PRECISION NOT NULL DEFAULT 0,
            trade_count INTEGER NOT NULL DEFAULT 0,
            stability DOUBLE PRECISION NOT NULL DEFAULT 0,
            assets_passed INTEGER NOT NULL DEFAULT 0,
            timeframes_passed INTEGER NOT NULL DEFAULT 0,
            regimes_passed INTEGER NOT NULL DEFAULT 0,
            validation_history JSONB NOT NULL,
            paper_performance JSONB NOT NULL DEFAULT '{}'::jsonb,
            promoted_to_paper_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT elite_research_candidates_unique UNIQUE(candidate_id, campaign_id),
            CONSTRAINT elite_research_candidates_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """
    )
    conn.execute(
        """
        ALTER TABLE elite_research_candidates
            ADD COLUMN IF NOT EXISTS forward_validation_state TEXT NOT NULL DEFAULT 'awaiting_paper_deployment',
            ADD COLUMN IF NOT EXISTS forward_validation_thresholds JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS forward_validation_updated_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS drift_status TEXT NOT NULL DEFAULT 'normal'
        """
    )
    ensure_worker_tables(conn)


def ensure_worker_tables(conn: psycopg.Connection) -> None:
    return None
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_campaign_scheduler (
            id BOOLEAN PRIMARY KEY DEFAULT TRUE,
            enabled BOOLEAN NOT NULL DEFAULT FALSE,
            cadence_seconds INTEGER NOT NULL DEFAULT 300,
            global_daily_job_limit INTEGER NOT NULL DEFAULT 1000,
            max_concurrent_workers INTEGER NOT NULL DEFAULT 1,
            max_concurrent_backtests INTEGER NOT NULL DEFAULT 1,
            max_concurrent_data_requests INTEGER NOT NULL DEFAULT 2,
            max_database_queue_depth INTEGER NOT NULL DEFAULT 100000,
            provider_rate_limits JSONB NOT NULL DEFAULT '{}'::jsonb,
            last_cycle_at TIMESTAMPTZ,
            next_cycle_at TIMESTAMPTZ,
            latest_result TEXT,
            latest_error TEXT,
            is_running BOOLEAN NOT NULL DEFAULT FALSE,
            running_since TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT research_campaign_scheduler_singleton CHECK (id = TRUE),
            CONSTRAINT research_campaign_scheduler_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """
    )
    conn.execute("INSERT INTO research_campaign_scheduler(id, enabled, simulation_only) VALUES (TRUE, FALSE, TRUE) ON CONFLICT(id) DO NOTHING")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_campaign_worker_cycles (
            id BIGSERIAL PRIMARY KEY,
            worker_id TEXT NOT NULL,
            campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
            status TEXT NOT NULL,
            claimed_jobs INTEGER NOT NULL DEFAULT 0,
            completed_jobs INTEGER NOT NULL DEFAULT 0,
            deferred_jobs INTEGER NOT NULL DEFAULT 0,
            blocked_jobs INTEGER NOT NULL DEFAULT 0,
            failed_jobs INTEGER NOT NULL DEFAULT 0,
            result JSONB NOT NULL DEFAULT '{}'::jsonb,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ,
            heartbeat_at TIMESTAMPTZ,
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT research_campaign_worker_cycles_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS elite_candidate_paper_rollups (
            id BIGSERIAL PRIMARY KEY,
            elite_candidate_id BIGINT NOT NULL REFERENCES elite_research_candidates(id) ON DELETE CASCADE,
            candidate_id TEXT NOT NULL,
            campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
            rollup_key TEXT NOT NULL UNIQUE,
            metrics JSONB NOT NULL,
            forward_validation_state TEXT NOT NULL,
            thresholds JSONB NOT NULL,
            calculated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT elite_candidate_paper_rollups_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS elite_candidate_evidence_drift (
            id BIGSERIAL PRIMARY KEY,
            elite_candidate_id BIGINT NOT NULL REFERENCES elite_research_candidates(id) ON DELETE CASCADE,
            candidate_id TEXT NOT NULL,
            campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
            drift_key TEXT NOT NULL UNIQUE,
            metric_name TEXT NOT NULL,
            historical_value DOUBLE PRECISION,
            paper_value DOUBLE PRECISION,
            absolute_difference DOUBLE PRECISION,
            percentage_difference DOUBLE PRECISION,
            drift_classification TEXT NOT NULL,
            detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT elite_candidate_evidence_drift_classification_check CHECK (
                drift_classification IN ('normal', 'warning', 'severe', 'insufficient_forward_sample')
            ),
            CONSTRAINT elite_candidate_evidence_drift_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """
    )
    ensure_operations_tables(conn)


def ensure_operations_tables(conn: psycopg.Connection) -> None:
    return None
    conn.execute(
        """
        ALTER TABLE research_campaign_jobs
            ADD COLUMN IF NOT EXISTS batch_id BIGINT,
            ADD COLUMN IF NOT EXISTS strategy_family TEXT,
            ADD COLUMN IF NOT EXISTS provider_latency_ms INTEGER,
            ADD COLUMN IF NOT EXISTS database_latency_ms INTEGER,
            ADD COLUMN IF NOT EXISTS recovery_classification TEXT,
            ADD COLUMN IF NOT EXISTS original_worker_id TEXT,
            ADD COLUMN IF NOT EXISTS original_lease_expires_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS recovery_worker_id TEXT,
            ADD COLUMN IF NOT EXISTS recovered_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS execution_resumed BOOLEAN,
            ADD COLUMN IF NOT EXISTS failure_history JSONB NOT NULL DEFAULT '[]'::jsonb
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_campaign_workers (
            worker_id TEXT PRIMARY KEY,
            campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
            process_id TEXT,
            hostname TEXT,
            status TEXT NOT NULL DEFAULT 'starting',
            registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            current_job_id BIGINT,
            stopped_at TIMESTAMPTZ,
            latest_cycle_at TIMESTAMPTZ,
            latest_error TEXT,
            processed_jobs INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT research_campaign_workers_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """
    )
    conn.execute(
        """
        ALTER TABLE research_campaign_workers
            ADD COLUMN IF NOT EXISTS campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ADD COLUMN IF NOT EXISTS current_job_id BIGINT,
            ADD COLUMN IF NOT EXISTS error_count INTEGER NOT NULL DEFAULT 0
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_campaign_batches (
            id BIGSERIAL PRIMARY KEY,
            campaign_id BIGINT NOT NULL REFERENCES research_campaigns(id) ON DELETE CASCADE,
            batch_key TEXT NOT NULL UNIQUE,
            batch_number INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            job_count INTEGER NOT NULL DEFAULT 0,
            completed_jobs INTEGER NOT NULL DEFAULT 0,
            failed_jobs INTEGER NOT NULL DEFAULT 0,
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT research_campaign_batches_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_campaign_analytics_snapshots (
            id BIGSERIAL PRIMARY KEY,
            campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE CASCADE,
            snapshot_key TEXT NOT NULL UNIQUE,
            analytics JSONB NOT NULL,
            strategy_family_intelligence JSONB NOT NULL DEFAULT '[]'::jsonb,
            asset_intelligence JSONB NOT NULL DEFAULT '[]'::jsonb,
            timeframe_intelligence JSONB NOT NULL DEFAULT '[]'::jsonb,
            heatmaps JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT research_campaign_analytics_snapshots_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_command_center_snapshots (
            id BIGSERIAL PRIMARY KEY,
            snapshot_key TEXT NOT NULL UNIQUE,
            payload JSONB NOT NULL,
            campaign_count INTEGER NOT NULL DEFAULT 0,
            completed_campaign_count INTEGER NOT NULL DEFAULT 0,
            calculation_version TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT research_command_center_snapshots_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_campaign_reports (
            id BIGSERIAL PRIMARY KEY,
            campaign_id BIGINT NOT NULL REFERENCES research_campaigns(id) ON DELETE CASCADE,
            report_key TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            summary JSONB NOT NULL,
            recommendations JSONB NOT NULL DEFAULT '[]'::jsonb,
            markdown_report TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT research_campaign_reports_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """
    )


def ensure_campaign_tables(conn: psycopg.Connection) -> None:
    global _CAMPAIGN_SCHEMA_READY
    is_real_connection = conn.__class__.__module__.startswith("psycopg")
    if is_real_connection and _CAMPAIGN_SCHEMA_READY:
        return
    with _CAMPAIGN_SCHEMA_LOCK:
        if is_real_connection and _CAMPAIGN_SCHEMA_READY:
            return
        _ensure_campaign_tables(conn)
        if is_real_connection:
            conn.commit()
            _CAMPAIGN_SCHEMA_READY = True


def ensure_universe_table(conn: psycopg.Connection) -> None:
    return None
    global _UNIVERSE_SCHEMA_READY
    is_real_connection = conn.__class__.__module__.startswith("psycopg")
    if is_real_connection and (_UNIVERSE_SCHEMA_READY or _CAMPAIGN_SCHEMA_READY):
        return
    with _UNIVERSE_SCHEMA_LOCK:
        if is_real_connection and (_UNIVERSE_SCHEMA_READY or _CAMPAIGN_SCHEMA_READY):
            return
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS research_universes (
                id BIGSERIAL PRIMARY KEY,
                universe_key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                assets JSONB NOT NULL,
                default_timeframes JSONB NOT NULL DEFAULT '[]'::jsonb,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
                CONSTRAINT research_universes_simulation_only_check CHECK (simulation_only = TRUE)
            )
            """
        )
        if is_real_connection:
            conn.commit()
            _UNIVERSE_SCHEMA_READY = True


def seed_default_universes(conn: psycopg.Connection) -> None:
    ensure_universe_table(conn)
    for universe in DEFAULT_UNIVERSES:
        conn.execute(
            """
            INSERT INTO research_universes(universe_key, name, description, assets, default_timeframes, metadata, simulation_only)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
            ON CONFLICT(universe_key) DO UPDATE
            SET name = EXCLUDED.name,
                description = EXCLUDED.description,
                updated_at = NOW()
            """,
            (
                universe["universe_key"],
                universe["name"],
                universe["description"],
                Jsonb(universe["assets"]),
                Jsonb(universe["default_timeframes"]),
                Jsonb(universe["metadata"]),
            ),
        )


def list_research_universes(conn: psycopg.Connection) -> dict[str, Any]:
    seed_default_universes(conn)
    rows = conn.execute(
        """
        SELECT universe_key, name, description, assets, default_timeframes, metadata, is_active, updated_at
        FROM research_universes
        WHERE is_active = TRUE AND simulation_only = TRUE
        ORDER BY universe_key
        """
    ).fetchall()
    conn.commit()
    return {"universes": [jsonable(dict(row)) for row in rows], "campaign_version": CAMPAIGN_VERSION, "simulation_only": True}


def upsert_research_universe(
    conn: psycopg.Connection,
    *,
    universe_key: str,
    name: str,
    description: str,
    assets: list[str],
    default_timeframes: list[str],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    normalized_assets = sorted({asset.strip().upper() for asset in assets if asset.strip()})
    normalized_timeframes = [timeframe.strip() for timeframe in default_timeframes if timeframe.strip()]
    if not normalized_assets:
        raise ValueError("research universe requires at least one asset")
    if not normalized_timeframes:
        raise ValueError("research universe requires at least one timeframe")
    row = conn.execute(
        """
        INSERT INTO research_universes(universe_key, name, description, assets, default_timeframes, metadata, simulation_only)
        VALUES (%s, %s, %s, %s, %s, %s, TRUE)
        ON CONFLICT(universe_key) DO UPDATE
        SET name = EXCLUDED.name,
            description = EXCLUDED.description,
            assets = EXCLUDED.assets,
            default_timeframes = EXCLUDED.default_timeframes,
            metadata = EXCLUDED.metadata,
            is_active = TRUE,
            updated_at = NOW()
        RETURNING universe_key, name, description, assets, default_timeframes, metadata, is_active, updated_at
        """,
        (universe_key, name, description, Jsonb(normalized_assets), Jsonb(normalized_timeframes), Jsonb(metadata or {})),
    ).fetchone()
    conn.commit()
    return jsonable(dict(row))


def campaign_generation_candidates(conn: psycopg.Connection, *, universe_key: str, max_candidates: int) -> tuple[list[DiscoveryCandidate], dict[str, Any]]:
    if universe_key == "research_core_ten":
        return elite_focused_research_core_candidates(conn, max_candidates=max_candidates)
    guidance = research_generation_guidance(conn)
    generated = generate_balanced_discovery_candidates(max_candidates=max_candidates * 3)
    ranked = rank_candidates_with_learning(generated, guidance)
    candidates = dedupe_candidates_by_execution_key(ranked, max_candidates)
    return candidates, {
        "mode": "global_learning_guided_balanced" if guidance.get("available") else "balanced",
        "attempted_candidate_generations": len(generated),
        "duplicates_prevented": max(0, len(generated) - len(candidates)),
        "jobs_skipped": max(0, max_candidates - len(candidates)),
        "channels": {"learning_guided": len(candidates)} if guidance.get("available") else {"balanced": len(candidates)},
        "learning_guidance": {
            "available": bool(guidance.get("available")),
            "calculation_version": guidance.get("calculation_version"),
            "policy": "rank generation candidates by accumulated evidence; keep deterministic exploration; validation thresholds unchanged",
        },
    }


def rank_candidates_with_learning(candidates: list[DiscoveryCandidate], guidance: dict[str, Any]) -> list[DiscoveryCandidate]:
    if not guidance.get("available"):
        return candidates
    indexed = list(enumerate(candidates))
    indexed.sort(key=lambda item: (score_candidate_for_guidance(item[1], guidance), -item[0], item[1].candidate_id), reverse=True)
    return [candidate for _index, candidate in indexed]


def elite_focused_research_core_candidates(conn: psycopg.Connection, *, max_candidates: int) -> tuple[list[DiscoveryCandidate], dict[str, Any]]:
    attempted = max(max_candidates * 4, 250)
    base = generate_balanced_discovery_candidates(max_candidates=attempted)
    parent_evidence = load_research_core_parent_evidence(conn, limit=max_candidates)
    parents = [row["candidate"] for row in parent_evidence]
    survivor_quota = int(max_candidates * 0.55)
    repair_quota = int(max_candidates * 0.25)
    adjacent_quota = int(max_candidates * 0.20)
    exploratory_quota = max_candidates - survivor_quota - repair_quota - adjacent_quota
    selected: list[DiscoveryCandidate] = []
    selected.extend(near_pass_repair_candidates(parent_evidence, survivor_quota + repair_quota))
    selected.extend(channel_candidates(base, adjacent_quota + max(0, survivor_quota + repair_quota - len(selected)), "adjacent", offset=len(selected)))
    selected.extend(channel_candidates(base, exploratory_quota, "exploratory", offset=len(selected) + adjacent_quota))
    if len(selected) < max_candidates:
        selected.extend(channel_candidates(base, max_candidates - len(selected), "exploratory_fill", offset=len(selected)))
    deduped = dedupe_candidates_by_execution_key(selected, max_candidates)
    if len(deduped) < max_candidates:
        deduped = dedupe_candidates_by_execution_key([*deduped, *channel_candidates(base, max_candidates, "exploratory_fill", offset=len(deduped))], max_candidates)
    channels = Counter(str(candidate.parameters.get("generation_channel") or "unknown") for candidate in deduped)
    return deduped, {
        "mode": "elite_focused_repair_55_25_20",
        "attempted_candidate_generations": len(selected),
        "duplicates_prevented": max(0, len(selected) - len(deduped)),
        "jobs_skipped": max(0, max_candidates - len(deduped)),
        "channels": dict(channels),
        "quota_policy": "near_pass_repair_then_adjacent_then_exploratory_without_execution_key_duplicates",
        "repair_policy": "profit_factor_trade_count_stability_and_cross_asset_mutations_without_gate_relaxation",
    }


def load_research_core_parent_evidence(conn: psycopg.Connection, *, limit: int) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT candidate, result, failure_reasons, symbol, timeframe, validation_score, status
            FROM research_campaign_jobs
            WHERE simulation_only = TRUE
              AND candidate IS NOT NULL
              AND status IN ('promoted', 'rejected', 'completed')
              AND (
                status = 'promoted'
                OR validation_score > 0
                OR COALESCE((result->'metrics'->>'expectancy_per_trade')::double precision, 0) > 0
                OR COALESCE((result->'metrics'->>'profit_factor')::double precision, 0) >= 1.0
              )
            ORDER BY validation_score DESC, updated_at DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    except Exception:
        safe_rollback(conn)
        rows = []
    evidence: list[dict[str, Any]] = []
    for row in rows:
        try:
            data = dict(row)
            evidence.append({**data, "candidate": candidate_from_payload(dict(data["candidate"]))})
        except Exception:
            continue
    return evidence


def near_pass_repair_candidates(parent_evidence: list[dict[str, Any]], count: int) -> list[DiscoveryCandidate]:
    if count <= 0:
        return []
    variants: list[DiscoveryCandidate] = []
    for evidence in parent_evidence:
        parent = evidence["candidate"]
        for channel, changes in repair_mutation_plan(evidence):
            variants.append(candidate_with_parameter_changes(parent, changes, channel))
            if len(variants) >= count:
                return variants
    return variants


def repair_mutation_plan(evidence: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    parent: DiscoveryCandidate = evidence["candidate"]
    params = parent.parameters
    result = evidence.get("result") or {}
    metrics = result.get("metrics") or {}
    failures = {str(reason) for reason in (evidence.get("failure_reasons") or result.get("failure_reasons") or [])}
    trade_count = finite_metric(metrics.get("number_of_trades"))
    profit_factor = validation_profit_factor({"result": result})
    expectancy = finite_metric(metrics.get("expectancy_per_trade"))
    base_entry_distance = float(params.get("entry_distance_to_ema20_max", 0.035))
    base_volume = float(params.get("volume_change_min", -0.25))
    base_rr = float(params.get("risk_reward", 2))
    base_holding = int(params.get("max_holding_bars") or 0)
    plan: list[tuple[str, dict[str, Any]]] = []

    if profit_factor >= 1.0 or "weak_profit_factor" in failures:
        plan.extend(
            [
                (
                    "profit_factor_repair",
                    {
                        "entry_distance_to_ema20_max": round(max(0.01, base_entry_distance * 0.72), 4),
                        "volume_change_min": round(max(base_volume, -0.05), 3),
                        "risk_reward": round(min(2.8, base_rr + 0.25), 2),
                        "block_sideways": True,
                        "sideways_distance_from_ema50_min": 0.012,
                    },
                ),
                (
                    "profit_factor_repair",
                    {
                        "entry_distance_to_ema20_max": round(max(0.012, base_entry_distance * 0.85), 4),
                        "risk_reward": round(min(3.0, base_rr + 0.4), 2),
                        "max_holding_bars": max(base_holding, 12),
                    },
                ),
            ]
        )
    if trade_count < 35 or "insufficient_trades" in failures or "projected_trade_count_below_gate" in failures:
        plan.extend(
            [
                (
                    "trade_count_repair",
                    {
                        "entry_distance_to_ema20_max": round(min(0.08, base_entry_distance * 1.35), 4),
                        "volume_change_min": round(min(base_volume, -0.18), 3),
                        "risk_reward": round(max(1.35, min(base_rr, 1.8)), 2),
                        "max_holding_bars": max(base_holding, 10),
                    },
                ),
                (
                    "trade_count_repair",
                    {
                        "frequency_screen_min_opportunities": 20,
                        "entry_distance_to_ema20_max": round(min(0.07, base_entry_distance * 1.2), 4),
                        "rsi_min": max(40, float(params.get("rsi_min", 55)) - 5),
                    },
                ),
            ]
        )
    if expectancy > 0:
        plan.append(
            (
                "cross_asset_robustness",
                {
                    "phase_9_11_require_4h_positive_returns": True,
                    "phase_9_11_require_4h_ema_positive": True,
                    "risk_reward": round(min(2.6, max(base_rr, 1.8)), 2),
                },
            )
        )
    plan.append(
        (
            "stability_repair",
            {
                "normal_volatility_min": 0.006,
                "normal_volatility_max": 0.025,
                "low_volatility_returns_5_min": 0.001,
                "max_holding_bars": max(base_holding, 14),
                "adaptive_volatility_profiles": True,
                "low_vol_risk_reward": round(max(1.35, min(base_rr, 1.7)), 2),
                "high_vol_risk_reward": round(min(2.8, max(base_rr, 2.0)), 2),
                "high_vol_volume_change_min": round(max(0.05, base_volume), 3),
            },
        )
    )
    return plan


def candidate_with_parameter_changes(parent: DiscoveryCandidate, changes: dict[str, Any], channel: str) -> DiscoveryCandidate:
    params = {**parent.parameters, **changes, "generation_channel": channel, "elite_repair_version": "elite_repair_v1"}
    key = canonical_candidate_key(parent.blocks, params, parent.candidate_id)
    return DiscoveryCandidate(
        candidate_id=f"sd_{sha256(key.encode()).hexdigest()[:14]}",
        family_id=parent.family_id,
        parent_candidate_id=parent.candidate_id,
        generation=parent.generation + 1,
        blocks=dict(parent.blocks),
        parameters=params,
        complexity=parent.complexity + 1,
        canonical_key=key,
    )


def channel_candidates(candidates: list[DiscoveryCandidate], count: int, channel: str, *, offset: int = 0) -> list[DiscoveryCandidate]:
    if count <= 0 or not candidates:
        return []
    result = []
    for candidate in candidates[offset:]:
        params = {**candidate.parameters, "generation_channel": channel}
        key = canonical_candidate_key(candidate.blocks, params, candidate.parent_candidate_id)
        result.append(
            DiscoveryCandidate(
                candidate_id=f"sd_{sha256(key.encode()).hexdigest()[:14]}",
                family_id=candidate.family_id,
                parent_candidate_id=candidate.parent_candidate_id,
                generation=candidate.generation,
                blocks=dict(candidate.blocks),
                parameters=params,
                complexity=candidate.complexity,
                canonical_key=key,
            )
        )
        if len(result) >= count:
            break
    return result


def dedupe_candidates_by_execution_key(candidates: list[DiscoveryCandidate], limit: int) -> list[DiscoveryCandidate]:
    selected: list[DiscoveryCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate_execution_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        selected.append(candidate)
        if len(selected) >= limit:
            break
    return selected


def scout_candidate_budget(max_candidates: int) -> int:
    return min(max_candidates, max(12, min(36, round(max_candidates * 0.12))))


def diverse_candidate_selection(candidates: list[DiscoveryCandidate], limit: int) -> list[DiscoveryCandidate]:
    """Round-robin families and reject candidates with nearly identical numeric inputs."""
    families: dict[str, list[DiscoveryCandidate]] = defaultdict(list)
    for candidate in dedupe_candidates_by_execution_key(candidates, len(candidates)):
        families[strategy_family_for_candidate(candidate)].append(candidate)
    selected: list[DiscoveryCandidate] = []
    while len(selected) < limit and any(families.values()):
        for family in sorted(families):
            while families[family]:
                candidate = families[family].pop(0)
                if all(candidate_parameter_distance(candidate, prior) >= 0.08 for prior in selected if strategy_family_for_candidate(prior) == family):
                    selected.append(candidate)
                    break
            if len(selected) >= limit:
                break
    return selected


def candidate_parameter_distance(left: DiscoveryCandidate, right: DiscoveryCandidate) -> float:
    keys = sorted(set(left.parameters) | set(right.parameters))
    executable = [key for key in keys if key not in {"generation_channel", "generation_stage", "hypothesis", "label", "name"}]
    differences: list[float] = []
    for key in executable:
        left_value = left.parameters.get(key)
        right_value = right.parameters.get(key)
        if isinstance(left_value, (int, float)) and not isinstance(left_value, bool) and isinstance(right_value, (int, float)) and not isinstance(right_value, bool):
            scale = max(abs(float(left_value)), abs(float(right_value)), 1.0)
            differences.append(min(1.0, abs(float(left_value) - float(right_value)) / scale))
        else:
            differences.append(0.0 if left_value == right_value else 1.0)
    return sum(differences) / len(differences) if differences else 0.0


def create_research_campaign(
    conn: psycopg.Connection,
    *,
    universe_key: str,
    name: str | None = None,
    max_candidates: int = 1000,
    asset_limit: int = 100,
    timeframes: list[str] | None = None,
    search_mode: str = "full",
    dataset_mode: str = "rolling",
    dataset_id: int | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    log_event("Research campaign launch requested", universe_key=universe_key, max_candidates=max_candidates, asset_limit=asset_limit, timeframes=timeframes)
    seed_default_universes(conn)
    universe = get_universe(conn, universe_key)
    assets = [str(asset).upper() for asset in (universe.get("assets") or [])][:asset_limit]
    selected_timeframes = list(timeframes or universe.get("default_timeframes") or DEFAULT_CAMPAIGN_TIMEFRAMES)
    dataset: dict[str, Any] | None = None
    if dataset_mode not in {"rolling", "reproducibility"}:
        raise ValueError("dataset_mode must be rolling or reproducibility")
    if dataset_mode == "reproducibility" or dataset_id is not None:
        from app.services.research_architecture import record_dataset_snapshot, verify_dataset_snapshot

        if dataset_id is None:
            dataset = record_dataset_snapshot(conn, assets=assets, timeframes=selected_timeframes, mode="reproducibility")
            dataset_id = int(dataset["id"])
        else:
            row = conn.execute("SELECT * FROM research_dataset_manifests WHERE id = %s", (dataset_id,)).fetchone()
            if not row:
                raise ValueError(f"research dataset {dataset_id} was not found")
            dataset = jsonable(dict(row))
        integrity = verify_dataset_snapshot(conn, dataset_id)
        if not integrity["passed"]:
            raise ValueError(f"research dataset {dataset_id} failed integrity verification")
        dataset_mode = str(dataset.get("mode") or "reproducibility")
    log_event("Job generation started", assets=len(assets), timeframes=len(selected_timeframes), strategies=max_candidates, expected_jobs=len(assets) * len(selected_timeframes) * max_candidates)
    if search_mode not in {"full", "scout_expand"}:
        raise ValueError("search_mode must be full or scout_expand")
    candidates, generation_metrics = campaign_generation_candidates(conn, universe_key=universe_key, max_candidates=max_candidates)
    queued_candidates = candidates
    scout_ids: list[str] = []
    if search_mode == "scout_expand":
        queued_candidates = diverse_candidate_selection(candidates, scout_candidate_budget(max_candidates))
        scout_ids = [candidate.candidate_id for candidate in queued_candidates]
    campaign_key = research_campaign_key(
        universe_key,
        assets,
        selected_timeframes,
        max_candidates,
        search_mode=search_mode,
        dataset_id=dataset_id,
    )
    campaign_name = name or f"{universe['name']} strategy discovery campaign"
    insert_started = time.perf_counter()
    log_event("Before INSERT research_campaigns", campaign_key=campaign_key, name=campaign_name)
    controls = Jsonb({
                "asset_limit": asset_limit,
                "timeframes": selected_timeframes,
                "campaign_version": CAMPAIGN_VERSION,
                "candidate_generation": "family_balanced_frequency_hypotheses_v1",
                "candidate_generation_mix": generation_metrics,
                "research_execution": {
                    "search_mode": search_mode,
                    "stage": "scout" if search_mode == "scout_expand" else "full",
                    "scout_candidate_count": len(queued_candidates) if search_mode == "scout_expand" else 0,
                    "scout_candidate_ids": scout_ids,
                    "full_candidate_budget": max_candidates,
                    "expanded_routes": [],
                    "expansion_jobs_created": 0,
                },
                "validation_policy": "All existing trade-count, quality, walk-forward, and cross-market gates remain unchanged.",
                "correlation_evidence": {
                    "required": dataset_id is not None,
                    "dataset_id": dataset_id,
                    "evidence_version": "aligned_marked_returns_v1",
                    "shared_across_all_jobs": dataset_id is not None,
                },
            })
    if dataset_id is None:
        row = conn.execute(
            """
            INSERT INTO research_campaigns(campaign_key, name, universe_key, status, requested_candidates, controls, scheduling_config, safety_statement, simulation_only)
            VALUES (%s, %s, %s, 'queued', %s, %s, %s, %s, TRUE)
            ON CONFLICT(campaign_key) DO UPDATE SET updated_at = NOW()
            RETURNING *
            """,
            (campaign_key, campaign_name, universe_key, max_candidates, controls, Jsonb(DEFAULT_SCHEDULING_CONFIG), SAFETY_STATEMENT),
        ).fetchone()
    else:
        row = conn.execute(
            """
            INSERT INTO research_campaigns(
                campaign_key, name, universe_key, status, requested_candidates, controls,
                scheduling_config, safety_statement, dataset_id, dataset_mode,
                immutable_config, generator_version, simulation_only
            )
            VALUES (%s, %s, %s, 'queued', %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            ON CONFLICT(campaign_key) DO UPDATE SET updated_at = NOW()
            RETURNING *
            """,
            (
                campaign_key, campaign_name, universe_key, max_candidates, controls,
                Jsonb(DEFAULT_SCHEDULING_CONFIG), SAFETY_STATEMENT, dataset_id, dataset_mode,
                Jsonb({
                    "dataset_id": dataset_id,
                    "dataset_content_hash": dataset.get("content_hash") if dataset else None,
                    "assets": assets,
                    "timeframes": selected_timeframes,
                    "candidate_generation": "family_balanced_frequency_hypotheses_v1",
                    "search_mode": search_mode,
                    "correlation_evidence_version": "aligned_marked_returns_v1",
                }),
                "portfolio_evidence_broad_v1",
            ),
        ).fetchone()
    log_event("After INSERT research_campaigns", elapsed_ms=elapsed_ms(insert_started), rows_affected=1 if row else 0, campaign_id=row["id"] if row else None)
    campaign_id = int(row["id"])
    created = queue_campaign_jobs(conn, campaign_id, queued_candidates, assets, selected_timeframes)
    if dataset_id is not None:
        conn.execute(
            "UPDATE research_campaign_jobs SET dataset_id = %s WHERE campaign_id = %s AND dataset_id IS NULL",
            (dataset_id, campaign_id),
        )
    update_campaign_counts(conn, campaign_id)
    log_event("Before COMMIT research_campaigns", campaign_id=campaign_id)
    conn.commit()
    log_event("After COMMIT research_campaigns", campaign_id=campaign_id, jobs_inserted=created)
    log_event("Campaign returned", campaign_id=campaign_id, elapsed_ms=elapsed_ms(started))
    return {
        "campaign": jsonable(dict(row)),
        "assets": assets,
        "timeframes": selected_timeframes,
        "candidates_generated": len(queued_candidates),
        "candidate_budget": len(candidates),
        "search_mode": search_mode,
        "dataset": dataset,
        "dataset_id": dataset_id,
        "dataset_mode": dataset_mode,
        "jobs_created": created,
        "campaign_version": CAMPAIGN_VERSION,
        "simulation_only": True,
        "safety": SAFETY_STATEMENT,
    }


def create_quality_first_research_campaign(
    conn: psycopg.Connection,
    *,
    source_campaign_id: int,
    name: str | None = None,
    max_variants_per_parent: int = 6,
    asset_limit: int = 4,
    timeframes: list[str] | None = None,
) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    source_campaign = get_campaign(conn, source_campaign_id)
    source_jobs = conn.execute(
        """
        SELECT *
        FROM research_campaign_jobs
        WHERE campaign_id = %s AND simulation_only = TRUE
        ORDER BY candidate_id, symbol, timeframe
        """,
        (source_campaign_id,),
    ).fetchall()
    blueprint = quality_first_campaign_blueprint(
        [dict(row) for row in source_jobs],
        max_variants_per_parent=max_variants_per_parent,
        asset_limit=asset_limit,
        timeframes=timeframes,
    )
    if not blueprint["candidates"]:
        raise ValueError("quality-first campaign requires at least one promoted or near-pass source candidate")
    campaign_key = quality_campaign_key(source_campaign_id, blueprint)
    campaign_name = name or f"Phase 9.7 quality-first follow-up for campaign {source_campaign_id}"
    row = conn.execute(
        """
        INSERT INTO research_campaigns(campaign_key, name, universe_key, status, requested_candidates, controls, scheduling_config, safety_statement, simulation_only)
        VALUES (%s, %s, %s, 'queued', %s, %s, %s, %s, TRUE)
        ON CONFLICT(campaign_key) DO UPDATE
        SET updated_at = NOW()
        RETURNING *
        """,
        (
            campaign_key,
            campaign_name,
            source_campaign["universe_key"],
            len(blueprint["candidates"]),
            Jsonb(
                {
                    "campaign_version": QUALITY_FIRST_CAMPAIGN_VERSION,
                    "source_campaign_id": source_campaign_id,
                    "objective": "Convert existing research candidates into elite candidates through targeted hypothesis testing.",
                    "candidate_quality_policy": "Queue parent confirmations and local mutations only; validation thresholds and evidence requirements are unchanged.",
                    "paper_deployment_policy": "Candidate-linked paper deployments are blocked until at least one elite_research_candidates row exists.",
                    "diagnostics": blueprint["diagnostics"],
                    "targeting": blueprint["targeting"],
                }
            ),
            Jsonb({**DEFAULT_SCHEDULING_CONFIG, "batch_size": min(12, DEFAULT_SCHEDULING_CONFIG["batch_size"]), "daily_experiment_budget": 60, "max_generated_candidates": len(blueprint["candidates"])}),
            SAFETY_STATEMENT,
        ),
    ).fetchone()
    campaign_id = int(row["id"])
    created = queue_campaign_jobs(conn, campaign_id, blueprint["candidates"], blueprint["targeting"]["assets"], blueprint["targeting"]["timeframes"])
    update_campaign_counts(conn, campaign_id)
    conn.commit()
    return {
        "campaign": jsonable(dict(row)),
        "source_campaign": {"id": source_campaign_id, "name": source_campaign["name"], "status": source_campaign["status"]},
        "diagnostics": blueprint["diagnostics"],
        "targeting": blueprint["targeting"],
        "candidates_generated": len(blueprint["candidates"]),
        "jobs_created": created,
        "campaign_version": QUALITY_FIRST_CAMPAIGN_VERSION,
        "simulation_only": True,
        "safety": SAFETY_STATEMENT,
    }


def quality_first_campaign_blueprint(
    jobs: list[dict[str, Any]],
    *,
    max_variants_per_parent: int = 6,
    asset_limit: int = 4,
    timeframes: list[str] | None = None,
) -> dict[str, Any]:
    diagnostics = phase_campaign_diagnostics(jobs)
    parent_rows = source_parent_rows(jobs)
    target_assets = strongest_quality_assets(jobs, parent_rows, asset_limit)
    target_timeframes = strongest_quality_timeframes(jobs, parent_rows, timeframes)
    candidates = targeted_quality_candidates(parent_rows, max_variants_per_parent=max_variants_per_parent)
    return {
        "diagnostics": diagnostics,
        "targeting": {
            "assets": target_assets,
            "timeframes": target_timeframes,
            "strategy_families": diagnostics["strongest_strategy_families"],
            "parameter_ranges": diagnostics["strongest_parameter_ranges"],
            "market_regimes": diagnostics["strongest_market_regimes"],
            "parents": [row["candidate_id"] for row in parent_rows],
        },
        "candidates": candidates,
        "simulation_only": True,
    }


def phase_campaign_diagnostics(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [row for row in jobs if row.get("status") in {"promoted", "rejected", "failed"}]
    candidate_ids = {str(row.get("candidate_id")) for row in jobs}
    candidates_with_single_market_pass = {str(row.get("candidate_id")) for row in jobs if row.get("status") == "promoted"}
    failure_counter: Counter[str] = Counter()
    for row in jobs:
        if row.get("status") not in {"rejected", "failed"}:
            continue
        reasons = list(row.get("failure_reasons") or [])
        if not reasons:
            reasons = infer_job_failure_reasons(row)
        for reason in reasons:
            failure_counter[normalize_failure_reason(reason)] += 1
    return {
        "source_jobs": len(jobs),
        "completed_jobs": len(completed),
        "generated_candidates": len(candidate_ids),
        "candidate_level_failures": max(0, len(candidate_ids) - len(candidates_with_single_market_pass)),
        "single_market_research_candidates": len(candidates_with_single_market_pass),
        "elite_candidates": 0,
        "primary_failure_reasons": [{"reason": reason, "count": count} for reason, count in failure_counter.most_common(10)],
        "why_candidates_failed": "The broad Phase 9.6 sweep produced too many over-restrictive combinations. Most jobs failed trade-count requirements first, then profit-factor and expectancy rules; the only surviving evidence was concentrated in AAPL 1h pullback variants, so no candidate passed cross-asset stability.",
        "strongest_strategy_families": grouped_quality_rank(jobs, "strategy_family")[:5],
        "strongest_assets": grouped_quality_rank(jobs, "symbol")[:8],
        "strongest_timeframes": grouped_quality_rank(jobs, "timeframe")[:4],
        "strongest_parameter_ranges": strongest_parameter_ranges(jobs),
        "strongest_market_regimes": strongest_market_regimes(jobs),
        "quality_campaign_design": "Retest the three single-market survivors and local deterministic variants across the strongest adjacent assets and the validated timeframe before any broad exploration.",
    }


def infer_job_failure_reasons(job: dict[str, Any]) -> list[str]:
    metrics = ((job.get("result") or {}).get("metrics") or {})
    reasons = []
    if finite_metric(metrics.get("number_of_trades")) < 30:
        reasons.append("insufficient_trades")
    if not profit_factor_passes(metrics, 1.2):
        reasons.append("weak_profit_factor")
    if finite_metric(metrics.get("expectancy_per_trade")) <= 0:
        reasons.append("poor_expectancy")
    if finite_metric(metrics.get("max_drawdown")) > 0.12:
        reasons.append("excessive_drawdown")
    return reasons or ["validation_rules_failed"]


def normalize_failure_reason(reason: Any) -> str:
    text = str(reason)
    lowered = text.lower()
    if "trade count" in lowered or "insufficient_trades" in lowered:
        return "insufficient_trades"
    if "profit factor" in lowered or "weak_profit_factor" in lowered:
        return "weak_profit_factor"
    if "expectancy" in lowered or "poor_expectancy" in lowered:
        return "poor_expectancy"
    if "drawdown" in lowered:
        return "excessive_drawdown"
    if "bull_trend" in lowered:
        return "fails_in_bull_trend"
    if "sideways" in lowered:
        return "fails_in_sideways"
    if "low_volatility" in lowered:
        return "fails_in_low_volatility"
    if "normal_volatility" in lowered:
        return "fails_in_normal_volatility"
    return text


def grouped_quality_rank(jobs: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in jobs:
        if row.get("status") in {"promoted", "rejected"}:
            grouped[str(row.get(field) or "unknown")].append(row)
    rows = []
    for name, items in grouped.items():
        metrics = [((row.get("result") or {}).get("metrics") or {}) for row in items]
        aggregate_pf, aggregate_pf_infinite = aggregate_profit_factor(metrics)
        promoted = sum(1 for row in items if row.get("status") == "promoted")
        rows.append(
            {
                "name": name,
                "tested": len(items),
                "single_market_passes": promoted,
                "pass_rate": round(promoted / len(items), 4) if items else 0,
                "average_profit_factor": aggregate_pf,
                "profit_factor_is_infinite": aggregate_pf_infinite,
                "average_expectancy": average(metric.get("expectancy_per_trade") for metric in metrics),
                "average_drawdown": average(metric.get("max_drawdown") for metric in metrics),
                "average_trades": average(metric.get("number_of_trades") for metric in metrics),
            }
        )
    return sorted(rows, key=lambda row: (row["single_market_passes"], row["average_profit_factor"], row["average_expectancy"], -row["average_drawdown"]), reverse=True)


def strongest_parameter_ranges(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    promoted = [row for row in jobs if row.get("status") == "promoted"]
    counters: dict[str, Counter[str]] = defaultdict(Counter)
    for row in promoted:
        params = dict((row.get("candidate") or {}).get("parameters") or {})
        for key in ("trend_fast", "trend_slow", "rsi_min", "risk_reward", "atr_multiplier", "volume_change_min", "entry_distance_to_ema20_max"):
            if key in params:
                counters[key][str(params[key])] += 1
    return [{"parameter": key, "values": [{"value": value, "count": count} for value, count in counter.most_common()]} for key, counter in sorted(counters.items())]


def strongest_market_regimes(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    regimes: Counter[str] = Counter()
    for row in jobs:
        if row.get("status") != "promoted":
            continue
        analysis = (row.get("result") or {}).get("regime_analysis") or {}
        for bucket in ("by_market_regime", "by_volatility_regime"):
            for item in analysis.get(bucket) or []:
                metrics = item.get("metrics") or {}
                if finite_metric(metrics.get("expectancy_per_trade")) > 0 and profit_factor_passes(metrics, 1):
                    regimes[str(item.get("regime") or item.get("condition") or "unknown")] += 1
    return [{"regime": regime, "supporting_passes": count} for regime, count in regimes.most_common(8)]


def source_parent_rows(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    promoted = [row for row in jobs if row.get("status") == "promoted"]
    if promoted:
        return best_row_per_candidate(promoted)
    near_pass = []
    for row in jobs:
        metrics = ((row.get("result") or {}).get("metrics") or {})
        if (
            profit_factor_passes(metrics, 1.1)
            and finite_metric(metrics.get("expectancy_per_trade")) > 0
            and finite_metric(metrics.get("max_drawdown")) <= 0.12
            and finite_metric(metrics.get("number_of_trades")) >= 20
        ):
            near_pass.append(row)
    return best_row_per_candidate(near_pass)


def best_row_per_candidate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("candidate_id"))
        current = best.get(key)
        if current is None or finite_metric(row.get("validation_score")) > finite_metric(current.get("validation_score")):
            best[key] = row
    return sorted(best.values(), key=lambda row: (finite_metric(row.get("validation_score")), str(row.get("candidate_id"))), reverse=True)


def strongest_quality_assets(jobs: list[dict[str, Any]], parents: list[dict[str, Any]], asset_limit: int) -> list[str]:
    selected: list[str] = []
    for row in parents:
        append_unique(selected, str(row.get("symbol", "")).upper())
    for row in grouped_quality_rank(jobs, "symbol"):
        if row["average_profit_factor"] >= 1.2 and row["average_expectancy"] > 0 and row["average_drawdown"] <= 0.12:
            append_unique(selected, row["name"].upper())
        if len(selected) >= max(2, asset_limit):
            break
    return selected[: max(2, asset_limit)]


def strongest_quality_timeframes(jobs: list[dict[str, Any]], parents: list[dict[str, Any]], explicit: list[str] | None) -> list[str]:
    if explicit:
        return [timeframe for timeframe in explicit if timeframe in DEFAULT_CAMPAIGN_TIMEFRAMES]
    selected: list[str] = []
    for row in parents:
        append_unique(selected, str(row.get("timeframe", "")))
    return selected or [grouped_quality_rank(jobs, "timeframe")[0]["name"] if grouped_quality_rank(jobs, "timeframe") else "1h"]


def targeted_quality_candidates(parent_rows: list[dict[str, Any]], *, max_variants_per_parent: int) -> list[DiscoveryCandidate]:
    candidates: list[DiscoveryCandidate] = []
    seen: set[str] = set()
    for row in parent_rows:
        parent = candidate_from_payload(row["candidate"])
        add_candidate(candidates, seen, parent)
        for variant in local_quality_variants(parent, max(0, max_variants_per_parent - 1)):
            add_candidate(candidates, seen, variant)
    return candidates


def local_quality_variants(parent: DiscoveryCandidate, limit: int) -> list[DiscoveryCandidate]:
    params = parent.parameters
    grids = {
        "risk_reward": local_numeric_values(float(params.get("risk_reward", 1.5)), [1.4, 1.5, 1.6, 1.8, 2.0, 2.2]),
        "volume_change_min": local_numeric_values(float(params.get("volume_change_min", 0)), [0.0, 0.05, 0.1, 0.15]),
        "rsi_min": local_numeric_values(float(params.get("rsi_min", 55)), [52, 55, 58]),
        "entry_distance_to_ema20_max": local_numeric_values(float(params.get("entry_distance_to_ema20_max", 0.035)), [0.03, 0.035, 0.04]),
        "atr_multiplier": local_numeric_values(float(params.get("atr_multiplier", 1.5)), [1.25, 1.5, 1.75]),
    }
    changes = []
    max_depth = max((len(values) for values in grids.values()), default=0)
    for index in range(max_depth):
        for key, values in grids.items():
            if index < len(values):
                changes.append({key: values[index]})
    variants = []
    for change in changes:
        if len(variants) >= limit:
            break
        next_params = {**parent.parameters, **change}
        key = canonical_candidate_key(parent.blocks, next_params, parent.candidate_id)
        variants.append(
            DiscoveryCandidate(
                candidate_id=f"sd_{sha256(key.encode()).hexdigest()[:14]}",
                family_id=parent.family_id,
                parent_candidate_id=parent.candidate_id,
                generation=parent.generation + 1,
                blocks=parent.blocks,
                parameters=next_params,
                complexity=parent.complexity + 1,
                canonical_key=key,
            )
        )
    return variants


def local_numeric_values(current: float, grid: list[float]) -> list[float]:
    return [value for value in sorted(grid, key=lambda value: (abs(value - current), value)) if value != current]


def add_candidate(candidates: list[DiscoveryCandidate], seen: set[str], candidate: DiscoveryCandidate) -> None:
    if candidate.canonical_key in seen:
        return
    seen.add(candidate.canonical_key)
    candidates.append(candidate)


def append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def quality_campaign_key(source_campaign_id: int, blueprint: dict[str, Any]) -> str:
    material = {
        "version": QUALITY_FIRST_CAMPAIGN_VERSION,
        "source_campaign_id": source_campaign_id,
        "parents": blueprint["targeting"]["parents"],
        "assets": blueprint["targeting"]["assets"],
        "timeframes": blueprint["targeting"]["timeframes"],
        "candidates": [candidate.candidate_id for candidate in blueprint["candidates"]],
    }
    return sha256(repr(material).encode("utf-8")).hexdigest()


def create_transferability_sample_size_campaign(
    conn: psycopg.Connection,
    *,
    pullback_campaign_id: int,
    trend_source_campaign_id: int,
    name: str | None = None,
) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    pullback_jobs = list(
        conn.execute(
            """
            SELECT *
            FROM research_campaign_jobs
            WHERE campaign_id = %s AND simulation_only = TRUE
            ORDER BY validation_score DESC, id ASC
            """,
            (pullback_campaign_id,),
        ).fetchall()
    )
    trend_jobs = list(
        conn.execute(
            """
            SELECT *
            FROM research_campaign_jobs
            WHERE campaign_id = %s
              AND strategy_family = 'Trend Following'
              AND simulation_only = TRUE
            ORDER BY validation_score DESC, id ASC
            """,
            (trend_source_campaign_id,),
        ).fetchall()
    )
    blueprint = transferability_sample_size_blueprint([dict(row) for row in pullback_jobs], [dict(row) for row in trend_jobs])
    source_campaign = get_campaign(conn, pullback_campaign_id)
    campaign_key = transfer_sample_size_campaign_key(blueprint)
    campaign_name = name or "Phase 9.8 Transferability and Sample-Size Research"
    row = conn.execute(
        """
        INSERT INTO research_campaigns(campaign_key, name, universe_key, status, requested_candidates, controls, scheduling_config, safety_statement, simulation_only)
        VALUES (%s, %s, %s, 'queued', %s, %s, %s, %s, TRUE)
        ON CONFLICT(campaign_key) DO UPDATE
        SET updated_at = NOW()
        RETURNING *
        """,
        (
            campaign_key,
            campaign_name,
            source_campaign["universe_key"],
            len(blueprint["candidates"]),
            Jsonb(
                {
                    "campaign_version": TRANSFER_SAMPLE_SIZE_CAMPAIGN_VERSION,
                    "source_campaigns": {"pullback": pullback_campaign_id, "trend_following": trend_source_campaign_id},
                    "objective": "Test Pullback transferability and Trend Following sample-size improvement without broad sweeps.",
                    "candidate_quality_policy": "Focused deterministic parent-child mutations only; validation thresholds and evidence requirements are unchanged.",
                    "paper_deployment_policy": "Do not create paper deployments unless an elite candidate passes every existing deterministic gate.",
                    "tracks": blueprint["tracks"],
                    "lineage": blueprint["lineage"],
                    "targeting": blueprint["targeting"],
                }
            ),
            Jsonb({**DEFAULT_SCHEDULING_CONFIG, "batch_size": 12, "daily_experiment_budget": 120, "max_generated_candidates": len(blueprint["candidates"])}),
            SAFETY_STATEMENT,
        ),
    ).fetchone()
    campaign_id = int(row["id"])
    created = queue_campaign_jobs(conn, campaign_id, blueprint["candidates"], blueprint["targeting"]["assets"], blueprint["targeting"]["timeframes"])
    update_campaign_counts(conn, campaign_id)
    conn.commit()
    return {
        "campaign": jsonable(dict(row)),
        "candidates_generated": len(blueprint["candidates"]),
        "jobs_created": created,
        "targeting": blueprint["targeting"],
        "tracks": blueprint["tracks"],
        "lineage": blueprint["lineage"],
        "campaign_version": TRANSFER_SAMPLE_SIZE_CAMPAIGN_VERSION,
        "simulation_only": True,
        "safety": SAFETY_STATEMENT,
    }


def transferability_sample_size_blueprint(pullback_jobs: list[dict[str, Any]], trend_jobs: list[dict[str, Any]]) -> dict[str, Any]:
    pullback_parents = best_row_per_candidate([row for row in pullback_jobs if row.get("status") == "promoted"])[:3]
    if len(pullback_parents) < 3:
        pullback_parents = best_row_per_candidate(pullback_jobs)[:3]
    trend_parents = best_row_per_candidate([row for row in trend_jobs if ((row.get("result") or {}).get("metrics") or {}).get("number_of_trades")])[:2]
    candidates: list[DiscoveryCandidate] = []
    seen: set[str] = set()
    lineage: list[dict[str, Any]] = []
    for parent_row in pullback_parents:
        parent = candidate_from_payload(parent_row["candidate"])
        for mutation in pullback_transfer_mutations(parent):
            child = phase_9_8_child(parent, mutation)
            add_candidate(candidates, seen, child)
            lineage.append(lineage_row(child, parent, mutation, parent_row))
    for parent_row in trend_parents:
        parent = candidate_from_payload(parent_row["candidate"])
        for mutation in trend_sample_size_mutations(parent):
            child = phase_9_8_child(parent, mutation)
            add_candidate(candidates, seen, child)
            lineage.append(lineage_row(child, parent, mutation, parent_row))
    return {
        "targeting": {
            "assets": ["AAPL", "NVDA", "AVGO", "LLY", "GOOGL", "JPM"],
            "timeframes": ["1h", "4h"],
            "strategy_families": ["Pullback", "Trend Following"],
            "candidate_count": len(candidates),
            "job_count": len(candidates) * 12,
        },
        "tracks": {
            "pullback_transferability": {
                "parents": [row["candidate_id"] for row in pullback_parents],
                "hypothesis": "AAPL Pullback evidence transfers if RSI, ATR, pullback distance, risk/reward, volume, and regime filters stay near the Phase 9.7 evidence cluster.",
                "falsification": "Reject transferability if candidates keep passing only AAPL 1h or fail cross-timeframe confirmation.",
            },
            "trend_following_sample_size": {
                "parents": [row["candidate_id"] for row in trend_parents],
                "hypothesis": "Trend Following can raise trade count through bounded entry and filter relaxation without destroying PF, expectancy, drawdown, or stability.",
                "falsification": "Reject if trade count improves only by producing weak PF, negative expectancy, excessive drawdown, or unstable regimes.",
            },
            "regime_filtering": {
                "hypothesis": "Deterministic filters can preserve bull-trend/low-volatility setups while reducing sideways and normal-volatility damage.",
                "comparison": "Every filtered child is compared to its unfiltered parent through normal campaign validation history.",
            },
            "breakout_containment": {"included": False, "reason": "No explicit Phase 9.8 Breakout falsification hypothesis was selected."},
        },
        "lineage": lineage,
        "candidates": candidates,
        "simulation_only": True,
    }


def pullback_transfer_mutations(parent: DiscoveryCandidate) -> list[dict[str, Any]]:
    base = {"rsi_min": 55, "trend_fast": 20, "trend_slow": 50}
    return [
        phase_9_8_mutation("pullback_parent_confirmation", {**base}, "Confirm parent near original parameters across six assets and two timeframes.", "Fails if passes remain isolated to AAPL 1h."),
        phase_9_8_mutation("pullback_volume_confirm", {**base, "volume_change_min": 0.15, "risk_reward": 1.4}, "Test stricter participation while keeping reward target closer.", "Fails if volume filter reduces transferability or trade count below gates."),
        phase_9_8_mutation("pullback_wider_depth", {**base, "entry_distance_to_ema20_max": 0.04, "atr_multiplier": 1.5}, "Test whether slightly wider pullback depth transfers beyond AAPL.", "Fails if sideways/normal-volatility losses increase."),
        phase_9_8_mutation("pullback_regime_filtered", {**base, "phase_9_8_regime_filter": True, "block_sideways": True, "sideways_distance_from_ema50_min": 0.012, "normal_volatility_rsi_min": 58, "normal_volatility_volume_change_min": 0.15, "risk_reward": 1.5}, "Block weak sideways exposure and require more confirmation in normal volatility.", "Fails if out-of-sample evidence does not improve versus parent."),
    ]


def trend_sample_size_mutations(parent: DiscoveryCandidate) -> list[dict[str, Any]]:
    base = {"trend_fast": 20, "trend_slow": 50, "entry": "trend_continuation"}
    return [
        phase_9_8_mutation("trend_parent_confirmation", {}, "Confirm strongest Trend Following parent before changing activation.", "Fails if sample size remains below gates or PF/expectancy deteriorate."),
        phase_9_8_mutation("trend_lower_activation", {**base, "returns_5_min": 0.008, "volume_change_min": 0.05, "risk_reward": 1.6, "max_holding_bars": 16}, "Increase entry frequency with bounded momentum and volume confirmation.", "Fails if extra trades destroy profit factor or expectancy."),
        phase_9_8_mutation("trend_rsi_confirmation", {**base, "momentum": "rsi", "rsi_min": 52, "volume_change_min": 0.1, "risk_reward": 1.5, "max_holding_bars": 18}, "Use less lagged momentum confirmation to improve sample size.", "Fails if stability or drawdown gates deteriorate."),
        phase_9_8_mutation("trend_regime_filtered", {**base, "returns_5_min": 0.008, "volume_change_min": 0.1, "risk_reward": 1.5, "max_holding_bars": 18, "phase_9_8_regime_filter": True, "block_sideways": True, "sideways_distance_from_ema50_min": 0.015, "normal_volatility_rsi_min": 55, "normal_volatility_volume_change_min": 0.1}, "Increase trade count while reducing sideways and normal-volatility damage.", "Fails if filter only improves aggregate history without cross-asset/timeframe evidence."),
        phase_9_8_mutation("trend_longer_exit", {**base, "returns_5_min": 0.01, "volume_change_min": 0.05, "risk_reward": 1.7, "max_holding_bars": 24}, "Allow longer trend continuation exits without removing risk controls.", "Fails if drawdown or losing-streak behavior weakens."),
        phase_9_8_mutation("trend_ema_distance", {**base, "returns_5_min": 0.006, "entry_distance_to_ema20_max": 0.045, "volume_change_min": 0.1, "risk_reward": 1.5}, "Increase activation near EMA support while preserving trend controls.", "Fails if trades increase but regime stability fails."),
    ]


def phase_9_8_mutation(mutation_type: str, changes: dict[str, Any], expected: str, falsification: str) -> dict[str, Any]:
    return {
        "mutation_type": mutation_type,
        "changes": changes,
        "expected_improvement": expected,
        "falsification_condition": falsification,
        "evidence_source": "Phase 9.6/9.7 stored campaign evidence",
        "campaign_version": TRANSFER_SAMPLE_SIZE_CAMPAIGN_VERSION,
    }


def phase_9_8_child(parent: DiscoveryCandidate, mutation: dict[str, Any]) -> DiscoveryCandidate:
    params = {
        **parent.parameters,
        **mutation["changes"],
        "phase_9_8_mutation_type": mutation["mutation_type"],
        "phase_9_8_expected_improvement": mutation["expected_improvement"],
        "phase_9_8_falsification_condition": mutation["falsification_condition"],
        "phase_9_8_evidence_source": mutation["evidence_source"],
        "phase_9_8_campaign_version": TRANSFER_SAMPLE_SIZE_CAMPAIGN_VERSION,
    }
    key = canonical_candidate_key(parent.blocks, params, parent.candidate_id)
    return DiscoveryCandidate(
        candidate_id=f"sd_{sha256(key.encode()).hexdigest()[:14]}",
        family_id=parent.family_id,
        parent_candidate_id=parent.candidate_id,
        generation=parent.generation + 1,
        blocks=parent.blocks,
        parameters=params,
        complexity=parent.complexity + 1,
        canonical_key=key,
    )


def lineage_row(child: DiscoveryCandidate, parent: DiscoveryCandidate, mutation: dict[str, Any], parent_row: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": child.candidate_id,
        "parent_candidate_id": parent.candidate_id,
        "family_id": child.family_id,
        "strategy_family": strategy_family_for_candidate(child),
        "mutation_type": mutation["mutation_type"],
        "mutation_value": mutation["changes"],
        "expected_improvement": mutation["expected_improvement"],
        "falsification_condition": mutation["falsification_condition"],
        "evidence_source": mutation["evidence_source"],
        "source_job": parent_row.get("id"),
        "source_symbol": parent_row.get("symbol"),
        "source_timeframe": parent_row.get("timeframe"),
        "campaign_version": TRANSFER_SAMPLE_SIZE_CAMPAIGN_VERSION,
    }


def transfer_sample_size_campaign_key(blueprint: dict[str, Any]) -> str:
    material = {
        "version": TRANSFER_SAMPLE_SIZE_CAMPAIGN_VERSION,
        "assets": blueprint["targeting"]["assets"],
        "timeframes": blueprint["targeting"]["timeframes"],
        "lineage": [(row["parent_candidate_id"], row["mutation_type"], row["mutation_value"]) for row in blueprint["lineage"]],
    }
    return sha256(repr(material).encode("utf-8")).hexdigest()


def create_overfit_regime_robustness_campaign(
    conn: psycopg.Connection,
    *,
    source_campaign_id: int,
    name: str | None = None,
) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    jobs = list(
        conn.execute(
            """
            SELECT *
            FROM research_campaign_jobs
            WHERE campaign_id = %s AND simulation_only = TRUE
            ORDER BY validation_score DESC, id ASC
            """,
            (source_campaign_id,),
        ).fetchall()
    )
    blueprint = overfit_regime_robustness_blueprint([dict(row) for row in jobs])
    source_campaign = get_campaign(conn, source_campaign_id)
    campaign_key = overfit_regime_robustness_campaign_key(blueprint)
    campaign_name = name or "Phase 9.9 Overfit Diagnosis and Regime-Robustness Research"
    row = conn.execute(
        """
        INSERT INTO research_campaigns(campaign_key, name, universe_key, status, requested_candidates, controls, scheduling_config, safety_statement, simulation_only)
        VALUES (%s, %s, %s, 'queued', %s, %s, %s, %s, TRUE)
        ON CONFLICT(campaign_key) DO UPDATE
        SET updated_at = NOW()
        RETURNING *
        """,
        (
            campaign_key,
            campaign_name,
            source_campaign["universe_key"],
            len(blueprint["candidates"]),
            Jsonb(
                {
                    "campaign_version": OVERFIT_REGIME_ROBUSTNESS_CAMPAIGN_VERSION,
                    "source_campaign": source_campaign_id,
                    "objective": "Diagnose whether AAPL Pullback is repeatable edge or asset-specific overfit, and test bounded Trend Following stability improvements.",
                    "candidate_quality_policy": "Small deterministic parent-child mutations only; validation thresholds and evidence requirements are unchanged.",
                    "paper_deployment_policy": "Do not create paper deployments unless an elite candidate passes every existing deterministic gate.",
                    "single_asset_robust_candidate_policy": "Informational classification only; never grants elite, Phase 10, or deployment eligibility.",
                    "tracks": blueprint["tracks"],
                    "lineage": blueprint["lineage"],
                    "targeting": blueprint["targeting"],
                    "overfit_diagnostics": blueprint["overfit_diagnostics"],
                }
            ),
            Jsonb({**DEFAULT_SCHEDULING_CONFIG, "batch_size": 12, "daily_experiment_budget": 96, "max_generated_candidates": len(blueprint["candidates"])}),
            SAFETY_STATEMENT,
        ),
    ).fetchone()
    campaign_id = int(row["id"])
    created = queue_campaign_jobs(conn, campaign_id, blueprint["candidates"], blueprint["targeting"]["assets"], blueprint["targeting"]["timeframes"])
    update_campaign_counts(conn, campaign_id)
    conn.commit()
    return {
        "campaign": jsonable(dict(row)),
        "candidates_generated": len(blueprint["candidates"]),
        "jobs_created": created,
        "targeting": blueprint["targeting"],
        "tracks": blueprint["tracks"],
        "lineage": blueprint["lineage"],
        "overfit_diagnostics": blueprint["overfit_diagnostics"],
        "campaign_version": OVERFIT_REGIME_ROBUSTNESS_CAMPAIGN_VERSION,
        "simulation_only": True,
        "safety": SAFETY_STATEMENT,
    }


def overfit_regime_robustness_blueprint(source_jobs: list[dict[str, Any]]) -> dict[str, Any]:
    pullback_rows = [
        row
        for row in source_jobs
        if row.get("strategy_family") == "Pullback" and str(row.get("symbol")) == "AAPL" and str(row.get("timeframe")) == "1h"
    ]
    trend_rows = [row for row in source_jobs if row.get("strategy_family") == "Trend Following"]
    pullback_parent_row = best_row_per_candidate([row for row in pullback_rows if row.get("status") == "promoted"] or pullback_rows)[0]
    trend_parent_row = next((row for row in trend_rows if row.get("candidate_id") == "sd_5cbcdf7c9eabcf"), None)
    if trend_parent_row is None:
        trend_parent_row = best_row_per_candidate(trend_rows)[0]
    pullback_parent = candidate_from_payload(pullback_parent_row["candidate"])
    trend_parent = candidate_from_payload(trend_parent_row["candidate"])
    candidates: list[DiscoveryCandidate] = []
    seen: set[str] = set()
    lineage: list[dict[str, Any]] = []
    for mutation in pullback_overfit_mutations():
        child = phase_9_9_child(pullback_parent, mutation)
        add_candidate(candidates, seen, child)
        lineage.append(phase_9_9_lineage_row(child, pullback_parent, mutation, pullback_parent_row))
    for mutation in trend_robustness_mutations():
        child = phase_9_9_child(trend_parent, mutation)
        add_candidate(candidates, seen, child)
        lineage.append(phase_9_9_lineage_row(child, trend_parent, mutation, trend_parent_row))
    return {
        "targeting": {
            "assets": ["AAPL", "NVDA"],
            "timeframes": ["1h", "4h"],
            "strategy_families": ["Pullback", "Trend Following"],
            "candidate_count": len(candidates),
            "job_count": len(candidates) * 4,
            "primary_timeframe": "1h",
            "diagnostic_timeframe": "4h",
        },
        "tracks": {
            "aapl_pullback_overfit_diagnosis": {
                "parent": pullback_parent.candidate_id,
                "hypothesis": "AAPL Pullback evidence is repeatable if the EMA20/50, RSI55, ATR1.5, pullback-distance 0.04, RR1.4, volume 0.15 cluster survives small perturbations and execution stress.",
                "falsification": "Flag overfit if evidence collapses under nearby parameters, alternate windows, delayed entry, higher costs, or 4h/NVDA diagnostics.",
            },
            "single_asset_robust_candidate": {
                "classification": "informational_only",
                "eligible_for_elite": False,
                "eligible_for_phase_10": False,
                "eligible_for_deployment": False,
            },
            "trend_following_stability": {
                "parent": trend_parent.candidate_id,
                "hypothesis": "Trend Following can preserve profitability while improving stability through bounded exit, persistence, slope, cooldown, volatility, RR, stop, and volume mutations.",
                "falsification": "Reject if stability gains come from too few trades or destroy PF, expectancy, drawdown, or regime robustness.",
            },
            "sideways_low_volatility_exclusion": {
                "comparison": "Each filtered child is compared with its parent and fails the hypothesis if filters eliminate nearly all trades.",
            },
            "data_execution_sensitivity": {
                "stressors": ["higher_fees", "higher_slippage", "one_bar_delayed_entry", "post_trade_cooldown", "conservative_same_candle_fill"],
            },
            "breakout_containment": {"included": False, "reason": "Phase 9.9 explicitly excludes Breakout."},
        },
        "overfit_diagnostics": {
            "pullback_cluster": {"trend_fast": 20, "trend_slow": 50, "rsi_min": 55, "atr_multiplier": 1.5, "entry_distance_to_ema20_max": 0.04, "risk_reward": 1.4, "volume_change_min": 0.15},
            "perturbation_ranges": {"rsi_min": [53, 55, 57], "atr_multiplier": [1.4, 1.5, 1.6], "entry_distance_to_ema20_max": [0.035, 0.04, 0.045], "risk_reward": [1.3, 1.4, 1.5], "volume_change_min": [0.10, 0.15, 0.20]},
            "window_tests": ["default_walk_forward", "anchored_60_train", "anchored_75_train", "4h_diagnostic"],
            "execution_stress": ["higher_costs", "delayed_entry", "cooldown", "same_candle_stop_first"],
        },
        "lineage": lineage,
        "candidates": candidates,
        "simulation_only": True,
    }


def pullback_overfit_mutations() -> list[dict[str, Any]]:
    base = {
        "trend_fast": 20,
        "trend_slow": 50,
        "momentum": "rsi",
        "rsi_min": 55,
        "volatility": "atr",
        "atr_multiplier": 1.5,
        "entry": "pullback",
        "entry_distance_to_ema20_max": 0.04,
        "risk_reward": 1.4,
        "volume_change_min": 0.15,
    }
    return [
        phase_9_9_mutation("aapl_pullback_parent_confirmation", "AAPL Pullback cluster is repeatable at the Phase 9.8 best point.", base, "Preserve AAPL 1h strength without creating 4h/NVDA collapse.", "Fails if validation remains isolated or core gates deteriorate.", "default_walk_forward", "all_regimes", {}),
        phase_9_9_mutation("aapl_pullback_rsi_53", "Lower RSI threshold keeps edge while improving sample size.", {**base, "rsi_min": 53}, "Increase trades without weakening expectancy.", "Fails if lower activation admits low-quality trades.", "default_walk_forward", "all_regimes", {}),
        phase_9_9_mutation("aapl_pullback_rsi_57", "Higher RSI threshold isolates stronger participation.", {**base, "rsi_min": 57}, "Improve PF and drawdown without starving trades.", "Fails if trade count or stability falls below gates.", "default_walk_forward", "all_regimes", {}),
        phase_9_9_mutation("aapl_pullback_atr_1_4", "Slightly tighter ATR stop preserves edge.", {**base, "atr_multiplier": 1.4}, "Improve drawdown while keeping expectancy positive.", "Fails if stop sensitivity collapses PF.", "default_walk_forward", "all_regimes", {}),
        phase_9_9_mutation("aapl_pullback_atr_1_6", "Slightly wider ATR stop survives noise.", {**base, "atr_multiplier": 1.6}, "Reduce stop-outs without excessive drawdown.", "Fails if drawdown or loss size expands.", "default_walk_forward", "all_regimes", {}),
        phase_9_9_mutation("aapl_pullback_distance_035", "Tighter pullback distance tests parameter sharpness.", {**base, "entry_distance_to_ema20_max": 0.035}, "Preserve quality with less entry looseness.", "Fails if evidence depends on exactly 0.04.", "default_walk_forward", "all_regimes", {}),
        phase_9_9_mutation("aapl_pullback_distance_045", "Wider pullback distance tests local robustness.", {**base, "entry_distance_to_ema20_max": 0.045}, "Hold PF while adding nearby setups.", "Fails if wider depth increases weak regimes.", "default_walk_forward", "all_regimes", {}),
        phase_9_9_mutation("aapl_pullback_rr_1_3", "Lower reward target tests fill realism.", {**base, "risk_reward": 1.3}, "Improve win conversion without negative expectancy.", "Fails if smaller winners erase edge.", "default_walk_forward", "all_regimes", {}),
        phase_9_9_mutation("aapl_pullback_rr_1_5", "Higher reward target tests payoff sensitivity.", {**base, "risk_reward": 1.5}, "Improve PF without starving wins.", "Fails if target sensitivity collapses trades.", "default_walk_forward", "all_regimes", {}),
        phase_9_9_mutation("aapl_pullback_volume_010", "Lower volume confirmation tests sample-size sensitivity.", {**base, "volume_change_min": 0.10}, "Gain trades without weak expectancy.", "Fails if weaker participation admits losers.", "default_walk_forward", "all_regimes", {}),
        phase_9_9_mutation("aapl_pullback_volume_020", "Higher volume confirmation tests participation quality.", {**base, "volume_change_min": 0.20}, "Improve PF and drawdown.", "Fails if trade count collapses.", "default_walk_forward", "all_regimes", {}),
        phase_9_9_mutation("aapl_pullback_high_cost", "AAPL Pullback survives higher execution costs.", {**base, "fee_rate": 0.001, "slippage_rate": 0.001}, "Keep positive expectancy under cost stress.", "Fails if edge disappears after costs.", "default_walk_forward", "all_regimes", {"fee_rate": 0.001, "slippage_rate": 0.001}),
        phase_9_9_mutation("aapl_pullback_delayed_entry", "AAPL Pullback survives one additional bar of entry delay.", {**base, "entry_delay_bars": 1}, "Preserve gates after delayed entry.", "Fails if timing dependence collapses evidence.", "default_walk_forward", "all_regimes", {"entry_delay_bars": 1}),
        phase_9_9_mutation("aapl_pullback_anchored_60", "AAPL Pullback is stable under an alternate anchored split.", {**base, "walk_forward_train_ratio": 0.6}, "Preserve validation gates with earlier validation start.", "Fails if window choice explains the edge.", "anchored_60_train", "all_regimes", {}),
        phase_9_9_mutation("aapl_pullback_sideways_lowvol_filter", "Sideways and low-volatility exclusion improves robustness.", {**base, "phase_9_9_regime_filter": True, "block_sideways": True, "sideways_distance_from_ema50_min": 0.012, "phase_9_9_low_volatility_block": True, "phase_9_9_low_volatility_min": 0.008}, "Reduce weak regimes without eliminating most trades.", "Fails if filters starve evidence or do not improve gates.", "default_walk_forward", "exclude_sideways_low_volatility", {}),
    ]


def trend_robustness_mutations() -> list[dict[str, Any]]:
    base = {"trend_fast": 20, "trend_slow": 50, "entry": "trend_continuation", "returns_5_min": 0.01, "volume_change_min": 0.05, "risk_reward": 1.7, "max_holding_bars": 24}
    return [
        phase_9_9_mutation("trend_parent_confirmation", "Trend parent profitability is reproducible before changing activation.", base, "Preserve PF and expectancy while measuring stability.", "Fails if Phase 9.8 result was not repeatable.", "default_walk_forward", "all_regimes", {}),
        phase_9_9_mutation("trend_shorter_exit", "Shorter exit duration reduces unstable holding risk.", {**base, "max_holding_bars": 18}, "Improve drawdown and stability without losing expectancy.", "Fails if exits truncate winners.", "default_walk_forward", "all_regimes", {}),
        phase_9_9_mutation("trend_longer_exit", "Longer exit duration captures continuation.", {**base, "max_holding_bars": 30}, "Improve PF while respecting drawdown.", "Fails if longer holds increase losses.", "default_walk_forward", "all_regimes", {}),
        phase_9_9_mutation("trend_ema_slope", "EMA slope confirmation improves trend persistence.", {**base, "phase_9_9_ema_slope_min": 0.0004}, "Raise stability without starving trades.", "Fails if sample size becomes insufficient.", "default_walk_forward", "bull_trend_focus", {}),
        phase_9_9_mutation("trend_entry_cooldown", "Post-trade cooldown removes clustered low-quality signals.", {**base, "entry_cooldown_bars": 3}, "Improve stability with acceptable trade count.", "Fails if cooldown eliminates evidence.", "default_walk_forward", "all_regimes", {"entry_cooldown_bars": 3}),
        phase_9_9_mutation("trend_lowvol_block", "Low-volatility exclusion improves trend quality.", {**base, "phase_9_9_regime_filter": True, "phase_9_9_low_volatility_block": True, "phase_9_9_low_volatility_min": 0.008}, "Improve PF and expectancy without eliminating most trades.", "Fails if low-vol filter starves trades.", "default_walk_forward", "exclude_low_volatility", {}),
        phase_9_9_mutation("trend_rr_1_5", "Lower RR improves realized win conversion.", {**base, "risk_reward": 1.5}, "Preserve positive expectancy while improving trade completion.", "Fails if lower target weakens PF.", "default_walk_forward", "all_regimes", {}),
        phase_9_9_mutation("trend_stop_1_4", "Tighter stop distance improves risk control.", {**base, "atr_multiplier": 1.4}, "Improve drawdown without reducing expectancy.", "Fails if stops become too tight.", "default_walk_forward", "all_regimes", {}),
        phase_9_9_mutation("trend_bull_only_volume", "Bull-trend and volume confirmation improves regime stability.", {**base, "phase_9_9_regime_filter": True, "phase_9_9_bull_trend_only": True, "phase_9_9_returns_5_min": 0.01, "volume_change_min": 0.10}, "Improve stability in trend regimes without starving trades.", "Fails if stability improves only by reducing evidence below gates.", "default_walk_forward", "bull_trend_only", {}),
    ]


def phase_9_9_mutation(
    mutation_type: str,
    hypothesis: str,
    changes: dict[str, Any],
    expected: str,
    falsification: str,
    data_window: str,
    regime_scope: str,
    execution_stress_config: dict[str, Any],
) -> dict[str, Any]:
    return {
        "mutation_type": mutation_type,
        "hypothesis": hypothesis,
        "changes": changes,
        "expected_improvement": expected,
        "falsification_condition": falsification,
        "data_window": data_window,
        "regime_scope": regime_scope,
        "execution_stress_config": execution_stress_config,
        "evidence_source": "Phase 9.8 stored campaign evidence",
        "campaign_version": OVERFIT_REGIME_ROBUSTNESS_CAMPAIGN_VERSION,
    }


def phase_9_9_child(parent: DiscoveryCandidate, mutation: dict[str, Any]) -> DiscoveryCandidate:
    params = {
        **parent.parameters,
        **mutation["changes"],
        "phase_9_9_mutation_type": mutation["mutation_type"],
        "phase_9_9_hypothesis": mutation["hypothesis"],
        "phase_9_9_expected_improvement": mutation["expected_improvement"],
        "phase_9_9_falsification_condition": mutation["falsification_condition"],
        "phase_9_9_data_window": mutation["data_window"],
        "phase_9_9_regime_scope": mutation["regime_scope"],
        "phase_9_9_execution_stress_config": mutation["execution_stress_config"],
        "phase_9_9_campaign_version": OVERFIT_REGIME_ROBUSTNESS_CAMPAIGN_VERSION,
    }
    key = canonical_candidate_key(parent.blocks, params, parent.candidate_id)
    return DiscoveryCandidate(
        candidate_id=f"sd_{sha256(key.encode()).hexdigest()[:14]}",
        family_id=parent.family_id,
        parent_candidate_id=parent.candidate_id,
        generation=parent.generation + 1,
        blocks=parent.blocks,
        parameters=params,
        complexity=parent.complexity + 1,
        canonical_key=key,
    )


def phase_9_9_lineage_row(child: DiscoveryCandidate, parent: DiscoveryCandidate, mutation: dict[str, Any], parent_row: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": child.candidate_id,
        "parent_candidate_id": parent.candidate_id,
        "family_id": child.family_id,
        "strategy_family": strategy_family_for_candidate(child),
        "hypothesis": mutation["hypothesis"],
        "mutation_type": mutation["mutation_type"],
        "mutation_value": mutation["changes"],
        "expected_improvement": mutation["expected_improvement"],
        "falsification_condition": mutation["falsification_condition"],
        "data_window": mutation["data_window"],
        "regime_scope": mutation["regime_scope"],
        "execution_stress_config": mutation["execution_stress_config"],
        "evidence_source": mutation["evidence_source"],
        "source_job": parent_row.get("id"),
        "source_symbol": parent_row.get("symbol"),
        "source_timeframe": parent_row.get("timeframe"),
        "campaign_version": OVERFIT_REGIME_ROBUSTNESS_CAMPAIGN_VERSION,
    }


def overfit_regime_robustness_campaign_key(blueprint: dict[str, Any]) -> str:
    material = {
        "version": OVERFIT_REGIME_ROBUSTNESS_CAMPAIGN_VERSION,
        "assets": blueprint["targeting"]["assets"],
        "timeframes": blueprint["targeting"]["timeframes"],
        "lineage": [(row["parent_candidate_id"], row["mutation_type"], row["mutation_value"]) for row in blueprint["lineage"]],
    }
    return sha256(repr(material).encode("utf-8")).hexdigest()


def create_single_asset_generalization_campaign(
    conn: psycopg.Connection,
    *,
    source_campaign_id: int,
    name: str | None = None,
) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    jobs = list(
        conn.execute(
            """
            SELECT *
            FROM research_campaign_jobs
            WHERE campaign_id = %s AND simulation_only = TRUE
            ORDER BY validation_score DESC, id ASC
            """,
            (source_campaign_id,),
        ).fetchall()
    )
    blueprint = single_asset_generalization_blueprint([dict(row) for row in jobs])
    source_campaign = get_campaign(conn, source_campaign_id)
    campaign_key = single_asset_generalization_campaign_key(blueprint)
    campaign_name = name or "Phase 9.10 Single-Asset Robustness and Generalization Research"
    row = conn.execute(
        """
        INSERT INTO research_campaigns(campaign_key, name, universe_key, status, requested_candidates, controls, scheduling_config, safety_statement, simulation_only)
        VALUES (%s, %s, %s, 'queued', %s, %s, %s, %s, TRUE)
        ON CONFLICT(campaign_key) DO UPDATE
        SET updated_at = NOW()
        RETURNING *
        """,
        (
            campaign_key,
            campaign_name,
            source_campaign["universe_key"],
            len(blueprint["candidates"]),
            Jsonb(
                {
                    "campaign_version": SINGLE_ASSET_GENERALIZATION_CAMPAIGN_VERSION,
                    "source_campaign": source_campaign_id,
                    "objective": "Determine whether AAPL Pullback is a robust single-asset strategy and whether economically similar assets or alternate sampling support legitimate generalization.",
                    "candidate_quality_policy": "Targeted hypothesis tests around Phase 9.9 survivors only; validation thresholds and evidence requirements are unchanged.",
                    "paper_deployment_policy": "Do not create paper deployments unless an elite candidate passes every existing deterministic gate.",
                    "diagnostic_classification_policy": "single_asset_robust and fragility labels are informational only and never bypass elite gates.",
                    "tracks": blueprint["tracks"],
                    "lineage": blueprint["lineage"],
                    "targeting": blueprint["targeting"],
                    "diagnostics": blueprint["diagnostics"],
                }
            ),
            Jsonb({**DEFAULT_SCHEDULING_CONFIG, "batch_size": 14, "daily_experiment_budget": 350, "max_generated_candidates": len(blueprint["candidates"])}),
            SAFETY_STATEMENT,
        ),
    ).fetchone()
    campaign_id = int(row["id"])
    created = queue_campaign_jobs(conn, campaign_id, blueprint["candidates"], blueprint["targeting"]["assets"], blueprint["targeting"]["timeframes"])
    update_campaign_counts(conn, campaign_id)
    conn.commit()
    return {
        "campaign": jsonable(dict(row)),
        "candidates_generated": len(blueprint["candidates"]),
        "jobs_created": created,
        "targeting": blueprint["targeting"],
        "tracks": blueprint["tracks"],
        "lineage": blueprint["lineage"],
        "diagnostics": blueprint["diagnostics"],
        "campaign_version": SINGLE_ASSET_GENERALIZATION_CAMPAIGN_VERSION,
        "simulation_only": True,
        "safety": SAFETY_STATEMENT,
    }


def single_asset_generalization_blueprint(source_jobs: list[dict[str, Any]]) -> dict[str, Any]:
    pullback_rows = [
        row
        for row in source_jobs
        if row.get("strategy_family") == "Pullback" and str(row.get("symbol")) == "AAPL" and str(row.get("timeframe")) == "1h"
    ]
    if not pullback_rows:
        pullback_rows = [row for row in source_jobs if row.get("strategy_family") == "Pullback"]
    trend_rows = [row for row in source_jobs if row.get("strategy_family") == "Trend Following"]
    pullback_parent_row = next((row for row in pullback_rows if row.get("candidate_id") == "sd_a8d9508bee3c46"), None)
    if pullback_parent_row is None:
        pullback_parent_row = best_row_per_candidate([row for row in pullback_rows if row.get("status") == "promoted"] or pullback_rows)[0]
    trend_parent_row = best_row_per_candidate(trend_rows)[0] if trend_rows else pullback_parent_row
    pullback_parent = candidate_from_payload(pullback_parent_row["candidate"])
    trend_parent = candidate_from_payload(trend_parent_row["candidate"])
    candidates: list[DiscoveryCandidate] = []
    seen: set[str] = set()
    lineage: list[dict[str, Any]] = []
    for mutation in pullback_single_asset_mutations():
        child = phase_9_10_child(pullback_parent, mutation)
        add_candidate(candidates, seen, child)
        lineage.append(phase_9_10_lineage_row(child, pullback_parent, mutation, pullback_parent_row))
    for mutation in trend_confirmation_mutations():
        child = phase_9_10_child(trend_parent, mutation)
        add_candidate(candidates, seen, child)
        lineage.append(phase_9_10_lineage_row(child, trend_parent, mutation, trend_parent_row))
    assets = ["AAPL", "MSFT", "GOOGL", "META", "QQQ", "SPY", "NVDA"]
    timeframes = ["1h", "4h"]
    return {
        "targeting": {
            "assets": assets,
            "timeframes": timeframes,
            "strategy_families": ["Pullback", "Trend Following"],
            "candidate_count": len(candidates),
            "pullback_candidate_count": sum(1 for row in lineage if row["strategy_family"] == "Pullback"),
            "trend_candidate_count": sum(1 for row in lineage if row["strategy_family"] == "Trend Following"),
            "job_count": len(candidates) * len(assets) * len(timeframes),
            "primary_asset": "AAPL",
            "primary_timeframe": "1h",
            "similar_assets": ["MSFT", "GOOGL", "META", "QQQ", "SPY", "NVDA"],
        },
        "tracks": {
            "aapl_temporal_stability": {
                "parent": pullback_parent.candidate_id,
                "hypothesis": "AAPL Pullback remains profitable under alternate walk-forward splits, delayed entry, and conservative execution stress.",
                "falsification": "Classify as temporally fragile if AAPL 1h quality depends on one split, timing assumption, or narrow recent window.",
            },
            "alternate_sampling": {
                "sampling_methods": ["default_1h", "anchored_60_train", "anchored_75_train", "regular_hours_proxy", "completed_candle_only", "4h_resample_proxy"],
                "hypothesis": "The edge survives alternate sampling without changing validation thresholds.",
            },
            "similar_asset_generalization": {
                "assets": ["MSFT", "GOOGL", "META", "QQQ", "SPY", "NVDA"],
                "hypothesis": "Economically similar large-cap/ETF assets provide legitimate generalization if gates pass beyond AAPL.",
                "falsification": "Classify as cross_asset_fragile if evidence remains AAPL-only.",
            },
            "volatility_normalized_pullback": {
                "hypothesis": "Volatility-normalized pullback distance and ATR stress improve transfer without relaxing gates.",
            },
            "regime_containment": {
                "hypothesis": "Sideways, low-volatility, and high-volatility containment improves robustness without starving sample size.",
            },
            "trend_following_confirmation": {
                "candidate_share_limit": 0.2,
                "hypothesis": "A small bounded Trend Following track either earns further exploration or remains secondary.",
            },
            "breakout_containment": {"included": False, "reason": "Phase 9.10 does not spend candidates on Breakout."},
        },
        "diagnostics": {
            "informational_labels": ["single_asset_robust", "temporally_fragile", "cross_asset_fragile", "regime_fragile", "execution_fragile"],
            "elite_gate_policy": "Informational diagnostics never override passes_cross_validation.",
            "stability_decomposition": ["asset_stability", "timeframe_stability", "temporal_window_stability", "regime_stability", "execution_stability"],
        },
        "lineage": lineage,
        "candidates": candidates,
        "simulation_only": True,
    }


def pullback_single_asset_mutations() -> list[dict[str, Any]]:
    base = {
        "trend_fast": 20,
        "trend_slow": 50,
        "momentum": "rsi",
        "rsi_min": 55,
        "volatility": "atr",
        "atr_multiplier": 1.5,
        "entry": "pullback",
        "entry_distance_to_ema20_max": 0.04,
        "risk_reward": 1.4,
        "volume_change_min": 0.15,
    }
    return [
        phase_9_10_mutation("aapl_parent_confirmation", "AAPL Pullback Phase 9.9 survivor remains the anchor.", base, "fixed_parent_logic", "default_1h", "full_oos", "all_regimes", "Reproduce AAPL 1h PF, expectancy, trades, drawdown, and stability.", "Fails if the parent cannot reproduce local evidence."),
        phase_9_10_mutation("aapl_anchored_60", "Earlier validation start tests temporal dependence.", {**base, "walk_forward_train_ratio": 0.6}, "fixed_logic_alternate_window", "anchored_60_train", "earlier_oos", "all_regimes", "Preserve quality under a 60 percent training split.", "Fails if the edge exists only in the default split."),
        phase_9_10_mutation("aapl_anchored_75", "Later validation start tests sample-window sensitivity.", {**base, "walk_forward_train_ratio": 0.75}, "fixed_logic_alternate_window", "anchored_75_train", "later_oos", "all_regimes", "Preserve quality under a 75 percent training split.", "Fails if recent-window dependence explains the edge."),
        phase_9_10_mutation("aapl_conservative_cost", "Higher costs test execution fragility.", {**base, "fee_rate": 0.0015, "slippage_rate": 0.001}, "fixed_logic_execution_stress", "default_1h", "full_oos", "all_regimes", "Keep positive expectancy under conservative cost assumptions.", "Fails if costs erase expectancy."),
        phase_9_10_mutation("aapl_delayed_entry", "One-bar entry delay tests timing dependence.", {**base, "entry_delay_bars": 1}, "fixed_logic_execution_stress", "completed_candle_only", "full_oos", "all_regimes", "Keep gates after delayed execution.", "Fails if entry timing is overly sharp."),
        phase_9_10_mutation("aapl_regular_hours_proxy", "Regular-hours proxy tests intraday sampling sensitivity.", {**base, "phase_9_10_regular_hours_proxy": True}, "fixed_logic_alternate_sampling", "regular_hours_proxy", "full_oos", "all_regimes", "Maintain AAPL quality under regular-hours-style sampling metadata.", "Fails if evidence relies on sampling artifacts."),
        phase_9_10_mutation("aapl_completed_candle_only", "Completed-candle-only execution tests same-candle assumptions.", {**base, "phase_9_10_completed_candle_only": True}, "fixed_logic_execution_stress", "completed_candle_only", "full_oos", "all_regimes", "Reduce execution optimism without destroying edge.", "Fails if same-candle assumptions explain profits."),
        phase_9_10_mutation("pullback_distance_035", "Tighter pullback distance tests local parameter robustness.", {**base, "entry_distance_to_ema20_max": 0.035}, "local_parameter_mutation", "default_1h", "full_oos", "all_regimes", "Preserve PF with less entry looseness.", "Fails if edge depends on exactly 0.04."),
        phase_9_10_mutation("pullback_distance_045", "Wider pullback distance tests transfer and sample size.", {**base, "entry_distance_to_ema20_max": 0.045}, "local_parameter_mutation", "default_1h", "full_oos", "all_regimes", "Add legitimate trades without weak regimes dominating.", "Fails if wider depth admits low-quality trades."),
        phase_9_10_mutation("pullback_volatility_normalized_035", "Volatility-normalized tighter pullback tests scale robustness.", {**base, "entry_distance_to_ema20_max": 0.035, "phase_9_10_volatility_normalized_pullback": True}, "normalized_logic", "default_1h", "full_oos", "all_regimes", "Improve cross-asset comparability.", "Fails if normalization reduces AAPL quality and transfer."),
        phase_9_10_mutation("pullback_volatility_normalized_045", "Volatility-normalized wider pullback tests transfer breadth.", {**base, "entry_distance_to_ema20_max": 0.045, "phase_9_10_volatility_normalized_pullback": True}, "normalized_logic", "default_1h", "full_oos", "all_regimes", "Improve similar-asset transfer.", "Fails if normalization creates weak PF or drawdown."),
        phase_9_10_mutation("pullback_atr_14", "Tighter ATR stop decomposes execution stability.", {**base, "atr_multiplier": 1.4}, "local_parameter_mutation", "default_1h", "full_oos", "all_regimes", "Improve drawdown without starving trades.", "Fails if stop sensitivity collapses expectancy."),
        phase_9_10_mutation("pullback_atr_16", "Wider ATR stop tests noise tolerance.", {**base, "atr_multiplier": 1.6}, "local_parameter_mutation", "default_1h", "full_oos", "all_regimes", "Reduce stop-outs without excessive drawdown.", "Fails if drawdown expands beyond gates."),
        phase_9_10_mutation("pullback_rr_13", "Lower reward target tests fill realism and win conversion.", {**base, "risk_reward": 1.3}, "local_parameter_mutation", "default_1h", "full_oos", "all_regimes", "Improve completion without negative expectancy.", "Fails if smaller winners weaken PF."),
        phase_9_10_mutation("pullback_volume_010", "Lower volume confirmation tests sample-size sensitivity.", {**base, "volume_change_min": 0.10}, "local_parameter_mutation", "default_1h", "full_oos", "all_regimes", "Increase trades without degrading expectancy.", "Fails if lower participation admits poor trades."),
        phase_9_10_mutation("pullback_sideways_filter", "Sideways containment targets the main Phase 9.9 failure mode.", {**base, "phase_9_9_regime_filter": True, "block_sideways": True, "sideways_distance_from_ema50_min": 0.012}, "regime_containment", "default_1h", "full_oos", "exclude_sideways", "Reduce sideways failures without starving trades.", "Fails if filter only removes evidence."),
        phase_9_10_mutation("pullback_lowvol_filter", "Low-volatility containment targets a repeated failure mode.", {**base, "phase_9_9_regime_filter": True, "phase_9_9_low_volatility_block": True, "phase_9_9_low_volatility_min": 0.008}, "regime_containment", "default_1h", "full_oos", "exclude_low_volatility", "Reduce low-volatility failures.", "Fails if trade count falls below gates."),
        phase_9_10_mutation("pullback_sideways_lowvol_filter", "Combined sideways/low-volatility containment tests regime robustness.", {**base, "phase_9_9_regime_filter": True, "block_sideways": True, "sideways_distance_from_ema50_min": 0.012, "phase_9_9_low_volatility_block": True, "phase_9_9_low_volatility_min": 0.008}, "regime_containment", "default_1h", "full_oos", "exclude_sideways_low_volatility", "Improve stability without sample starvation.", "Fails if robustness comes only from too few trades."),
        phase_9_10_mutation("pullback_highvol_filter", "High-volatility containment tests drawdown stability.", {**base, "phase_9_10_high_volatility_block": True, "phase_9_10_high_volatility_max": 0.035}, "regime_containment", "default_1h", "full_oos", "exclude_high_volatility", "Improve drawdown while preserving PF.", "Fails if high-vol filter removes profitable regimes."),
        phase_9_10_mutation("pullback_similar_asset_transfer", "Similar assets test whether AAPL edge generalizes economically.", {**base, "phase_9_10_transfer_scope": "similar_assets"}, "fixed_logic_transfer", "default_1h_and_4h", "full_oos", "all_regimes", "Pass beyond AAPL without threshold changes.", "Fails if evidence is AAPL-only."),
    ]


def trend_confirmation_mutations() -> list[dict[str, Any]]:
    base = {"trend_fast": 20, "trend_slow": 50, "entry": "trend_continuation", "returns_5_min": 0.01, "volume_change_min": 0.05, "risk_reward": 1.7, "max_holding_bars": 24}
    return [
        phase_9_10_mutation("trend_parent_confirmation", "Trend Following is checked as a bounded secondary track.", base, "fixed_parent_logic", "default_1h", "full_oos", "all_regimes", "Confirm whether Trend deserves more exploration.", "Fails if sample size or stability remains weak."),
        phase_9_10_mutation("trend_shorter_exit", "Shorter exit tests drawdown and holding-risk containment.", {**base, "max_holding_bars": 18}, "local_parameter_mutation", "default_1h", "full_oos", "all_regimes", "Improve drawdown without destroying expectancy.", "Fails if exits truncate winners."),
        phase_9_10_mutation("trend_slope_confirmed", "EMA slope confirmation tests persistence quality.", {**base, "phase_9_9_ema_slope_min": 0.0004}, "regime_containment", "default_1h", "full_oos", "bull_trend_focus", "Improve stability without starving trades.", "Fails if fewer trades explain any improvement."),
        phase_9_10_mutation("trend_lowvol_block", "Low-volatility exclusion tests Trend fragility.", {**base, "phase_9_9_regime_filter": True, "phase_9_9_low_volatility_block": True, "phase_9_9_low_volatility_min": 0.008}, "regime_containment", "default_1h", "full_oos", "exclude_low_volatility", "Reduce weak trend regimes.", "Fails if evidence remains insufficient."),
        phase_9_10_mutation("trend_rr_15_volume", "Lower RR plus stricter volume tests sample quality.", {**base, "risk_reward": 1.5, "volume_change_min": 0.10}, "local_parameter_mutation", "default_1h", "full_oos", "all_regimes", "Improve realized conversion while preserving PF.", "Fails if lower reward weakens PF or expectancy."),
    ]


def phase_9_10_mutation(
    mutation_type: str,
    hypothesis: str,
    changes: dict[str, Any],
    normalized_or_fixed_logic: str,
    sampling_method: str,
    temporal_window: str,
    regime_scope: str,
    expected: str,
    falsification: str,
) -> dict[str, Any]:
    return {
        "mutation_type": mutation_type,
        "hypothesis": hypothesis,
        "changes": changes,
        "normalized_or_fixed_logic": normalized_or_fixed_logic,
        "asset_scope": "AAPL primary plus economically similar transfer assets",
        "timeframe_sampling": sampling_method,
        "temporal_window": temporal_window,
        "regime_scope": regime_scope,
        "expected_improvement": expected,
        "falsification_condition": falsification,
        "evidence_source": "Phase 9.9 stored candidate lifecycle evidence",
        "campaign_version": SINGLE_ASSET_GENERALIZATION_CAMPAIGN_VERSION,
    }


def phase_9_10_child(parent: DiscoveryCandidate, mutation: dict[str, Any]) -> DiscoveryCandidate:
    params = {
        **parent.parameters,
        **mutation["changes"],
        "phase_9_10_mutation_type": mutation["mutation_type"],
        "phase_9_10_hypothesis": mutation["hypothesis"],
        "phase_9_10_normalized_or_fixed_logic": mutation["normalized_or_fixed_logic"],
        "phase_9_10_asset_scope": mutation["asset_scope"],
        "phase_9_10_timeframe_sampling": mutation["timeframe_sampling"],
        "phase_9_10_temporal_window": mutation["temporal_window"],
        "phase_9_10_regime_scope": mutation["regime_scope"],
        "phase_9_10_expected_improvement": mutation["expected_improvement"],
        "phase_9_10_falsification_condition": mutation["falsification_condition"],
        "phase_9_10_campaign_version": SINGLE_ASSET_GENERALIZATION_CAMPAIGN_VERSION,
    }
    key = canonical_candidate_key(parent.blocks, params, parent.candidate_id)
    return DiscoveryCandidate(
        candidate_id=f"sd_{sha256(key.encode()).hexdigest()[:14]}",
        family_id=parent.family_id,
        parent_candidate_id=parent.candidate_id,
        generation=parent.generation + 1,
        blocks=parent.blocks,
        parameters=params,
        complexity=parent.complexity + 1,
        canonical_key=key,
    )


def phase_9_10_lineage_row(child: DiscoveryCandidate, parent: DiscoveryCandidate, mutation: dict[str, Any], parent_row: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": child.candidate_id,
        "parent_candidate_id": parent.candidate_id,
        "family_id": child.family_id,
        "strategy_family": strategy_family_for_candidate(child),
        "hypothesis": mutation["hypothesis"],
        "mutation_type": mutation["mutation_type"],
        "mutation_value": mutation["changes"],
        "normalized_or_fixed_logic": mutation["normalized_or_fixed_logic"],
        "asset_scope": mutation["asset_scope"],
        "timeframe_sampling": mutation["timeframe_sampling"],
        "temporal_window": mutation["temporal_window"],
        "regime_scope": mutation["regime_scope"],
        "expected_improvement": mutation["expected_improvement"],
        "falsification_condition": mutation["falsification_condition"],
        "evidence_source": mutation["evidence_source"],
        "source_job": parent_row.get("id"),
        "source_symbol": parent_row.get("symbol"),
        "source_timeframe": parent_row.get("timeframe"),
        "campaign_version": SINGLE_ASSET_GENERALIZATION_CAMPAIGN_VERSION,
    }


def single_asset_generalization_campaign_key(blueprint: dict[str, Any]) -> str:
    material = {
        "version": SINGLE_ASSET_GENERALIZATION_CAMPAIGN_VERSION,
        "assets": blueprint["targeting"]["assets"],
        "timeframes": blueprint["targeting"]["timeframes"],
        "lineage": [(row["parent_candidate_id"], row["mutation_type"], row["mutation_value"]) for row in blueprint["lineage"]],
    }
    return sha256(repr(material).encode("utf-8")).hexdigest()


def create_strategy_redesign_campaign(
    conn: psycopg.Connection,
    *,
    source_campaign_id: int,
    name: str | None = None,
) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    jobs = list(
        conn.execute(
            """
            SELECT *
            FROM research_campaign_jobs
            WHERE campaign_id = %s AND simulation_only = TRUE
            ORDER BY validation_score DESC, id ASC
            """,
            (source_campaign_id,),
        ).fetchall()
    )
    blueprint = strategy_redesign_blueprint([dict(row) for row in jobs])
    source_campaign = get_campaign(conn, source_campaign_id)
    campaign_key = strategy_redesign_campaign_key(blueprint)
    campaign_name = name or "Phase 9.11 Strategy Redesign and Research-Direction Decision"
    row = conn.execute(
        """
        INSERT INTO research_campaigns(campaign_key, name, universe_key, status, requested_candidates, controls, scheduling_config, safety_statement, simulation_only)
        VALUES (%s, %s, %s, 'queued', %s, %s, %s, %s, TRUE)
        ON CONFLICT(campaign_key) DO UPDATE
        SET updated_at = NOW()
        RETURNING *
        """,
        (
            campaign_key,
            campaign_name,
            source_campaign["universe_key"],
            len(blueprint["candidates"]),
            Jsonb(
                {
                    "campaign_version": STRATEGY_REDESIGN_CAMPAIGN_VERSION,
                    "source_campaign": source_campaign_id,
                    "objective": "Decide whether Pullback can be structurally redesigned for robustness or whether research should shift to new deterministic families.",
                    "candidate_quality_policy": "Structural changes only; no broad local parameter tuning; validation thresholds and evidence gates are unchanged.",
                    "paper_deployment_policy": "Create candidate-linked simulation-only deployment only after existing elite gates pass.",
                    "tracks": blueprint["tracks"],
                    "lineage": blueprint["lineage"],
                    "targeting": blueprint["targeting"],
                    "complexity_policy": blueprint["complexity_policy"],
                }
            ),
            Jsonb({**DEFAULT_SCHEDULING_CONFIG, "batch_size": 15, "daily_experiment_budget": 210, "max_generated_candidates": len(blueprint["candidates"])}),
            SAFETY_STATEMENT,
        ),
    ).fetchone()
    campaign_id = int(row["id"])
    created = queue_campaign_jobs(conn, campaign_id, blueprint["candidates"], blueprint["targeting"]["assets"], blueprint["targeting"]["timeframes"])
    update_campaign_counts(conn, campaign_id)
    conn.commit()
    return {
        "campaign": jsonable(dict(row)),
        "candidates_generated": len(blueprint["candidates"]),
        "jobs_created": created,
        "targeting": blueprint["targeting"],
        "tracks": blueprint["tracks"],
        "lineage": blueprint["lineage"],
        "complexity_policy": blueprint["complexity_policy"],
        "campaign_version": STRATEGY_REDESIGN_CAMPAIGN_VERSION,
        "simulation_only": True,
        "safety": SAFETY_STATEMENT,
    }


def strategy_redesign_blueprint(source_jobs: list[dict[str, Any]]) -> dict[str, Any]:
    pullback_rows = [row for row in source_jobs if row.get("strategy_family") == "Pullback"]
    trend_rows = [row for row in source_jobs if row.get("strategy_family") == "Trend Following"]
    pullback_parent_row = next((row for row in pullback_rows if row.get("candidate_id") == "sd_3ffffc89f82b5c"), None)
    if pullback_parent_row is None:
        pullback_parent_row = best_row_per_candidate([row for row in pullback_rows if row.get("status") == "promoted"] or pullback_rows)[0]
    trend_parent_row = best_row_per_candidate(trend_rows)[0] if trend_rows else pullback_parent_row
    pullback_parent = candidate_from_payload(pullback_parent_row["candidate"])
    trend_parent = candidate_from_payload(trend_parent_row["candidate"])
    candidates: list[DiscoveryCandidate] = []
    seen: set[str] = set()
    lineage: list[dict[str, Any]] = []
    for mutation in pullback_redesign_mutations():
        child = phase_9_11_child(pullback_parent, mutation)
        add_candidate(candidates, seen, child)
        lineage.append(phase_9_11_lineage_row(child, pullback_parent, mutation, pullback_parent_row))
    for mutation in new_family_pilot_mutations():
        child = phase_9_11_child(pullback_parent, mutation)
        add_candidate(candidates, seen, child)
        lineage.append(phase_9_11_lineage_row(child, pullback_parent, mutation, pullback_parent_row))
    for mutation in trend_pause_confirmation_mutations():
        child = phase_9_11_child(trend_parent, mutation)
        add_candidate(candidates, seen, child)
        lineage.append(phase_9_11_lineage_row(child, trend_parent, mutation, trend_parent_row))
    assets = ["AAPL", "MSFT", "GOOGL", "META", "NVDA", "QQQ", "SPY"]
    return {
        "targeting": {
            "assets": assets,
            "timeframes": ["1h"],
            "candidate_count": len(candidates),
            "job_count": len(candidates) * len(assets),
            "primary_asset": "AAPL",
            "context_assets": ["SPY", "QQQ"],
            "context_timeframes": ["4h"],
            "strategy_mix": dict(Counter(row["strategy_family"] for row in lineage)),
        },
        "tracks": {
            "pullback_architecture_redesign": {"candidate_count": 20, "hypothesis": "Meaningful structural filters can improve robustness where local parameter tuning failed."},
            "higher_timeframe_context": {"execution_timeframe": "1h", "context_timeframe": "4h", "hypothesis": "4h context improves 1h entries without causing 4h execution sample starvation."},
            "market_relative_logic": {"context_assets": ["SPY", "QQQ"], "hypothesis": "AAPL pullback quality depends on constructive broad-market or relative-strength context."},
            "sideways_transition_redesign": {"hypothesis": "Transition recognition can outperform simple sideways exclusion."},
            "new_family_pilot": {"candidate_count": 8, "families": ["Volatility Expansion", "Relative Strength Continuation", "Sideways Transition", "Regime Adaptive Mean Reversion"]},
            "trend_following_pause_decision": {"candidate_count": 2, "policy": "Pause if no market-level passes are produced."},
        },
        "complexity_policy": {
            "max_added_rules": 2,
            "max_added_parameters": 5,
            "reject_if_improvement_depends_on_excessive_complexity": True,
        },
        "lineage": lineage,
        "candidates": candidates,
        "simulation_only": True,
    }


def pullback_redesign_mutations() -> list[dict[str, Any]]:
    base = phase_9_11_pullback_base()
    return [
        phase_9_11_mutation("Pullback", "ema_slope_pullback", "Trend-aligned pullback with explicit EMA slope.", {**base, "phase_9_9_ema_slope_min": 0.0003}, "explicit EMA slope", "Improve trend alignment and reduce sideways entries.", "Fails if trade count or cross-asset passes do not improve.", 1, 1),
        phase_9_11_mutation("Pullback", "adx_proxy_pullback", "Pullback plus trend-strength confirmation.", {**base, "momentum": "adx_proxy", "returns_5_min": 0.008}, "returns-based ADX proxy", "Improve robustness through trend-strength confirmation.", "Fails if momentum proxy starves evidence.", 1, 2),
        phase_9_11_mutation("Pullback", "volatility_contraction_pullback", "Pullback after volatility contraction.", {**base, "phase_9_11_volatility_contraction": True, "phase_9_11_volatility_max": 0.018}, "volatility contraction filter", "Avoid noisy pullbacks while preserving setups.", "Fails if contraction filter only reduces trades.", 1, 2),
        phase_9_11_mutation("Pullback", "relative_volume_pullback", "Pullback with stronger relative-volume confirmation.", {**base, "volume_change_min": 0.2}, "relative volume confirmation", "Improve participation quality.", "Fails if higher volume does not improve robustness.", 1, 1),
        phase_9_11_mutation("Pullback", "context_4h_ema_pullback", "1h entry with positive 4h EMA context.", {**base, "phase_9_11_require_4h_ema_positive": True}, "4h EMA context", "Improve transfer and stability without 4h execution.", "Fails if 4h context reduces trades below gate.", 1, 1, "higher_timeframe_context"),
        phase_9_11_mutation("Pullback", "context_4h_momentum_pullback", "1h entry with constructive 4h momentum.", {**base, "phase_9_11_require_4h_positive_returns": True}, "4h momentum context", "Improve timing and OOS consistency.", "Fails if 4h momentum does not improve PF/expectancy.", 1, 1, "higher_timeframe_context"),
        phase_9_11_mutation("Pullback", "context_4h_bull_pullback", "1h entry only when 4h regime is bull trend.", {**base, "phase_9_11_regime_filter": True, "phase_9_11_require_4h_bull": True}, "4h bull-regime context", "Reduce weak 1h regimes.", "Fails if bull filter starves trades.", 1, 2, "higher_timeframe_context"),
        phase_9_11_mutation("Pullback", "context_4h_sideways_block", "1h entry blocks 4h sideways context.", {**base, "phase_9_11_regime_filter": True, "phase_9_11_block_4h_sideways": True}, "4h sideways block", "Improve sideways robustness.", "Fails if sideways block lacks material improvement.", 1, 2, "higher_timeframe_context"),
        phase_9_11_mutation("Pullback", "spy_trend_confirmed_pullback", "Pullback requires SPY trend confirmation.", {**base, "phase_9_11_require_spy_trend": True, "phase_9_11_market_returns_min": 0}, "SPY trend confirmation", "Test broad-market dependency.", "Fails if market confirmation does not improve stability.", 1, 2, "market_relative_logic"),
        phase_9_11_mutation("Pullback", "qqq_trend_confirmed_pullback", "Pullback requires QQQ trend confirmation.", {**base, "phase_9_11_require_qqq_trend": True, "phase_9_11_market_returns_min": 0}, "QQQ trend confirmation", "Test tech-index dependency.", "Fails if QQQ confirmation does not improve transfer.", 1, 2, "market_relative_logic"),
        phase_9_11_mutation("Pullback", "relative_spy_pullback", "AAPL relative strength versus SPY confirms pullback.", {**base, "phase_9_11_require_relative_spy": True, "phase_9_11_relative_returns_min": 0}, "relative strength vs SPY", "Improve AAPL-specific selection quality.", "Fails if relative strength is not additive.", 1, 2, "market_relative_logic"),
        phase_9_11_mutation("Pullback", "relative_qqq_pullback", "AAPL relative strength versus QQQ confirms pullback.", {**base, "phase_9_11_require_relative_qqq": True, "phase_9_11_relative_returns_min": 0}, "relative strength vs QQQ", "Improve large-cap tech context.", "Fails if relative strength reduces robustness.", 1, 2, "market_relative_logic"),
        phase_9_11_mutation("Pullback", "dynamic_atr_distance", "Pullback uses dynamic ATR-normalized distance.", {**base, "entry_distance_to_ema20_max": 0.045, "phase_9_10_volatility_normalized_pullback": True}, "ATR-normalized pullback distance", "Improve cross-asset comparability.", "Fails if transfer remains AAPL-only.", 1, 2),
        phase_9_11_mutation("Pullback", "regime_adaptive_exit", "Pullback uses lower RR in weak regimes proxy.", {**base, "risk_reward": 1.25, "phase_9_11_regime_filter": True, "phase_9_11_block_4h_sideways": True}, "adaptive conservative exit", "Improve realized conversion and drawdown.", "Fails if lower target weakens PF.", 2, 3),
        phase_9_11_mutation("Pullback", "multi_bar_momentum_pullback", "Pullback after multi-bar momentum confirmation.", {**base, "returns_5_min": 0.006, "trend_requires_positive_returns": True}, "multi-bar momentum confirmation", "Reduce low-quality pullbacks.", "Fails if momentum filter does not improve stability.", 1, 2),
        phase_9_11_mutation("Pullback", "sideways_transition_pullback", "Pullback enters after sideways breakout transition.", {**base, "phase_9_11_entry_mode": "sideways_transition", "phase_9_11_transition_distance_min": 0.012, "returns_5_min": 0.008}, "sideways transition entry", "Recognize transitions instead of blocking sideways.", "Fails if transition evidence is too sparse.", 2, 3, "sideways_transition"),
        phase_9_11_mutation("Pullback", "trend_strength_after_consolidation", "Pullback requires increasing EMA separation.", {**base, "phase_9_11_ema_separation_increasing": True}, "increasing EMA separation", "Capture trend strengthening after consolidation.", "Fails if separation condition is not additive.", 1, 1, "sideways_transition"),
        phase_9_11_mutation("Pullback", "vol_expansion_after_contraction", "Pullback requires volatility expansion after contraction proxy.", {**base, "phase_9_11_volatility_expansion": True, "phase_9_11_volatility_min": 0.008, "returns_5_min": 0.008}, "volatility expansion transition", "Improve post-consolidation entries.", "Fails if volatility expansion increases drawdown.", 1, 2, "sideways_transition"),
        phase_9_11_mutation("Pullback", "spy_and_4h_context", "Pullback combines one market and one 4h context rule.", {**base, "phase_9_11_require_spy_trend": True, "phase_9_11_require_4h_ema_positive": True}, "SPY plus 4h context", "Test minimal combined context.", "Fails if two added rules do not improve robustness.", 2, 2),
        phase_9_11_mutation("Pullback", "qqq_relative_and_4h_context", "Pullback combines QQQ relative strength and 4h momentum.", {**base, "phase_9_11_require_relative_qqq": True, "phase_9_11_require_4h_positive_returns": True}, "QQQ relative plus 4h momentum", "Test tech-relative context.", "Fails if added complexity is not justified.", 2, 2),
    ]


def new_family_pilot_mutations() -> list[dict[str, Any]]:
    base = phase_9_11_pullback_base()
    return [
        phase_9_11_mutation("Volatility Expansion", "volatility_expansion_continuation", "Continuation after volatility expansion.", {**base, "entry": "trend_continuation", "momentum": "roc", "phase_9_11_entry_mode": "volatility_expansion_continuation", "returns_5_min": 0.008, "phase_9_11_volatility_min": 0.008, "phase_9_11_strategy_family": "Volatility Expansion"}, "volatility expansion continuation", "Pilot a distinct expansion family with enough signal frequency.", "Fails if expansion produces weak PF or high drawdown.", 2, 4, "new_family_pilot"),
        phase_9_11_mutation("Volatility Expansion", "vol_expansion_market_confirmed", "Volatility expansion with SPY confirmation.", {**base, "entry": "trend_continuation", "momentum": "roc", "phase_9_11_entry_mode": "volatility_expansion_continuation", "returns_5_min": 0.006, "phase_9_11_volatility_min": 0.008, "phase_9_11_require_spy_trend": True, "phase_9_11_strategy_family": "Volatility Expansion"}, "expansion plus market confirmation", "Reduce false expansion signals.", "Fails if market filter does not improve quality.", 2, 5, "new_family_pilot"),
        phase_9_11_mutation("Relative Strength Continuation", "relative_spy_continuation", "Relative-strength continuation versus SPY.", {**base, "entry": "trend_continuation", "momentum": "roc", "phase_9_11_entry_mode": "relative_strength_continuation", "phase_9_11_require_relative_spy": True, "returns_5_min": 0.006, "phase_9_11_strategy_family": "Relative Strength Continuation"}, "relative strength continuation", "Pilot broad-market relative momentum.", "Fails if relative momentum remains AAPL-only.", 2, 4, "new_family_pilot"),
        phase_9_11_mutation("Relative Strength Continuation", "relative_qqq_continuation", "Relative-strength continuation versus QQQ.", {**base, "entry": "trend_continuation", "momentum": "roc", "phase_9_11_entry_mode": "relative_strength_continuation", "phase_9_11_require_relative_qqq": True, "returns_5_min": 0.006, "phase_9_11_strategy_family": "Relative Strength Continuation"}, "relative strength vs QQQ", "Pilot tech-relative continuation.", "Fails if no market-level passes emerge.", 2, 4, "new_family_pilot"),
        phase_9_11_mutation("Sideways Transition", "sideways_transition_continuation", "Continuation after sideways transition.", {**base, "entry": "trend_continuation", "momentum": "roc", "phase_9_11_entry_mode": "sideways_transition", "phase_9_11_transition_distance_min": 0.012, "returns_5_min": 0.008, "phase_9_11_strategy_family": "Sideways Transition"}, "sideways transition continuation", "Pilot transition recognition as distinct family.", "Fails if transitions are sparse or unstable.", 2, 4, "new_family_pilot"),
        phase_9_11_mutation("Multi-Timeframe Momentum", "mtf_momentum", "1h momentum with 4h constructive context.", {**base, "entry": "trend_continuation", "momentum": "roc", "returns_5_min": 0.008, "phase_9_11_require_4h_positive_returns": True, "phase_9_11_strategy_family": "Multi-Timeframe Momentum"}, "multi-timeframe momentum", "Pilot MTF momentum without 4h execution.", "Fails if 4h context starves trades.", 2, 3, "new_family_pilot"),
        phase_9_11_mutation("Trend-Breakout Hybrid", "ema_separation_continuation", "Trend continuation with increasing EMA separation.", {**base, "entry": "trend_continuation", "momentum": "roc", "returns_5_min": 0.008, "phase_9_11_ema_separation_increasing": True, "phase_9_11_strategy_family": "Trend-Breakout Hybrid"}, "EMA separation continuation", "Pilot deterministic trend-breakout hybrid without broad breakout sweep.", "Fails if it behaves like unstable Trend Following.", 2, 3, "new_family_pilot"),
        phase_9_11_mutation("Regime Adaptive Mean Reversion", "regime_adaptive_mean_reversion", "Mean reversion only with market trend support.", {**base, "entry": "mean_reversion", "momentum": "stochastic_proxy", "rsi_min": 35, "rsi_max": 55, "rsi_oversold": 42, "phase_9_11_require_spy_trend": True, "phase_9_11_strategy_family": "Regime Adaptive Mean Reversion"}, "regime-adaptive mean reversion", "Pilot distinct deterministic mean reversion in supportive market context.", "Fails if expectancy or drawdown fails gates.", 2, 5, "new_family_pilot"),
    ]


def trend_pause_confirmation_mutations() -> list[dict[str, Any]]:
    base = {"trend_fast": 20, "trend_slow": 50, "entry": "trend_continuation", "momentum": "roc", "returns_5_min": 0.008, "volume_change_min": 0.05, "risk_reward": 1.5, "atr_multiplier": 1.5}
    return [
        phase_9_11_mutation("Trend Following", "trend_context_confirmed", "Final Trend confirmation with SPY and 4h support.", {**base, "phase_9_11_require_spy_trend": True, "phase_9_11_require_4h_positive_returns": True}, "SPY plus 4h trend context", "Give Trend Following one final bounded confirmation.", "Pause if no market-level passes.", 2, 4, "trend_pause_confirmation"),
        phase_9_11_mutation("Trend Following", "trend_lowvol_block_confirmed", "Final Trend confirmation excluding low-volatility weakness.", {**base, "phase_9_9_regime_filter": True, "phase_9_9_low_volatility_block": True, "phase_9_9_low_volatility_min": 0.008}, "low-volatility block", "Check whether the repeated weakness is removable.", "Pause if still no promotions.", 1, 3, "trend_pause_confirmation"),
    ]


def phase_9_11_pullback_base() -> dict[str, Any]:
    return {
        "trend_fast": 20,
        "trend_slow": 50,
        "trend_method": "ema",
        "momentum": "rsi",
        "rsi_min": 55,
        "volatility": "atr",
        "atr_multiplier": 1.5,
        "entry": "pullback",
        "entry_distance_to_ema20_max": 0.04,
        "risk_reward": 1.4,
        "volume_change_min": 0.15,
        "fee_rate": 0.001,
        "slippage_rate": 0.0005,
    }


def phase_9_11_mutation(strategy_family: str, mutation_type: str, hypothesis: str, changes: dict[str, Any], new_rule: str, expected: str, falsification: str, added_rules: int, added_parameters: int, track: str = "pullback_redesign") -> dict[str, Any]:
    return {
        "strategy_family": strategy_family,
        "mutation_type": mutation_type,
        "hypothesis": hypothesis,
        "changes": changes,
        "new_rule": new_rule,
        "expected_improvement": expected,
        "falsification_condition": falsification,
        "added_complexity": {"added_rules": added_rules, "added_parameters": added_parameters},
        "track": track,
        "evidence_source": "Phase 9.10 completed campaign evidence",
        "campaign_version": STRATEGY_REDESIGN_CAMPAIGN_VERSION,
    }


def phase_9_11_child(parent: DiscoveryCandidate, mutation: dict[str, Any]) -> DiscoveryCandidate:
    blocks = dict(parent.blocks)
    params = {
        **parent.parameters,
        **mutation["changes"],
        "phase_9_11_mutation_type": mutation["mutation_type"],
        "phase_9_11_strategy_family": mutation["strategy_family"],
        "phase_9_11_hypothesis": mutation["hypothesis"],
        "phase_9_11_new_rule": mutation["new_rule"],
        "phase_9_11_expected_improvement": mutation["expected_improvement"],
        "phase_9_11_falsification_condition": mutation["falsification_condition"],
        "phase_9_11_added_complexity": mutation["added_complexity"],
        "phase_9_11_track": mutation["track"],
        "phase_9_11_campaign_version": STRATEGY_REDESIGN_CAMPAIGN_VERSION,
    }
    if "entry" in params:
        blocks["entry"] = str(params["entry"])
    if "momentum" in params:
        blocks["momentum"] = str(params["momentum"])
    key = canonical_candidate_key(blocks, params, parent.candidate_id)
    return DiscoveryCandidate(
        candidate_id=f"sd_{sha256(key.encode()).hexdigest()[:14]}",
        family_id=parent.family_id,
        parent_candidate_id=parent.candidate_id,
        generation=parent.generation + 1,
        blocks=blocks,
        parameters=params,
        complexity=parent.complexity + mutation["added_complexity"]["added_rules"],
        canonical_key=key,
    )


def phase_9_11_lineage_row(child: DiscoveryCandidate, parent: DiscoveryCandidate, mutation: dict[str, Any], parent_row: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": child.candidate_id,
        "parent_candidate_id": parent.candidate_id,
        "strategy_family": mutation["strategy_family"],
        "structural_change": mutation["mutation_type"],
        "economic_hypothesis": mutation["hypothesis"],
        "new_rule": mutation["new_rule"],
        "expected_improvement": mutation["expected_improvement"],
        "falsification_condition": mutation["falsification_condition"],
        "added_complexity": mutation["added_complexity"],
        "entry_rule_count": 5 + mutation["added_complexity"]["added_rules"],
        "filter_count": mutation["added_complexity"]["added_rules"],
        "exit_rule_count": 1,
        "parameter_count": len(child.parameters),
        "track": mutation["track"],
        "evidence_source": mutation["evidence_source"],
        "source_job": parent_row.get("id"),
        "source_symbol": parent_row.get("symbol"),
        "source_timeframe": parent_row.get("timeframe"),
        "campaign_version": STRATEGY_REDESIGN_CAMPAIGN_VERSION,
    }


def strategy_redesign_campaign_key(blueprint: dict[str, Any]) -> str:
    material = {
        "version": STRATEGY_REDESIGN_CAMPAIGN_VERSION,
        "assets": blueprint["targeting"]["assets"],
        "timeframes": blueprint["targeting"]["timeframes"],
        "lineage": [(row["parent_candidate_id"], row["structural_change"], row["new_rule"]) for row in blueprint["lineage"]],
    }
    return sha256(repr(material).encode("utf-8")).hexdigest()


def create_volatility_adaptive_relative_strength_campaign(
    conn: psycopg.Connection,
    *,
    source_campaign_id: int,
    name: str | None = None,
) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    jobs = [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM research_campaign_jobs
            WHERE campaign_id = %s AND simulation_only = TRUE
            ORDER BY validation_score DESC, id ASC
            """,
            (source_campaign_id,),
        ).fetchall()
    ]
    blueprint = volatility_adaptive_relative_strength_blueprint(jobs)
    source_campaign = get_campaign(conn, source_campaign_id)
    campaign_key = volatility_adaptive_relative_strength_campaign_key(blueprint)
    row = conn.execute(
        """
        INSERT INTO research_campaigns(campaign_key, name, universe_key, status, requested_candidates, controls, scheduling_config, safety_statement, simulation_only)
        VALUES (%s, %s, %s, 'queued', %s, %s, %s, %s, TRUE)
        ON CONFLICT(campaign_key) DO UPDATE SET updated_at = NOW()
        RETURNING *
        """,
        (
            campaign_key,
            name or "Phase 9.12 Volatility-Adaptive Relative-Strength Validation",
            source_campaign["universe_key"],
            len(blueprint["candidates"]),
            Jsonb(
                {
                    "campaign_version": VOLATILITY_ADAPTIVE_RELATIVE_STRENGTH_CAMPAIGN_VERSION,
                    "source_campaign": source_campaign_id,
                    "objective": "Validate one asset-agnostic relative-strength architecture across AAPL and NVDA with volatility-selected parameter profiles.",
                    "candidate_quality_policy": "Focused falsification around measured volatility regimes; validation thresholds and evidence gates are unchanged.",
                    "paper_deployment_policy": "Candidate-linked simulation is allowed only after every existing elite gate passes; Phase 10 remains locked.",
                    "targeting": blueprint["targeting"],
                    "lineage": blueprint["lineage"],
                }
            ),
            Jsonb({**DEFAULT_SCHEDULING_CONFIG, "batch_size": 24, "daily_experiment_budget": 24, "max_generated_candidates": len(blueprint["candidates"])}),
            SAFETY_STATEMENT,
        ),
    ).fetchone()
    campaign_id = int(row["id"])
    created = queue_campaign_jobs(conn, campaign_id, blueprint["candidates"], blueprint["targeting"]["assets"], blueprint["targeting"]["timeframes"])
    update_campaign_counts(conn, campaign_id)
    conn.commit()
    return {
        "campaign": jsonable(dict(row)),
        "candidates_generated": len(blueprint["candidates"]),
        "jobs_created": created,
        "targeting": blueprint["targeting"],
        "lineage": blueprint["lineage"],
        "campaign_version": VOLATILITY_ADAPTIVE_RELATIVE_STRENGTH_CAMPAIGN_VERSION,
        "simulation_only": True,
        "safety": SAFETY_STATEMENT,
    }


def volatility_adaptive_relative_strength_blueprint(source_jobs: list[dict[str, Any]]) -> dict[str, Any]:
    parents = [row for row in source_jobs if row.get("strategy_family") == "Relative Strength Continuation"]
    if not parents:
        raise ValueError("Phase 9.12 requires a Phase 9.11 Relative Strength Continuation parent.")
    parent_row = best_row_per_candidate(parents)[0]
    parent = candidate_from_payload(parent_row["candidate"])
    profiles = [
        ("balanced_007", .007, .002, -.0075, 1.6, 5, .001, .004, 1.7, 8),
        ("balanced_006", .006, .003, -.005, 1.6, 3, .001, .004, 1.7, 5),
        ("momentum_007", .007, .003, -.005, 1.6, 5, 0, .004, 1.8, 8),
        ("wider_high_reward", .007, .003, -.005, 1.6, 3, 0, .004, 1.9, 8),
        ("low_boundary", .0065, .003, -.005, 1.6, 3, 0, .003, 1.8, 8),
        ("high_boundary", .0075, .002, -.0075, 1.6, 5, .001, .004, 1.7, 8),
        ("relative_003", .007, .002, -.005, 1.6, 5, 0, .003, 1.7, 8),
        ("relative_005", .007, .002, -.005, 1.6, 5, 0, .005, 1.7, 8),
        ("high_rr_18", .007, .002, -.0075, 1.6, 5, .001, .004, 1.8, 8),
        ("low_rr_15", .007, .002, -.0075, 1.5, 5, .001, .004, 1.7, 8),
        ("short_swing", .007, .002, -.0075, 1.6, 3, .001, .004, 1.7, 8),
        ("high_swing_5", .007, .002, -.0075, 1.6, 5, .001, .004, 1.7, 5),
    ]
    candidates = []
    lineage = []
    for name, boundary, low_ret, low_rel, low_rr, low_swing, high_ret, high_rel, high_rr, high_swing in profiles:
        params = {
            **parent.parameters,
            "strategy_architecture": "relative_strength_continuation_v2",
            "recent_candle_window_bars": 220,
            "adaptive_volatility_profiles": True,
            "volatility_profile_boundary": boundary,
            "rsi_min": 45,
            "rsi_max": 70,
            "low_vol_returns_5_min": low_ret,
            "low_vol_relative_returns_5_min": low_rel,
            "low_vol_volume_change_min": .1,
            "low_vol_risk_reward": low_rr,
            "low_vol_swing_lookback": low_swing,
            "high_vol_returns_5_min": high_ret,
            "high_vol_relative_returns_5_min": high_rel,
            "high_vol_volume_change_min": .1,
            "high_vol_risk_reward": high_rr,
            "high_vol_swing_lookback": high_swing,
            "phase_9_12_profile": name,
            "phase_9_12_strategy_family": "Volatility-Adaptive Relative Strength",
            "phase_9_12_campaign_version": VOLATILITY_ADAPTIVE_RELATIVE_STRENGTH_CAMPAIGN_VERSION,
        }
        key = canonical_candidate_key(parent.blocks, params, parent.candidate_id)
        child = DiscoveryCandidate(
            candidate_id=f"sd_{sha256(key.encode()).hexdigest()[:14]}",
            family_id=parent.family_id,
            parent_candidate_id=parent.candidate_id,
            generation=parent.generation + 1,
            blocks=dict(parent.blocks),
            parameters=params,
            complexity=parent.complexity + 2,
            canonical_key=key,
        )
        candidates.append(child)
        lineage.append(
            {
                "candidate_id": child.candidate_id,
                "parent_candidate_id": parent.candidate_id,
                "strategy_family": "Volatility-Adaptive Relative Strength",
                "profile": name,
                "hypothesis": "A volatility-observed profile boundary transfers one relative-strength rule set across AAPL and NVDA.",
                "falsification_condition": "Reject unless both assets pass every unchanged single-market gate and the aggregate passes every elite gate.",
                "source_job": parent_row.get("id"),
                "campaign_version": VOLATILITY_ADAPTIVE_RELATIVE_STRENGTH_CAMPAIGN_VERSION,
            }
        )
    return {
        "candidates": candidates,
        "lineage": lineage,
        "targeting": {"assets": ["AAPL", "NVDA"], "timeframes": ["1h"], "candidate_count": len(candidates), "job_count": len(candidates) * 2},
    }


def volatility_adaptive_relative_strength_campaign_key(blueprint: dict[str, Any]) -> str:
    material = {
        "version": VOLATILITY_ADAPTIVE_RELATIVE_STRENGTH_CAMPAIGN_VERSION,
        "assets": blueprint["targeting"]["assets"],
        "timeframes": blueprint["targeting"]["timeframes"],
        "candidates": [candidate.candidate_id for candidate in blueprint["candidates"]],
    }
    return sha256(repr(material).encode("utf-8")).hexdigest()


def get_universe(conn: psycopg.Connection, universe_key: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT universe_key, name, description, assets, default_timeframes, metadata
        FROM research_universes
        WHERE universe_key = %s AND is_active = TRUE AND simulation_only = TRUE
        """,
        (universe_key,),
    ).fetchone()
    if not row:
        raise ValueError(f"research universe '{universe_key}' was not found")
    return dict(row)


def queue_campaign_jobs(
    conn: psycopg.Connection,
    campaign_id: int,
    candidates: list[DiscoveryCandidate],
    assets: list[str],
    timeframes: list[str],
) -> int:
    started = time.perf_counter()
    original_candidates = len(candidates)
    candidates = dedupe_candidates_by_execution_key(candidates, len(candidates))
    expected_jobs = len(candidates) * len(assets) * len(timeframes)
    log_event("Generating jobs", campaign_id=campaign_id, assets=len(assets), strategies=len(candidates), timeframes=len(timeframes), expected_jobs=expected_jobs)
    created = 0
    batch_number = 1
    batch_job_count = 0
    batch_id = ensure_campaign_batch(conn, campaign_id, batch_number)
    for candidate in candidates:
        payload = Jsonb(jsonable(asdict(candidate)))
        for symbol in assets:
            for timeframe in timeframes:
                if batch_job_count >= DEFAULT_BATCH_SIZE:
                    batch_number += 1
                    batch_job_count = 0
                    batch_id = ensure_campaign_batch(conn, campaign_id, batch_number)
                job_key = research_job_key(campaign_id, candidate.candidate_id, symbol, timeframe)
                row = conn.execute(
                    """
                    INSERT INTO research_campaign_jobs(campaign_id, batch_id, job_key, candidate_id, family_id, symbol, timeframe, strategy_family, candidate, simulation_only)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT(job_key) DO NOTHING
                    RETURNING id
                    """,
                    (campaign_id, batch_id, job_key, candidate.candidate_id, candidate.family_id, symbol, timeframe, strategy_family_for_candidate(candidate), payload),
                ).fetchone()
                if row:
                    created += 1
                    batch_job_count += 1
                    conn.execute("UPDATE research_campaign_batches SET job_count = job_count + 1, updated_at = NOW() WHERE id = %s", (batch_id,))
    log_event("Jobs inserted", campaign_id=campaign_id, expected_jobs=expected_jobs, actual_jobs_inserted=created, elapsed_ms=elapsed_ms(started))
    if original_candidates != len(candidates):
        conn.execute(
            """
            UPDATE research_campaigns
            SET controls = controls || %s::jsonb,
                updated_at = NOW()
            WHERE id = %s
            """,
            (
                Jsonb(
                    {
                        "execution_key_deduplication": {
                            "attempted_candidate_generations": original_candidates,
                            "duplicates_prevented": original_candidates - len(candidates),
                            "jobs_skipped": (original_candidates - len(candidates)) * len(assets) * len(timeframes),
                        }
                    }
                ),
                campaign_id,
            ),
        )
    return created


def ensure_campaign_batch(conn: psycopg.Connection, campaign_id: int, batch_number: int) -> int:
    key = campaign_batch_key(campaign_id, batch_number)
    row = conn.execute(
        """
        INSERT INTO research_campaign_batches(campaign_id, batch_key, batch_number, simulation_only)
        VALUES (%s, %s, %s, TRUE)
        ON CONFLICT(batch_key) DO UPDATE SET updated_at = NOW()
        RETURNING id
        """,
        (campaign_id, key, batch_number),
    ).fetchone()
    return int(row["id"])


def campaign_batch_key(campaign_id: int, batch_number: int) -> str:
    return sha256(f"campaign_batch|{campaign_id}|{batch_number}".encode()).hexdigest()


def strategy_family_for_candidate(candidate: DiscoveryCandidate | dict[str, Any]) -> str:
    blocks = candidate.blocks if isinstance(candidate, DiscoveryCandidate) else dict(candidate.get("blocks") or {})
    params = candidate.parameters if isinstance(candidate, DiscoveryCandidate) else dict(candidate.get("parameters") or {})
    if params.get("phase2_strategy_family"):
        return str(params["phase2_strategy_family"])
    if params.get("phase_9_12_strategy_family"):
        return str(params["phase_9_12_strategy_family"])
    if params.get("phase_9_11_strategy_family"):
        return str(params["phase_9_11_strategy_family"])
    if params.get("hypothesis_strategy_family"):
        return str(params["hypothesis_strategy_family"])
    entry = str(blocks.get("entry", ""))
    trend = str(blocks.get("trend", ""))
    momentum = str(blocks.get("momentum", ""))
    if "mean_reversion" in entry:
        return "Mean Reversion"
    if "breakout" in entry or "opening_range" in entry:
        return "Breakout"
    if "pullback" in entry:
        return "Pullback"
    if "trend" in entry or "ema" in trend or "supertrend" in trend:
        return "Trend Following"
    if momentum:
        return "Momentum"
    return "Other"


def run_research_campaign_batch(
    conn: psycopg.Connection,
    *,
    campaign_id: int,
    batch_size: int = 50,
    worker_id: str | None = None,
    ensure_tables: bool = True,
    coordinate_campaign: bool = True,
    dataset_cache: dict[tuple[Any, ...], dict[str, Any]] | None = None,
    allowed_dataset_keys: list[str] | None = None,
) -> dict[str, Any]:
    if ensure_tables:
        ensure_campaign_tables(conn)
    campaign = get_campaign(conn, campaign_id)
    if campaign["status"] in {"paused", "canceled", "completed"}:
        return {"campaign_id": campaign_id, "status": campaign["status"], "processed": 0, "simulation_only": True}
    config = scheduling_config(campaign)
    if coordinate_campaign:
        recover_expired_campaign_jobs(conn, campaign_id=campaign_id, retry_limit=int(config["retry_limit"]))
        conn.execute(
            """
            UPDATE research_campaigns
            SET status = 'running', started_at = COALESCE(started_at, NOW()), updated_at = NOW()
            WHERE id = %s AND status IN ('queued', 'running', 'failed')
            """,
            (campaign_id,),
        )
    worker_id = worker_id or deterministic_worker_id(campaign_id)
    claim_started = time.perf_counter()
    jobs = claim_campaign_jobs(
        conn,
        campaign_id=campaign_id,
        worker_id=worker_id,
        batch_size=min(batch_size, int(config["batch_size"])),
        lease_seconds=int(config["worker_lease_seconds"]),
        retry_limit=int(config["retry_limit"]),
        allowed_dataset_keys=allowed_dataset_keys,
    )
    # Make claims and leases visible before simulations begin. This prevents other
    # workers from reporting an empty pool and makes crash recovery durable.
    conn.commit()
    queue_claim_ms = round((time.perf_counter() - claim_started) * 1000, 3)
    completed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    worker_dataset_cache = dataset_cache if dataset_cache is not None else {}
    for job in jobs:
        started = time.perf_counter()
        try:
            set_campaign_worker_assignment(conn, worker_id, campaign_id=campaign_id, current_job_id=int(job["id"]), status="running")
            conn.commit()
            readiness = data_readiness_for_job(conn, dict(job))
            if not readiness["ready"]:
                mark_claimed_job_deferred(conn, dict(job), readiness)
                best_effort_worker_heartbeat(conn, worker_id, status="running")
                conn.commit()
                failed.append({"job_id": job["id"], "candidate_id": job["candidate_id"], "symbol": job["symbol"], "timeframe": job["timeframe"], "deferred": readiness["status"], "reason": readiness["reason"]})
                continue
            refresh_job_heartbeat(conn, int(job["id"]), worker_id, int(config["worker_lease_seconds"]))
            best_effort_worker_heartbeat(conn, worker_id, status="running")
            conn.commit()
            job_payload = {**dict(job), "_dataset_cache": worker_dataset_cache}
            result = run_campaign_job(conn, job_payload)
            runtime_ms = int((time.perf_counter() - started) * 1000)
            status = "promoted" if passes_single_market_validation(result) else "rejected"
            profile = dict(result.pop("execution_profile", {}))
            profile["queue_operations_ms"] = round(queue_claim_ms / max(1, len(jobs)), 3)
            write_started = time.perf_counter()
            conn.execute(
                """
                UPDATE research_campaign_jobs
                SET status = %s,
                    result = %s,
                    validation_score = %s,
                    failure_reasons = %s,
                    latest_error = NULL,
                    worker_id = NULL,
                    lease_expires_at = NULL,
                    completed_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    status,
                    Jsonb(jsonable(result)),
                    finite_metric(result.get("research_score")),
                    Jsonb(jsonable(result.get("failure_reasons") or [])),
                    job["id"],
                ),
            )
            profile["writing_results_ms"] = round((time.perf_counter() - write_started) * 1000, 3)
            record_job_runtime(conn, int(job["id"]), runtime_ms, profile)
            conn.commit()
            best_effort_worker_heartbeat(conn, worker_id, status="running")
            completed.append({"job_id": job["id"], "candidate_id": job["candidate_id"], "symbol": job["symbol"], "timeframe": job["timeframe"], "status": status})
        except Exception as error:  # noqa: BLE001 - one failed asset/timeframe must not stop the campaign
            safe_rollback(conn)
            fail_or_retry_claimed_job(conn, dict(job), error, config)
            conn.commit()
            best_effort_worker_heartbeat(conn, worker_id, status="running")
            failed.append({"job_id": job["id"], "candidate_id": job["candidate_id"], "symbol": job["symbol"], "timeframe": job["timeframe"], "error": str(error)})
    set_campaign_worker_assignment(conn, worker_id, campaign_id=None, current_job_id=None, status="idle")
    remaining = open_job_count(conn, campaign_id)
    analytics: dict[str, Any] = {}
    if coordinate_campaign:
        if remaining == 0:
            expansion = expand_scout_campaign(conn, campaign_id)
            remaining = open_job_count(conn, campaign_id)
            if remaining == 0:
                analytics = finalize_research_campaign(conn, campaign_id)
            else:
                update_campaign_counts(conn, campaign_id)
                analytics = {**campaign_progress_analytics(conn, campaign_id), "scout_expansion": expansion}
        else:
            update_campaign_counts(conn, campaign_id)
            analytics = campaign_progress_analytics(conn, campaign_id)
    conn.commit()
    return {
        "campaign_id": campaign_id,
        "processed": len(jobs),
        "completed": len(completed),
        "failed": len(failed),
        "remaining": remaining,
        "results": completed,
        "errors": failed,
        "analytics": analytics,
        "campaign_version": CAMPAIGN_VERSION,
        "worker_id": worker_id,
        "simulation_only": True,
    }


def claim_campaign_jobs(
    conn: psycopg.Connection,
    *,
    campaign_id: int,
    worker_id: str,
    batch_size: int,
    lease_seconds: int,
    retry_limit: int,
    allowed_dataset_keys: list[str] | None = None,
) -> list[dict[str, Any]]:
    dataset_filter = ""
    params: tuple[Any, ...]
    if allowed_dataset_keys:
        dataset_filter = "AND (symbol || '|' || timeframe) = ANY(%s::text[])"
        params = (campaign_id, retry_limit, allowed_dataset_keys, batch_size, worker_id, lease_seconds, worker_id)
    else:
        params = (campaign_id, retry_limit, batch_size, worker_id, lease_seconds, worker_id)
    rows = conn.execute(
        f"""
        WITH claimable AS (
            SELECT id
            FROM research_campaign_jobs
            WHERE campaign_id = %s
              AND simulation_only = TRUE
              AND attempts < %s
              {dataset_filter}
              AND (
                  status = 'queued'
                  OR (status IN ('retrying', 'blocked_data', 'deferred_rate_limit') AND (deferred_until IS NULL OR deferred_until <= NOW()))
                  OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= NOW())
              )
            ORDER BY symbol ASC, timeframe ASC, id ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        UPDATE research_campaign_jobs j
        SET status = 'running',
            worker_id = %s,
            claimed_at = NOW(),
            lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
            heartbeat_at = NOW(),
            started_at = COALESCE(started_at, NOW()),
            attempts = attempts + 1,
            execution_resumed = CASE WHEN recovery_classification = 'recovered_stale_lease' THEN TRUE ELSE execution_resumed END,
            recovery_worker_id = CASE WHEN recovery_classification = 'recovered_stale_lease' THEN %s ELSE recovery_worker_id END,
            latest_error = NULL,
            updated_at = NOW()
        FROM claimable
        WHERE j.id = claimable.id
        RETURNING j.*
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def record_job_runtime(conn: psycopg.Connection, job_id: int, runtime_ms: int, profile: dict[str, Any] | None = None) -> None:
    try:
        conn.execute(
            "UPDATE research_campaign_jobs SET execution_runtime_ms = %s, execution_profile = %s WHERE id = %s",
            (runtime_ms, Jsonb(jsonable(profile or {})), job_id),
        )
    except Exception:
        safe_rollback(conn)


def recover_expired_campaign_jobs(conn: psycopg.Connection, *, campaign_id: int, retry_limit: int, recovery_worker_id: str | None = None) -> dict[str, Any]:
    retrying = conn.execute(
        """
        UPDATE research_campaign_jobs
        SET status = 'retrying',
            original_worker_id = COALESCE(original_worker_id, worker_id),
            original_lease_expires_at = COALESCE(original_lease_expires_at, lease_expires_at),
            recovery_worker_id = %s::text,
            recovered_at = NOW(),
            recovery_classification = 'recovered_stale_lease',
            execution_resumed = FALSE,
            failure_history = COALESCE(failure_history, '[]'::jsonb) || jsonb_build_array(jsonb_build_object(
                'classification', COALESCE(failure_classification, 'worker_timeout'),
                'recovery_classification', 'recovered_stale_lease',
                'original_worker_id', worker_id,
                'lease_expires_at', lease_expires_at,
                'recovery_worker_id', %s::text,
                'recovered_at', NOW(),
                'execution_resumed', FALSE,
                'latest_error', latest_error
            )),
            worker_id = NULL,
            lease_expires_at = NULL,
            latest_error = 'Worker lease expired before completion.',
            deferred_until = NOW(),
            updated_at = NOW()
        WHERE campaign_id = %s
          AND status = 'running'
          AND (
              (lease_expires_at IS NOT NULL AND lease_expires_at <= NOW())
              OR (heartbeat_at IS NOT NULL AND heartbeat_at <= NOW() - INTERVAL '5 minutes')
          )
          AND attempts < %s
        RETURNING id
        """,
        (recovery_worker_id, recovery_worker_id, campaign_id, retry_limit),
    ).fetchall()
    failed = conn.execute(
        """
        UPDATE research_campaign_jobs
        SET status = 'failed',
            original_worker_id = COALESCE(original_worker_id, worker_id),
            original_lease_expires_at = COALESCE(original_lease_expires_at, lease_expires_at),
            recovery_worker_id = %s::text,
            recovered_at = NOW(),
            recovery_classification = 'actual_worker_execution_timeout',
            execution_resumed = FALSE,
            failure_history = COALESCE(failure_history, '[]'::jsonb) || jsonb_build_array(jsonb_build_object(
                'classification', 'worker_timeout',
                'recovery_classification', 'actual_worker_execution_timeout',
                'original_worker_id', worker_id,
                'lease_expires_at', lease_expires_at,
                'recovery_worker_id', %s::text,
                'recovered_at', NOW(),
                'execution_resumed', FALSE,
                'latest_error', latest_error
            )),
            worker_id = NULL,
            lease_expires_at = NULL,
            failure_classification = 'worker_timeout',
            latest_error = 'Worker lease expired and retry limit was exceeded.',
            completed_at = NOW(),
            updated_at = NOW()
        WHERE campaign_id = %s
          AND status = 'running'
          AND (
              (lease_expires_at IS NOT NULL AND lease_expires_at <= NOW())
              OR (heartbeat_at IS NOT NULL AND heartbeat_at <= NOW() - INTERVAL '5 minutes')
          )
          AND attempts >= %s
        RETURNING id
        """,
        (recovery_worker_id, recovery_worker_id, campaign_id, retry_limit),
    ).fetchall()
    return {"retrying": len(retrying), "failed": len(failed), "recovered_stale_leases": len(retrying), "actual_worker_execution_timeouts": len(failed)}


def refresh_job_heartbeat(conn: psycopg.Connection, job_id: int, worker_id: str, lease_seconds: int) -> None:
    conn.execute(
        """
        UPDATE research_campaign_jobs
        SET heartbeat_at = NOW(),
            lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
            updated_at = NOW()
        WHERE id = %s AND worker_id = %s AND status = 'running'
        """,
        (lease_seconds, job_id, worker_id),
    )


def mark_claimed_job_deferred(conn: psycopg.Connection, job: dict[str, Any], readiness: dict[str, Any]) -> None:
    conn.execute(
        """
        UPDATE research_campaign_jobs
        SET status = %s,
            blocked_reason = %s,
            failure_classification = %s,
            latest_error = %s,
            deferred_until = NOW() + (%s * INTERVAL '1 second'),
            worker_id = NULL,
            lease_expires_at = NULL,
            updated_at = NOW()
        WHERE id = %s
        """,
        (
            readiness["status"],
            readiness["reason"],
            readiness["failure_classification"],
            readiness["reason"],
            int(readiness.get("retry_after_seconds") or 900),
            job["id"],
        ),
    )


def fail_or_retry_claimed_job(conn: psycopg.Connection, job: dict[str, Any], error: Exception, config: dict[str, Any]) -> None:
    classification = classify_worker_error(error)
    retryable = classification in {"provider_error", "database_error", "worker_timeout", "unknown_error"}
    retry_limit = int(config["retry_limit"])
    attempts = int(job.get("attempts") or 0)
    status = "retrying" if retryable and attempts < retry_limit else "failed"
    conn.execute(
        """
        UPDATE research_campaign_jobs
        SET status = %s,
            latest_error = %s,
            failure_classification = %s,
            failure_reasons = %s,
            deferred_until = CASE WHEN %s = 'retrying' THEN NOW() + (%s * INTERVAL '1 second') ELSE deferred_until END,
            worker_id = NULL,
            lease_expires_at = NULL,
            completed_at = CASE WHEN %s = 'failed' THEN NOW() ELSE completed_at END,
            updated_at = NOW()
        WHERE id = %s
        """,
        (
            status,
            str(error),
            classification,
            Jsonb([classification]),
            status,
            int(config["retry_backoff_seconds"]),
            status,
            job["id"],
        ),
    )


def run_campaign_job(
    conn: psycopg.Connection,
    job: dict[str, Any],
) -> dict[str, Any]:
    symbol = job["symbol"]
    timeframe = job["timeframe"]
    candidate = candidate_from_payload(job["candidate"])
    needs_enriched_context = (
        candidate.parameters.get("phase_9_11_campaign_version") == STRATEGY_REDESIGN_CAMPAIGN_VERSION
        or candidate.parameters.get("phase_9_12_campaign_version") == VOLATILITY_ADAPTIVE_RELATIVE_STRENGTH_CAMPAIGN_VERSION
    )
    provided_cache = job.get("_dataset_cache")
    cache = provided_cache if isinstance(provided_cache, dict) else {}
    dataset_id = int(job["dataset_id"]) if job.get("dataset_id") is not None else None
    cache_key = (str(symbol), str(timeframe), needs_enriched_context, dataset_id)
    dataset = cache.get(cache_key)
    cache_hit = dataset is not None
    if cache_hit and provided_cache is cache:
        cache.pop(cache_key)
        cache[cache_key] = dataset
    if dataset is None:
        dataset = load_campaign_dataset(conn, symbol, timeframe, needs_enriched_context, dataset_id=dataset_id)
        cache_limit = max(1, int(settings.campaign_dataset_cache_entries or 8))
        while len(cache) >= cache_limit:
            cache.pop(next(iter(cache)))
        cache[cache_key] = dataset
    simulation_started = time.perf_counter()
    row = evaluate_candidate(
        candidate,
        dataset["candles"],
        dataset["features"],
        dataset["context_by_time"],
        market_arrays=dataset["market_arrays"],
    )
    row["execution_profile"] = {
        "data_loading_ms": 0 if cache_hit else dataset["data_loading_ms"],
        "indicator_calculation_ms": 0 if cache_hit else dataset["indicator_calculation_ms"],
        "simulation_ms": round((time.perf_counter() - simulation_started) * 1000, 3),
        "dataset_cache_hit": cache_hit,
    }
    from app.services.research_architecture import validation_gate_diagnostics

    result = {**row, "symbol": symbol, "timeframe": timeframe, "campaign_version": CAMPAIGN_VERSION, "dataset_id": dataset_id}
    result["gate_diagnostics"] = validation_gate_diagnostics(result)
    return result


def load_campaign_dataset(
    conn: psycopg.Connection,
    symbol: str,
    timeframe: str,
    enriched_context: bool,
    *,
    dataset_id: int | None = None,
) -> dict[str, Any]:
    load_started = time.perf_counter()
    if dataset_id is not None:
        from app.services.research_architecture import load_frozen_campaign_dataset

        frozen = load_frozen_campaign_dataset(conn, dataset_id=dataset_id, symbol=symbol, timeframe=timeframe)
        candles = frozen["candles"]
        features = frozen["features"]
        regimes = frozen["regimes"]
    else:
        candle_limit = max(500, int(settings.campaign_backtest_candle_limit or 4000))
        candles = load_candles(conn, symbol, timeframe, limit=candle_limit)
        first_timestamp = candles[0]["timestamp"] if candles else None
        features = list(
            conn.execute(
                """
                SELECT *
                FROM features
                WHERE symbol = %s AND timeframe = %s
                  AND (%s::timestamptz IS NULL OR timestamp >= %s::timestamptz)
                ORDER BY timestamp ASC
                """,
                (symbol, timeframe, first_timestamp, first_timestamp),
            ).fetchall()
        )
        regimes = load_regimes(conn, symbol=symbol, timeframe=timeframe)
        if not regimes:
            sync_market_regimes(conn, symbol=symbol, timeframe=timeframe)
            regimes = load_regimes(conn, symbol=symbol, timeframe=timeframe)
        if first_timestamp is not None:
            regimes = [row for row in regimes if row.get("timestamp") is None or row["timestamp"] >= first_timestamp]
    data_loading_ms = round((time.perf_counter() - load_started) * 1000, 3)
    indicator_started = time.perf_counter()
    if enriched_context and dataset_id is None:
        features = enrich_phase_9_11_context(conn, symbol, timeframe, features)
    context_by_time = build_context_by_time(candles, features, regimes)
    rows = combine_candles_features(candles, features)
    return {
        "candles": candles,
        "features": features,
        "context_by_time": context_by_time,
        "market_arrays": build_market_arrays(rows),
        "data_loading_ms": data_loading_ms,
        "indicator_calculation_ms": round((time.perf_counter() - indicator_started) * 1000, 3),
    }


def enrich_phase_9_11_context(conn: psycopg.Connection, symbol: str, timeframe: str, features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if timeframe != "1h" or not features:
        return features
    context_4h_features = load_context_features(conn, symbol, "4h")
    context_4h_regimes = load_context_regimes(conn, symbol, "4h")
    spy_features = load_context_features(conn, "SPY", "1h")
    qqq_features = load_context_features(conn, "QQQ", "1h")
    enriched = []
    context_indexes = {"four_h": -1, "four_h_regime": -1, "spy": -1, "qqq": -1}
    for row in features:
        timestamp = row["timestamp"]
        four_h, context_indexes["four_h"] = advance_context_cursor(context_4h_features, context_indexes["four_h"], timestamp)
        four_h_regime, context_indexes["four_h_regime"] = advance_context_cursor(context_4h_regimes, context_indexes["four_h_regime"], timestamp)
        spy, context_indexes["spy"] = advance_context_cursor(spy_features, context_indexes["spy"], timestamp)
        qqq, context_indexes["qqq"] = advance_context_cursor(qqq_features, context_indexes["qqq"], timestamp)
        base_returns = finite_metric(row.get("returns_5"))
        item = dict(row)
        if four_h:
            item["context_4h_returns_5"] = four_h.get("returns_5")
            item["context_4h_distance_from_ema_50"] = four_h.get("distance_from_ema_50")
        if four_h_regime:
            item["context_4h_trend_regime"] = four_h_regime.get("trend_regime")
            item["context_4h_volatility_regime"] = four_h_regime.get("volatility_regime")
        if spy:
            spy_returns = finite_metric(spy.get("returns_5"))
            item["context_spy_returns_5"] = spy.get("returns_5")
            item["context_relative_spy_returns_5"] = base_returns - spy_returns
        if qqq:
            qqq_returns = finite_metric(qqq.get("returns_5"))
            item["context_qqq_returns_5"] = qqq.get("returns_5")
            item["context_relative_qqq_returns_5"] = base_returns - qqq_returns
        enriched.append(item)
    return enriched


def advance_context_cursor(rows: list[dict[str, Any]], index: int, timestamp: Any) -> tuple[dict[str, Any] | None, int]:
    while index + 1 < len(rows) and rows[index + 1]["timestamp"] <= timestamp:
        index += 1
    return (rows[index] if index >= 0 else None), index


def load_context_features(conn: psycopg.Connection, symbol: str, timeframe: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT *
            FROM features
            WHERE symbol = %s AND timeframe = %s
            ORDER BY timestamp ASC
            """,
            (symbol, timeframe),
        ).fetchall()
    ]


def load_context_regimes(conn: psycopg.Connection, symbol: str, timeframe: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT *
            FROM market_regimes
            WHERE symbol = %s AND timeframe = %s
            ORDER BY timestamp ASC
            """,
            (symbol, timeframe),
        ).fetchall()
    ]


def latest_context_at_or_before(rows: list[dict[str, Any]], timestamp: Any) -> dict[str, Any] | None:
    latest = None
    for row in rows:
        if row["timestamp"] <= timestamp:
            latest = row
        else:
            break
    return latest


def data_readiness_for_job(conn: psycopg.Connection, job: dict[str, Any]) -> dict[str, Any]:
    symbol = str(job["symbol"]).upper()
    timeframe = str(job["timeframe"])
    if job.get("dataset_id") is not None:
        dataset_id = int(job["dataset_id"])
        row = conn.execute(
            """
            SELECT COUNT(*) AS candle_count, MAX(timestamp) AS latest_candle_timestamp
            FROM research_dataset_candles
            WHERE dataset_id = %s AND symbol = %s AND timeframe = %s
            """,
            (dataset_id, symbol, timeframe),
        ).fetchone()
        candle_count = int((row or {}).get("candle_count") or 0)
        if candle_count < MIN_CAMPAIGN_CANDLES:
            classification = "missing_dataset" if candle_count == 0 else "insufficient_historical_depth"
            return readiness_block(
                "blocked_data",
                classification,
                f"Immutable dataset {dataset_id} contains {candle_count} candles; {MIN_CAMPAIGN_CANDLES} are required.",
                retry_after_seconds=86400,
                job=job,
                symbol_row={"symbol": symbol, "asset_class": "snapshot", "is_active": True},
                candle_count=candle_count,
                latest_candle_timestamp=(row or {}).get("latest_candle_timestamp"),
            )
        return {
            "ready": True,
            "status": "eligible",
            "reason": "immutable campaign dataset passed the stored candle-depth check",
            "failure_classification": None,
            "preflight": {
                "symbol": symbol,
                "timeframe": timeframe,
                "dataset_id": dataset_id,
                "dataset_mode": "immutable_snapshot",
                "candle_count": candle_count,
                "feature_count": candle_count,
                "latest_candle_timestamp": jsonable((row or {}).get("latest_candle_timestamp")),
                "classification": "eligible",
                "explanation": "Snapshot campaigns intentionally do not fail freshness checks after creation.",
            },
        }
    try:
        symbol_row = conn.execute(
            "SELECT symbol, asset_class, provider_symbol, primary_provider, is_active FROM symbols WHERE symbol = %s LIMIT 1",
            (symbol,),
        ).fetchone()
    except Exception:
        safe_rollback(conn)
        symbol_row = conn.execute(
            "SELECT symbol, asset_class, is_active FROM symbols WHERE symbol = %s LIMIT 1",
            (symbol,),
        ).fetchone()
    if not symbol_row:
        return readiness_block("blocked_data", "unsupported_symbol", "Symbol is not registered in the market-data universe.", retry_after_seconds=86400, job=job, symbol_row=None, preflight_status="unsupported")
    if symbol_row and symbol_row.get("is_active") is False:
        return readiness_block("blocked_data", "unsupported_symbol", "Asset is inactive or unsupported.", retry_after_seconds=86400, job=job, symbol_row=dict(symbol_row), preflight_status="unsupported")
    if timeframe not in DEFAULT_CAMPAIGN_TIMEFRAMES:
        return readiness_block("blocked_data", "unsupported_timeframe", f"Timeframe {timeframe} is not supported by campaign preflight.", retry_after_seconds=86400, job=job, symbol_row=dict(symbol_row), preflight_status="unsupported")
    row = conn.execute(
        """
        SELECT COUNT(*) AS candle_count, MAX(timestamp) AS latest_candle_timestamp
        FROM candles
        WHERE symbol = %s AND timeframe = %s
        """,
        (symbol, timeframe),
    ).fetchone()
    candle_count = int((row or {}).get("candle_count") or 0)
    latest = (row or {}).get("latest_candle_timestamp")
    if candle_count < MIN_CAMPAIGN_CANDLES:
        classification = "missing_dataset" if candle_count == 0 else "insufficient_historical_depth"
        return readiness_block("blocked_data", classification, f"Only {candle_count} candles are available; {MIN_CAMPAIGN_CANDLES} are required.", retry_after_seconds=3600, job=job, symbol_row=dict(symbol_row), candle_count=candle_count, latest_candle_timestamp=latest)
    freshness = data_freshness(latest, timeframe, (symbol_row or {}).get("asset_class"))
    if freshness["stale"]:
        return readiness_block("blocked_data", "stale_data", freshness["reason"], retry_after_seconds=1800, job=job, symbol_row=dict(symbol_row), candle_count=candle_count, latest_candle_timestamp=latest, freshness=freshness)
    features = conn.execute(
        """
        SELECT COUNT(*) AS feature_count
        FROM features
        WHERE symbol = %s AND timeframe = %s
        """,
        (symbol, timeframe),
    ).fetchone()
    feature_count = int((features or {}).get("feature_count") or 0)
    if feature_count < MIN_CAMPAIGN_FEATURES:
        return readiness_block("blocked_data", "feature_generation_failure", f"Only {feature_count} feature rows are available; {MIN_CAMPAIGN_FEATURES} are required.", retry_after_seconds=1800, job=job, symbol_row=dict(symbol_row), candle_count=candle_count, feature_count=feature_count, latest_candle_timestamp=latest, freshness=freshness)
    return {
        "ready": True,
        "status": "eligible",
        "reason": "stored market data and features are ready",
        "failure_classification": None,
        "preflight": preflight_detail(job, dict(symbol_row), candle_count, feature_count, latest, freshness, "eligible", "Ready for execution."),
    }


def research_campaign_preflight(conn: psycopg.Connection, *, assets: list[str], timeframes: list[str]) -> dict[str, Any]:
    started = time.perf_counter()
    log_event("Campaign preflight started", assets=len(assets), timeframes=len(timeframes))
    normalized_assets = sorted({str(asset).strip().upper() for asset in assets if str(asset).strip()})
    normalized_timeframes = sorted({str(timeframe).strip() for timeframe in timeframes if str(timeframe).strip()})
    if not normalized_assets:
        raise ValueError("campaign preflight requires at least one asset")
    if not normalized_timeframes:
        raise ValueError("campaign preflight requires at least one timeframe")

    symbol_rows = conn.execute(
        """
        SELECT symbol, asset_class, provider_symbol, primary_provider, is_active
        FROM symbols
        WHERE symbol = ANY(%s)
        """,
        (normalized_assets,),
    ).fetchall()
    symbols = {str(row["symbol"]): dict(row) for row in symbol_rows}
    candle_rows = conn.execute(
        """
        SELECT symbol, timeframe, COUNT(*) AS candle_count, MAX(timestamp) AS latest_candle_timestamp
        FROM candles
        WHERE symbol = ANY(%s) AND timeframe = ANY(%s)
        GROUP BY symbol, timeframe
        """,
        (normalized_assets, normalized_timeframes),
    ).fetchall()
    candles = {(str(row["symbol"]), str(row["timeframe"])): dict(row) for row in candle_rows}
    feature_rows = conn.execute(
        """
        SELECT symbol, timeframe, COUNT(*) AS feature_count
        FROM features
        WHERE symbol = ANY(%s) AND timeframe = ANY(%s)
        GROUP BY symbol, timeframe
        """,
        (normalized_assets, normalized_timeframes),
    ).fetchall()
    features = {(str(row["symbol"]), str(row["timeframe"])): int(row["feature_count"] or 0) for row in feature_rows}

    issues: list[dict[str, Any]] = []
    classifications: Counter[str] = Counter()
    eligible_datasets = 0
    eligible_by_symbol: dict[str, set[str]] = {symbol: set() for symbol in normalized_assets}
    for symbol in normalized_assets:
        symbol_row = symbols.get(symbol)
        for timeframe in normalized_timeframes:
            classification: str | None = None
            reason: str | None = None
            candle_row = candles.get((symbol, timeframe), {})
            candle_count = int(candle_row.get("candle_count") or 0)
            feature_count = features.get((symbol, timeframe), 0)

            if not symbol_row or symbol_row.get("is_active") is False:
                classification = "unsupported_symbol"
                reason = "Symbol is not registered as an active market-data asset."
            elif timeframe not in DEFAULT_CAMPAIGN_TIMEFRAMES:
                classification = "unsupported_timeframe"
                reason = f"Timeframe {timeframe} is not supported by campaign preflight."
            elif candle_count < MIN_CAMPAIGN_CANDLES:
                classification = "missing_dataset" if candle_count == 0 else "insufficient_historical_depth"
                reason = f"Only {candle_count} candles are available; {MIN_CAMPAIGN_CANDLES} are required."
            else:
                freshness = data_freshness(candle_row.get("latest_candle_timestamp"), timeframe, symbol_row.get("asset_class"))
                if freshness["stale"]:
                    classification = "stale_data"
                    reason = str(freshness["reason"])
                elif feature_count < MIN_CAMPAIGN_FEATURES:
                    classification = "feature_generation_failure"
                    reason = f"Only {feature_count} feature rows are available; {MIN_CAMPAIGN_FEATURES} are required."

            if classification:
                classifications[classification] += 1
                if len(issues) < 100:
                    issues.append(
                        {
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "classification": classification,
                            "reason": reason,
                            "candle_count": candle_count,
                            "feature_count": feature_count,
                            "provider": symbol_row.get("primary_provider") if symbol_row else None,
                        }
                    )
            else:
                eligible_datasets += 1
                eligible_by_symbol.setdefault(symbol, set()).add(timeframe)

    dataset_count = len(normalized_assets) * len(normalized_timeframes)
    blocked_datasets = dataset_count - eligible_datasets
    required_timeframes = set(normalized_timeframes)
    executable_assets = [symbol for symbol in normalized_assets if eligible_by_symbol.get(symbol, set()) >= required_timeframes]
    excluded_assets = [symbol for symbol in normalized_assets if symbol not in executable_assets]
    result = {
        "ready": blocked_datasets == 0,
        "can_launch": bool(executable_assets),
        "assets_total": len(normalized_assets),
        "executable_assets": executable_assets,
        "executable_assets_total": len(executable_assets),
        "excluded_assets": excluded_assets,
        "excluded_assets_total": len(excluded_assets),
        "timeframes": normalized_timeframes,
        "datasets_total": dataset_count,
        "eligible_datasets": eligible_datasets,
        "blocked_datasets": blocked_datasets,
        "classifications": dict(classifications),
        "issues": issues,
        "issues_truncated": blocked_datasets > len(issues),
        "simulation_only": True,
    }
    log_event("Campaign preflight finished", ready=result["ready"], datasets_total=dataset_count, blocked_datasets=blocked_datasets, elapsed_ms=elapsed_ms(started))
    return result


def readiness_block(
    status: str,
    classification: str,
    reason: str,
    *,
    retry_after_seconds: int,
    job: dict[str, Any] | None = None,
    symbol_row: dict[str, Any] | None = None,
    candle_count: int = 0,
    feature_count: int = 0,
    latest_candle_timestamp: Any = None,
    freshness: dict[str, Any] | None = None,
    preflight_status: str | None = None,
) -> dict[str, Any]:
    display_status = preflight_status or status
    return {
        "ready": False,
        "status": status,
        "reason": reason,
        "failure_classification": classification,
        "retry_after_seconds": retry_after_seconds,
        "preflight": preflight_detail(job or {}, symbol_row or {}, candle_count, feature_count, latest_candle_timestamp, freshness or {}, display_status, reason, classification),
    }


def preflight_detail(
    job: dict[str, Any],
    symbol_row: dict[str, Any],
    candle_count: int,
    feature_count: int,
    latest_candle_timestamp: Any,
    freshness: dict[str, Any],
    status: str,
    reason: str,
    classification: str | None = None,
) -> dict[str, Any]:
    symbol = str(job.get("symbol") or symbol_row.get("symbol") or "").upper()
    timeframe = str(job.get("timeframe") or "")
    required = MIN_CAMPAIGN_CANDLES
    return {
        "campaign_job_id": job.get("id"),
        "symbol": symbol,
        "asset_class": symbol_row.get("asset_class"),
        "timeframe": timeframe,
        "configured_provider": symbol_row.get("primary_provider"),
        "provider_symbol": symbol_row.get("provider_symbol") or symbol,
        "latest_stored_candle": latest_candle_timestamp,
        "latest_expected_completed_candle": freshness.get("expected_completed_candle"),
        "historical_candles_required": required,
        "historical_candles_available": candle_count,
        "missing_candle_count": max(0, required - candle_count),
        "feature_rows_available": feature_count,
        "freshness_classification": freshness.get("classification") or classification or status,
        "preflight_status": status,
        "exact_block_reason": reason,
        "retry_eligibility": status not in {"unsupported"},
        "recommended_remediation": remediation_for_preflight(classification or status, symbol_row.get("primary_provider")),
    }


def remediation_for_preflight(classification: str, provider: str | None) -> str:
    if classification in {"missing_dataset", "insufficient_historical_depth"}:
        return f"Backfill candles with {provider or 'the configured provider'} and regenerate features."
    if classification == "stale_latest_candle":
        return f"Ingest the latest completed candle with {provider or 'the configured provider'}."
    if classification == "market_closed":
        return "No repair required while the market is closed and the expected completed candle is present."
    if classification == "feature_generation_failure":
        return "Regenerate feature rows after candle coverage is complete."
    if classification.startswith("unsupported"):
        return "Record explicit unsupported-data classification; do not silently remove the asset."
    return "Inspect provider, candle continuity, and feature-generation logs."


def data_freshness(timestamp: Any, timeframe: str, asset_class: str | None) -> dict[str, Any]:
    parsed = parse_timestamp(timestamp)
    if parsed is None:
        return {"stale": True, "classification": "missing_dataset", "reason": "No completed candle timestamp is available.", "expected_completed_candle": None}
    expected = expected_completed_candle(timeframe, asset_class)
    if market_closed_for_asset(asset_class) and parsed >= expected - timedelta(hours=4):
        return {"stale": False, "classification": "market_closed", "reason": "Market closed: latest completed candle is expected.", "expected_completed_candle": expected}
    max_age_hours = {"15m": 2, "30m": 4, "60m": 8, "1h": 8, "4h": 24, "1d": 96}.get(timeframe, 24)
    age_hours = (datetime.now(UTC) - parsed).total_seconds() / 3600
    if is_equity_market_asset(asset_class) and parsed.weekday() == 4 and datetime.now(UTC).weekday() in {5, 6, 0}:
        max_age_hours = max(max_age_hours, 96)
    if age_hours > max_age_hours:
        return {"stale": True, "classification": "stale_latest_candle", "reason": f"Latest completed candle is {age_hours:.1f}h old; max allowed for {timeframe} is {max_age_hours}h.", "expected_completed_candle": expected}
    classification = "market_closed" if market_closed_for_asset(asset_class) else "healthy"
    return {"stale": False, "classification": classification, "reason": "Latest completed candle is fresh.", "expected_completed_candle": expected}


def market_closed_for_asset(asset_class: str | None) -> bool:
    if not is_equity_market_asset(asset_class):
        return False
    now = datetime.now(UTC)
    return now.weekday() >= 5 or not (dt_time(14, 30) <= now.time() <= dt_time(21, 0))


def expected_completed_candle(timeframe: str, asset_class: str | None) -> datetime:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    if is_equity_market_asset(asset_class) and market_closed_for_asset(asset_class):
        expected = now
        if now.time() < dt_time(14, 30):
            expected -= timedelta(days=1)
        while expected.weekday() >= 5:
            expected -= timedelta(days=1)
        close_hour = 20 if timeframe in {"1h", "60m"} else 16 if timeframe == "4h" else 0
        return expected.replace(hour=close_hour, minute=0)
    if timeframe in {"1h", "60m"}:
        expected = now.replace(minute=0)
    elif timeframe == "4h":
        expected = now.replace(hour=(now.hour // 4) * 4, minute=0)
    elif timeframe == "1d":
        expected = now.replace(hour=0, minute=0)
    else:
        expected = now
    return expected


def is_equity_market_asset(asset_class: str | None) -> bool:
    normalized = str(asset_class or "").lower()
    return "equity" in normalized or normalized == "etf"


def parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def classify_worker_error(error: Exception) -> str:
    message = str(error).lower()
    if any(token in message for token in ("invalid input syntax", "jsonb", "postgres", "database", "sql", "connection refused", "connection reset")):
        return "database_error"
    if "stale" in message:
        return "stale_data"
    if "provider" in message or "rate" in message:
        return "provider_error"
    if any(token in message for token in ("no candle", "missing candle", "candle history", "no feature", "missing feature", "market data unavailable")):
        return "data_unavailable"
    if "validation" in message:
        return "validation_error"
    if "strategy" in message:
        return "strategy_error"
    return "unknown_error"


def scheduling_config(campaign: dict[str, Any]) -> dict[str, Any]:
    config = {**DEFAULT_SCHEDULING_CONFIG, **dict(campaign.get("scheduling_config") or {})}
    controls = dict(campaign.get("controls") or {})
    if controls.get("batch_size"):
        config["batch_size"] = int(controls["batch_size"])
    return config


def campaign_worker_limit() -> int:
    configured = settings.max_campaign_workers
    if configured is not None:
        return max(1, int(configured))
    cpu_count = os.cpu_count() or 2
    return max(1, min(8, max(2, cpu_count - 1)))


def default_campaign_workers() -> int:
    return min(campaign_worker_limit(), max(2, (os.cpu_count() or 2) - 1))


def worker_stale_seconds() -> int:
    return max(15, int(settings.campaign_worker_stale_seconds or 45))


def worker_heartbeat_seconds() -> int:
    return max(5, int(settings.campaign_worker_heartbeat_seconds or 10))


def update_campaign_scheduling_config(conn: psycopg.Connection, campaign_id: int, updates: dict[str, Any]) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    campaign = get_campaign(conn, campaign_id)
    config = scheduling_config(campaign)
    allowed = set(DEFAULT_SCHEDULING_CONFIG)
    clean = {key: value for key, value in updates.items() if key in allowed and value is not None}
    if "mode" in clean and clean["mode"] not in {"manual", "scheduled"}:
        raise ValueError("campaign scheduling mode must be manual or scheduled")
    next_config = {**config, **clean}
    conn.execute(
        """
        UPDATE research_campaigns
        SET scheduling_config = %s, updated_at = NOW()
        WHERE id = %s
        """,
        (Jsonb(jsonable(next_config)), campaign_id),
    )
    conn.commit()
    return {"campaign_id": campaign_id, "scheduling_config": next_config, "simulation_only": True}


def get_campaign_scheduler_status(conn: psycopg.Connection) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    scheduler = conn.execute("SELECT * FROM research_campaign_scheduler WHERE id = TRUE").fetchone()
    return {"scheduler": jsonable(dict(scheduler)) if scheduler else {}, "workers": worker_status(conn), "simulation_only": True}


def update_campaign_scheduler(conn: psycopg.Connection, updates: dict[str, Any]) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    allowed = {
        "enabled",
        "cadence_seconds",
        "global_daily_job_limit",
        "max_concurrent_workers",
        "max_concurrent_backtests",
        "max_concurrent_data_requests",
        "max_database_queue_depth",
        "provider_rate_limits",
    }
    existing = dict(conn.execute("SELECT * FROM research_campaign_scheduler WHERE id = TRUE").fetchone() or {})
    next_values = {key: updates[key] for key in allowed if key in updates and updates[key] is not None}
    merged = {**existing, **next_values}
    conn.execute(
        """
        UPDATE research_campaign_scheduler
        SET enabled = %s,
            cadence_seconds = %s,
            global_daily_job_limit = %s,
            max_concurrent_workers = %s,
            max_concurrent_backtests = %s,
            max_concurrent_data_requests = %s,
            max_database_queue_depth = %s,
            provider_rate_limits = %s,
            updated_at = NOW()
        WHERE id = TRUE
        """,
        (
            bool(merged.get("enabled", False)),
            int(merged.get("cadence_seconds") or 300),
            int(merged.get("global_daily_job_limit") or 1000),
            int(merged.get("max_concurrent_workers") or 1),
            int(merged.get("max_concurrent_backtests") or 1),
            int(merged.get("max_concurrent_data_requests") or 2),
            int(merged.get("max_database_queue_depth") or 100000),
            Jsonb(jsonable(merged.get("provider_rate_limits") or {})),
        ),
    )
    conn.commit()
    return get_campaign_scheduler_status(conn)


def run_campaign_scheduler_cycle(conn: psycopg.Connection, *, force: bool = False, worker_id: str | None = None) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    scheduler = dict(conn.execute("SELECT * FROM research_campaign_scheduler WHERE id = TRUE").fetchone() or {})
    worker = worker_id or f"{WORKER_VERSION}_{sha256(str(datetime.now(UTC).timestamp()).encode()).hexdigest()[:10]}"
    register_campaign_worker(conn, worker_id=worker, status="running")
    if not force and not scheduler.get("enabled"):
        mark_campaign_worker_idle(conn, worker)
        return {"skipped": True, "reason": "scheduler_disabled", "simulation_only": True}
    active_workers = active_worker_count(conn)
    if active_workers >= int(scheduler.get("max_concurrent_workers") or 1):
        mark_campaign_worker_idle(conn, worker)
        return {"skipped": True, "reason": "max_concurrent_workers", "active_workers": active_workers, "simulation_only": True}
    if database_queue_depth(conn) > int(scheduler.get("max_database_queue_depth") or 100000):
        mark_campaign_worker_idle(conn, worker)
        return {"skipped": True, "reason": "max_database_queue_depth", "simulation_only": True}
    conn.execute("UPDATE research_campaign_scheduler SET is_running = TRUE, running_since = NOW(), updated_at = NOW() WHERE id = TRUE")
    campaigns = eligible_campaigns(conn)
    results = []
    total_processed = 0
    try:
        for campaign in campaigns:
            config = scheduling_config(dict(campaign))
            if not force and config.get("mode") != "scheduled":
                continue
            if not within_execution_window(config):
                results.append({"campaign_id": campaign["id"], "skipped": "outside_execution_window"})
                continue
            limit = remaining_campaign_budget(conn, dict(campaign), config)
            if limit <= 0:
                results.append({"campaign_id": campaign["id"], "skipped": "daily_budget_exhausted"})
                continue
            batch = min(int(config["max_jobs_per_cycle"]), int(config["batch_size"]), limit)
            result = run_research_campaign_batch(conn, campaign_id=int(campaign["id"]), batch_size=batch, worker_id=worker)
            total_processed += int(result.get("processed") or 0)
            results.append(result)
        next_cycle = datetime.now(UTC) + timedelta(seconds=int(scheduler.get("cadence_seconds") or 300))
        conn.execute(
            """
            UPDATE research_campaign_scheduler
            SET is_running = FALSE,
                running_since = NULL,
                last_cycle_at = NOW(),
                next_cycle_at = %s,
                latest_result = %s,
                latest_error = NULL,
                updated_at = NOW()
            WHERE id = TRUE
            """,
            (next_cycle, f"Processed {total_processed} campaign job(s)."),
        )
        mark_campaign_worker_idle(conn, worker, processed_jobs=total_processed)
        conn.commit()
        return {"worker_id": worker, "processed": total_processed, "campaigns": results, "simulation_only": True}
    except Exception as error:
        safe_rollback(conn)
        conn.execute(
            """
            UPDATE research_campaign_scheduler
            SET is_running = FALSE,
                running_since = NULL,
                latest_error = %s,
                updated_at = NOW()
            WHERE id = TRUE
            """,
            (str(error),),
        )
        mark_campaign_worker_error(conn, worker, str(error))
        conn.commit()
        raise


def register_campaign_worker(conn: psycopg.Connection, *, worker_id: str, status: str = "running") -> dict[str, Any]:
    ensure_operations_tables(conn)
    row = conn.execute(
        """
        INSERT INTO research_campaign_workers(worker_id, process_id, hostname, status, heartbeat_at, last_heartbeat_at, latest_cycle_at, simulation_only)
        VALUES (%s, %s, %s, %s, NOW(), NOW(), NOW(), TRUE)
        ON CONFLICT(worker_id) DO UPDATE
        SET status = EXCLUDED.status,
            heartbeat_at = NOW(),
            last_heartbeat_at = NOW(),
            latest_cycle_at = NOW(),
            stopped_at = NULL,
            latest_error = NULL
        RETURNING *
        """,
        (worker_id, str(os.getpid()), socket.gethostname(), status),
    ).fetchone()
    return jsonable(dict(row))


def heartbeat_campaign_worker(conn: psycopg.Connection, worker_id: str, *, status: str = "running") -> None:
    conn.execute(
        """
        UPDATE research_campaign_workers
        SET status = CASE WHEN status = 'draining' THEN status ELSE %s END,
            heartbeat_at = NOW(), last_heartbeat_at = NOW(), latest_cycle_at = NOW()
        WHERE worker_id = %s
        """,
        (status, worker_id),
    )


def set_campaign_worker_assignment(
    conn: psycopg.Connection,
    worker_id: str,
    *,
    campaign_id: int | None,
    current_job_id: int | None = None,
    status: str = "running",
) -> None:
    conn.execute(
        """
        UPDATE research_campaign_workers
        SET campaign_id = %s,
            current_job_id = %s,
            status = %s,
            heartbeat_at = NOW(),
            last_heartbeat_at = NOW(),
            latest_cycle_at = NOW()
        WHERE worker_id = %s
        """,
        (campaign_id, current_job_id, status, worker_id),
    )


def best_effort_worker_heartbeat(conn: psycopg.Connection, worker_id: str, *, status: str = "running") -> None:
    try:
        heartbeat_campaign_worker(conn, worker_id, status=status)
    except Exception:
        safe_rollback(conn)


def mark_campaign_worker_idle(conn: psycopg.Connection, worker_id: str, processed_jobs: int = 0) -> None:
    conn.execute(
        """
        UPDATE research_campaign_workers
        SET status = 'idle',
            campaign_id = NULL,
            current_job_id = NULL,
            heartbeat_at = NOW(),
            last_heartbeat_at = NOW(),
            latest_cycle_at = NOW(),
            processed_jobs = processed_jobs + %s
        WHERE worker_id = %s
        """,
        (processed_jobs, worker_id),
    )


def mark_campaign_worker_error(conn: psycopg.Connection, worker_id: str, error: str) -> None:
    conn.execute(
        """
        UPDATE research_campaign_workers
        SET status = 'error',
            heartbeat_at = NOW(),
            last_heartbeat_at = NOW(),
            latest_error = %s,
            error_count = error_count + 1
        WHERE worker_id = %s
        """,
        (error, worker_id),
    )


def stop_campaign_worker(conn: psycopg.Connection, worker_id: str) -> dict[str, Any]:
    ensure_operations_tables(conn)
    row = conn.execute(
        """
        UPDATE research_campaign_workers
        SET status = 'stopped',
            campaign_id = NULL,
            current_job_id = NULL,
            stopped_at = NOW(),
            heartbeat_at = NOW(),
            last_heartbeat_at = NOW()
        WHERE worker_id = %s
        RETURNING *
        """,
        (worker_id,),
    ).fetchone()
    conn.commit()
    return {"worker": jsonable(dict(row)) if row else None, "simulation_only": True}


def run_durable_campaign_worker_cycle(
    conn: psycopg.Connection,
    *,
    worker_id: str,
    dataset_cache: dict[tuple[Any, ...], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ensure_universe_table(conn)
    register_campaign_worker(conn, worker_id=worker_id, status="idle")
    campaigns = executable_target_campaigns(conn)
    for campaign in campaigns:
        campaign_id = int(campaign["id"])
        target = min(int(campaign.get("target_workers") or 0), campaign_worker_limit())
        runtime = campaign_runtime_snapshot(conn, campaign_id)
        if target <= 0:
            continue
        if int(runtime["effective_workers"]) >= target:
            mark_campaign_worker_idle(conn, worker_id)
            conn.commit()
            continue
        # Reserve capacity while the campaign row lock is still held. Other logical
        # slots will observe this heartbeat after commit and cannot exceed target.
        set_campaign_worker_assignment(conn, worker_id, campaign_id=campaign_id, current_job_id=None, status="starting")
        conn.commit()
        result = run_research_campaign_batch(
            conn,
            campaign_id=campaign_id,
            batch_size=1,
            worker_id=worker_id,
            dataset_cache=dataset_cache,
        )
        processed = int(result.get("processed") or 0)
        if processed:
            return result
    mark_campaign_worker_idle(conn, worker_id)
    conn.commit()
    return {"worker_id": worker_id, "processed": 0, "campaigns": [], "simulation_only": True}


def executable_target_campaigns(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM research_campaigns
        WHERE simulation_only = TRUE
          AND status IN ('queued', 'running', 'failed')
          AND target_workers > 0
        ORDER BY updated_at ASC, id ASC
        FOR UPDATE SKIP LOCKED
        """
    ).fetchall()
    return [dict(row) for row in rows]


def run_background_campaign_worker(
    conn_factory,
    *,
    worker_id: str | None = None,
    poll_seconds: float = 5.0,
    max_cycles: int | None = None,
    stop_file: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    worker = worker_id or f"{WORKER_VERSION}_{socket.gethostname()}_{os.getpid()}"
    if max_cycles is None and os.name == "posix" and int(settings.campaign_worker_nice or 0) > 0:
        try:
            os.nice(int(settings.campaign_worker_nice))
        except OSError as error:
            log_event("Worker priority unchanged", worker_id=worker, error=str(error))
    log_event("Worker started", worker_id=worker, poll_seconds=poll_seconds, max_cycles=max_cycles, stop_file=stop_file)
    cycles = 0
    processed = 0
    dataset_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
    try:
        with conn_factory() as conn:
            register_campaign_worker(conn, worker_id=worker, status="running")
            conn.commit()
        while max_cycles is None or cycles < max_cycles:
            if stop_file and os.path.exists(stop_file):
                log_event("Worker cancelled", worker_id=worker, reason="stop_file")
                break
            cycle_started = time.perf_counter()
            with conn_factory() as conn:
                heartbeat_campaign_worker(conn, worker)
                result = run_durable_campaign_worker_cycle(conn, worker_id=worker, dataset_cache=dataset_cache)
                processed += int(result.get("processed") or 0)
                conn.commit()
            log_event("Task completed", task="worker_cycle", worker_id=worker, cycle=cycles + 1, processed=result.get("processed"), elapsed_ms=elapsed_ms(cycle_started))
            cycles += 1
            if (max_cycles is None or cycles < max_cycles) and int(result.get("processed") or 0) == 0:
                time.sleep(poll_seconds)
        with conn_factory() as conn:
            stop_campaign_worker(conn, worker)
        log_event("Worker stopped", worker_id=worker, cycles=cycles, processed=processed, elapsed_ms=elapsed_ms(started))
        return {"worker_id": worker, "cycles": cycles, "processed": processed, "simulation_only": True}
    except Exception as error:
        log_exception("Worker exception", error, worker_id=worker, cycles=cycles, processed=processed, elapsed_ms=elapsed_ms(started))
        raise


def run_background_campaign_worker_pool(
    conn_factory,
    *,
    worker_id_prefix: str | None = None,
    slots: int | None = None,
    poll_seconds: float = 5.0,
    max_cycles: int | None = None,
    stop_file: str | None = None,
    use_processes: bool = True,
) -> dict[str, Any]:
    """Run durable logical slots as real processes for CPU-bound simulations."""
    slot_count = max(1, min(campaign_worker_limit(), int(slots or campaign_worker_limit())))
    prefix = worker_id_prefix or f"{WORKER_VERSION}_{socket.gethostname()}_{os.getpid()}"
    log_event("Worker pool started", worker_id_prefix=prefix, slots=slot_count)
    results: list[dict[str, Any]] = []
    executor_type = ProcessPoolExecutor if use_processes else ThreadPoolExecutor
    executor_options = {"max_workers": slot_count}
    if not use_processes:
        executor_options["thread_name_prefix"] = "campaign-slot"
    with executor_type(**executor_options) as executor:
        futures = [
            executor.submit(
                run_background_campaign_worker,
                conn_factory,
                worker_id=f"{prefix}-slot-{index + 1}",
                poll_seconds=poll_seconds,
                max_cycles=max_cycles,
                stop_file=stop_file,
            )
            for index in range(slot_count)
        ]
        for future in as_completed(futures):
            results.append(future.result())
    return {
        "worker_id_prefix": prefix,
        "slots": slot_count,
        "processed": sum(int(row.get("processed") or 0) for row in results),
        "workers": results,
        "simulation_only": True,
    }


def scout_job_quality(job: dict[str, Any]) -> float:
    result = dict(job.get("result") or {})
    metrics = dict(result.get("metrics") or {})
    if not bool((metrics.get("walk_forward") or {}).get("enabled")):
        return 0.0
    profit_factor = min(1.5, validation_profit_factor({"result": result}) / 1.2)
    expectancy = finite_metric(metrics.get("expectancy_per_trade"))
    expectancy_score = max(0.0, min(1.0, expectancy / 5.0))
    drawdown_score = max(0.0, 1.0 - finite_metric(metrics.get("max_drawdown")) / 0.12)
    trade_score = min(1.0, finite_metric(metrics.get("number_of_trades")) / 30.0)
    gate_score = 1.0 if job.get("status") == "promoted" else min(1.0, finite_metric(job.get("validation_score")) / 5.0)
    return round(0.25 * profit_factor + 0.20 * expectancy_score + 0.20 * drawdown_score + 0.20 * trade_score + 0.15 * gate_score, 6)


def select_scout_expansion_routes(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scored = []
    for job in jobs:
        result = dict(job.get("result") or {})
        metrics = dict(result.get("metrics") or {})
        score = scout_job_quality(job)
        has_signal = (
            job.get("status") == "promoted"
            or validation_profit_factor({"result": result}) >= 1.0
            or finite_metric(metrics.get("expectancy_per_trade")) > 0
        )
        if has_signal and score >= 0.5:
            scored.append({
                "symbol": str(job["symbol"]),
                "timeframe": str(job["timeframe"]),
                "strategy_family": str(job.get("strategy_family") or strategy_family_for_candidate(job.get("candidate") or {})),
                "candidate_id": str(job["candidate_id"]),
                "score": score,
                "job": job,
            })
    best_by_route: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in scored:
        key = (row["symbol"], row["timeframe"], row["strategy_family"])
        if key not in best_by_route or row["score"] > best_by_route[key]["score"]:
            best_by_route[key] = row
    ranked = sorted(best_by_route.values(), key=lambda row: (row["score"], row["symbol"]), reverse=True)
    limit = max(2, min(12, round(len(ranked) * 0.35))) if ranked else 0
    selected = ranked[:limit]
    # A second related asset lets genuinely transferable candidates reach the
    # unchanged cross-asset gate instead of being mislabeled as broad elites.
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in ranked:
        groups[(row["strategy_family"], row["timeframe"])].append(row)
    for row in list(selected):
        siblings = groups[(row["strategy_family"], row["timeframe"])]
        if sum(1 for item in selected if item["strategy_family"] == row["strategy_family"] and item["timeframe"] == row["timeframe"]) < 2:
            sibling = next((item for item in siblings if item["symbol"] != row["symbol"]), None)
            if sibling and sibling not in selected:
                selected.append(sibling)
    return selected


def expansion_candidate_mix(
    base_candidates: list[DiscoveryCandidate],
    parent_evidence: list[dict[str, Any]],
    budget: int,
) -> list[DiscoveryCandidate]:
    repair_quota = round(budget * 0.70)
    adjacent_quota = round(budget * 0.20)
    exploratory_quota = max(0, budget - repair_quota - adjacent_quota)
    repairs = near_pass_repair_candidates(parent_evidence, repair_quota)
    adjacent = channel_candidates(base_candidates, adjacent_quota + max(0, repair_quota - len(repairs)), "adjacent")
    exploratory = channel_candidates(base_candidates, exploratory_quota, "exploratory", offset=len(adjacent))
    combined = [*repairs, *adjacent, *exploratory]
    if len(combined) < budget:
        combined.extend(channel_candidates(base_candidates, budget - len(combined), "exploratory_fill", offset=len(combined)))
    return diverse_candidate_selection(combined, budget)


def queue_campaign_route_jobs(
    conn: psycopg.Connection,
    campaign_id: int,
    candidates: list[DiscoveryCandidate],
    routes: list[dict[str, Any]],
) -> int:
    created = 0
    candidates_by_family: dict[str, list[DiscoveryCandidate]] = defaultdict(list)
    for candidate in candidates:
        candidates_by_family[strategy_family_for_candidate(candidate)].append(candidate)
    for route in routes:
        matching = candidates_by_family.get(route["strategy_family"], [])
        if matching:
            created += queue_campaign_jobs(conn, campaign_id, matching, [route["symbol"]], [route["timeframe"]])
    return created


def expand_scout_campaign(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    # Serialize the final-scout race. The winning worker creates expansion jobs;
    # followers observe the updated stage and cannot finalize prematurely.
    campaign = conn.execute(
        "SELECT * FROM research_campaigns WHERE id = %s AND simulation_only = TRUE FOR UPDATE",
        (campaign_id,),
    ).fetchone()
    if not campaign:
        return {"expanded": False, "reason": "campaign_not_found"}
    campaign = dict(campaign)
    controls = dict(campaign.get("controls") or {})
    execution = dict(controls.get("research_execution") or {})
    if execution.get("search_mode") != "scout_expand" or execution.get("stage") != "scout":
        return {"expanded": False, "reason": "not_scout_stage"}
    jobs = [dict(row) for row in conn.execute(
        "SELECT * FROM research_campaign_jobs WHERE campaign_id = %s AND simulation_only = TRUE ORDER BY id",
        (campaign_id,),
    ).fetchall()]
    routes = select_scout_expansion_routes(jobs)
    if not routes:
        execution.update({"stage": "stopped_no_signal", "expanded_routes": [], "expansion_jobs_created": 0})
        conn.execute("UPDATE research_campaigns SET controls = %s, updated_at = NOW() WHERE id = %s", (Jsonb({**controls, "research_execution": execution}), campaign_id))
        return {"expanded": False, "reason": "no_positive_walk_forward_routes"}
    universe = get_universe(conn, str(campaign["universe_key"]))
    full_budget = int(execution.get("full_candidate_budget") or campaign.get("requested_candidates") or 250)
    base_candidates, _generation = campaign_generation_candidates(conn, universe_key=str(campaign["universe_key"]), max_candidates=full_budget)
    parent_evidence = [
        {"candidate": candidate_from_payload(dict(route["job"]["candidate"])), "result": route["job"].get("result") or {}, "failure_reasons": route["job"].get("failure_reasons") or []}
        for route in routes
    ]
    scout_count = int(execution.get("scout_candidate_count") or 0)
    candidates = expansion_candidate_mix(base_candidates, parent_evidence, max(0, full_budget - scout_count))
    created = queue_campaign_route_jobs(conn, campaign_id, candidates, routes)
    if campaign.get("dataset_id") is not None:
        conn.execute(
            "UPDATE research_campaign_jobs SET dataset_id = %s WHERE campaign_id = %s AND dataset_id IS NULL",
            (campaign["dataset_id"], campaign_id),
        )
    route_payload = [{key: row[key] for key in ("symbol", "timeframe", "strategy_family", "candidate_id", "score")} for row in routes]
    execution.update({"stage": "expanded" if created else "stopped_no_inventory", "expanded_routes": route_payload, "expansion_jobs_created": created})
    controls["research_execution"] = execution
    controls["candidate_generation_mix"] = {
        **dict(controls.get("candidate_generation_mix") or {}),
        "expansion_policy": "70_percent_near_pass_repairs_20_percent_adjacent_10_percent_exploratory",
        "walk_forward_first": True,
        "family_asset_routing": True,
        "near_duplicate_diversity_filter": 0.08,
    }
    conn.execute("UPDATE research_campaigns SET controls = %s, updated_at = NOW() WHERE id = %s", (Jsonb(jsonable(controls)), campaign_id))
    return {"expanded": created > 0, "jobs_created": created, "routes": route_payload, "candidate_count": len(candidates), "assets_considered": len(universe.get("assets") or [])}


def eligible_campaigns(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM research_campaigns
        WHERE simulation_only = TRUE
          AND status IN ('queued', 'running', 'failed')
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def active_worker_count(conn: psycopg.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT worker_id) AS count
        FROM research_campaign_jobs
        WHERE status = 'running'
          AND worker_id IS NOT NULL
          AND lease_expires_at > NOW()
        """
    ).fetchone()
    return int((row or {}).get("count") or 0)


def database_queue_depth(conn: psycopg.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM research_campaign_jobs
        WHERE status IN ('queued', 'retrying', 'blocked_data', 'deferred_rate_limit')
        """
    ).fetchone()
    return int((row or {}).get("count") or 0)


def remaining_campaign_budget(conn: psycopg.Connection, campaign: dict[str, Any], config: dict[str, Any]) -> int:
    today = datetime.now(UTC).date()
    executed = 0 if campaign.get("daily_budget_date") != today else int(campaign.get("daily_jobs_executed") or 0)
    return max(0, int(config.get("daily_experiment_budget") or 0) - executed)


def within_execution_window(config: dict[str, Any]) -> bool:
    window = config.get("execution_window_utc")
    if not window:
        return True
    start = window.get("start")
    end = window.get("end")
    if not start or not end:
        return True
    now = datetime.now(UTC).time()
    start_time = datetime.strptime(start, "%H:%M").time()
    end_time = datetime.strptime(end, "%H:%M").time()
    return start_time <= now <= end_time if start_time <= end_time else now >= start_time or now <= end_time


def worker_status(conn: psycopg.Connection) -> dict[str, Any]:
    registry_rows = conn.execute(
        """
        SELECT *
        FROM research_campaign_workers
        WHERE simulation_only = TRUE
        ORDER BY heartbeat_at DESC
        """
    ).fetchall()
    claimed_rows = conn.execute(
        """
        SELECT worker_id, COUNT(*) AS claimed_jobs, MAX(heartbeat_at) AS last_heartbeat, MAX(lease_expires_at) AS lease_expires_at
        FROM research_campaign_jobs
        WHERE worker_id IS NOT NULL
        GROUP BY worker_id
        ORDER BY MAX(heartbeat_at) DESC
        """
    ).fetchall()
    claimed_by_worker = {row["worker_id"]: dict(row) for row in claimed_rows}
    try:
        current_job_rows = conn.execute(
            """
            SELECT DISTINCT ON (worker_id)
                   worker_id, id AS job_id, campaign_id, symbol, timeframe, status, claimed_at, heartbeat_at, lease_expires_at
            FROM research_campaign_jobs
            WHERE worker_id IS NOT NULL
            ORDER BY worker_id, claimed_at DESC NULLS LAST, id DESC
            """
        ).fetchall()
    except Exception:
        safe_rollback(conn)
        current_job_rows = []
    current_jobs = {row["worker_id"]: dict(row) for row in current_job_rows}
    now = datetime.now(UTC)
    workers = []
    seen = set()
    for registry in registry_rows:
        row = dict(registry)
        claimed = claimed_by_worker.get(row["worker_id"], {})
        current = current_jobs.get(row["worker_id"])
        heartbeat = parse_timestamp(row.get("last_heartbeat_at") or row.get("heartbeat_at"))
        registered = parse_timestamp(row.get("registered_at"))
        healthy = heartbeat is not None and (now - heartbeat) <= timedelta(seconds=worker_stale_seconds()) and row.get("status") not in {"stopped", "error"}
        heartbeat_age = round((now - heartbeat).total_seconds(), 2) if heartbeat else None
        uptime_seconds = round((now - registered).total_seconds(), 2) if registered and row.get("status") not in {"stopped"} else 0
        workers.append({
            **jsonable(row),
            "state": row.get("status"),
            "start_time": row.get("registered_at"),
            "uptime_seconds": uptime_seconds,
            "heartbeat_age_seconds": heartbeat_age,
            "lease_state": lease_state(current, now),
            "current_job": jsonable(current) if current else None,
            "claimed_jobs": int(claimed.get("claimed_jobs") or 0),
            "completed_job_count": int(row.get("processed_jobs") or 0),
            "retry_count": retry_count_for_worker(conn, row["worker_id"]),
            "provider_latency_ms": worker_latency(conn, row["worker_id"], "provider_latency_ms"),
            "database_latency_ms": worker_latency(conn, row["worker_id"], "database_latency_ms"),
            "last_error": row.get("latest_error"),
            "last_successful_job": last_successful_job(conn, row["worker_id"]),
            "health": "healthy" if healthy else "stale",
        })
        seen.add(row["worker_id"])
    for worker_id, claimed in claimed_by_worker.items():
        if worker_id in seen:
            continue
        lease = parse_timestamp(claimed.get("lease_expires_at"))
        healthy = lease is not None and lease > now
        current = current_jobs.get(worker_id)
        heartbeat = parse_timestamp(claimed.get("last_heartbeat"))
        workers.append({
            **jsonable(claimed),
            "status": "running",
            "state": "running",
            "heartbeat_age_seconds": round((now - heartbeat).total_seconds(), 2) if heartbeat else None,
            "lease_state": lease_state(current or claimed, now),
            "current_job": jsonable(current) if current else None,
            "completed_job_count": 0,
            "retry_count": retry_count_for_worker(conn, worker_id),
            "provider_latency_ms": worker_latency(conn, worker_id, "provider_latency_ms"),
            "database_latency_ms": worker_latency(conn, worker_id, "database_latency_ms"),
            "last_error": None,
            "last_successful_job": last_successful_job(conn, worker_id),
            "health": "healthy" if healthy else "stale",
        })
    return {
        "active_worker_count": sum(1 for row in workers if row["health"] == "healthy"),
        "healthy_worker_count": sum(1 for row in workers if row["health"] == "healthy"),
        "stale_worker_count": sum(1 for row in workers if row["health"] == "stale"),
        "workers": workers,
    }


def lease_state(job: dict[str, Any] | None, now: datetime) -> str:
    if not job:
        return "idle"
    lease = parse_timestamp(job.get("lease_expires_at"))
    if lease and lease > now:
        return "leased"
    if lease and lease <= now:
        return "expired"
    return "unleased"


def retry_count_for_worker(conn: psycopg.Connection, worker_id: str) -> int:
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(GREATEST(attempts - 1, 0)), 0) AS count FROM research_campaign_jobs WHERE worker_id = %s",
            (worker_id,),
        ).fetchone()
        return int((row or {}).get("count") or 0)
    except Exception:
        safe_rollback(conn)
        return 0


def worker_latency(conn: psycopg.Connection, worker_id: str, column: str) -> float:
    if column not in {"provider_latency_ms", "database_latency_ms"}:
        return 0
    try:
        row = conn.execute(f"SELECT AVG({column}) AS value FROM research_campaign_jobs WHERE worker_id = %s AND {column} IS NOT NULL", (worker_id,)).fetchone()
        return round(finite_metric((row or {}).get("value")), 2)
    except Exception:
        safe_rollback(conn)
        return 0


def last_successful_job(conn: psycopg.Connection, worker_id: str) -> dict[str, Any] | None:
    try:
        row = conn.execute(
            """
            SELECT id AS job_id, campaign_id, symbol, timeframe, status, completed_at
            FROM research_campaign_jobs
            WHERE worker_id = %s
              AND status IN ('completed', 'promoted', 'rejected')
            ORDER BY completed_at DESC NULLS LAST, id DESC
            LIMIT 1
            """,
            (worker_id,),
        ).fetchone()
        return jsonable(dict(row)) if row else None
    except Exception:
        safe_rollback(conn)
        return None


def safe_rollback(conn: psycopg.Connection) -> None:
    rollback = getattr(conn, "rollback", None)
    if callable(rollback):
        try:
            rollback()
        except Exception:
            pass


def deterministic_worker_id(campaign_id: int) -> str:
    return f"{WORKER_VERSION}_manual_{campaign_id}"


def candidate_from_payload(payload: dict[str, Any]) -> DiscoveryCandidate:
    data = dict(payload)
    return DiscoveryCandidate(
        candidate_id=str(data["candidate_id"]),
        family_id=str(data["family_id"]),
        parent_candidate_id=data.get("parent_candidate_id"),
        generation=int(data["generation"]),
        blocks=dict(data["blocks"]),
        parameters=dict(data["parameters"]),
        complexity=int(data["complexity"]),
        canonical_key=str(data["canonical_key"]),
    )


def passes_single_market_validation(result: dict[str, Any]) -> bool:
    metrics = result.get("metrics") or {}
    readiness = result.get("paper_readiness") or {}
    return (
        profit_factor_passes(metrics, 1.2)
        and finite_metric(metrics.get("expectancy_per_trade")) > 0
        and finite_metric(metrics.get("max_drawdown")) <= 0.12
        and finite_metric(metrics.get("number_of_trades")) >= 30
        and bool((metrics.get("walk_forward") or {}).get("enabled"))
        and bool(readiness.get("paper_ready"))
    )


def finalize_research_campaign(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    # Close the scout-expansion / finalize race that produced Campaign 34's
    # "completed with 480 queued jobs" invariant violation. We take the same
    # row lock the expansion path uses and re-check the open-job count inside
    # it: if another worker enqueued expansion jobs after our caller observed
    # open_job_count == 0, we refuse to finalize and return progress instead.
    locked = conn.execute(
        "SELECT status FROM research_campaigns WHERE id = %s AND simulation_only = TRUE FOR UPDATE",
        (campaign_id,),
    ).fetchone()
    if locked is None:
        raise ValueError("research campaign not found")
    remaining_open = open_job_count(conn, campaign_id)
    if remaining_open > 0:
        update_campaign_counts(conn, campaign_id)
        log_event(
            "Campaign finalize skipped: open jobs remain",
            campaign_id=campaign_id,
            open_jobs=remaining_open,
            prior_status=str(locked["status"]),
        )
        return {**campaign_progress_analytics(conn, campaign_id), "finalize_skipped_open_jobs": remaining_open}
    jobs = list(
        conn.execute(
            """
            SELECT *
            FROM research_campaign_jobs
            WHERE campaign_id = %s AND simulation_only = TRUE
            ORDER BY candidate_id, symbol, timeframe
            """,
            (campaign_id,),
        ).fetchall()
    )
    summaries = candidate_consistency_summaries(jobs)
    promoted = 0
    rejected = 0
    status_counts: dict[str, int] = {}
    for job in jobs:
        status = str(job.get("status") or "")
        status_counts[status] = status_counts.get(status, 0) + 1
    for summary in summaries:
        if passes_cross_validation(summary):
            promoted += 1
            persist_elite_candidate(conn, campaign_id, summary)
            promote_elite_to_paper_simulation(conn, campaign_id, summary)
        else:
            rejected += 1
    analytics = campaign_analytics(jobs, summaries)
    analytics = {**analytics, **campaign_operational_intelligence(conn, campaign_id, jobs)}
    conn.execute(
        """
        UPDATE research_campaigns
        SET status = 'completed',
            completed_at = NOW(),
            queued_jobs = %s,
            completed_jobs = %s,
            failed_jobs = %s,
            promoted_candidates = %s,
            rejected_candidates = %s,
            analytics = %s,
            updated_at = NOW()
        WHERE id = %s
        """,
        (
            len(jobs),
            status_counts.get("completed", 0) + status_counts.get("rejected", 0) + status_counts.get("promoted", 0),
            status_counts.get("failed", 0),
            promoted,
            rejected,
            Jsonb(jsonable(analytics)),
            campaign_id,
        ),
    )
    update_job_consistency_scores(conn, campaign_id, summaries)
    try:
        learning = learn_from_completed_campaign(conn, campaign_id)
        analytics["research_learning"] = {
            "knowledge_items": len(learning.get("knowledge", [])),
            "failure_patterns": len(learning.get("failure_patterns", [])),
            "success_patterns": len(learning.get("success_patterns", [])),
            "recommendations": len(learning.get("recommendations", [])),
            "evolved_variants": len(learning.get("evolution_history", [])),
        }
    except Exception as error:  # noqa: BLE001 - learning must not invalidate completed simulation evidence
        analytics["research_learning"] = {"error": str(error)}
    campaign_metadata = get_campaign(conn, campaign_id)
    if campaign_metadata.get("dataset_id") is not None:
        from app.services.research_architecture import finalize_architecture_campaign

        analytics = finalize_architecture_campaign(conn, campaign_id, summaries=summaries, analytics=analytics)
    from app.services.automated_scientific_reporting import generate_automated_scientific_report

    scientific_report = generate_automated_scientific_report(conn, campaign_id, analytics=analytics)
    analytics["automated_scientific_reporting"] = {
        "report_id": scientific_report["id"],
        "report_key": scientific_report["report_key"],
        "calculation_version": "automated_scientific_reporting_v1",
    }
    if campaign_metadata.get("dataset_id") is not None:
        from app.services.research_architecture import persist_campaign_archive

        archive = persist_campaign_archive(conn, campaign_id)
        analytics.setdefault("research_architecture", {})["archive"] = {
            key: archive.get(key)
            for key in ("archive_key", "content_hash", "storage_locations")
        }
    conn.execute(
        "UPDATE research_campaigns SET analytics = %s, updated_at = NOW() WHERE id = %s",
        (Jsonb(jsonable(analytics)), campaign_id),
    )
    persist_campaign_analytics_snapshot(conn, campaign_id, analytics)
    refresh_command_center_aggregate_snapshot(conn)
    return analytics


def generate_campaign_report(conn: psycopg.Connection, campaign_id: int, analytics: dict[str, Any] | None = None) -> dict[str, Any]:
    campaign = get_campaign(conn, campaign_id)
    analytics = analytics or refresh_campaign_analytics(conn, campaign_id)
    report_key = sha256(f"campaign_report|{campaign_id}".encode()).hexdigest()
    recommendations = campaign_recommendations(analytics)
    summary = {
        "campaign_id": campaign_id,
        "name": campaign["name"],
        "runtime": analytics.get("runtime"),
        "assets_tested": sorted({row.get("x") for row in (analytics.get("heatmaps") or {}).get("asset_heatmap", []) if row.get("x")}),
        "strategies_generated": analytics.get("strategies_generated", 0),
        "validation_results": {
            "tested": analytics.get("strategies_tested", 0),
            "promoted": analytics.get("promoted", 0),
            "rejected": analytics.get("rejected", 0),
            "validation_pass_rate": analytics.get("validation_pass_rate", 0),
        },
        "paper_deployments": len(analytics.get("recent_promotions") or []),
        "strongest_discoveries": analytics.get("best_candidates", [])[:5],
        "weakest_discoveries": analytics.get("worst_candidates", [])[:5],
    }
    markdown = campaign_report_markdown(campaign, summary, recommendations)
    row = conn.execute(
        """
        INSERT INTO research_campaign_reports(campaign_id, report_key, title, summary, recommendations, markdown_report, simulation_only)
        VALUES (%s, %s, %s, %s, %s, %s, TRUE)
        ON CONFLICT(report_key) DO UPDATE
        SET summary = EXCLUDED.summary,
            recommendations = EXCLUDED.recommendations,
            markdown_report = EXCLUDED.markdown_report,
            created_at = NOW()
        RETURNING *
        """,
        (campaign_id, report_key, f"Campaign Report: {campaign['name']}", Jsonb(jsonable(summary)), Jsonb(jsonable(recommendations)), markdown),
    ).fetchone()
    return jsonable(dict(row))


def campaign_recommendations(analytics: dict[str, Any]) -> list[dict[str, str]]:
    recommendations = []
    failures = analytics.get("failure_distribution") or []
    if failures:
        recommendations.append({"title": "Address top failure mode", "recommendation": f"Prioritize fixes for {failures[0]['reason']} before scaling the next campaign."})
    strongest = (analytics.get("strategy_family_intelligence") or [{}])[0]
    if strongest.get("name"):
        recommendations.append({"title": "Expand strongest family", "recommendation": f"Allocate the next campaign toward {strongest['name']} variants with stricter forward-validation monitoring."})
    if analytics.get("validation_pass_rate", 0) < 0.05:
        recommendations.append({"title": "Tighten generation filters", "recommendation": "Promotion rate is low; reduce redundant candidate generation before adding more assets."})
    return recommendations or [{"title": "Continue evidence collection", "recommendation": "Run a follow-up campaign with the same safety controls and compare forward paper evidence."}]


def campaign_report_markdown(campaign: dict[str, Any], summary: dict[str, Any], recommendations: list[dict[str, str]]) -> str:
    lines = [
        f"# Campaign Report: {campaign['name']}",
        "",
        "Simulation-only research report. No broker routing or live execution is enabled.",
        "",
        f"- Strategies generated: {summary['strategies_generated']}",
        f"- Strategies tested: {summary['validation_results']['tested']}",
        f"- Promoted: {summary['validation_results']['promoted']}",
        f"- Rejected: {summary['validation_results']['rejected']}",
        f"- Validation pass rate: {summary['validation_results']['validation_pass_rate']}",
        "",
        "## Recommendations",
    ]
    lines.extend(f"- {row['title']}: {row['recommendation']}" for row in recommendations)
    return "\n".join(lines)


def get_campaign_analytics(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    analytics = refresh_campaign_analytics(conn, campaign_id)
    return {"campaign_id": campaign_id, "analytics": analytics, "simulation_only": True}


def get_campaign_report(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    row = conn.execute(
        """
        SELECT *
        FROM research_campaign_reports
        WHERE campaign_id = %s AND simulation_only = TRUE
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (campaign_id,),
    ).fetchone()
    if not row:
        row = generate_campaign_report(conn, campaign_id)
        conn.commit()
        return row
    return jsonable(dict(row))


def get_campaign_intelligence(conn: psycopg.Connection, campaign_id: int, kind: str) -> dict[str, Any]:
    analytics = refresh_campaign_analytics(conn, campaign_id)
    key = {
        "strategy-family": "strategy_family_intelligence",
        "asset": "asset_intelligence",
        "timeframe": "timeframe_intelligence",
        "heatmaps": "heatmaps",
        "throughput": "queue_statistics",
    }[kind]
    return {"campaign_id": campaign_id, kind.replace("-", "_"): analytics.get(key), "simulation_only": True}


def candidate_consistency_summaries(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for job in jobs:
        grouped[job["candidate_id"]].append(dict(job))
    summaries = []
    for candidate_id, rows in grouped.items():
        passed = [row for row in rows if row["status"] == "promoted"]
        results = [row.get("result") or {} for row in rows if row.get("result")]
        metrics = [result.get("metrics") or {} for result in results]
        aggregate_pf, aggregate_pf_infinite = aggregate_profit_factor(metrics)
        consistency_score = round(len(passed) / len(rows), 4) if rows else 0.0
        regime_counter: Counter[str] = Counter()
        for result in results:
            for bucket in ("by_market_regime", "by_volatility_regime"):
                for regime_row in ((result.get("regime_analysis") or {}).get(bucket) or []):
                    row_metrics = regime_row.get("metrics") or {}
                    if finite_metric(row_metrics.get("number_of_trades")) > 0 and finite_metric(row_metrics.get("expectancy_per_trade")) > 0:
                        regime_counter[str(regime_row.get("regime", "unknown"))] += 1
        first = rows[0]
        summary = {
                "candidate_id": candidate_id,
                "family_id": first["family_id"],
                "strategy_name": "autonomous_strategy_discovery",
                "strategy_version": candidate_id,
                "research_score": average(row.get("validation_score") for row in rows),
                "profit_factor": aggregate_pf,
                "profit_factor_is_infinite": aggregate_pf_infinite,
                "expectancy": average(metric.get("expectancy_per_trade") for metric in metrics),
                "max_drawdown": average(metric.get("max_drawdown") for metric in metrics),
                "trade_count": int(sum(finite_metric(metric.get("number_of_trades")) for metric in metrics)),
                "stability": consistency_score,
                "assets_passed": len({row["symbol"] for row in passed}),
                "timeframes_passed": len({row["timeframe"] for row in passed}),
                "regimes_passed": len(regime_counter),
                "consistency_score": consistency_score,
                "validation_history": [jsonable(row.get("result") or {"status": row["status"], "error": row.get("latest_error")}) for row in rows],
                "failure_reasons": sorted({reason for row in rows for reason in list(row.get("failure_reasons") or [])}),
                "passed_markets": [{"symbol": row["symbol"], "timeframe": row["timeframe"]} for row in passed],
            }
        summary["multi_objective_score"] = multi_objective_candidate_score(summary)
        summaries.append(summary)
    return sorted(summaries, key=lambda row: (row["multi_objective_score"], row["stability"], row["research_score"]), reverse=True)


def multi_objective_candidate_score(summary: dict[str, Any]) -> float:
    profit_factor_score = min(1.0, validation_profit_factor(summary) / 1.5)
    expectancy_score = max(0.0, min(1.0, finite_metric(summary.get("expectancy")) / 5.0))
    drawdown_score = max(0.0, 1.0 - finite_metric(summary.get("max_drawdown")) / 0.12)
    trade_score = min(1.0, finite_metric(summary.get("trade_count")) / 90.0)
    stability_score = max(0.0, min(1.0, finite_metric(summary.get("stability"))))
    cross_asset_score = min(1.0, finite_metric(summary.get("assets_passed")) / 3.0)
    return round(
        profit_factor_score * 0.20
        + expectancy_score * 0.20
        + drawdown_score * 0.15
        + trade_score * 0.15
        + stability_score * 0.20
        + cross_asset_score * 0.10,
        6,
    )


def passes_cross_validation(summary: dict[str, Any]) -> bool:
    return (
        summary["research_score"] > 0
        and profit_factor_passes(summary, 1.2)
        and summary["expectancy"] > 0
        and summary["max_drawdown"] <= 0.12
        and summary["trade_count"] >= 60
        and summary["stability"] >= 0.6
        and summary["assets_passed"] >= 2
        and summary["timeframes_passed"] >= 1
    )


def persist_elite_candidate(conn: psycopg.Connection, campaign_id: int, summary: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO elite_research_candidates(
            campaign_id, candidate_id, family_id, strategy_name, strategy_version, research_score,
            profit_factor, expectancy, max_drawdown, trade_count, stability, assets_passed,
            timeframes_passed, regimes_passed, validation_history, paper_performance, simulation_only
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
        ON CONFLICT(candidate_id, campaign_id) DO UPDATE
        SET research_score = EXCLUDED.research_score,
            validation_history = EXCLUDED.validation_history
        """,
        (
            campaign_id,
            summary["candidate_id"],
            summary["family_id"],
            summary["strategy_name"],
            summary["strategy_version"],
            summary["research_score"],
            summary["profit_factor"],
            summary["expectancy"],
            summary["max_drawdown"],
            summary["trade_count"],
            summary["stability"],
            summary["assets_passed"],
            summary["timeframes_passed"],
            summary["regimes_passed"],
            Jsonb(jsonable(summary["validation_history"])),
            Jsonb({"paper_trades": 0, "paper_pnl": 0, "drawdown": 0, "daily_performance": [], "signal_frequency": 0}),
        ),
    )


def promote_elite_to_paper_simulation(conn: psycopg.Connection, campaign_id: int, summary: dict[str, Any]) -> None:
    elite_count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM elite_research_candidates
        WHERE campaign_id = %s AND simulation_only = TRUE
        """,
        (campaign_id,),
    ).fetchone()
    if int((elite_count or {}).get("count") or 0) < 1:
        return
    existing = conn.execute(
        """
        SELECT id
        FROM strategy_deployments
        WHERE campaign_id = %s
          AND candidate_id = %s
          AND status = 'active'
          AND simulation_only = TRUE
        LIMIT 1
        """,
        (campaign_id, summary["candidate_id"]),
    ).fetchone()
    if existing or not summary["passed_markets"]:
        return
    candidate_row = conn.execute(
        """
        SELECT candidate
        FROM research_campaign_jobs
        WHERE campaign_id = %s
          AND candidate_id = %s
          AND simulation_only = TRUE
        ORDER BY id
        LIMIT 1
        """,
        (campaign_id, summary["candidate_id"]),
    ).fetchone()
    if not candidate_row or not candidate_row.get("candidate"):
        return
    candidate_payload = dict(candidate_row["candidate"])
    candidate_parameters = dict(candidate_payload.get("parameters") or {})
    account = ensure_candidate_forward_account(
        conn,
        summary["candidate_id"],
        Decimal(str(candidate_parameters.get("initial_equity", 10000))),
    )
    market = summary["passed_markets"][0]
    deployment = conn.execute(
        """
        INSERT INTO strategy_deployments(
            account_id, strategy_name, strategy_version, symbol, timeframe, parameters, status, simulation_only,
            campaign_id, candidate_id, forward_validation_started_at, evidence_version, lifecycle_state, deployment_origin
        )
        VALUES (%s, %s, %s, %s, %s, %s, 'active', TRUE, %s, %s, NOW(), %s, 'active_forward_validation', 'elite_candidate_campaign')
        RETURNING id
        """,
        (
            account["id"],
            summary["strategy_name"],
            summary["strategy_version"],
            market["symbol"],
            market["timeframe"],
            Jsonb(
                {
                    **candidate_parameters,
                    "campaign_id": campaign_id,
                    "candidate_id": summary["candidate_id"],
                    "simulation_only": True,
                }
            ),
            campaign_id,
            summary["candidate_id"],
            "candidate_linked_forward_evidence_v1",
        ),
    ).fetchone()
    conn.execute(
        """
        UPDATE elite_research_candidates
        SET promoted_to_paper_at = NOW()
        WHERE campaign_id = %s AND candidate_id = %s
        """,
        (campaign_id, summary["candidate_id"]),
    )
    conn.execute(
        """
        INSERT INTO execution_logs(account_id, deployment_id, event_type, message, payload, simulation_only)
        VALUES (%s, %s, 'elite_candidate_paper_deployed', %s, %s, TRUE)
        """,
        (
            account["id"],
            deployment["id"],
            "Elite research candidate was deployed to internal paper simulation only.",
            Jsonb({"campaign_id": campaign_id, "candidate_id": summary["candidate_id"], "market": market}),
        ),
    )


def ensure_candidate_forward_account(
    conn: psycopg.Connection,
    candidate_id: str,
    starting_cash: Decimal,
) -> dict[str, Any]:
    name = f"Candidate {candidate_id} Forward Validation"
    row = conn.execute(
        """
        SELECT *
        FROM paper_accounts
        WHERE name = %s
          AND simulation_only = TRUE
        ORDER BY id
        LIMIT 1
        """,
        (name,),
    ).fetchone()
    if row:
        return dict(row)
    row = conn.execute(
        """
        INSERT INTO paper_accounts(name, base_currency, starting_cash, cash_balance, status, simulation_only)
        VALUES (%s, 'USD', %s, %s, 'active', TRUE)
        RETURNING *
        """,
        (name, starting_cash, starting_cash),
    ).fetchone()
    return dict(row)


def refresh_elite_candidate_forward_evidence(conn: psycopg.Connection, *, elite_candidate_id: int | None = None) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    if elite_candidate_id:
        rows = conn.execute(
            "SELECT * FROM elite_research_candidates WHERE id = %s AND simulation_only = TRUE",
            (elite_candidate_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT *
            FROM elite_research_candidates
            WHERE simulation_only = TRUE
              AND forward_validation_state NOT IN ('archived')
            ORDER BY created_at ASC
            """
        ).fetchall()
    refreshed = []
    drift_rows = []
    for elite in rows:
        rollup = calculate_elite_paper_rollup(conn, dict(elite))
        state = forward_validation_state(rollup["metrics"], rollup["thresholds"], bool(dict(elite).get("promoted_to_paper_at")))
        persist_paper_rollup(conn, dict(elite), rollup, state)
        drift = calculate_evidence_drift(dict(elite), rollup["metrics"])
        persist_evidence_drift(conn, dict(elite), drift)
        if any(row["drift_classification"] == "severe" for row in drift):
            create_drift_alert(conn, dict(elite), drift)
        conn.execute(
            """
            UPDATE elite_research_candidates
            SET paper_performance = %s,
                forward_validation_state = %s,
                forward_validation_thresholds = %s,
                forward_validation_updated_at = NOW(),
                drift_status = %s
            WHERE id = %s
            """,
            (
                Jsonb(jsonable(rollup["metrics"])),
                state,
                Jsonb(jsonable(rollup["thresholds"])),
                max_drift_classification(drift),
                elite["id"],
            ),
        )
        refreshed.append({"elite_candidate_id": elite["id"], "candidate_id": elite["candidate_id"], "forward_validation_state": state, "paper_performance": rollup["metrics"]})
        drift_rows.extend(drift)
    conn.commit()
    return {"refreshed": len(refreshed), "elite_candidates": refreshed, "drift": drift_rows, "simulation_only": True}


def calculate_elite_paper_rollup(conn: psycopg.Connection, elite: dict[str, Any]) -> dict[str, Any]:
    deployments = conn.execute(
        """
        SELECT *
        FROM strategy_deployments
        WHERE strategy_name = %s
          AND strategy_version = %s
          AND simulation_only = TRUE
        ORDER BY created_at ASC
        """,
        (elite["strategy_name"], elite["strategy_version"]),
    ).fetchall()
    deployment_ids = [row["id"] for row in deployments]
    if not deployment_ids:
        return {"metrics": empty_paper_metrics(), "thresholds": thresholds_for_elite(elite)}
    orders = rows_for_ids(conn, "paper_orders", "deployment_id", deployment_ids)
    fills = rows_for_ids(conn, "paper_fills", "order_id", [row["id"] for row in orders])
    logs = rows_for_ids(conn, "execution_logs", "deployment_id", deployment_ids)
    positions = conn.execute(
        """
        SELECT *
        FROM paper_positions
        WHERE simulation_only = TRUE
          AND account_id = ANY(%s)
          AND symbol = ANY(%s)
        """,
        ([row["account_id"] for row in deployments], [row["symbol"] for row in deployments]),
    ).fetchall()
    first_deployment = min((parse_timestamp(row.get("created_at")) for row in deployments), default=None)
    last_activity = max(
        [parsed for parsed in [*(parse_timestamp(row.get("submitted_at")) for row in orders), *(parse_timestamp(row.get("filled_at")) for row in fills), *(parse_timestamp(row.get("created_at")) for row in logs)] if parsed is not None],
        default=None,
    )
    realized = sum_decimal(row.get("realized_pnl") for row in positions)
    unrealized = sum_decimal(row.get("unrealized_pnl") for row in positions)
    slippage = average(Decimal(str(row.get("slippage") or 0)) for row in fills)
    attribution = closed_trade_attribution(fills)
    setup_count = sum(1 for row in logs if row.get("event_type") == "paper_scan_completed" and ((row.get("payload") or {}).get("decision") or {}).get("signal") == "setup")
    skipped = sum(1 for row in logs if "skipped" in str(row.get("event_type")))
    stale = sum(1 for row in logs if row.get("event_type") == "paper_scan_stale_data_skipped")
    errors = sum(1 for row in logs if "error" in str(row.get("event_type")))
    active_days = max(0, (datetime.now(UTC) - first_deployment).days) if first_deployment else 0
    closed_trade_count = attribution["closed_trade_count"]
    total_orders = len(orders)
    metrics = {
        "deployment_age_days": active_days,
        "active_paper_trading_days": len({parse_timestamp(row.get("filled_at")).date().isoformat() for row in fills if parse_timestamp(row.get("filled_at"))}),
        "generated_setups": setup_count,
        "simulated_orders": total_orders,
        "simulated_fills": len(fills),
        "closed_trade_count": closed_trade_count,
        "open_position_count": sum(1 for row in positions if Decimal(str(row.get("quantity") or 0)) > 0),
        "realized_pnl": float(realized),
        "unrealized_pnl": float(unrealized),
        "total_simulated_pnl": float(realized + unrealized),
        "paper_profit_factor": attribution["paper_profit_factor"],
        "paper_expectancy": attribution["paper_expectancy"],
        "paper_win_rate": attribution["paper_win_rate"],
        "average_win": attribution["average_win"],
        "average_loss": attribution["average_loss"],
        "average_trade_duration_hours": attribution["average_trade_duration_hours"],
        "closed_trades": attribution["closed_trades"],
        "paper_max_drawdown": paper_max_drawdown(conn, deployments),
        "average_simulated_slippage": float(slippage),
        "signal_frequency": round(setup_count / active_days, 4) if active_days else 0.0,
        "skipped_signal_count": skipped,
        "stale_data_block_count": stale,
        "execution_error_count": errors,
        "execution_error_rate": round(errors / max(1, len(logs)), 4),
        "stale_data_block_rate": round(stale / max(1, len(logs)), 4),
        "last_paper_activity_timestamp": last_activity.isoformat() if last_activity else None,
    }
    return {"metrics": metrics, "thresholds": thresholds_for_elite(elite)}


def rows_for_ids(conn: psycopg.Connection, table: str, column: str, ids: list[Any]) -> list[dict[str, Any]]:
    if not ids:
        return []
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE simulation_only = TRUE AND {column} = ANY(%s)",
        (ids,),
    ).fetchall()
    return [dict(row) for row in rows]


def empty_paper_metrics() -> dict[str, Any]:
    return {
        "deployment_age_days": 0,
        "active_paper_trading_days": 0,
        "generated_setups": 0,
        "simulated_orders": 0,
        "simulated_fills": 0,
        "closed_trade_count": 0,
        "open_position_count": 0,
        "realized_pnl": 0,
        "unrealized_pnl": 0,
        "total_simulated_pnl": 0,
        "paper_profit_factor": 0,
        "paper_expectancy": 0,
        "paper_win_rate": 0,
        "paper_max_drawdown": 0,
        "average_simulated_slippage": 0,
        "signal_frequency": 0,
        "skipped_signal_count": 0,
        "stale_data_block_count": 0,
        "execution_error_count": 0,
        "execution_error_rate": 0,
        "stale_data_block_rate": 0,
        "last_paper_activity_timestamp": None,
    }


def thresholds_for_elite(elite: dict[str, Any]) -> dict[str, Any]:
    return {**DEFAULT_FORWARD_THRESHOLDS, **dict(elite.get("forward_validation_thresholds") or {})}


def forward_validation_state(metrics: dict[str, Any], thresholds: dict[str, Any], deployed: bool) -> str:
    if not deployed and metrics["simulated_orders"] == 0:
        return "awaiting_paper_deployment"
    if metrics["active_paper_trading_days"] < thresholds["minimum_active_paper_days"] or metrics["closed_trade_count"] < thresholds["minimum_closed_trades"]:
        return "insufficient_forward_sample"
    if (
        metrics["paper_expectancy"] > thresholds["minimum_paper_expectancy"]
        and metrics["paper_profit_factor"] >= thresholds["minimum_paper_profit_factor"]
        and metrics["paper_max_drawdown"] <= thresholds["maximum_paper_drawdown"]
        and metrics["execution_error_rate"] <= thresholds["maximum_execution_error_rate"]
        and metrics["stale_data_block_rate"] <= thresholds["maximum_stale_data_block_rate"]
    ):
        return "forward_validation_passed"
    return "forward_validation_failed"


def persist_paper_rollup(conn: psycopg.Connection, elite: dict[str, Any], rollup: dict[str, Any], state: str) -> None:
    key = rollup_key(elite["candidate_id"], datetime.now(UTC).date())
    conn.execute(
        """
        INSERT INTO elite_candidate_paper_rollups(elite_candidate_id, candidate_id, campaign_id, rollup_key, metrics, forward_validation_state, thresholds, simulation_only)
        VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
        ON CONFLICT(rollup_key) DO UPDATE
        SET metrics = EXCLUDED.metrics,
            forward_validation_state = EXCLUDED.forward_validation_state,
            thresholds = EXCLUDED.thresholds,
            calculated_at = NOW()
        """,
        (elite["id"], elite["candidate_id"], elite.get("campaign_id"), key, Jsonb(jsonable(rollup["metrics"])), state, Jsonb(jsonable(rollup["thresholds"]))),
    )


def calculate_evidence_drift(elite: dict[str, Any], paper: dict[str, Any]) -> list[dict[str, Any]]:
    comparisons = {
        "profit_factor": (elite.get("profit_factor"), paper.get("paper_profit_factor")),
        "expectancy": (elite.get("expectancy"), paper.get("paper_expectancy")),
        "win_rate": (historical_metric(elite, "win_rate"), paper.get("paper_win_rate")),
        "drawdown": (elite.get("max_drawdown"), paper.get("paper_max_drawdown")),
        "trade_frequency": (historical_metric(elite, "trade_frequency"), paper.get("signal_frequency")),
        "average_holding_period": (historical_metric(elite, "average_holding_period"), 0),
        "slippage": (historical_metric(elite, "slippage"), paper.get("average_simulated_slippage")),
    }
    if paper.get("closed_trade_count") == 0:
        return [
            {
                "metric_name": metric,
                "historical_value": finite_metric(historical),
                "paper_value": None,
                "absolute_difference": None,
                "percentage_difference": None,
                "drift_classification": "insufficient_forward_sample",
                "detected_at": datetime.now(UTC).isoformat(),
            }
            for metric, (historical, _paper_value) in comparisons.items()
        ]
    drift = []
    for metric, (historical, paper_value) in comparisons.items():
        h = finite_metric(historical)
        p = finite_metric(paper_value)
        diff = p - h
        pct = abs(diff / h) if h else (abs(diff) if p else 0)
        drift.append(
            {
                "metric_name": metric,
                "historical_value": h,
                "paper_value": p,
                "absolute_difference": round(abs(diff), 6),
                "percentage_difference": round(pct, 6),
                "drift_classification": classify_drift(metric, pct),
                "detected_at": datetime.now(UTC).isoformat(),
            }
        )
    return drift


def historical_metric(elite: dict[str, Any], metric: str) -> float:
    history = elite.get("validation_history") or []
    values = []
    for row in history:
        metrics = (row or {}).get("metrics") or {}
        if metric == "win_rate":
            values.append(metrics.get("win_rate"))
        elif metric == "trade_frequency":
            values.append(metrics.get("number_of_trades"))
        elif metric == "average_holding_period":
            values.append(metrics.get("average_holding_period"))
        elif metric == "slippage":
            values.append((row.get("parameters") or {}).get("slippage_rate"))
    return average(value for value in values if value is not None)


def classify_drift(metric: str, pct: float) -> str:
    thresholds = DRIFT_THRESHOLDS[metric]
    if pct >= thresholds["severe"]:
        return "severe"
    if pct >= thresholds["warning"]:
        return "warning"
    return "normal"


def persist_evidence_drift(conn: psycopg.Connection, elite: dict[str, Any], drift: list[dict[str, Any]]) -> None:
    for row in drift:
        key = drift_key(elite["candidate_id"], row["metric_name"], datetime.now(UTC).date())
        conn.execute(
            """
            INSERT INTO elite_candidate_evidence_drift(
                elite_candidate_id, candidate_id, campaign_id, drift_key, metric_name, historical_value,
                paper_value, absolute_difference, percentage_difference, drift_classification, simulation_only
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            ON CONFLICT(drift_key) DO UPDATE
            SET historical_value = EXCLUDED.historical_value,
                paper_value = EXCLUDED.paper_value,
                absolute_difference = EXCLUDED.absolute_difference,
                percentage_difference = EXCLUDED.percentage_difference,
                drift_classification = EXCLUDED.drift_classification,
                detected_at = NOW()
            """,
            (
                elite["id"],
                elite["candidate_id"],
                elite.get("campaign_id"),
                key,
                row["metric_name"],
                row["historical_value"],
                row["paper_value"],
                row["absolute_difference"],
                row["percentage_difference"],
                row["drift_classification"],
            ),
        )


def create_drift_alert(conn: psycopg.Connection, elite: dict[str, Any], drift: list[dict[str, Any]]) -> None:
    severe = [row for row in drift if row["drift_classification"] == "severe"]
    create_evidence_alert(
        conn,
        symbol="SYSTEM",
        timeframe="forward-validation",
        strategy_id=f"{elite['strategy_name']}_{elite['strategy_version']}",
        alert_type="evidence_drift_warning",
        severity="critical",
        verdict="Avoid",
        evidence_summary=f"Severe forward evidence drift detected for {elite['candidate_id']}.",
        matched_rules=[],
        failed_rules=[f"{row['metric_name']} drift is severe." for row in severe],
    )


def rollup_key(candidate_id: str, day: date) -> str:
    return sha256(f"paper_rollup|{candidate_id}|{day.isoformat()}".encode()).hexdigest()


def drift_key(candidate_id: str, metric: str, day: date) -> str:
    return sha256(f"evidence_drift|{candidate_id}|{metric}|{day.isoformat()}".encode()).hexdigest()


def max_drift_classification(rows: list[dict[str, Any]]) -> str:
    if any(row["drift_classification"] == "severe" for row in rows):
        return "severe"
    if any(row["drift_classification"] == "warning" for row in rows):
        return "warning"
    if rows and all(row["drift_classification"] == "insufficient_forward_sample" for row in rows):
        return "insufficient_forward_sample"
    return "normal"


def paper_profit_factor(realized: Decimal) -> float:
    if realized > 0:
        return 999.0
    if realized < 0:
        return 0.0
    return 0.0


def closed_trade_attribution(fills: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(fills, key=lambda row: parse_timestamp(row.get("filled_at")) or datetime.min.replace(tzinfo=UTC))
    open_lots: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    for fill in ordered:
        side = str(fill.get("side"))
        quantity = Decimal(str(fill.get("quantity") or 0))
        price = Decimal(str(fill.get("fill_price") or 0))
        fee = Decimal(str(fill.get("fee") or 0))
        slippage = Decimal(str(fill.get("slippage") or 0))
        timestamp = parse_timestamp(fill.get("filled_at"))
        if side == "buy":
            open_lots.append(
                {
                    "quantity": quantity,
                    "original_quantity": quantity,
                    "price": price,
                    "fee": fee,
                    "slippage": slippage,
                    "timestamp": timestamp,
                    "fill": fill,
                }
            )
            continue
        remaining = quantity
        while remaining > 0 and open_lots:
            lot = open_lots[0]
            matched = min(remaining, lot["quantity"])
            entry_fee = lot["fee"] * (matched / lot["original_quantity"]) if lot["original_quantity"] else Decimal("0")
            pnl = (price - lot["price"]) * matched - entry_fee - fee * (matched / quantity if quantity else Decimal("0"))
            duration = 0.0
            if timestamp and lot["timestamp"]:
                duration = (timestamp - lot["timestamp"]).total_seconds() / 3600
            trades.append(
                {
                    "entry_price": float(lot["price"]),
                    "exit_price": float(price),
                    "quantity": float(matched),
                    "realized_pnl": float(pnl),
                    "holding_period_hours": round(duration, 4),
                    "slippage": float(lot["slippage"] + slippage),
                    "commission": float(entry_fee + fee),
                    "symbol": fill.get("symbol") or (lot["fill"] or {}).get("symbol"),
                    "timeframe": fill.get("timeframe") or (lot["fill"] or {}).get("timeframe"),
                    "account_id": fill.get("account_id") or (lot["fill"] or {}).get("account_id"),
                    "entry_order_id": (lot["fill"] or {}).get("order_id"),
                    "exit_order_id": fill.get("order_id"),
                    "entry_fill_id": (lot["fill"] or {}).get("id"),
                    "exit_fill_id": fill.get("id"),
                    "entry_timestamp": lot["timestamp"],
                    "exit_timestamp": timestamp,
                    "entry_candle_timestamp": (lot["fill"] or {}).get("candle_timestamp"),
                    "exit_candle_timestamp": fill.get("candle_timestamp"),
                    "deployment_id": fill.get("deployment_id") or (lot["fill"] or {}).get("deployment_id"),
                    "campaign_id": fill.get("campaign_id") or (lot["fill"] or {}).get("campaign_id"),
                    "candidate_id": fill.get("candidate_id") or (lot["fill"] or {}).get("candidate_id"),
                    "strategy_id": fill.get("strategy_id") or (lot["fill"] or {}).get("strategy_id"),
                    "strategy_version": fill.get("strategy_version") or (lot["fill"] or {}).get("strategy_version"),
                    "decision_id": fill.get("decision_id") or (lot["fill"] or {}).get("decision_id"),
                    "signal_timestamp": fill.get("signal_timestamp") or (lot["fill"] or {}).get("signal_timestamp"),
                    "evidence_origin": fill.get("evidence_origin") or (lot["fill"] or {}).get("evidence_origin"),
                    "deployment_created_at": fill.get("deployment_created_at") or (lot["fill"] or {}).get("deployment_created_at"),
                    "forward_validation_started_at": fill.get("forward_validation_started_at") or (lot["fill"] or {}).get("forward_validation_started_at"),
                    "deployment_lifecycle_state": fill.get("deployment_lifecycle_state") or (lot["fill"] or {}).get("deployment_lifecycle_state"),
                    "deployment_origin": fill.get("deployment_origin") or (lot["fill"] or {}).get("deployment_origin"),
                    "simulation_only": bool(fill.get("simulation_only", True) and (lot["fill"] or {}).get("simulation_only", True)),
                }
            )
            lot["quantity"] -= matched
            remaining -= matched
            if lot["quantity"] <= 0:
                open_lots.pop(0)
    wins = [Decimal(str(row["realized_pnl"])) for row in trades if Decimal(str(row["realized_pnl"])) > 0]
    losses = [abs(Decimal(str(row["realized_pnl"]))) for row in trades if Decimal(str(row["realized_pnl"])) < 0]
    gross_profit = sum(wins, Decimal("0"))
    gross_loss = sum(losses, Decimal("0"))
    count = len(trades)
    return {
        "closed_trade_count": count,
        "paper_profit_factor": float(gross_profit / gross_loss) if gross_loss else (999.0 if gross_profit else 0.0),
        "paper_expectancy": float((gross_profit - gross_loss) / Decimal(count)) if count else 0.0,
        "paper_win_rate": round(len(wins) / count, 4) if count else 0.0,
        "average_win": float(gross_profit / Decimal(len(wins))) if wins else 0.0,
        "average_loss": float(gross_loss / Decimal(len(losses))) if losses else 0.0,
        "average_trade_duration_hours": average(row["holding_period_hours"] for row in trades),
        "closed_trades": trades[-50:],
    }


def paper_max_drawdown(conn: psycopg.Connection, deployments: list[dict[str, Any]]) -> float:
    account_ids = sorted({row["account_id"] for row in deployments if row.get("account_id")})
    if not account_ids:
        return 0.0
    rows = conn.execute(
        """
        SELECT equity
        FROM paper_equity_curve
        WHERE account_id = ANY(%s) AND simulation_only = TRUE
        ORDER BY timestamp ASC
        """,
        (account_ids,),
    ).fetchall()
    peak = Decimal("0")
    max_dd = Decimal("0")
    for row in rows:
        equity = Decimal(str(row.get("equity") or 0))
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    return float(max_dd)


def sum_decimal(values: Any) -> Decimal:
    return sum((Decimal(str(value or 0)) for value in values), Decimal("0"))


def refresh_campaign_analytics(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    refresh_campaign_batches(conn, campaign_id)
    jobs = conn.execute("SELECT * FROM research_campaign_jobs WHERE campaign_id = %s", (campaign_id,)).fetchall()
    summaries = candidate_consistency_summaries(list(jobs))
    analytics = campaign_analytics(list(jobs), summaries)
    intelligence = campaign_operational_intelligence(conn, campaign_id, list(jobs))
    analytics = {**analytics, **intelligence}
    conn.execute("UPDATE research_campaigns SET analytics = %s, updated_at = NOW() WHERE id = %s", (Jsonb(jsonable(analytics)), campaign_id))
    persist_campaign_analytics_snapshot(conn, campaign_id, analytics)
    update_campaign_counts(conn, campaign_id)
    return analytics


def refresh_campaign_batches(conn: psycopg.Connection, campaign_id: int) -> None:
    rows = conn.execute(
        """
        SELECT batch_id, status, COUNT(*) AS count
        FROM research_campaign_jobs
        WHERE campaign_id = %s AND batch_id IS NOT NULL
        GROUP BY batch_id, status
        """,
        (campaign_id,),
    ).fetchall()
    grouped: dict[int, dict[str, int]] = defaultdict(dict)
    for row in rows:
        grouped[int(row["batch_id"])][row["status"]] = int(row["count"])
    for batch_id, counts in grouped.items():
        total = sum(counts.values())
        completed = sum(counts.get(status, 0) for status in ("promoted", "rejected", "completed"))
        failed = counts.get("failed", 0)
        status = "completed" if total and completed + failed + counts.get("canceled", 0) == total else ("failed" if failed and not counts.get("queued", 0) else "running")
        conn.execute(
            """
            UPDATE research_campaign_batches
            SET status = %s,
                job_count = %s,
                completed_jobs = %s,
                failed_jobs = %s,
                started_at = COALESCE(started_at, NOW()),
                completed_at = CASE WHEN %s = 'completed' THEN COALESCE(completed_at, NOW()) ELSE completed_at END,
                updated_at = NOW()
            WHERE id = %s
            """,
            (status, total, completed, failed, status, batch_id),
        )


def campaign_operational_intelligence(conn: psycopg.Connection, campaign_id: int, jobs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "strategy_family_intelligence": grouped_research_intelligence(conn, campaign_id, jobs, "strategy_family"),
        "asset_intelligence": grouped_research_intelligence(conn, campaign_id, jobs, "symbol"),
        "timeframe_intelligence": grouped_research_intelligence(conn, campaign_id, jobs, "timeframe"),
        "heatmaps": research_heatmaps(jobs),
        "batch_progress": campaign_batch_progress(conn, campaign_id),
        "recent_promotions": recent_elite_candidates(conn, campaign_id),
        "recent_forward_validation_failures": recent_forward_failures(conn, campaign_id),
    }


def grouped_research_intelligence(conn: psycopg.Connection, campaign_id: int, jobs: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in jobs:
        grouped[str(row.get(field) or "unknown")].append(row)
    elite_rows = conn.execute(
        """
        SELECT *
        FROM elite_research_candidates
        WHERE campaign_id = %s AND simulation_only = TRUE
        """,
        (campaign_id,),
    ).fetchall()
    elite_by_candidate = {row["candidate_id"]: dict(row) for row in elite_rows}
    result = []
    for name, rows in grouped.items():
        completed = [row for row in rows if row.get("status") in {"promoted", "rejected", "completed"}]
        promoted = [row for row in rows if row.get("status") == "promoted"]
        rejected = [row for row in rows if row.get("status") == "rejected"]
        metrics = [(row.get("result") or {}).get("metrics") or {} for row in completed]
        aggregate_pf, aggregate_pf_infinite = aggregate_profit_factor(metrics)
        paper = [elite_by_candidate[row["candidate_id"]].get("paper_performance") or {} for row in promoted if row["candidate_id"] in elite_by_candidate]
        result.append(
            {
                "name": name,
                "strategies_tested": len(completed),
                "promoted": len(promoted),
                "rejected": len(rejected),
                "average_research_score": average(row.get("validation_score") for row in completed),
                "average_profit_factor": aggregate_pf,
                "profit_factor_is_infinite": aggregate_pf_infinite,
                "average_expectancy": average(metric.get("expectancy_per_trade") for metric in metrics),
                "average_drawdown": average(metric.get("max_drawdown") for metric in metrics),
                "promotion_rate": round(len(promoted) / len(completed), 4) if completed else 0,
                "rejection_rate": round(len(rejected) / len(completed), 4) if completed else 0,
                "average_paper_performance": average(item.get("total_simulated_pnl") for item in paper),
                "forward_validation_success": sum(1 for item in paper if item.get("forward_validation_state") == "forward_validation_passed"),
                "classification": intelligence_classification(len(promoted), len(rejected), average(row.get("validation_score") for row in completed)),
            }
        )
    return sorted(result, key=lambda row: (row["promotion_rate"], row["average_research_score"], row["strategies_tested"]), reverse=True)


def intelligence_classification(promoted: int, rejected: int, score: float) -> str:
    if promoted and score > 0:
        return "strongest"
    if rejected > promoted * 2:
        return "weakest"
    return "unstable" if promoted and rejected else "developing"


def research_heatmaps(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "asset_heatmap": heatmap_rows(jobs, "symbol", "strategy_family"),
        "strategy_family_heatmap": heatmap_rows(jobs, "strategy_family", "timeframe"),
        "timeframe_heatmap": heatmap_rows(jobs, "timeframe", "strategy_family"),
        "validation_heatmap": heatmap_rows(jobs, "symbol", "timeframe", value="validation_score"),
        "paper_performance_heatmap": [],
    }


def heatmap_rows(jobs: list[dict[str, Any]], x_field: str, y_field: str, value: str = "validation_score") -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in jobs:
        grouped[(str(row.get(x_field) or "unknown"), str(row.get(y_field) or "unknown"))].append(row)
    return [
        {"x": x, "y": y, "job_count": len(rows), "average_value": average(row.get(value) for row in rows), "promotion_rate": round(sum(1 for row in rows if row.get("status") == "promoted") / len(rows), 4)}
        for (x, y), rows in sorted(grouped.items())
    ]


def campaign_batch_progress(conn: psycopg.Connection, campaign_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM research_campaign_batches
        WHERE campaign_id = %s
        ORDER BY batch_number ASC
        """,
        (campaign_id,),
    ).fetchall()
    return [jsonable(dict(row)) for row in rows]


def recent_elite_candidates(conn: psycopg.Connection, campaign_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT candidate_id, family_id, research_score, profit_factor, expectancy, max_drawdown, forward_validation_state, created_at
        FROM elite_research_candidates
        WHERE campaign_id = %s AND simulation_only = TRUE
        ORDER BY created_at DESC
        LIMIT 10
        """,
        (campaign_id,),
    ).fetchall()
    return [jsonable(dict(row)) for row in rows]


def recent_forward_failures(conn: psycopg.Connection, campaign_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT candidate_id, forward_validation_state, drift_status, paper_performance, forward_validation_updated_at
        FROM elite_research_candidates
        WHERE campaign_id = %s
          AND simulation_only = TRUE
          AND forward_validation_state = 'forward_validation_failed'
        ORDER BY forward_validation_updated_at DESC NULLS LAST
        LIMIT 10
        """,
        (campaign_id,),
    ).fetchall()
    return [jsonable(dict(row)) for row in rows]


def persist_campaign_analytics_snapshot(conn: psycopg.Connection, campaign_id: int, analytics: dict[str, Any]) -> None:
    key = sha256(f"campaign_analytics|{campaign_id}|{datetime.now(UTC).replace(second=0, microsecond=0).isoformat()}".encode()).hexdigest()
    conn.execute(
        """
        INSERT INTO research_campaign_analytics_snapshots(
            campaign_id, snapshot_key, analytics, strategy_family_intelligence, asset_intelligence, timeframe_intelligence, heatmaps, simulation_only
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
        ON CONFLICT(snapshot_key) DO UPDATE
        SET analytics = EXCLUDED.analytics,
            strategy_family_intelligence = EXCLUDED.strategy_family_intelligence,
            asset_intelligence = EXCLUDED.asset_intelligence,
            timeframe_intelligence = EXCLUDED.timeframe_intelligence,
            heatmaps = EXCLUDED.heatmaps
        """,
        (
            campaign_id,
            key,
            Jsonb(jsonable(analytics)),
            Jsonb(jsonable(analytics.get("strategy_family_intelligence") or [])),
            Jsonb(jsonable(analytics.get("asset_intelligence") or [])),
            Jsonb(jsonable(analytics.get("timeframe_intelligence") or [])),
            Jsonb(jsonable(analytics.get("heatmaps") or {})),
        ),
    )


def refresh_command_center_aggregate_snapshot(conn: psycopg.Connection) -> dict[str, Any]:
    campaigns = [
        jsonable(dict(row))
        for row in conn.execute(
            """
            SELECT id, campaign_key, name, universe_key, status, requested_candidates,
                   analytics, created_at, started_at, completed_at, updated_at
            FROM research_campaigns
            WHERE simulation_only = TRUE
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()
    ]
    completed_campaigns = [row for row in campaigns if row.get("status") == "completed"]
    overview = aggregate_snapshot_overview(conn, completed_campaigns)
    assets = aggregate_snapshot_dimension(completed_campaigns, "asset_intelligence")
    families = aggregate_snapshot_dimension(completed_campaigns, "strategy_family_intelligence")
    timeframes = aggregate_snapshot_dimension(completed_campaigns, "timeframe_intelligence")
    rejection = aggregate_snapshot_rejections(completed_campaigns)
    payload = {
        "campaign": {
            "id": None,
            "campaign_key": "all_campaign_evidence",
            "name": "All campaign evidence",
            "universe_key": "all",
            "status": "completed",
            "requested_candidates": sum(int(row.get("requested_candidates") or 0) for row in campaigns),
            "campaign_count": len(campaigns),
        },
        "campaigns": campaigns,
        "filters": {},
        "overview": overview,
        "candidate_funnel": snapshot_funnel(overview),
        "filter_options": {
            "assets": [row["name"] for row in assets],
            "asset_classes": ["equity"],
            "timeframes": [row["name"] for row in timeframes],
            "strategy_families": [row["name"] for row in families],
            "candidate_states": ["completed", "rejected", "promoted"],
            "validation_rules": [row["name"] for row in rejection["validation_rules"]],
            "regimes": [],
        },
        "strategy_intelligence": {"rows": families, "highlights": {}},
        "asset_intelligence": {"rows": assets, "highlights": {}},
        "timeframe_intelligence": {"rows": timeframes, "highlights": {}},
        "rejection_analysis": rejection,
        "near_pass_candidates": [],
        "duplicate_analysis": {"unique_candidates": overview["candidates_generated"], "exact_duplicates": 0, "near_duplicates": 0, "duplicate_validation_outcomes": 0, "redundant_parameter_regions": []},
        "recommendations": snapshot_recommendations(rejection, families, assets),
        "next_campaign_proposal": snapshot_next_campaign_proposal(families, assets, timeframes),
        "historical_research": {},
        "terminology": {},
        "source": {
            "authoritative_tables": ["research_campaigns", "research_campaign_jobs", "elite_research_candidates", "strategy_deployments", "research_command_center_snapshots"],
            "candidate_grain": "distinct candidate_id across completed campaigns; research and elite stages are exclusive",
            "refreshed_at": datetime.now(UTC),
        },
        "live_evidence": any(row.get("status") in {"queued", "running"} for row in campaigns),
        "simulation_only": True,
    }
    snapshot_key = sha256(f"command_center_v2|{len(completed_campaigns)}|{max((str(row.get('updated_at') or '') for row in completed_campaigns), default='none')}".encode()).hexdigest()
    conn.execute(
        """
        INSERT INTO research_command_center_snapshots(
            snapshot_key, payload, campaign_count, completed_campaign_count, calculation_version, simulation_only
        )
        VALUES (%s, %s, %s, %s, %s, TRUE)
        ON CONFLICT(snapshot_key) DO UPDATE
        SET payload = EXCLUDED.payload,
            campaign_count = EXCLUDED.campaign_count,
            completed_campaign_count = EXCLUDED.completed_campaign_count,
            created_at = NOW()
        """,
        (snapshot_key, Jsonb(jsonable(payload)), len(campaigns), len(completed_campaigns), "command_center_aggregate_v2"),
    )
    clear_command_center_cache()
    return payload


def aggregate_snapshot_overview(conn: psycopg.Connection, campaigns: list[dict[str, Any]]) -> dict[str, int]:
    campaign_ids = [int(row["id"]) for row in campaigns if row.get("id") is not None]
    if not campaign_ids:
        return {
            "campaign_jobs": 0,
            "candidates_generated": 0,
            "candidates_tested": 0,
            "candidates_rejected": 0,
            "candidates_completed": 0,
            "needs_more_evidence": 0,
            "research_candidates": 0,
            "elite_candidates": 0,
            "candidate_linked_deployments": 0,
        }
    row = conn.execute(
        """
        WITH candidate_status AS (
            SELECT
                candidate_id,
                BOOL_OR(status IN ('completed','rejected','promoted')) AS tested,
                BOOL_OR(status = 'promoted') AS promoted,
                BOOL_AND(status IN ('completed','rejected','promoted','failed','canceled')) AS terminal
            FROM research_campaign_jobs
            WHERE campaign_id = ANY(%s) AND simulation_only = TRUE
            GROUP BY candidate_id
        ),
        elite_ids AS (
            SELECT DISTINCT candidate_id
            FROM elite_research_candidates
            WHERE campaign_id = ANY(%s) AND simulation_only = TRUE
        )
        SELECT
            (SELECT COUNT(*) FROM research_campaign_jobs WHERE campaign_id = ANY(%s) AND simulation_only = TRUE) AS campaign_jobs,
            COUNT(*) AS candidates_generated,
            COUNT(*) FILTER (WHERE candidate_status.tested) AS candidates_tested,
            COUNT(*) FILTER (WHERE candidate_status.terminal AND NOT candidate_status.promoted) AS candidates_rejected,
            COUNT(*) FILTER (WHERE candidate_status.terminal) AS candidates_completed,
            COUNT(*) FILTER (WHERE candidate_status.tested AND NOT candidate_status.terminal AND NOT candidate_status.promoted) AS needs_more_evidence,
            COUNT(*) FILTER (WHERE candidate_status.promoted AND elite_ids.candidate_id IS NULL) AS research_candidates,
            COUNT(elite_ids.candidate_id) AS elite_candidates,
            (
              SELECT COUNT(DISTINCT candidate_id)
              FROM strategy_deployments
              WHERE campaign_id = ANY(%s) AND candidate_id IS NOT NULL AND simulation_only = TRUE
            ) AS candidate_linked_deployments
        FROM candidate_status
        LEFT JOIN elite_ids USING(candidate_id)
        """,
        (campaign_ids, campaign_ids, campaign_ids, campaign_ids),
    ).fetchone() or {}
    return {key: int(row.get(key) or 0) for key in (
        "campaign_jobs",
        "candidates_generated",
        "candidates_tested",
        "candidates_rejected",
        "candidates_completed",
        "needs_more_evidence",
        "research_candidates",
        "elite_candidates",
        "candidate_linked_deployments",
    )}


def aggregate_snapshot_dimension(campaigns: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {"campaigns": set(), "validation_runs": 0, "passed": 0, "rejected": 0, "pf": [], "expectancy": [], "drawdown": []})
    for campaign in campaigns:
        campaign_id = campaign.get("id")
        for row in (campaign.get("analytics") or {}).get(key) or []:
            name = str(row.get("name") or "unknown")
            group = grouped[name]
            group["campaigns"].add(campaign_id)
            runs = int(row.get("strategies_tested") or row.get("validation_runs") or 0)
            group["validation_runs"] += runs
            group["passed"] += int(row.get("promoted") or 0)
            group["rejected"] += int(row.get("rejected") or 0)
            if runs:
                group["pf"].append((finite_metric(row.get("average_profit_factor")), runs))
                group["expectancy"].append((finite_metric(row.get("average_expectancy")), runs))
                group["drawdown"].append((finite_metric(row.get("average_drawdown") or row.get("median_drawdown")), runs))
    result = []
    for name, group in grouped.items():
        runs = int(group["validation_runs"])
        passed = int(group["passed"])
        rejected = int(group["rejected"])
        pass_rate = round(passed / runs, 4) if runs else 0
        result.append({
            "name": name,
            "candidates_tested": runs,
            "campaign_count": len(group["campaigns"]),
            "validation_runs": runs,
            "rejection_rate": round(rejected / runs, 4) if runs else 0,
            "pass_rate": pass_rate,
            "average_profit_factor": weighted_average(group["pf"]),
            "average_expectancy": weighted_average(group["expectancy"]),
            "median_trade_count": None,
            "median_drawdown": weighted_average(group["drawdown"]),
            "stability_pass_rate": None,
            "confidence_interval_pass_rate": None,
            "candidate_quality_score": round(pass_rate * 100, 2),
            "dominant_failure_reason": "Aggregate View" if rejected else None,
            "best_asset": None,
            "best_timeframe": None,
            "deprioritize": runs >= 3 and passed == 0,
            "inactive": runs == 0,
        })
    return sorted(result, key=lambda row: (row["candidate_quality_score"], row["validation_runs"], row["name"]), reverse=True)[:20]


def aggregate_snapshot_rejections(campaigns: list[dict[str, Any]]) -> dict[str, Any]:
    counter: Counter[str] = Counter()
    for campaign in campaigns:
        for row in (campaign.get("analytics") or {}).get("failure_distribution") or []:
            counter[str(row.get("reason") or "unknown")] += int(row.get("count") or 0)
    total = sum(counter.values())
    rules = [{"name": name, "count": count, "rate": round(count / total, 4) if total else 0, "candidate_count": count, "candidate_rate": round(count / total, 4) if total else 0} for name, count in counter.most_common(20)]
    return {
        "rejected_validation_runs": total,
        "rejected_candidates_observed": total,
        "validation_rules": rules,
        "strategy_families": [],
        "assets": [],
        "timeframes": [],
        "market_regimes": [],
        "parameter_ranges": [],
        "metric_ranges": [{"metric": row["name"], "range": "stored campaign failures", "rejected_runs": row["count"]} for row in rules[:10]],
        "dominant_reasons": rules[:5],
    }


def snapshot_funnel(overview: dict[str, int]) -> list[dict[str, Any]]:
    generated = overview["candidates_generated"]
    stages = [
        ("generated", "Generated", generated),
        ("tested", "Tested", overview["candidates_tested"]),
        ("rejected", "Rejected", overview["candidates_rejected"]),
        ("needs_more_evidence", "Needs More Evidence", overview["needs_more_evidence"]),
        ("research_candidate", "Research Candidate", overview["research_candidates"]),
        ("elite_candidate", "Elite Candidate", overview["elite_candidates"]),
        ("paper_deployed", "Paper Deployed", overview["candidate_linked_deployments"]),
    ]
    return [{"key": key, "label": label, "count": count, "rate_from_generated": round(count / generated, 4) if generated else 0} for key, label, count in stages]


def snapshot_recommendations(rejection: dict[str, Any], families: list[dict[str, Any]], assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    recommendations = []
    dominant = (rejection.get("dominant_reasons") or [None])[0]
    if dominant:
        recommendations.append({"recommendation": f"Reduce repeated {str(dominant['name']).replace('_', ' ')} failures in the next research allocation.", "evidence_source": "research_command_center_snapshots.rejection_analysis", "candidate_count": dominant["count"], "support_rate": dominant["rate"], "validation_thresholds_changed": False})
    weak_asset = next((row for row in reversed(assets) if row["deprioritize"]), None)
    if weak_asset:
        recommendations.append({"recommendation": f"Deprioritize {weak_asset['name']} until a repair hypothesis improves aggregate evidence.", "evidence_source": "research_command_center_snapshots.asset_intelligence", "candidate_count": weak_asset["validation_runs"], "support_rate": weak_asset["rejection_rate"], "validation_thresholds_changed": False})
    return recommendations


def snapshot_next_campaign_proposal(families: list[dict[str, Any]], assets: list[dict[str, Any]], timeframes: list[dict[str, Any]]) -> dict[str, Any] | None:
    retain_assets = [row["name"] for row in assets if not row["deprioritize"]][:10]
    deprioritize_assets = [row["name"] for row in assets if row["deprioritize"]][:10]
    if not retain_assets and not deprioritize_assets:
        return None
    return {
        "proposal_version": "command_center_aggregate_v1",
        "strategy_families_to_retain": [row["name"] for row in families if not row["deprioritize"]][:5],
        "strategy_families_to_deprioritize": [row["name"] for row in families if row["deprioritize"]][:5],
        "assets_to_retain": retain_assets,
        "assets_to_deprioritize": deprioritize_assets,
        "timeframes_to_retain": [row["name"] for row in timeframes if not row["deprioritize"]][:3],
        "timeframes_to_deprioritize": [row["name"] for row in timeframes if row["deprioritize"]][:3],
        "candidate_count": sum(row["validation_runs"] for row in families),
        "expected_duplicate_work_reduction": 0,
        "new_hypothesis_tests": ["Confirm promising aggregate regions with unchanged validation thresholds."],
        "source_campaign_version": CAMPAIGN_VERSION,
        "validation_thresholds_changed": False,
    }


def weighted_average(values: list[tuple[float, int]]) -> float:
    total_weight = sum(weight for _value, weight in values)
    return round(sum(value * weight for value, weight in values) / total_weight, 4) if total_weight else 0.0


def campaign_analytics(jobs: list[dict[str, Any]], summaries: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(str(row["status"]) for row in jobs)
    completed = [row for row in jobs if row["status"] in {"completed", "rejected", "promoted"}]
    results = [row.get("result") or {} for row in completed]
    metrics = [result.get("metrics") or {} for result in results]
    failure_counter = Counter(reason for row in jobs for reason in list(row.get("failure_reasons") or []))
    total_jobs = len(jobs)
    tested = len(completed)
    promoted = statuses.get("promoted", 0)
    rejected = statuses.get("rejected", 0)
    aggregate_pf, aggregate_pf_infinite = aggregate_profit_factor(metrics)
    return {
        "strategies_generated": len({row["candidate_id"] for row in jobs}),
        "strategies_tested": tested,
        "jobs_total": total_jobs,
        "completion_percentage": round(((tested + statuses.get("failed", 0) + statuses.get("canceled", 0)) / total_jobs) * 100, 2) if total_jobs else 0,
        "estimated_remaining_jobs": sum(statuses.get(status, 0) for status in ("queued", "running", "retrying", "blocked_data", "deferred_rate_limit")),
        "jobs_by_status": dict(statuses),
        "validation_pass_rate": round(promoted / tested, 4) if tested else 0,
        "rejection_rate": round(rejected / tested, 4) if tested else 0,
        "rejected": rejected,
        "promoted": promoted,
        "average_research_score": average(row.get("validation_score") for row in completed),
        "average_profit_factor": aggregate_pf,
        "profit_factor_is_infinite": aggregate_pf_infinite,
        "average_runtime_ms": average(row.get("execution_runtime_ms") for row in completed),
        "runtime_by_strategy_family": grouped_runtime(jobs, "strategy_family"),
        "runtime_by_asset": grouped_runtime(jobs, "symbol"),
        "runtime_by_timeframe": grouped_runtime(jobs, "timeframe"),
        "retry_frequency": round(sum(int(row.get("attempts") or 0) for row in jobs) / total_jobs, 4) if total_jobs else 0,
        "failure_distribution": [{"reason": reason, "count": count} for reason, count in failure_counter.most_common()],
        "provider_latency_ms": average(row.get("provider_latency_ms") for row in jobs),
        "database_latency_ms": average(row.get("database_latency_ms") for row in jobs),
        "campaign_efficiency": round(promoted / max(1, total_jobs), 4),
        "best_candidates": summaries[:10],
        "worst_candidates": list(reversed(summaries[-10:])),
        "validation_bottlenecks": [{"reason": reason, "count": count} for reason, count in failure_counter.most_common(10)],
        "queue_statistics": {
            "queued": statuses.get("queued", 0),
            "running": statuses.get("running", 0),
            "retrying": statuses.get("retrying", 0),
            "deferred": statuses.get("deferred_rate_limit", 0),
            "blocked_data": statuses.get("blocked_data", 0),
            "failed": statuses.get("failed", 0),
            "completed": len(completed),
        },
        "throttled_jobs": statuses.get("deferred_rate_limit", 0),
        "deferred_jobs": statuses.get("deferred_rate_limit", 0),
        "blocked_data_jobs": statuses.get("blocked_data", 0),
        "runtime": {"measured_by": "database timestamps", "campaign_version": CAMPAIGN_VERSION},
    }


def grouped_runtime(jobs: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in jobs:
        grouped[str(row.get(field) or "unknown")].append(row)
    result = []
    for key, rows in grouped.items():
        result.append({"group": key, "job_count": len(rows), "average_runtime_ms": average(row.get("execution_runtime_ms") for row in rows)})
    return sorted(result, key=lambda row: row["job_count"], reverse=True)


def update_job_consistency_scores(conn: psycopg.Connection, campaign_id: int, summaries: list[dict[str, Any]]) -> None:
    for summary in summaries:
        conn.execute(
            """
            UPDATE research_campaign_jobs
            SET consistency_score = %s
            WHERE campaign_id = %s AND candidate_id = %s
            """,
            (summary["consistency_score"], campaign_id, summary["candidate_id"]),
        )


def update_campaign_counts(conn: psycopg.Connection, campaign_id: int) -> None:
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM research_campaign_jobs
        WHERE campaign_id = %s
        GROUP BY status
        """,
        (campaign_id,),
    ).fetchall()
    counts = {row["status"]: int(row["count"]) for row in rows}
    conn.execute(
        """
        UPDATE research_campaigns
        SET queued_jobs = %s,
            completed_jobs = %s,
            failed_jobs = %s,
            updated_at = NOW()
        WHERE id = %s
        """,
        (
            sum(counts.values()),
            counts.get("completed", 0) + counts.get("rejected", 0) + counts.get("promoted", 0),
            counts.get("failed", 0),
            campaign_id,
        ),
    )


def queued_job_count(conn: psycopg.Connection, campaign_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM research_campaign_jobs WHERE campaign_id = %s AND status = 'queued'",
        (campaign_id,),
    ).fetchone()
    return int(row["count"])


def open_job_count(conn: psycopg.Connection, campaign_id: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM research_campaign_jobs
        WHERE campaign_id = %s
          AND status IN ('queued', 'running', 'retrying', 'blocked_data', 'deferred_rate_limit')
        """,
        (campaign_id,),
    ).fetchone()
    return int(row["count"])


def get_campaign(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM research_campaigns WHERE id = %s AND simulation_only = TRUE", (campaign_id,)).fetchone()
    if not row:
        raise ValueError("research campaign not found")
    return dict(row)


# --- Phase A: deterministic campaign reliability -----------------------------
# A job is "terminal" when no worker will ever act on it again. A campaign may
# only finalize once every job is terminal. `blocked_terminal` (migration 040)
# is the terminal form of a data block whose retry ceiling is exhausted.
TERMINAL_JOB_STATUSES = frozenset({"completed", "rejected", "promoted", "failed", "canceled", "blocked_terminal"})
OPEN_JOB_STATUSES = frozenset({"queued", "running", "retrying", "blocked_data", "deferred_rate_limit"})
RETRYABLE_BLOCKED_STATUSES = frozenset({"blocked_data", "deferred_rate_limit", "retrying"})
CAMPAIGN_TERMINAL_STATUSES = frozenset({"completed", "canceled", "failed"})


def live_campaign_worker_count(conn: psycopg.Connection) -> int:
    """Authoritative count of workers whose heartbeat is within the stale window.

    This is the single source of truth for "workers alive" -- never a frontend
    cache. A worker that stopped heartbeating longer than `worker_stale_seconds`
    ago is not counted, regardless of what its last recorded status said.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM research_campaign_workers
        WHERE status <> 'stopped'
          AND heartbeat_at IS NOT NULL
          AND heartbeat_at >= NOW() - (%s * INTERVAL '1 second')
        """,
        (worker_stale_seconds(),),
    ).fetchone()
    return int(row["count"]) if row else 0


def campaign_progress_breakdown(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    """Authoritative, terminology-correct progress for one campaign.

    Distinguishes completed / running / queued / retryable-blocked /
    terminal-blocked / failed / cancelled, exposes worker liveness, and flags
    `repair_required` whenever a campaign invariant is broken (a completed
    campaign still holding open jobs, or a running campaign that cannot make
    progress because no worker is alive and nothing is runnable).
    """
    campaign = get_campaign(conn, campaign_id)
    config = scheduling_config(campaign)
    retry_limit = int(config["retry_limit"])
    rows = conn.execute(
        """
        SELECT status,
               COUNT(*) AS count,
               COUNT(*) FILTER (WHERE status = 'blocked_data' AND attempts >= %s) AS exhausted_blocked,
               COUNT(*) FILTER (
                   WHERE status = 'running'
                     AND (lease_expires_at IS NULL OR lease_expires_at <= NOW())
               ) AS stale_leases
        FROM research_campaign_jobs
        WHERE campaign_id = %s AND simulation_only = TRUE
        GROUP BY status
        """,
        (retry_limit, campaign_id),
    ).fetchall()
    counts = {str(row["status"]): int(row["count"]) for row in rows}
    exhausted_blocked = sum(int(row["exhausted_blocked"] or 0) for row in rows)
    stale_leases = sum(int(row["stale_leases"] or 0) for row in rows)
    total = sum(counts.values())
    completed = counts.get("completed", 0) + counts.get("rejected", 0) + counts.get("promoted", 0)
    running = counts.get("running", 0)
    queued = counts.get("queued", 0)
    retryable_blocked = counts.get("blocked_data", 0) + counts.get("deferred_rate_limit", 0) + counts.get("retrying", 0)
    terminal_blocked = counts.get("blocked_terminal", 0)
    failed = counts.get("failed", 0)
    cancelled = counts.get("canceled", 0)
    open_jobs = sum(counts.get(status, 0) for status in OPEN_JOB_STATUSES)
    terminal_jobs = sum(counts.get(status, 0) for status in TERMINAL_JOB_STATUSES)
    live_workers = live_campaign_worker_count(conn)
    status = str(campaign.get("status") or "")

    completed_with_open = status == "completed" and open_jobs > 0
    running_no_progress = status == "running" and open_jobs > 0 and live_workers == 0
    exhausted_blocks_present = exhausted_blocked > 0
    stale_leases_present = stale_leases > 0
    repair_required = bool(completed_with_open or exhausted_blocks_present or stale_leases_present or (running_no_progress and queued == 0))

    return {
        "campaign_id": campaign_id,
        "campaign_status": status,
        "execution_status": campaign.get("execution_status"),
        "total_jobs": total,
        "buckets": {
            "completed": completed,
            "running": running,
            "queued": queued,
            "retryable_blocked": retryable_blocked,
            "terminal_blocked": terminal_blocked,
            "failed": failed,
            "cancelled": cancelled,
        },
        "raw_status_counts": counts,
        "open_jobs": open_jobs,
        "terminal_jobs": terminal_jobs,
        "progress_pct": round(100.0 * terminal_jobs / total, 2) if total else 0.0,
        "live_workers": live_workers,
        "target_workers": int(campaign.get("target_workers") or 0),
        "exhausted_blocked_jobs": exhausted_blocked,
        "stale_leases": stale_leases,
        "retry_limit": retry_limit,
        "invariants": {
            "completed_with_open_jobs": completed_with_open,
            "running_without_workers_or_runnable": running_no_progress and queued == 0,
            "exhausted_blocks_present": exhausted_blocks_present,
            "stale_leases_present": stale_leases_present,
        },
        "repair_required": repair_required,
        "all_terminal": open_jobs == 0 and total > 0,
    }


def campaign_repair_plan(
    jobs: list[dict[str, Any]],
    *,
    campaign_status: str,
    retry_limit: int,
    now: datetime,
    terminalize_exhausted_blocks: bool = True,
) -> dict[str, Any]:
    """Pure, deterministic decision of what a repair must do to a job set.

    Given the current jobs (each a dict with at least id/status/attempts/
    lease_expires_at) it decides, without touching the database:
      - which running jobs have a stale lease and must return to `queued`,
      - which blocked_data jobs have exhausted their retry ceiling and must
        become terminal `blocked_terminal`,
      - the resulting open-job count,
      - whether a terminal-marked campaign must be reopened (open jobs remain),
      - whether the campaign may now finalize (no open jobs remain).

    Keeping this pure makes every recovery rule unit-testable and guarantees
    the same inputs always yield the same plan (determinism requirement).
    """
    release_lease_ids: list[Any] = []
    terminalize_ids: list[Any] = []
    resulting_status: dict[Any, str] = {}
    for job in jobs:
        status = str(job.get("status") or "")
        job_id = job.get("id")
        lease = job.get("lease_expires_at")
        attempts = int(job.get("attempts") or 0)
        if status == "running" and (lease is None or lease <= now):
            release_lease_ids.append(job_id)
            resulting_status[job_id] = "queued"
        elif status == "blocked_data" and terminalize_exhausted_blocks and attempts >= retry_limit:
            terminalize_ids.append(job_id)
            resulting_status[job_id] = "blocked_terminal"
        else:
            resulting_status[job_id] = status

    open_after = sum(1 for status in resulting_status.values() if status in OPEN_JOB_STATUSES)
    total = len(jobs)
    reopen = campaign_status in CAMPAIGN_TERMINAL_STATUSES and open_after > 0
    finalize = open_after == 0 and total > 0
    return {
        "release_lease_ids": release_lease_ids,
        "terminalize_ids": terminalize_ids,
        "open_after": open_after,
        "total": total,
        "reopen": reopen,
        "finalize": finalize,
    }


def repair_campaign(
    conn: psycopg.Connection,
    campaign_id: int,
    *,
    operator: str = "cli",
    terminalize_exhausted_blocks: bool = True,
) -> dict[str, Any]:
    """Deterministically move a campaign back to a consistent state.

    Idempotent. Under a campaign row lock it applies `campaign_repair_plan`:
      1. Releases stale leases (running jobs whose lease expired -> queued).
      2. Terminalizes unrecoverable blocked jobs (blocked_data whose retry
         ceiling is exhausted -> blocked_terminal, reason preserved).
      3. Reopens a campaign wrongly marked completed while open jobs remain
         (the scout-expansion/finalize race) so its jobs can drain.
      4. Recomputes campaign totals.
      5. Finalizes the campaign iff every job is now terminal.

    It never fabricates results, never weakens thresholds, and never deletes
    evidence. Every action is counted and returned for audit.
    """
    ensure_campaign_tables(conn)
    before = campaign_progress_breakdown(conn, campaign_id)
    campaign = conn.execute(
        "SELECT * FROM research_campaigns WHERE id = %s AND simulation_only = TRUE FOR UPDATE",
        (campaign_id,),
    ).fetchone()
    if not campaign:
        raise ValueError("research campaign not found")
    campaign = dict(campaign)
    config = scheduling_config(campaign)
    retry_limit = int(config["retry_limit"])
    jobs = [
        dict(row)
        for row in conn.execute(
            "SELECT id, status, attempts, lease_expires_at FROM research_campaign_jobs WHERE campaign_id = %s AND simulation_only = TRUE",
            (campaign_id,),
        ).fetchall()
    ]
    now = conn.execute("SELECT NOW() AS now").fetchone()["now"]
    plan = campaign_repair_plan(
        jobs,
        campaign_status=str(campaign.get("status") or ""),
        retry_limit=retry_limit,
        now=now,
        terminalize_exhausted_blocks=terminalize_exhausted_blocks,
    )

    if plan["release_lease_ids"]:
        conn.execute(
            """
            UPDATE research_campaign_jobs
            SET status = 'queued',
                worker_id = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                heartbeat_at = NULL,
                recovery_classification = 'stale_lease_released',
                recovered_at = NOW(),
                recovery_worker_id = %s,
                updated_at = NOW()
            WHERE id = ANY(%s)
            """,
            (f"repair::{operator}", plan["release_lease_ids"]),
        )
    if plan["terminalize_ids"]:
        conn.execute(
            """
            UPDATE research_campaign_jobs
            SET status = 'blocked_terminal',
                blocked_reason = COALESCE(blocked_reason, latest_error, 'retry ceiling exhausted'),
                failure_classification = COALESCE(failure_classification, 'blocked_terminal'),
                updated_at = NOW()
            WHERE id = ANY(%s)
            """,
            (plan["terminalize_ids"],),
        )
    if plan["reopen"]:
        conn.execute(
            """
            UPDATE research_campaigns
            SET status = 'running',
                completed_at = NULL,
                finalized_at = NULL,
                canceled_at = NULL,
                updated_at = NOW()
            WHERE id = %s
            """,
            (campaign_id,),
        )

    update_campaign_counts(conn, campaign_id)

    if plan["finalize"]:
        finalize_research_campaign(conn, campaign_id)

    actions = {
        "stale_leases_released": len(plan["release_lease_ids"]),
        "blocked_jobs_terminalized": len(plan["terminalize_ids"]),
        "campaign_reopened": 1 if plan["reopen"] else 0,
        "campaign_finalized": 1 if plan["finalize"] else 0,
    }
    conn.commit()
    after = campaign_progress_breakdown(conn, campaign_id)
    log_event(
        "Campaign repair complete",
        campaign_id=campaign_id,
        operator=operator,
        actions=actions,
        repair_required_before=before["repair_required"],
        repair_required_after=after["repair_required"],
    )
    return {
        "campaign_id": campaign_id,
        "operator": operator,
        "actions": actions,
        "reopened": plan["reopen"],
        "finalized": plan["finalize"],
        "before": before,
        "after": after,
        "repair_resolved": not after["repair_required"],
    }


def campaign_progress_analytics(
    conn: psycopg.Connection,
    campaign_id: int,
    stored_analytics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return live campaign progress without loading candidate or result payloads."""
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM research_campaign_jobs
        WHERE campaign_id = %s AND simulation_only = TRUE
        GROUP BY status
        """,
        (campaign_id,),
    ).fetchall()
    statuses = {str(row["status"]): int(row["count"]) for row in rows}
    generated = conn.execute(
        """
        SELECT COUNT(DISTINCT candidate_id) AS count
        FROM research_campaign_jobs
        WHERE campaign_id = %s AND simulation_only = TRUE
        """,
        (campaign_id,),
    ).fetchone()
    total_jobs = sum(statuses.values())
    tested = sum(statuses.get(status, 0) for status in ("completed", "rejected", "promoted"))
    terminal = tested + statuses.get("failed", 0) + statuses.get("canceled", 0)
    promoted = statuses.get("promoted", 0)
    rejected = statuses.get("rejected", 0)
    analytics = dict(stored_analytics or {})
    analytics.update(
        {
            "strategies_generated": int((generated or {}).get("count") or 0),
            "strategies_tested": tested,
            "jobs_total": total_jobs,
            "completion_percentage": round((terminal / total_jobs) * 100, 2) if total_jobs else 0,
            "estimated_remaining_jobs": sum(
                statuses.get(status, 0)
                for status in ("queued", "running", "retrying", "blocked_data", "deferred_rate_limit")
            ),
            "jobs_by_status": statuses,
            "validation_pass_rate": round(promoted / tested, 4) if tested else 0,
            "rejection_rate": round(rejected / tested, 4) if tested else 0,
            "rejected": rejected,
            "promoted": promoted,
        }
    )
    return analytics


def list_research_campaigns(conn: psycopg.Connection, *, limit: int = 50) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT
            c.id,
            c.name,
            c.universe_key,
            c.status,
            c.dataset_id,
            c.dataset_mode,
            c.generator_version,
            c.requested_candidates,
            c.target_workers,
            c.controls,
            c.created_at,
            c.started_at,
            c.completed_at,
            c.updated_at,
            COUNT(j.id) AS total_jobs,
            COUNT(j.id) FILTER (WHERE j.status = 'queued') AS queued_jobs,
            COUNT(j.id) FILTER (WHERE j.status = 'running') AS running_jobs,
            COUNT(j.id) FILTER (WHERE j.status = 'blocked_data') AS blocked_jobs,
            COUNT(j.id) FILTER (WHERE j.status = 'deferred_rate_limit') AS deferred_jobs,
            COUNT(j.id) FILTER (WHERE j.status IN ('completed', 'promoted', 'rejected', 'failed', 'canceled')) AS terminal_jobs,
            COUNT(j.id) FILTER (
                WHERE j.status IN ('completed', 'promoted', 'rejected', 'failed', 'canceled')
                  AND j.updated_at >= NOW() - INTERVAL '15 minutes'
            ) AS recent_terminal_jobs,
            COUNT(j.id) FILTER (
                WHERE j.status IN ('completed', 'promoted', 'rejected', 'failed', 'canceled')
                  AND j.updated_at >= NOW() - INTERVAL '5 minutes'
            ) AS terminal_jobs_5m,
            COUNT(j.id) FILTER (
                WHERE j.status IN ('completed', 'promoted', 'rejected', 'failed', 'canceled')
                  AND j.updated_at >= NOW() - INTERVAL '15 minutes'
            ) AS terminal_jobs_15m,
            AVG(j.execution_runtime_ms) FILTER (WHERE j.execution_runtime_ms IS NOT NULL) AS average_profiled_runtime_ms,
            COUNT(j.id) FILTER (WHERE j.execution_runtime_ms IS NOT NULL) AS profiled_jobs,
            AVG(EXTRACT(EPOCH FROM (j.claimed_at - j.created_at)) * 1000) FILTER (WHERE j.claimed_at IS NOT NULL) AS average_queue_delay_ms,
            COUNT(j.id) FILTER (WHERE j.status = 'promoted') AS promoted_jobs,
            COUNT(j.id) FILTER (WHERE j.status = 'rejected') AS rejected_jobs
        FROM research_campaigns c
        LEFT JOIN research_campaign_jobs j ON j.campaign_id = c.id
        WHERE c.simulation_only = TRUE
        GROUP BY c.id
        ORDER BY c.updated_at DESC, c.id DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    campaigns = [campaign_list_row_with_eta(dict(row)) for row in rows]
    return {
        "campaigns": campaigns,
        "summary": {
            "running": sum(1 for row in campaigns if row["status"] == "running"),
            "queued": sum(1 for row in campaigns if row["status"] == "queued"),
            "paused": sum(1 for row in campaigns if row["status"] == "paused"),
        },
        "simulation_only": True,
    }


def campaign_list_row_with_eta(row: dict[str, Any]) -> dict[str, Any]:
    controls = dict(row.pop("controls", {}) or {})
    execution = dict(controls.get("research_execution") or {})
    row["search_mode"] = execution.get("search_mode", "full")
    row["research_stage"] = execution.get("stage", "full")
    row["scout_candidate_count"] = int(execution.get("scout_candidate_count") or 0)
    row["expanded_routes"] = len(execution.get("expanded_routes") or [])
    row["expansion_jobs_created"] = int(execution.get("expansion_jobs_created") or 0)
    total_jobs = int(row.get("total_jobs") or 0)
    terminal_jobs = int(row.get("terminal_jobs") or 0)
    blocked_jobs = int(row.get("blocked_jobs") or 0)
    deferred_jobs = int(row.get("deferred_jobs") or 0)
    executable_remaining = max(total_jobs - terminal_jobs - blocked_jobs - deferred_jobs, 0)
    terminal_5m = int(row.pop("terminal_jobs_5m", 0) or 0)
    terminal_15m = int(row.pop("terminal_jobs_15m", 0) or 0)
    row.pop("recent_terminal_jobs", None)
    jobs_per_minute_5m = terminal_5m / 5 if terminal_5m > 0 else 0.0
    jobs_per_minute_15m = terminal_15m / 15 if terminal_15m > 0 else 0.0
    average_runtime_ms = finite_metric(row.get("average_profiled_runtime_ms"))
    profiled_jobs = int(row.get("profiled_jobs") or 0)
    effective_workers = max(1, int(row.get("target_workers") or 0))
    eta_seconds = None
    eta_method = "estimating"
    sampled_terminal_jobs = 0
    if row.get("status") in {"queued", "running"} and executable_remaining > 0:
        if terminal_5m >= 20 and jobs_per_minute_5m > 0:
            eta_seconds = round((executable_remaining / jobs_per_minute_5m) * 60)
            eta_method = "rolling_5m"
            sampled_terminal_jobs = terminal_5m
        elif terminal_15m >= 20 and jobs_per_minute_15m > 0:
            eta_seconds = round((executable_remaining / jobs_per_minute_15m) * 60)
            eta_method = "rolling_15m"
            sampled_terminal_jobs = terminal_15m
        elif profiled_jobs >= 5 and average_runtime_ms > 0:
            jobs_per_second = effective_workers / max(average_runtime_ms / 1000, 0.001)
            eta_seconds = round(executable_remaining / jobs_per_second)
            eta_method = "profiled_runtime"
            sampled_terminal_jobs = profiled_jobs
    row["estimated_seconds_remaining"] = eta_seconds
    row["eta_seconds"] = eta_seconds
    row["eta_method"] = eta_method
    row["sampled_terminal_jobs"] = sampled_terminal_jobs
    row["jobs_per_minute"] = round(jobs_per_minute_5m or jobs_per_minute_15m, 2)
    row["jobs_per_minute_5m"] = round(jobs_per_minute_5m, 2)
    row["jobs_per_minute_15m"] = round(jobs_per_minute_15m, 2)
    row["executable_remaining_jobs"] = executable_remaining
    row["average_queue_delay_ms"] = round(finite_metric(row.get("average_queue_delay_ms")), 2)
    return jsonable(row)


def get_campaign_performance_profile(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    campaign = get_campaign(conn, campaign_id)
    row = conn.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE execution_runtime_ms IS NOT NULL) AS profiled_jobs,
            AVG(execution_runtime_ms) FILTER (WHERE execution_runtime_ms IS NOT NULL) AS total_runtime_ms,
            AVG(COALESCE((execution_profile->>'data_loading_ms')::double precision, 0)) FILTER (WHERE execution_runtime_ms IS NOT NULL) AS data_loading_ms,
            AVG(COALESCE((execution_profile->>'indicator_calculation_ms')::double precision, 0)) FILTER (WHERE execution_runtime_ms IS NOT NULL) AS indicator_calculation_ms,
            AVG(COALESCE((execution_profile->>'simulation_ms')::double precision, 0)) FILTER (WHERE execution_runtime_ms IS NOT NULL) AS simulation_ms,
            AVG(COALESCE((execution_profile->>'writing_results_ms')::double precision, 0)) FILTER (WHERE execution_runtime_ms IS NOT NULL) AS writing_results_ms,
            AVG(COALESCE((execution_profile->>'queue_operations_ms')::double precision, 0)) FILTER (WHERE execution_runtime_ms IS NOT NULL) AS queue_operations_ms,
            AVG(CASE WHEN execution_profile->>'dataset_cache_hit' = 'true' THEN 1.0 ELSE 0.0 END) FILTER (WHERE execution_runtime_ms IS NOT NULL) AS dataset_cache_hit_rate,
            COUNT(DISTINCT worker_id) FILTER (
                WHERE status = 'running'
                  AND worker_id IS NOT NULL
                  AND lease_expires_at > NOW()
            ) AS active_parallel_workers,
            COUNT(*) FILTER (
                WHERE status = 'running'
                  AND worker_id IS NOT NULL
                  AND lease_expires_at > NOW()
            ) AS active_parallel_jobs
        FROM research_campaign_jobs
        WHERE campaign_id = %s
        """,
        (campaign_id,),
    ).fetchone() or {}
    pool = parallel_pool_snapshot(campaign_id)
    durable_runtime = campaign_runtime_snapshot(conn, campaign_id, campaign=campaign)
    efficiency = campaign_efficiency_metrics(conn, campaign_id, durable_runtime, campaign=campaign)
    active_parallel_workers = max(int(row.get("active_parallel_workers") or 0), int(pool.get("live_workers") or 0))
    return {
        "campaign_id": campaign_id,
        "profiled_jobs": int(row.get("profiled_jobs") or 0),
        "average_ms": {
            "total": round(finite_metric(row.get("total_runtime_ms")), 3),
            "loading_market_data": round(finite_metric(row.get("data_loading_ms")), 3),
            "calculating_indicators": round(finite_metric(row.get("indicator_calculation_ms")), 3),
            "running_simulation": round(finite_metric(row.get("simulation_ms")), 3),
            "writing_results": round(finite_metric(row.get("writing_results_ms")), 3),
            "database_queue_operations": round(finite_metric(row.get("queue_operations_ms")), 3),
        },
        "dataset_cache_hit_rate": round(finite_metric(row.get("dataset_cache_hit_rate")), 4),
        "runtime": {
            "active_parallel_workers": max(active_parallel_workers, int(durable_runtime["effective_workers"])),
            "active_parallel_jobs": int(row.get("active_parallel_jobs") or 0),
            "configured_parallel_workers": int(durable_runtime["target_workers"] or pool.get("workers") or active_parallel_workers),
            "starting_parallel_workers": int(durable_runtime["starting_workers"] or pool.get("starting_workers") or 0),
            "parallel_pool_active": bool(durable_runtime["target_workers"] or pool.get("active")),
            "parallel_pool_status": "running" if durable_runtime["target_workers"] else str(pool.get("status") or "idle"),
            "processed_parallel_jobs": int(pool.get("processed_jobs") or 0),
            "preloaded_datasets": int(pool.get("preloaded_datasets") or 0),
            "resident_memory_mb": round(process_resident_memory_bytes() / (1024 * 1024), 1),
            **durable_runtime,
        },
        "efficiency": efficiency,
        "simulation_only": True,
    }


def campaign_efficiency_metrics(
    conn: psycopg.Connection,
    campaign_id: int,
    runtime: dict[str, Any] | None = None,
    *,
    campaign: dict[str, Any] | None = None,
) -> dict[str, Any]:
    campaign = campaign or get_campaign(conn, campaign_id)
    controls = dict(campaign.get("controls") or {})
    generation = dict(controls.get("candidate_generation_mix") or {})
    dedupe = dict(controls.get("execution_key_deduplication") or {})
    row = conn.execute(
        """
        SELECT
            COUNT(DISTINCT candidate_id) FILTER (WHERE status IN ('completed', 'promoted', 'rejected', 'failed')) AS evaluated_unique_candidates,
            COUNT(DISTINCT candidate_id) FILTER (WHERE status = 'promoted') AS near_pass_candidates,
            AVG(execution_runtime_ms) FILTER (WHERE execution_runtime_ms IS NOT NULL) AS average_simulation_time_ms,
            AVG(EXTRACT(EPOCH FROM (claimed_at - created_at)) * 1000) FILTER (WHERE claimed_at IS NOT NULL) AS average_queue_delay_ms,
            (SELECT COUNT(*) FROM elite_research_candidates WHERE campaign_id = %s AND simulation_only = TRUE AND forward_validation_state = 'forward_validation_passed') AS elite_count
        FROM research_campaign_jobs
        WHERE campaign_id = %s
        """,
        (campaign_id, campaign_id),
    ).fetchone() or {}
    elite_count = int(row.get("elite_count") or 0)
    evaluated = int(row.get("evaluated_unique_candidates") or 0)
    duplicates_prevented = int(generation.get("duplicates_prevented") or 0) + int(dedupe.get("duplicates_prevented") or 0)
    attempted = int(generation.get("attempted_candidate_generations") or evaluated or 0)
    runtime = runtime or campaign_runtime_snapshot(conn, campaign_id)
    target = max(1, int(runtime.get("target_workers") or 1))
    effective = int(runtime.get("effective_workers") or 0)
    return {
        "duplicate_candidates_prevented": duplicates_prevented,
        "jobs_skipped": int(generation.get("jobs_skipped") or 0) + int(dedupe.get("jobs_skipped") or 0),
        "near_pass_ratio": round(int(row.get("near_pass_candidates") or 0) / evaluated, 4) if evaluated else 0,
        "elite_ratio": round(elite_count / evaluated, 4) if evaluated else 0,
        "worker_utilization": round(effective / target, 4) if target else 0,
        "duplicate_prevention_rate": round(duplicates_prevented / attempted, 4) if attempted else 0,
        "average_simulation_time_ms": round(finite_metric(row.get("average_simulation_time_ms")), 2),
        "average_queue_delay_ms": round(finite_metric(row.get("average_queue_delay_ms")), 2),
    }


def process_resident_memory_bytes() -> int:
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("page_fault_count", wintypes.DWORD),
                    ("peak_working_set_size", ctypes.c_size_t),
                    ("working_set_size", ctypes.c_size_t),
                    ("quota_peak_paged_pool_usage", ctypes.c_size_t),
                    ("quota_paged_pool_usage", ctypes.c_size_t),
                    ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
                    ("quota_non_paged_pool_usage", ctypes.c_size_t),
                    ("pagefile_usage", ctypes.c_size_t),
                    ("peak_pagefile_usage", ctypes.c_size_t),
                ]

            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            get_current_process = ctypes.windll.kernel32.GetCurrentProcess
            get_current_process.restype = wintypes.HANDLE
            get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
            get_process_memory_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD]
            get_process_memory_info.restype = wintypes.BOOL
            process = get_current_process()
            if get_process_memory_info(process, ctypes.byref(counters), counters.cb):
                return int(counters.working_set_size)
        elif os.path.exists("/proc/self/statm"):
            with open("/proc/self/statm", encoding="ascii") as statm:
                resident_pages = int(statm.read().split()[1])
            return resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, TypeError, ValueError):
        return 0
    return 0


def run_parallel_campaign_batch(
    conn: psycopg.Connection,
    *,
    campaign_id: int,
    workers: int,
    jobs_per_worker: int,
) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    campaign = get_campaign(conn, campaign_id)
    if campaign["status"] in {"paused", "canceled", "completed"}:
        raise ValueError(f"Campaign must be queued or running before parallel execution; current status is {campaign['status']}.")
    worker_count = max(1, min(campaign_worker_limit(), int(workers)))
    batch_size = max(1, min(100, int(jobs_per_worker)))
    previous_target = int(campaign.get("target_workers") or 0)
    config = {**scheduling_config(campaign), "batch_size": batch_size, "target_workers": worker_count}
    recover_expired_campaign_jobs(conn, campaign_id=campaign_id, retry_limit=int(config["retry_limit"]), recovery_worker_id=f"durable_coordinator_{campaign_id}")
    conn.execute(
        """
        UPDATE research_campaigns
        SET status = 'running',
            started_at = COALESCE(started_at, NOW()),
            target_workers = %s,
            execution_status = CASE WHEN %s > 0 THEN 'running' ELSE 'idle' END,
            execution_updated_at = NOW(),
            scheduling_config = %s,
            updated_at = NOW()
        WHERE id = %s AND status IN ('queued', 'running', 'failed')
        """,
        (worker_count, worker_count, Jsonb(jsonable(config)), campaign_id),
    )
    if worker_count < previous_target:
        conn.execute(
            """
            WITH ranked AS (
                SELECT worker_id,
                       ROW_NUMBER() OVER (ORDER BY registered_at ASC, worker_id ASC) AS ordinal
                FROM research_campaign_workers
                WHERE campaign_id = %s
                  AND status IN ('starting', 'running', 'draining')
                  AND COALESCE(last_heartbeat_at, heartbeat_at) >= %s
            )
            UPDATE research_campaign_workers worker
            SET status = 'draining',
                last_heartbeat_at = NOW(),
                heartbeat_at = NOW()
            FROM ranked
            WHERE worker.worker_id = ranked.worker_id
              AND ranked.ordinal > %s
            """,
            (campaign_id, datetime.now(UTC) - timedelta(seconds=worker_stale_seconds()), worker_count),
        )
    conn.commit()
    runtime = campaign_runtime_snapshot(conn, campaign_id)
    return {
        "campaign_id": campaign_id,
        "started": previous_target == 0 and worker_count > 0,
        "already_active": previous_target > 0,
        "scaled": worker_count != previous_target,
        "workers": worker_count,
        "jobs_per_worker": batch_size,
        "remaining": open_job_count(conn, campaign_id),
        "runtime": runtime,
        "simulation_only": True,
    }


def parallel_pool_snapshot(campaign_id: int) -> dict[str, Any]:
    with _PARALLEL_POOLS_LOCK:
        return dict(_PARALLEL_POOLS.get(campaign_id) or {})


def campaign_runtime_snapshot(
    conn: psycopg.Connection,
    campaign_id: int,
    *,
    campaign: dict[str, Any] | None = None,
) -> dict[str, Any]:
    campaign = campaign or get_campaign(conn, campaign_id)
    stale_cutoff = datetime.now(UTC) - timedelta(seconds=worker_stale_seconds())
    workers = conn.execute(
        """
        SELECT *
        FROM research_campaign_workers
        WHERE simulation_only = TRUE
          AND campaign_id = %s
          AND COALESCE(last_heartbeat_at, heartbeat_at) >= %s
          AND status NOT IN ('stopped', 'error')
        """,
        (campaign_id, stale_cutoff),
    ).fetchall()
    rows = [dict(row) for row in workers]
    live = [row for row in rows if row.get("status") in {"running", "idle", "starting", "draining"}]
    draining = [row for row in rows if row.get("status") == "draining"]
    target = min(int(campaign.get("target_workers") or 0), campaign_worker_limit())
    effective = sum(1 for row in live if row.get("status") in {"running", "starting"})
    return {
        "worker_limit": campaign_worker_limit(),
        "target_workers": target,
        "live_workers": len(live),
        "starting_workers": sum(1 for row in live if row.get("status") == "starting"),
        "draining_workers": len(draining),
        "effective_workers": min(effective, target),
        "heartbeat_seconds": worker_heartbeat_seconds(),
        "stale_after_seconds": worker_stale_seconds(),
    }


def _execute_parallel_campaign_worker(
    campaign_id: int,
    worker_index: int,
    pool_id: str,
    batch_size: int,
    assigned_datasets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Execute one real OS process worth of CPU-bound campaign simulations."""

    from app.db import connect

    started = time.perf_counter()
    totals = {"processed": 0, "completed": 0, "failed": 0}
    worker_id = f"parallel_{campaign_id}_{worker_index}_{pool_id.rsplit('_', 1)[-1]}"
    log_event("Worker started", worker_id=worker_id, campaign_id=campaign_id, worker_index=worker_index, assigned_datasets=len(assigned_datasets))
    allowed_dataset_keys = [f"{row['symbol']}|{row['timeframe']}" for row in assigned_datasets]
    worker_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
    with connect() as worker_conn:
        for dataset in assigned_datasets:
            symbol = str(dataset["symbol"])
            timeframe = str(dataset["timeframe"])
            enriched_context = bool(dataset.get("enriched_context"))
            dataset_id = int(dataset["dataset_id"]) if dataset.get("dataset_id") is not None else None
            worker_cache[(symbol, timeframe, enriched_context, dataset_id)] = load_campaign_dataset(
                worker_conn,
                symbol,
                timeframe,
                enriched_context,
                dataset_id=dataset_id,
            )
        worker_conn.commit()
        while True:
            result = run_research_campaign_batch(
                worker_conn,
                campaign_id=campaign_id,
                batch_size=batch_size,
                worker_id=worker_id,
                ensure_tables=False,
                coordinate_campaign=False,
                dataset_cache=worker_cache,
                allowed_dataset_keys=allowed_dataset_keys,
            )
            for key in totals:
                totals[key] += int(result.get(key) or 0)
            if result.get("status") in {"paused", "canceled", "completed"} or int(result.get("processed") or 0) == 0:
                break
    log_event("Worker stopped", worker_id=worker_id, campaign_id=campaign_id, processed=totals["processed"], completed=totals["completed"], failed=totals["failed"], elapsed_ms=elapsed_ms(started))
    return {**totals, "preloaded_datasets": len(worker_cache)}


def _run_persistent_parallel_pool(*, pool_id: str, campaign_id: int, worker_count: int, batch_size: int) -> None:
    from app.db import connect

    started = time.perf_counter()
    log_event("Task created", task="persistent_parallel_pool", campaign_id=campaign_id, pool_id=pool_id, workers=worker_count, jobs_per_worker=batch_size)
    with connect() as assignment_conn:
        dataset_rows = assignment_conn.execute(
            """
            SELECT
                symbol,
                timeframe,
                dataset_id,
                BOOL_OR(
                    COALESCE(candidate->'parameters'->>'phase_9_11_campaign_version', '') = %s
                    OR COALESCE(candidate->'parameters'->>'phase_9_12_campaign_version', '') = %s
                ) AS enriched_context
            FROM research_campaign_jobs
            WHERE campaign_id = %s
            GROUP BY symbol, timeframe, dataset_id
            ORDER BY symbol, timeframe, dataset_id
            """,
            (STRATEGY_REDESIGN_CAMPAIGN_VERSION, VOLATILITY_ADAPTIVE_RELATIVE_STRENGTH_CAMPAIGN_VERSION, campaign_id),
        ).fetchall()
    datasets = [dict(row) for row in dataset_rows]
    worker_datasets = [datasets[index::worker_count] for index in range(worker_count)]
    if datasets:
        for index, assigned in enumerate(worker_datasets):
            if not assigned:
                worker_datasets[index] = [datasets[index % len(datasets)]]

    worker_errors: list[str] = []
    try:
        with _PARALLEL_POOLS_LOCK:
            state = _PARALLEL_POOLS.get(campaign_id)
            if state and state.get("pool_id") == pool_id:
                state["live_workers"] = worker_count
                state["starting_workers"] = 0
                state["status"] = "running"
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(
                    _execute_parallel_campaign_worker,
                    campaign_id,
                    index,
                    pool_id,
                    batch_size,
                    worker_datasets[index],
                )
                for index in range(worker_count)
            ]
            for future in as_completed(futures):
                try:
                    totals = future.result()
                    log_event("Task completed", task="parallel_worker", campaign_id=campaign_id, pool_id=pool_id, processed=totals.get("processed"), completed=totals.get("completed"), failed=totals.get("failed"))
                    with _PARALLEL_POOLS_LOCK:
                        state = _PARALLEL_POOLS.get(campaign_id)
                        if state and state.get("pool_id") == pool_id:
                            state["processed_jobs"] = int(state.get("processed_jobs") or 0) + int(totals.get("processed") or 0)
                            state["preloaded_datasets"] = int(state.get("preloaded_datasets") or 0) + int(totals.get("preloaded_datasets") or 0)
                            state["live_workers"] = max(0, int(state.get("live_workers") or 0) - 1)
                except Exception as error:
                    log_exception("Worker exception", error, campaign_id=campaign_id, pool_id=pool_id)
                    worker_errors.append(str(error))
                    with _PARALLEL_POOLS_LOCK:
                        state = _PARALLEL_POOLS.get(campaign_id)
                        if state and state.get("pool_id") == pool_id:
                            state["live_workers"] = max(0, int(state.get("live_workers") or 0) - 1)

        with connect() as coordinator_conn:
            remaining = open_job_count(coordinator_conn, campaign_id)
            if remaining == 0:
                finalize_research_campaign(coordinator_conn, campaign_id)
            else:
                update_campaign_counts(coordinator_conn, campaign_id)
            refresh_campaign_analytics(coordinator_conn, campaign_id)
            coordinator_conn.commit()
    finally:
        log_event("Task completed", task="persistent_parallel_pool", campaign_id=campaign_id, pool_id=pool_id, worker_errors=len(worker_errors), elapsed_ms=elapsed_ms(started))
        with _PARALLEL_POOLS_LOCK:
            state = _PARALLEL_POOLS.get(campaign_id)
            if state and state.get("pool_id") == pool_id:
                state["live_workers"] = 0
                if worker_errors:
                    state["worker_errors"] = worker_errors
                _PARALLEL_POOLS.pop(campaign_id, None)


def _run_parallel_campaign_batch(
    conn: psycopg.Connection,
    *,
    campaign_id: int,
    workers: int,
    jobs_per_worker: int,
) -> dict[str, Any]:
    """Run one bounded parallel batch for internal and diagnostic callers."""
    ensure_campaign_tables(conn)
    campaign = get_campaign(conn, campaign_id)
    if campaign["status"] in {"paused", "canceled", "completed"}:
        raise ValueError(f"Campaign must be queued or running before parallel execution; current status is {campaign['status']}.")
    worker_count = max(1, min(8, int(workers)))
    batch_size = max(1, min(100, int(jobs_per_worker)))
    config = scheduling_config(campaign)
    recover_expired_campaign_jobs(conn, campaign_id=campaign_id, retry_limit=int(config["retry_limit"]), recovery_worker_id=f"parallel_coordinator_{campaign_id}")
    conn.execute(
        """
        UPDATE research_campaigns
        SET status = 'running', started_at = COALESCE(started_at, NOW()), updated_at = NOW()
        WHERE id = %s AND status IN ('queued', 'running', 'failed')
        """,
        (campaign_id,),
    )
    conn.commit()

    from app.db import connect

    def execute_bounded(worker_index: int) -> dict[str, Any]:
        with connect() as worker_conn:
            return run_research_campaign_batch(
                worker_conn,
                campaign_id=campaign_id,
                batch_size=batch_size,
                worker_id=f"parallel_{campaign_id}_{worker_index}_{int(time.time() * 1000)}",
                ensure_tables=False,
                coordinate_campaign=False,
            )

    results = []
    worker_errors = []
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix=f"campaign-{campaign_id}") as executor:
        futures = [executor.submit(execute_bounded, index) for index in range(worker_count)]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as error:
                worker_errors.append(str(error))
    remaining = open_job_count(conn, campaign_id)
    analytics = refresh_campaign_analytics(conn, campaign_id)
    if remaining == 0:
        finalize_research_campaign(conn, campaign_id)
        analytics = refresh_campaign_analytics(conn, campaign_id)
    else:
        update_campaign_counts(conn, campaign_id)
    conn.commit()
    return {
        "campaign_id": campaign_id,
        "workers": worker_count,
        "jobs_per_worker": batch_size,
        "processed": sum(int(result.get("processed") or 0) for result in results),
        "completed": sum(int(result.get("completed") or 0) for result in results),
        "failed": sum(int(result.get("failed") or 0) for result in results),
        "remaining": remaining,
        "analytics": analytics,
        "worker_errors": worker_errors,
        "worker_results": results,
        "simulation_only": True,
    }


def delete_research_campaign(conn: psycopg.Connection, campaign_id: int, *, force: bool = False) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    campaign = get_campaign(conn, campaign_id)
    if campaign["status"] == "running":
        raise ValueError("Pause the campaign before deleting it.")
    if campaign["status"] == "completed" and not force:
        raise ValueError("Completed campaign evidence cannot be deleted.")

    counts = conn.execute(
        """
        SELECT
            COUNT(*) AS job_count,
            COUNT(*) FILTER (WHERE status IN ('completed', 'promoted', 'rejected')) AS evidence_job_count
        FROM research_campaign_jobs
        WHERE campaign_id = %s
        """,
        (campaign_id,),
    ).fetchone()
    elite_count = conn.execute(
        "SELECT COUNT(*) AS count FROM elite_research_candidates WHERE campaign_id = %s",
        (campaign_id,),
    ).fetchone()
    if not force and (int((counts or {}).get("evidence_job_count") or 0) > 0 or int((elite_count or {}).get("count") or 0) > 0):
        raise ValueError("Campaigns with completed or promoted evidence cannot be deleted.")

    deleted = conn.execute(
        "DELETE FROM research_campaigns WHERE id = %s AND simulation_only = TRUE RETURNING id, name",
        (campaign_id,),
    ).fetchone()
    if not deleted:
        raise ValueError("Research campaign was not found.")
    conn.commit()
    return {
        "deleted": True,
        "campaign_id": int(deleted["id"]),
        "name": str(deleted["name"]),
        "deleted_jobs": int((counts or {}).get("job_count") or 0),
        "deleted_evidence_jobs": int((counts or {}).get("evidence_job_count") or 0),
        "forced": force,
        "simulation_only": True,
    }


def campaign_status(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    campaign = get_campaign(conn, campaign_id)
    analytics = campaign_progress_analytics(conn, campaign_id, campaign.get("analytics") or {})
    jobs = conn.execute(
        """
        SELECT id, candidate_id, symbol, timeframe, status, validation_score, consistency_score, failure_reasons, latest_error, updated_at
        FROM research_campaign_jobs
        WHERE campaign_id = %s
        ORDER BY updated_at DESC, id DESC
        LIMIT 50
        """,
        (campaign_id,),
    ).fetchall()
    candidates = conn.execute(
        """
        SELECT *
        FROM elite_research_candidates
        WHERE campaign_id = %s AND simulation_only = TRUE
        ORDER BY research_score DESC, created_at DESC
        LIMIT 50
        """,
        (campaign_id,),
    ).fetchall()
    conn.commit()
    candidate_rows = [jsonable(dict(row)) for row in candidates]
    elite = [row for row in candidate_rows if row.get("forward_validation_state") == "forward_validation_passed"]
    awaiting_forward = [row for row in candidate_rows if row.get("forward_validation_state") != "forward_validation_passed"]
    return {
        "campaign": jsonable(campaign),
        "analytics": analytics,
        "recent_jobs": [jsonable(dict(row)) for row in jobs],
        "forward_validation_candidates": awaiting_forward,
        "elite_candidates": elite,
        "simulation_only": True,
    }


def list_campaign_jobs(conn: psycopg.Connection, campaign_id: int, *, status: str | None = None, limit: int = 100) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    if status:
        rows = conn.execute(
            """
            SELECT *
            FROM research_campaign_jobs
            WHERE campaign_id = %s AND status = %s
            ORDER BY updated_at DESC, id DESC
            LIMIT %s
            """,
            (campaign_id, status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT *
            FROM research_campaign_jobs
            WHERE campaign_id = %s
            ORDER BY updated_at DESC, id DESC
            LIMIT %s
            """,
            (campaign_id, limit),
        ).fetchall()
    return {"campaign_id": campaign_id, "jobs": [jsonable(dict(row)) for row in rows], "simulation_only": True}


def retry_campaign_job(conn: psycopg.Connection, campaign_id: int, job_id: int) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    row = conn.execute(
        """
        UPDATE research_campaign_jobs
        SET status = 'queued',
            worker_id = NULL,
            claimed_at = NULL,
            lease_expires_at = NULL,
            heartbeat_at = NULL,
            failure_classification = NULL,
            deferred_until = NULL,
            blocked_reason = NULL,
            latest_error = NULL,
            updated_at = NOW()
        WHERE campaign_id = %s
          AND id = %s
          AND status IN ('failed', 'blocked_data', 'deferred_rate_limit', 'retrying')
        RETURNING *
        """,
        (campaign_id, job_id),
    ).fetchone()
    if not row:
        raise ValueError("campaign job is not retryable or was not found")
    conn.commit()
    return {"job": jsonable(dict(row)), "simulation_only": True}


def blocked_campaign_jobs(conn: psycopg.Connection, campaign_id: int, *, limit: int = 100) -> dict[str, Any]:
    return list_campaign_jobs(conn, campaign_id, status="blocked_data", limit=limit)


def elite_candidate_forward_details(conn: psycopg.Connection, candidate_id: str) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    elite = conn.execute(
        """
        SELECT *
        FROM elite_research_candidates
        WHERE candidate_id = %s AND simulation_only = TRUE
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (candidate_id,),
    ).fetchone()
    if not elite:
        raise ValueError("elite candidate not found")
    rollups = conn.execute(
        """
        SELECT *
        FROM elite_candidate_paper_rollups
        WHERE candidate_id = %s AND simulation_only = TRUE
        ORDER BY calculated_at DESC
        LIMIT 30
        """,
        (candidate_id,),
    ).fetchall()
    drift = conn.execute(
        """
        SELECT *
        FROM elite_candidate_evidence_drift
        WHERE candidate_id = %s AND simulation_only = TRUE
        ORDER BY detected_at DESC
        LIMIT 100
        """,
        (candidate_id,),
    ).fetchall()
    return {"elite_candidate": jsonable(dict(elite)), "paper_rollups": [jsonable(dict(row)) for row in rollups], "evidence_drift": [jsonable(dict(row)) for row in drift], "simulation_only": True}


def campaign_control(conn: psycopg.Connection, *, campaign_id: int, action: str) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    action = action.lower()
    if action == "pause":
        conn.execute("UPDATE research_campaigns SET status = 'paused', updated_at = NOW() WHERE id = %s AND status IN ('queued', 'running', 'failed')", (campaign_id,))
    elif action == "resume":
        conn.execute("UPDATE research_campaigns SET status = 'queued', updated_at = NOW() WHERE id = %s AND status = 'paused'", (campaign_id,))
    elif action == "cancel":
        conn.execute("UPDATE research_campaigns SET status = 'canceled', canceled_at = NOW(), updated_at = NOW() WHERE id = %s AND status <> 'completed'", (campaign_id,))
        conn.execute("UPDATE research_campaign_jobs SET status = 'canceled', updated_at = NOW() WHERE campaign_id = %s AND status IN ('queued', 'running')", (campaign_id,))
    elif action == "restart":
        conn.execute("UPDATE research_campaigns SET status = 'queued', started_at = NULL, completed_at = NULL, canceled_at = NULL, updated_at = NOW() WHERE id = %s", (campaign_id,))
        conn.execute(
            """
            UPDATE research_campaign_jobs
            SET status = 'queued', result = '{}'::jsonb, validation_score = 0, consistency_score = 0,
                failure_reasons = '[]'::jsonb, latest_error = NULL, started_at = NULL, completed_at = NULL, updated_at = NOW()
            WHERE campaign_id = %s
            """,
            (campaign_id,),
        )
    else:
        raise ValueError("unsupported campaign control action")
    update_campaign_counts(conn, campaign_id)
    conn.commit()
    return campaign_status(conn, campaign_id)


def campaign_mission_control_summary(conn: psycopg.Connection) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    scheduler = dict(conn.execute("SELECT * FROM research_campaign_scheduler WHERE id = TRUE").fetchone() or {})
    campaigns = conn.execute(
        """
        SELECT id, name, universe_key, status, queued_jobs, completed_jobs, failed_jobs,
               promoted_candidates, rejected_candidates, analytics, started_at, completed_at, updated_at
        FROM research_campaigns
        WHERE simulation_only = TRUE
        ORDER BY created_at DESC
        LIMIT 12
        """
    ).fetchall()
    rows = [dict(row) for row in campaigns]
    latest_completed_campaign = latest_completed_campaign_summary(conn, rows)
    active = [row for row in rows if row["status"] in {"queued", "running", "paused"}]
    completed = [row for row in rows if row["status"] == "completed"]
    completed_campaign_summaries = [summary for summary in (latest_completed_campaign_summary(conn, [row]) for row in completed) if summary]
    queued_jobs = sum(int(row.get("queued_jobs") or 0) - int(row.get("completed_jobs") or 0) - int(row.get("failed_jobs") or 0) for row in active)
    workers = worker_status(conn)
    ops = campaign_worker_observability(conn)
    return {
        "scheduler_enabled": bool(scheduler.get("enabled")),
        "last_scheduler_cycle": scheduler.get("last_cycle_at"),
        "next_eligible_cycle": scheduler.get("next_cycle_at"),
        "active_campaigns": len(active),
        "completed_campaigns": len(completed),
        "queued_campaigns": sum(1 for row in rows if row["status"] == "queued"),
        "queued_jobs": max(queued_jobs, 0),
        "worker_health": "Warning" if workers["stale_worker_count"] or ops["failed_jobs"] else "Healthy",
        "active_worker_count": workers["active_worker_count"],
        "healthy_worker_count": workers["healthy_worker_count"],
        "stale_worker_count": workers["stale_worker_count"],
        "claimed_jobs": ops["claimed_jobs"],
        "running_jobs": ops["running_jobs"],
        "completed_jobs": ops["completed_jobs"],
        "rejected_jobs": ops["rejected_jobs"],
        "completed_or_rejected_jobs": ops["completed_or_rejected_jobs"],
        "retrying_jobs": ops["retrying_jobs"],
        "deferred_jobs": ops["deferred_jobs"],
        "blocked_data_jobs": ops["blocked_data_jobs"],
        "failed_jobs": ops["failed_jobs"],
        "genuine_failed_jobs": ops["genuine_failed_jobs"],
        "recovered_stale_leases": ops["recovered_stale_leases"],
        "queue_depth": ops["queue_depth"],
        "status_counts": ops["status_counts"],
        "count_reconciliation": ops["count_reconciliation"],
        "oldest_queued_job_age_hours": ops["oldest_queued_job_age_hours"],
        "average_job_runtime_ms": ops["average_job_runtime_ms"],
        "jobs_completed_last_24h": ops["jobs_completed_last_24h"],
        "campaigns_completed_last_24h": ops["campaigns_completed_last_24h"],
        "worker_error_summary": ops["worker_error_summary"],
        "worker_utilization": worker_utilization(workers),
        "queue_throughput": ops["jobs_completed_last_24h"],
        "batch_progress": first_analytics(rows, "batch_progress", []),
        "campaign_eta": campaign_eta(rows, ops),
        "campaign_efficiency": first_analytics(rows, "campaign_efficiency", 0),
        "strategy_family_summaries": first_analytics(rows, "strategy_family_intelligence", []),
        "asset_summaries": first_analytics(rows, "asset_intelligence", []),
        "timeframe_summaries": first_analytics(rows, "timeframe_intelligence", []),
        "research_heatmaps": first_analytics(rows, "heatmaps", {}),
        "recent_promotions": first_analytics(rows, "recent_promotions", []),
        "recent_forward_validation_failures": first_analytics(rows, "recent_forward_validation_failures", []),
        "latest_completed_campaign": latest_completed_campaign,
        "completed_campaign_summaries": completed_campaign_summaries,
        "current_experiment": current_campaign_label(active),
        "generated_candidates": sum(int(((row.get("analytics") or {}).get("strategies_generated") or 0)) for row in rows),
        "promoted_candidates": sum(int(row.get("promoted_candidates") or 0) for row in rows),
        "rejection_rate": rejection_rate(rows),
        "workers": workers["workers"],
        "campaigns": [jsonable(row) for row in rows],
        "simulation_only": True,
    }


def campaign_mission_control_operations(conn: psycopg.Connection) -> dict[str, Any]:
    """Return bounded operational counters without loading campaign analytics JSON."""
    scheduler = dict(conn.execute("SELECT * FROM research_campaign_scheduler WHERE id = TRUE").fetchone() or {})
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT id, name, status, queued_jobs, completed_jobs, failed_jobs,
                   promoted_candidates, rejected_candidates, started_at, completed_at, updated_at
            FROM research_campaigns
            WHERE simulation_only = TRUE
            ORDER BY created_at DESC
            LIMIT 12
            """
        ).fetchall()
    ]
    active = [row for row in rows if row.get("status") in {"queued", "running", "paused"}]
    completed = [row for row in rows if row.get("status") == "completed"]
    queued_jobs = sum(
        int(row.get("queued_jobs") or 0)
        - int(row.get("completed_jobs") or 0)
        - int(row.get("failed_jobs") or 0)
        for row in active
    )
    ops = campaign_worker_observability(conn)
    worker_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT worker_id, status, heartbeat_at, last_heartbeat_at
            FROM research_campaign_workers
            WHERE simulation_only = TRUE
            ORDER BY heartbeat_at DESC
            """
        ).fetchall()
    ]
    now = datetime.now(UTC)
    stale_after = timedelta(seconds=worker_stale_seconds())
    healthy_count = sum(
        1
        for row in worker_rows
        if row.get("status") not in {"stopped", "error"}
        and (heartbeat := parse_timestamp(row.get("last_heartbeat_at") or row.get("heartbeat_at"))) is not None
        and now - heartbeat <= stale_after
    )
    promoted = sum(int(row.get("promoted_candidates") or 0) for row in rows)
    rejected = sum(int(row.get("rejected_candidates") or 0) for row in rows)
    candidate_outcomes = promoted + rejected
    return {
        "scheduler_enabled": bool(scheduler.get("enabled")),
        "last_scheduler_cycle": scheduler.get("last_cycle_at"),
        "next_eligible_cycle": scheduler.get("next_cycle_at"),
        "active_campaigns": len(active),
        "completed_campaigns": len(completed),
        "queued_campaigns": sum(1 for row in rows if row.get("status") == "queued"),
        "queued_jobs": max(queued_jobs, 0),
        "active_worker_count": healthy_count,
        "healthy_worker_count": healthy_count,
        "stale_worker_count": len(worker_rows) - healthy_count,
        "worker_utilization": round(healthy_count / max(1, len(worker_rows)), 4),
        "claimed_jobs": ops["claimed_jobs"],
        "running_jobs": ops["running_jobs"],
        "completed_jobs": ops["completed_jobs"],
        "rejected_jobs": ops["rejected_jobs"],
        "completed_or_rejected_jobs": ops["completed_or_rejected_jobs"],
        "retrying_jobs": ops["retrying_jobs"],
        "deferred_jobs": ops["deferred_jobs"],
        "blocked_data_jobs": ops["blocked_data_jobs"],
        "failed_jobs": ops["failed_jobs"],
        "genuine_failed_jobs": ops["genuine_failed_jobs"],
        "recovered_stale_leases": ops["recovered_stale_leases"],
        "queue_depth": ops["queue_depth"],
        "count_reconciliation": ops["count_reconciliation"],
        "oldest_queued_job_age_hours": ops["oldest_queued_job_age_hours"],
        "average_job_runtime_ms": ops["average_job_runtime_ms"],
        "jobs_completed_last_24h": ops["jobs_completed_last_24h"],
        "queue_throughput": ops["jobs_completed_last_24h"],
        "campaign_eta": campaign_eta(rows, ops),
        "current_experiment": current_campaign_label(active),
        "promoted_candidates": promoted,
        "rejection_rate": round(rejected / candidate_outcomes, 4) if candidate_outcomes else 0.0,
        "simulation_only": True,
    }


def latest_completed_campaign_summary(conn: psycopg.Connection, rows: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    completed = [row for row in (rows or []) if row.get("status") == "completed"]
    if completed:
        campaign = sorted(completed, key=lambda row: (row.get("completed_at") or row.get("updated_at") or datetime.min.replace(tzinfo=UTC), int(row.get("id") or 0)), reverse=True)[0]
    else:
        row = conn.execute(
            """
            SELECT id, name, universe_key, status, requested_candidates, queued_jobs, completed_jobs, failed_jobs,
                   promoted_candidates, rejected_candidates, analytics, controls, started_at, completed_at, updated_at
            FROM research_campaigns
            WHERE simulation_only = TRUE AND status = 'completed'
            ORDER BY completed_at DESC NULLS LAST, updated_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        campaign = dict(row)
    campaign_id = int(campaign["id"])
    jobs = [dict(row) for row in conn.execute("SELECT * FROM research_campaign_jobs WHERE campaign_id = %s ORDER BY candidate_id, symbol, timeframe", (campaign_id,)).fetchall()]
    summaries = candidate_consistency_summaries(jobs)
    lifecycle_rows = [candidate_lifecycle_summary(row) for row in summaries]
    lifecycle_counts = Counter(row["lifecycle"] for row in lifecycle_rows)
    elite_count = int((conn.execute("SELECT COUNT(1) AS count FROM elite_research_candidates WHERE campaign_id = %s AND simulation_only = TRUE AND forward_validation_state = 'forward_validation_passed'", (campaign_id,)).fetchone() or {}).get("count") or 0)
    deployment_count = int((conn.execute("SELECT COUNT(1) AS count FROM strategy_deployments WHERE campaign_id = %s AND simulation_only = TRUE", (campaign_id,)).fetchone() or {}).get("count") or 0)
    failure_counter: Counter[str] = Counter()
    for job in jobs:
        if job.get("status") != "rejected":
            continue
        reasons = list(job.get("failure_reasons") or [])
        if not reasons:
            reasons = infer_job_failure_reasons(job)
        for reason in reasons:
            failure_counter[normalize_failure_reason(reason)] += 1
    terminal_jobs = [job for job in jobs if job.get("status") in {"promoted", "rejected", "failed"}]
    job_status_counts = Counter(str(job.get("status")) for job in jobs)
    return {
        "id": campaign_id,
        "name": campaign.get("name"),
        "campaign_key": campaign.get("campaign_key"),
        "universe_key": campaign.get("universe_key"),
        "status": campaign.get("status"),
        "requested_candidates": int(campaign.get("requested_candidates") or len(summaries)),
        "generated_candidates": len(summaries),
        "tested_candidates": len({job["candidate_id"] for job in terminal_jobs}),
        "candidate_lifecycle_counts": {
            "rejected": lifecycle_counts.get("rejected", 0),
            "needs_more_evidence": lifecycle_counts.get("needs_more_evidence", 0),
            "research_candidate": lifecycle_counts.get("research_candidate", 0),
            "forward_validation_candidate": max(0, lifecycle_counts.get("elite_candidate", 0) - elite_count),
            "elite_candidate": elite_count,
        },
        "cross_validation_rejected_candidates": int(campaign.get("rejected_candidates") or 0),
        "queued_jobs": int(campaign.get("queued_jobs") or len(jobs)),
        "jobs_executed": len(terminal_jobs),
        "jobs_rejected_by_evidence": job_status_counts.get("rejected", 0),
        "operationally_failed_jobs": job_status_counts.get("failed", 0),
        "promoted_single_market_jobs": job_status_counts.get("promoted", 0),
        "job_status_counts": dict(job_status_counts),
        "best_candidate": best_simple_candidate(lifecycle_rows),
        "top_failure_reasons": [{"reason": reason, "count": count} for reason, count in failure_counter.most_common(10)],
        "elite_candidates": elite_count,
        "candidate_linked_deployments": deployment_count,
        "started_at": campaign.get("started_at"),
        "completed_at": campaign.get("completed_at"),
        "updated_at": campaign.get("updated_at"),
        "simulation_only": True,
    }


def candidate_lifecycle_summary(summary: dict[str, Any]) -> dict[str, Any]:
    if passes_cross_validation(summary):
        lifecycle = "elite_candidate"
    elif summary.get("passed_markets"):
        lifecycle = "research_candidate"
    elif profit_factor_passes(summary, 1.0) and finite_metric(summary.get("trade_count")) > 0:
        lifecycle = "needs_more_evidence"
    else:
        lifecycle = "rejected"
    return {
        "candidate_id": summary["candidate_id"],
        "lifecycle": lifecycle,
        "research_score": summary.get("research_score"),
        "profit_factor": summary.get("profit_factor"),
        "profit_factor_is_infinite": bool(summary.get("profit_factor_is_infinite")),
        "expectancy": summary.get("expectancy"),
        "max_drawdown": summary.get("max_drawdown"),
        "trade_count": summary.get("trade_count"),
        "stability": summary.get("stability"),
        "assets_passed": summary.get("assets_passed"),
        "timeframes_passed": summary.get("timeframes_passed"),
        "passed_markets": summary.get("passed_markets") or [],
        "failure_reasons": summary.get("failure_reasons") or [],
        "strategy_family": strategy_family_from_history(summary),
    }


def best_simple_candidate(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    ranked = sorted(
        rows,
        key=lambda row: (
            row["lifecycle"] == "elite_candidate",
            row["lifecycle"] == "research_candidate",
            finite_metric(row.get("stability")),
            finite_metric(row.get("research_score")),
            validation_profit_factor(row),
        ),
        reverse=True,
    )
    best = dict(ranked[0])
    first_pass = (best.get("passed_markets") or [{}])[0]
    best["symbol"] = first_pass.get("symbol")
    best["timeframe"] = first_pass.get("timeframe")
    return best


def strategy_family_from_history(summary: dict[str, Any]) -> str | None:
    for row in summary.get("validation_history") or []:
        blocks = row.get("blocks") or {}
        entry = blocks.get("entry") or ((row.get("parameters") or {}).get("entry"))
        if entry == "pullback":
            return "Pullback"
        if entry in {"trend_continuation", "gap_proxy"}:
            return "Trend Following"
        if entry in {"breakout", "opening_range_proxy"}:
            return "Breakout"
    return None


def first_analytics(rows: list[dict[str, Any]], key: str, default: Any) -> Any:
    for row in rows:
        analytics = row.get("analytics") or {}
        if analytics.get(key):
            return analytics[key]
    return default


def worker_utilization(workers: dict[str, Any]) -> float:
    total = max(1, len(workers.get("workers") or []))
    return round(workers.get("healthy_worker_count", 0) / total, 4)


def campaign_eta(rows: list[dict[str, Any]], ops: dict[str, Any]) -> str | None:
    throughput = ops.get("jobs_completed_last_24h") or 0
    queue = ops.get("queue_depth") or 0
    if throughput <= 0 or queue <= 0:
        return None
    return f"{round(queue / throughput, 2)} day(s) at current 24h throughput"


def campaign_worker_observability(conn: psycopg.Connection) -> dict[str, Any]:
    status_rows = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM research_campaign_jobs
        GROUP BY status
        """
    ).fetchall()
    counts = {row["status"]: int(row["count"]) for row in status_rows}
    total_jobs = sum(counts.values())
    terminal_completed = counts.get("completed", 0)
    terminal_rejected = counts.get("rejected", 0)
    terminal_promoted = counts.get("promoted", 0)
    recovered_stale_leases = count_rows(conn, "research_campaign_jobs", "recovery_classification = 'recovered_stale_lease'")
    genuine_failures = count_rows(
        conn,
        "research_campaign_jobs",
        "status = 'failed' AND COALESCE(recovery_classification, '') <> 'recovered_stale_lease'",
    )
    oldest = conn.execute(
        """
        SELECT MIN(created_at) AS oldest
        FROM research_campaign_jobs
        WHERE status IN ('queued', 'retrying', 'blocked_data', 'deferred_rate_limit')
        """
    ).fetchone()
    runtime = conn.execute(
        """
        SELECT AVG(execution_runtime_ms) AS average_runtime
        FROM research_campaign_jobs
        WHERE execution_runtime_ms IS NOT NULL
        """
    ).fetchone()
    completed_24h = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM research_campaign_jobs
        WHERE completed_at >= NOW() - INTERVAL '24 hours'
        """
    ).fetchone()
    campaigns_24h = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM research_campaigns
        WHERE completed_at >= NOW() - INTERVAL '24 hours'
        """
    ).fetchone()
    errors = conn.execute(
        """
        SELECT COALESCE(failure_classification, 'unknown_error') AS classification, COUNT(*) AS count
        FROM research_campaign_jobs
        WHERE status IN ('failed', 'blocked_data', 'deferred_rate_limit', 'retrying')
        GROUP BY COALESCE(failure_classification, 'unknown_error')
        ORDER BY count DESC
        LIMIT 10
        """
    ).fetchall()
    oldest_time = parse_timestamp((oldest or {}).get("oldest"))
    return {
        "claimed_jobs": sum(counts.get(status, 0) for status in ("running",)),
        "running_jobs": counts.get("running", 0),
        "completed_jobs": terminal_completed + terminal_promoted,
        "rejected_jobs": terminal_rejected,
        "completed_or_rejected_jobs": terminal_completed + terminal_promoted + terminal_rejected,
        "retrying_jobs": counts.get("retrying", 0),
        "deferred_jobs": counts.get("deferred_rate_limit", 0),
        "blocked_data_jobs": counts.get("blocked_data", 0),
        "failed_jobs": counts.get("failed", 0),
        "genuine_failed_jobs": genuine_failures,
        "recovered_stale_leases": recovered_stale_leases,
        "queue_depth": sum(counts.get(status, 0) for status in ("queued", "retrying", "blocked_data", "deferred_rate_limit")),
        "status_counts": counts,
        "count_reconciliation": campaign_count_reconciliation(counts, total_jobs),
        "oldest_queued_job_age_hours": round((datetime.now(UTC) - oldest_time).total_seconds() / 3600, 2) if oldest_time else 0,
        "average_job_runtime_ms": round(finite_metric((runtime or {}).get("average_runtime")), 2),
        "jobs_completed_last_24h": int((completed_24h or {}).get("count") or 0),
        "campaigns_completed_last_24h": int((campaigns_24h or {}).get("count") or 0),
        "worker_error_summary": [{"classification": row["classification"], "count": int(row["count"])} for row in errors],
    }


def campaign_count_reconciliation(counts: dict[str, int], total_jobs: int) -> dict[str, Any]:
    components = {
        "queued": counts.get("queued", 0),
        "claimed": 0,
        "running": counts.get("running", 0),
        "completed": counts.get("completed", 0) + counts.get("promoted", 0),
        "rejected": counts.get("rejected", 0),
        "retrying": counts.get("retrying", 0),
        "deferred": counts.get("deferred_rate_limit", 0),
        "blocked_data": counts.get("blocked_data", 0),
        "failed": counts.get("failed", 0),
        "canceled": counts.get("canceled", 0),
    }
    component_total = sum(components.values())
    return {
        "total_jobs": total_jobs,
        "components": components,
        "component_total": component_total,
        "mismatch": total_jobs - component_total,
        "passed": total_jobs == component_total,
    }


def current_campaign_label(active: list[dict[str, Any]]) -> str | None:
    if not active:
        return None
    row = active[0]
    return f"{row['name']} ({row['status']})"


def rejection_rate(rows: list[dict[str, Any]]) -> float:
    rejected = sum(int(row.get("rejected_candidates") or 0) for row in rows)
    promoted = sum(int(row.get("promoted_candidates") or 0) for row in rows)
    total = rejected + promoted
    return round(rejected / total, 4) if total else 0.0


def research_campaign_key(
    universe_key: str,
    assets: list[str],
    timeframes: list[str],
    max_candidates: int,
    *,
    search_mode: str = "full",
    dataset_id: int | None = None,
) -> str:
    mode_suffix = "" if search_mode == "full" else f"|{search_mode}_v1"
    dataset_suffix = "" if dataset_id is None else f"|dataset:{dataset_id}|portfolio_evidence_v1"
    raw = f"{CAMPAIGN_VERSION}|{universe_key}|{','.join(assets)}|{','.join(timeframes)}|{max_candidates}{mode_suffix}{dataset_suffix}"
    return sha256(raw.encode("utf-8")).hexdigest()


def research_job_key(campaign_id: int, candidate_id: str, symbol: str, timeframe: str) -> str:
    raw = f"{CAMPAIGN_VERSION}|{campaign_id}|{candidate_id}|{symbol}|{timeframe}"
    return sha256(raw.encode("utf-8")).hexdigest()


def average(values: Any) -> float:
    parsed = [finite_metric(value) for value in values if value is not None]
    return round(sum(parsed) / len(parsed), 4) if parsed else 0.0


def count_rows(conn: psycopg.Connection, table: str, where: str = "TRUE") -> int:
    try:
        row = conn.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE {where}").fetchone()
        return int((row or {}).get("count") or 0)
    except Exception:
        safe_rollback(conn)
        return 0
