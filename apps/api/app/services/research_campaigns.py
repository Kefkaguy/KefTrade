from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import os
import socket
import time
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.services.evidence_alerts import create_evidence_alert
from app.services.features import load_candles
from app.services.regimes import load_regimes, sync_market_regimes
from app.services.research_learning import learn_from_completed_campaign
from app.services.strategy_discovery import (
    SAFETY_STATEMENT,
    DiscoveryCandidate,
    evaluate_candidate,
    generate_discovery_candidates,
    jsonable,
)
from app.services.strategy_research import build_context_by_time, finite_metric


CAMPAIGN_VERSION = "large_scale_research_campaign_v1"
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


def ensure_campaign_tables(conn: psycopg.Connection) -> None:
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
            ADD COLUMN IF NOT EXISTS daily_budget_date DATE
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
            CONSTRAINT research_campaign_jobs_status_check CHECK (status IN ('queued', 'running', 'completed', 'rejected', 'promoted', 'failed', 'canceled')),
            CONSTRAINT research_campaign_jobs_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """
    )
    conn.execute("ALTER TABLE research_campaign_jobs DROP CONSTRAINT IF EXISTS research_campaign_jobs_status_check")
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
            ADD COLUMN IF NOT EXISTS execution_runtime_ms INTEGER
        """
    )
    conn.execute(
        """
        ALTER TABLE research_campaign_jobs ADD CONSTRAINT research_campaign_jobs_status_check
        CHECK (status IN ('queued', 'running', 'completed', 'rejected', 'promoted', 'failed', 'canceled', 'blocked_data', 'deferred_rate_limit', 'retrying'))
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
            CONSTRAINT elite_candidate_evidence_drift_simulation_only_check CHECK (simulation_only = TRUE)
        )
        """
    )
    ensure_operations_tables(conn)


def ensure_operations_tables(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        ALTER TABLE research_campaign_jobs
            ADD COLUMN IF NOT EXISTS batch_id BIGINT,
            ADD COLUMN IF NOT EXISTS strategy_family TEXT,
            ADD COLUMN IF NOT EXISTS provider_latency_ms INTEGER,
            ADD COLUMN IF NOT EXISTS database_latency_ms INTEGER
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_campaign_workers (
            worker_id TEXT PRIMARY KEY,
            process_id TEXT,
            hostname TEXT,
            status TEXT NOT NULL DEFAULT 'starting',
            registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            stopped_at TIMESTAMPTZ,
            latest_cycle_at TIMESTAMPTZ,
            latest_error TEXT,
            processed_jobs INTEGER NOT NULL DEFAULT 0,
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT research_campaign_workers_simulation_only_check CHECK (simulation_only = TRUE)
        )
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


def seed_default_universes(conn: psycopg.Connection) -> None:
    ensure_campaign_tables(conn)
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


def create_research_campaign(
    conn: psycopg.Connection,
    *,
    universe_key: str,
    name: str | None = None,
    max_candidates: int = 1000,
    asset_limit: int = 100,
    timeframes: list[str] | None = None,
) -> dict[str, Any]:
    seed_default_universes(conn)
    universe = get_universe(conn, universe_key)
    assets = [str(asset).upper() for asset in (universe.get("assets") or [])][:asset_limit]
    selected_timeframes = list(timeframes or universe.get("default_timeframes") or DEFAULT_CAMPAIGN_TIMEFRAMES)
    candidates = generate_discovery_candidates(max_candidates=max_candidates)
    campaign_key = research_campaign_key(universe_key, assets, selected_timeframes, max_candidates)
    campaign_name = name or f"{universe['name']} strategy discovery campaign"
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
            universe_key,
            max_candidates,
            Jsonb({"asset_limit": asset_limit, "timeframes": selected_timeframes, "campaign_version": CAMPAIGN_VERSION}),
            Jsonb(DEFAULT_SCHEDULING_CONFIG),
            SAFETY_STATEMENT,
        ),
    ).fetchone()
    campaign_id = int(row["id"])
    created = queue_campaign_jobs(conn, campaign_id, candidates, assets, selected_timeframes)
    update_campaign_counts(conn, campaign_id)
    conn.commit()
    return {
        "campaign": jsonable(dict(row)),
        "assets": assets,
        "timeframes": selected_timeframes,
        "candidates_generated": len(candidates),
        "jobs_created": created,
        "campaign_version": CAMPAIGN_VERSION,
        "simulation_only": True,
        "safety": SAFETY_STATEMENT,
    }


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


def run_research_campaign_batch(conn: psycopg.Connection, *, campaign_id: int, batch_size: int = 50, worker_id: str | None = None) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    campaign = get_campaign(conn, campaign_id)
    if campaign["status"] in {"paused", "canceled", "completed"}:
        return {"campaign_id": campaign_id, "status": campaign["status"], "processed": 0, "simulation_only": True}
    config = scheduling_config(campaign)
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
    jobs = claim_campaign_jobs(
        conn,
        campaign_id=campaign_id,
        worker_id=worker_id,
        batch_size=min(batch_size, int(config["batch_size"])),
        lease_seconds=int(config["worker_lease_seconds"]),
        retry_limit=int(config["retry_limit"]),
    )
    completed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for job in jobs:
        try:
            readiness = data_readiness_for_job(conn, dict(job))
            if not readiness["ready"]:
                mark_claimed_job_deferred(conn, dict(job), readiness)
                failed.append({"job_id": job["id"], "candidate_id": job["candidate_id"], "symbol": job["symbol"], "timeframe": job["timeframe"], "deferred": readiness["status"], "reason": readiness["reason"]})
                continue
            refresh_job_heartbeat(conn, int(job["id"]), worker_id, int(config["worker_lease_seconds"]))
            result = run_campaign_job(conn, dict(job))
            status = "promoted" if passes_single_market_validation(result) else "rejected"
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
            completed.append({"job_id": job["id"], "candidate_id": job["candidate_id"], "symbol": job["symbol"], "timeframe": job["timeframe"], "status": status})
        except Exception as error:  # noqa: BLE001 - one failed asset/timeframe must not stop the campaign
            fail_or_retry_claimed_job(conn, dict(job), error, config)
            failed.append({"job_id": job["id"], "candidate_id": job["candidate_id"], "symbol": job["symbol"], "timeframe": job["timeframe"], "error": str(error)})
    analytics = refresh_campaign_analytics(conn, campaign_id)
    remaining = open_job_count(conn, campaign_id)
    if remaining == 0:
        finalize_research_campaign(conn, campaign_id)
        analytics = refresh_campaign_analytics(conn, campaign_id)
    else:
        update_campaign_counts(conn, campaign_id)
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
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH claimable AS (
            SELECT id
            FROM research_campaign_jobs
            WHERE campaign_id = %s
              AND simulation_only = TRUE
              AND attempts < %s
              AND (
                  status = 'queued'
                  OR (status IN ('retrying', 'blocked_data', 'deferred_rate_limit') AND (deferred_until IS NULL OR deferred_until <= NOW()))
                  OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= NOW())
              )
            ORDER BY id ASC
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
            latest_error = NULL,
            updated_at = NOW()
        FROM claimable
        WHERE j.id = claimable.id
        RETURNING j.*
        """,
        (campaign_id, retry_limit, batch_size, worker_id, lease_seconds),
    ).fetchall()
    return [dict(row) for row in rows]


def recover_expired_campaign_jobs(conn: psycopg.Connection, *, campaign_id: int, retry_limit: int) -> dict[str, Any]:
    retrying = conn.execute(
        """
        UPDATE research_campaign_jobs
        SET status = 'retrying',
            worker_id = NULL,
            lease_expires_at = NULL,
            failure_classification = 'worker_timeout',
            latest_error = 'Worker lease expired before completion.',
            deferred_until = NOW(),
            updated_at = NOW()
        WHERE campaign_id = %s
          AND status = 'running'
          AND lease_expires_at IS NOT NULL
          AND lease_expires_at <= NOW()
          AND attempts < %s
        RETURNING id
        """,
        (campaign_id, retry_limit),
    ).fetchall()
    failed = conn.execute(
        """
        UPDATE research_campaign_jobs
        SET status = 'failed',
            worker_id = NULL,
            lease_expires_at = NULL,
            failure_classification = 'worker_timeout',
            latest_error = 'Worker lease expired and retry limit was exceeded.',
            completed_at = NOW(),
            updated_at = NOW()
        WHERE campaign_id = %s
          AND status = 'running'
          AND lease_expires_at IS NOT NULL
          AND lease_expires_at <= NOW()
          AND attempts >= %s
        RETURNING id
        """,
        (campaign_id, retry_limit),
    ).fetchall()
    return {"retrying": len(retrying), "failed": len(failed)}


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


def run_campaign_job(conn: psycopg.Connection, job: dict[str, Any]) -> dict[str, Any]:
    symbol = job["symbol"]
    timeframe = job["timeframe"]
    sync_market_regimes(conn, symbol=symbol, timeframe=timeframe)
    candles = load_candles(conn, symbol, timeframe)
    features = list(
        conn.execute(
            """
            SELECT *
            FROM features
            WHERE symbol = %s AND timeframe = %s
            ORDER BY timestamp ASC
            """,
            (symbol, timeframe),
        ).fetchall()
    )
    regimes = load_regimes(conn, symbol=symbol, timeframe=timeframe)
    context_by_time = build_context_by_time(candles, features, regimes)
    candidate = candidate_from_payload(job["candidate"])
    row = evaluate_candidate(candidate, candles, features, context_by_time)
    return {**row, "symbol": symbol, "timeframe": timeframe, "campaign_version": CAMPAIGN_VERSION}


def data_readiness_for_job(conn: psycopg.Connection, job: dict[str, Any]) -> dict[str, Any]:
    symbol = str(job["symbol"]).upper()
    timeframe = str(job["timeframe"])
    symbol_row = conn.execute(
        "SELECT symbol, asset_class, is_active FROM symbols WHERE symbol = %s LIMIT 1",
        (symbol,),
    ).fetchone()
    if symbol_row and symbol_row.get("is_active") is False:
        return readiness_block("blocked_data", "data_unavailable", "Asset is inactive or unsupported.", retry_after_seconds=3600)
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
        return readiness_block("blocked_data", "data_unavailable", f"Only {candle_count} candles are available; {MIN_CAMPAIGN_CANDLES} are required.", retry_after_seconds=3600)
    freshness = data_freshness(latest, timeframe, (symbol_row or {}).get("asset_class"))
    if freshness["stale"]:
        return readiness_block("blocked_data", "stale_data", freshness["reason"], retry_after_seconds=1800)
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
        return readiness_block("blocked_data", "data_unavailable", f"Only {feature_count} feature rows are available; {MIN_CAMPAIGN_FEATURES} are required.", retry_after_seconds=1800)
    return {"ready": True, "status": "ready", "reason": "stored market data and features are ready", "failure_classification": None}


def readiness_block(status: str, classification: str, reason: str, *, retry_after_seconds: int) -> dict[str, Any]:
    return {"ready": False, "status": status, "reason": reason, "failure_classification": classification, "retry_after_seconds": retry_after_seconds}


def data_freshness(timestamp: Any, timeframe: str, asset_class: str | None) -> dict[str, Any]:
    parsed = parse_timestamp(timestamp)
    if parsed is None:
        return {"stale": True, "reason": "No completed candle timestamp is available."}
    max_age_hours = {"15m": 2, "30m": 4, "60m": 8, "1h": 8, "4h": 24, "1d": 96}.get(timeframe, 24)
    age_hours = (datetime.now(UTC) - parsed).total_seconds() / 3600
    if asset_class and "equity" in str(asset_class).lower() and parsed.weekday() == 4 and datetime.now(UTC).weekday() in {5, 6, 0}:
        max_age_hours = max(max_age_hours, 96)
    if age_hours > max_age_hours:
        return {"stale": True, "reason": f"Latest completed candle is {age_hours:.1f}h old; max allowed for {timeframe} is {max_age_hours}h."}
    return {"stale": False, "reason": "Latest completed candle is fresh."}


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
    if "stale" in message:
        return "stale_data"
    if "candle" in message or "feature" in message or "data" in message:
        return "data_unavailable"
    if "provider" in message or "rate" in message:
        return "provider_error"
    if "validation" in message:
        return "validation_error"
    if "strategy" in message:
        return "strategy_error"
    if "database" in message or "sql" in message:
        return "database_error"
    return "unknown_error"


def scheduling_config(campaign: dict[str, Any]) -> dict[str, Any]:
    config = {**DEFAULT_SCHEDULING_CONFIG, **dict(campaign.get("scheduling_config") or {})}
    controls = dict(campaign.get("controls") or {})
    if controls.get("batch_size"):
        config["batch_size"] = int(controls["batch_size"])
    return config


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
        INSERT INTO research_campaign_workers(worker_id, process_id, hostname, status, heartbeat_at, latest_cycle_at, simulation_only)
        VALUES (%s, %s, %s, %s, NOW(), NOW(), TRUE)
        ON CONFLICT(worker_id) DO UPDATE
        SET status = EXCLUDED.status,
            heartbeat_at = NOW(),
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
        SET status = %s, heartbeat_at = NOW(), latest_cycle_at = NOW()
        WHERE worker_id = %s
        """,
        (status, worker_id),
    )


def mark_campaign_worker_idle(conn: psycopg.Connection, worker_id: str, processed_jobs: int = 0) -> None:
    conn.execute(
        """
        UPDATE research_campaign_workers
        SET status = 'idle',
            heartbeat_at = NOW(),
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
        SET status = 'error', heartbeat_at = NOW(), latest_error = %s
        WHERE worker_id = %s
        """,
        (error, worker_id),
    )


def stop_campaign_worker(conn: psycopg.Connection, worker_id: str) -> dict[str, Any]:
    ensure_operations_tables(conn)
    row = conn.execute(
        """
        UPDATE research_campaign_workers
        SET status = 'stopped', stopped_at = NOW(), heartbeat_at = NOW()
        WHERE worker_id = %s
        RETURNING *
        """,
        (worker_id,),
    ).fetchone()
    conn.commit()
    return {"worker": jsonable(dict(row)) if row else None, "simulation_only": True}


def run_background_campaign_worker(
    conn_factory,
    *,
    worker_id: str | None = None,
    poll_seconds: float = 5.0,
    max_cycles: int | None = None,
    stop_file: str | None = None,
) -> dict[str, Any]:
    worker = worker_id or f"{WORKER_VERSION}_{socket.gethostname()}_{os.getpid()}"
    cycles = 0
    processed = 0
    with conn_factory() as conn:
        register_campaign_worker(conn, worker_id=worker, status="running")
        conn.commit()
    while max_cycles is None or cycles < max_cycles:
        if stop_file and os.path.exists(stop_file):
            break
        with conn_factory() as conn:
            heartbeat_campaign_worker(conn, worker)
            result = run_campaign_scheduler_cycle(conn, force=False, worker_id=worker)
            processed += int(result.get("processed") or 0)
            conn.commit()
        cycles += 1
        if max_cycles is None or cycles < max_cycles:
            time.sleep(poll_seconds)
    with conn_factory() as conn:
        stop_campaign_worker(conn, worker)
    return {"worker_id": worker, "cycles": cycles, "processed": processed, "simulation_only": True}


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
    now = datetime.now(UTC)
    workers = []
    seen = set()
    for registry in registry_rows:
        row = dict(registry)
        claimed = claimed_by_worker.get(row["worker_id"], {})
        heartbeat = parse_timestamp(row.get("heartbeat_at"))
        healthy = heartbeat is not None and (now - heartbeat) <= timedelta(minutes=5) and row.get("status") not in {"stopped", "error"}
        workers.append({**jsonable(row), "claimed_jobs": int(claimed.get("claimed_jobs") or 0), "health": "healthy" if healthy else "stale"})
        seen.add(row["worker_id"])
    for worker_id, claimed in claimed_by_worker.items():
        if worker_id in seen:
            continue
        lease = parse_timestamp(claimed.get("lease_expires_at"))
        healthy = lease is not None and lease > now
        workers.append({**jsonable(claimed), "status": "running", "health": "healthy" if healthy else "stale"})
    return {
        "active_worker_count": sum(1 for row in workers if row["health"] == "healthy"),
        "healthy_worker_count": sum(1 for row in workers if row["health"] == "healthy"),
        "stale_worker_count": sum(1 for row in workers if row["health"] == "stale"),
        "workers": workers,
    }


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
        finite_metric(metrics.get("profit_factor")) >= 1.2
        and finite_metric(metrics.get("expectancy_per_trade")) > 0
        and finite_metric(metrics.get("max_drawdown")) <= 0.12
        and finite_metric(metrics.get("number_of_trades")) >= 30
        and bool((metrics.get("walk_forward") or {}).get("enabled"))
        and bool(readiness.get("paper_ready"))
    )


def finalize_research_campaign(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
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
    for summary in summaries:
        if passes_cross_validation(summary):
            promoted += 1
            persist_elite_candidate(conn, campaign_id, summary)
            promote_elite_to_paper_simulation(conn, campaign_id, summary)
        else:
            rejected += 1
    analytics = campaign_analytics(jobs, summaries)
    conn.execute(
        """
        UPDATE research_campaigns
        SET status = 'completed',
            completed_at = NOW(),
            promoted_candidates = %s,
            rejected_candidates = %s,
            analytics = %s,
            updated_at = NOW()
        WHERE id = %s
        """,
        (promoted, rejected, Jsonb(jsonable(analytics)), campaign_id),
    )
    update_job_consistency_scores(conn, campaign_id, summaries)
    generate_campaign_report(conn, campaign_id, analytics)
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
        consistency_score = round(len(passed) / len(rows), 4) if rows else 0.0
        regime_counter: Counter[str] = Counter()
        for result in results:
            for bucket in ("by_market_regime", "by_volatility_regime"):
                for regime_row in ((result.get("regime_analysis") or {}).get(bucket) or []):
                    row_metrics = regime_row.get("metrics") or {}
                    if finite_metric(row_metrics.get("number_of_trades")) > 0 and finite_metric(row_metrics.get("expectancy_per_trade")) > 0:
                        regime_counter[str(regime_row.get("regime", "unknown"))] += 1
        first = rows[0]
        summaries.append(
            {
                "candidate_id": candidate_id,
                "family_id": first["family_id"],
                "strategy_name": "autonomous_strategy_discovery",
                "strategy_version": candidate_id,
                "research_score": average(row.get("validation_score") for row in rows),
                "profit_factor": average(metric.get("profit_factor") for metric in metrics),
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
        )
    return sorted(summaries, key=lambda row: (row["stability"], row["research_score"], row["profit_factor"]), reverse=True)


def passes_cross_validation(summary: dict[str, Any]) -> bool:
    return (
        summary["research_score"] > 0
        and summary["profit_factor"] >= 1.2
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
    account = conn.execute(
        """
        SELECT *
        FROM paper_accounts
        WHERE status = 'active' AND simulation_only = TRUE
        ORDER BY created_at ASC
        LIMIT 1
        """
    ).fetchone()
    if not account or not summary["passed_markets"]:
        return
    market = summary["passed_markets"][0]
    existing = conn.execute(
        """
        SELECT id
        FROM strategy_deployments
        WHERE account_id = %s
          AND strategy_name = %s
          AND strategy_version = %s
          AND symbol = %s
          AND timeframe = %s
          AND status = 'active'
          AND simulation_only = TRUE
        LIMIT 1
        """,
        (account["id"], summary["strategy_name"], summary["strategy_version"], market["symbol"], market["timeframe"]),
    ).fetchone()
    if existing:
        return
    deployment = conn.execute(
        """
        INSERT INTO strategy_deployments(account_id, strategy_name, strategy_version, symbol, timeframe, parameters, status, simulation_only)
        VALUES (%s, %s, %s, %s, %s, %s, 'active', TRUE)
        RETURNING id
        """,
        (
            account["id"],
            summary["strategy_name"],
            summary["strategy_version"],
            market["symbol"],
            market["timeframe"],
            Jsonb({"campaign_id": campaign_id, "candidate_id": summary["candidate_id"], "simulation_only": True}),
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
          AND symbol = ANY(%s)
        """,
        ([row["symbol"] for row in deployments],),
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
            open_lots.append({"quantity": quantity, "price": price, "fee": fee, "slippage": slippage, "timestamp": timestamp})
            continue
        remaining = quantity
        while remaining > 0 and open_lots:
            lot = open_lots[0]
            matched = min(remaining, lot["quantity"])
            entry_fee = lot["fee"] * (matched / lot["quantity"]) if lot["quantity"] else Decimal("0")
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
        paper = [elite_by_candidate[row["candidate_id"]].get("paper_performance") or {} for row in promoted if row["candidate_id"] in elite_by_candidate]
        result.append(
            {
                "name": name,
                "strategies_tested": len(completed),
                "promoted": len(promoted),
                "rejected": len(rejected),
                "average_research_score": average(row.get("validation_score") for row in completed),
                "average_profit_factor": average(metric.get("profit_factor") for metric in metrics),
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
        "average_profit_factor": average(metric.get("profit_factor") for metric in metrics),
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


def campaign_status(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    ensure_campaign_tables(conn)
    campaign = get_campaign(conn, campaign_id)
    analytics = refresh_campaign_analytics(conn, campaign_id)
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
    elite = conn.execute(
        """
        SELECT *
        FROM elite_research_candidates
        WHERE campaign_id = %s AND simulation_only = TRUE
        ORDER BY research_score DESC, created_at DESC
        """,
        (campaign_id,),
    ).fetchall()
    conn.commit()
    return {"campaign": jsonable(campaign), "analytics": analytics, "recent_jobs": [jsonable(dict(row)) for row in jobs], "elite_candidates": [jsonable(dict(row)) for row in elite], "simulation_only": True}


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
    refresh_campaign_analytics(conn, campaign_id)
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
    active = [row for row in rows if row["status"] in {"queued", "running", "paused"}]
    completed = [row for row in rows if row["status"] == "completed"]
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
        "retrying_jobs": ops["retrying_jobs"],
        "deferred_jobs": ops["deferred_jobs"],
        "blocked_data_jobs": ops["blocked_data_jobs"],
        "failed_jobs": ops["failed_jobs"],
        "queue_depth": ops["queue_depth"],
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
        "current_experiment": current_campaign_label(active),
        "generated_candidates": sum(int(((row.get("analytics") or {}).get("strategies_generated") or 0)) for row in rows),
        "promoted_candidates": sum(int(row.get("promoted_candidates") or 0) for row in rows),
        "rejection_rate": rejection_rate(rows),
        "workers": workers["workers"],
        "campaigns": [jsonable(row) for row in rows],
        "simulation_only": True,
    }


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
        WHERE status = 'failed' OR failure_classification IS NOT NULL
        GROUP BY COALESCE(failure_classification, 'unknown_error')
        ORDER BY count DESC
        LIMIT 10
        """
    ).fetchall()
    oldest_time = parse_timestamp((oldest or {}).get("oldest"))
    return {
        "claimed_jobs": sum(counts.get(status, 0) for status in ("running",)),
        "running_jobs": counts.get("running", 0),
        "completed_jobs": counts.get("completed", 0) + counts.get("promoted", 0) + counts.get("rejected", 0),
        "retrying_jobs": counts.get("retrying", 0),
        "deferred_jobs": counts.get("deferred_rate_limit", 0),
        "blocked_data_jobs": counts.get("blocked_data", 0),
        "failed_jobs": counts.get("failed", 0),
        "queue_depth": sum(counts.get(status, 0) for status in ("queued", "retrying", "blocked_data", "deferred_rate_limit")),
        "oldest_queued_job_age_hours": round((datetime.now(UTC) - oldest_time).total_seconds() / 3600, 2) if oldest_time else 0,
        "average_job_runtime_ms": round(finite_metric((runtime or {}).get("average_runtime")), 2),
        "jobs_completed_last_24h": int((completed_24h or {}).get("count") or 0),
        "campaigns_completed_last_24h": int((campaigns_24h or {}).get("count") or 0),
        "worker_error_summary": [{"classification": row["classification"], "count": int(row["count"])} for row in errors],
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


def research_campaign_key(universe_key: str, assets: list[str], timeframes: list[str], max_candidates: int) -> str:
    raw = f"{CAMPAIGN_VERSION}|{universe_key}|{','.join(assets)}|{','.join(timeframes)}|{max_candidates}"
    return sha256(raw.encode("utf-8")).hexdigest()


def research_job_key(campaign_id: int, candidate_id: str, symbol: str, timeframe: str) -> str:
    raw = f"{CAMPAIGN_VERSION}|{campaign_id}|{candidate_id}|{symbol}|{timeframe}"
    return sha256(raw.encode("utf-8")).hexdigest()


def average(values: Any) -> float:
    parsed = [finite_metric(value) for value in values if value is not None]
    return round(sum(parsed) / len(parsed), 4) if parsed else 0.0
