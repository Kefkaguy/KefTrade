ALTER TABLE research_campaign_jobs DROP CONSTRAINT IF EXISTS research_campaign_jobs_status_check;

ALTER TABLE research_campaign_jobs
    ADD COLUMN IF NOT EXISTS worker_id TEXT,
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS failure_classification TEXT,
    ADD COLUMN IF NOT EXISTS deferred_until TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS blocked_reason TEXT,
    ADD COLUMN IF NOT EXISTS execution_runtime_ms INTEGER;

-- 'blocked_terminal' (added in migration 040) is included here so that this
-- migration, which the migrate job re-applies on every deploy, stays
-- consistent with the current status set and does not fail against rows that
-- already carry the terminal status.
ALTER TABLE research_campaign_jobs ADD CONSTRAINT research_campaign_jobs_status_check
    CHECK (status IN ('queued', 'running', 'completed', 'rejected', 'promoted', 'failed', 'canceled', 'blocked_data', 'blocked_terminal', 'deferred_rate_limit', 'retrying'));

ALTER TABLE research_campaign_jobs DROP CONSTRAINT IF EXISTS research_campaign_jobs_failure_classification_check;
ALTER TABLE research_campaign_jobs ADD CONSTRAINT research_campaign_jobs_failure_classification_check
    CHECK (
        failure_classification IS NULL OR failure_classification IN (
            'data_unavailable',
            'stale_data',
            'provider_error',
            'validation_error',
            'strategy_error',
            'database_error',
            'worker_timeout',
            'unknown_error',
            'rate_limit',
            'budget_exhausted'
        )
    );

CREATE INDEX IF NOT EXISTS research_campaign_jobs_claim_idx
    ON research_campaign_jobs(campaign_id, status, deferred_until, lease_expires_at, id);

CREATE INDEX IF NOT EXISTS research_campaign_jobs_worker_idx
    ON research_campaign_jobs(worker_id, heartbeat_at DESC)
    WHERE worker_id IS NOT NULL;

ALTER TABLE research_campaigns
    ADD COLUMN IF NOT EXISTS scheduling_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS last_scheduler_cycle_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS next_scheduler_cycle_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS daily_jobs_executed INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS daily_budget_date DATE;

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
    CONSTRAINT research_campaign_scheduler_positive_cadence CHECK (cadence_seconds > 0),
    CONSTRAINT research_campaign_scheduler_simulation_only_check CHECK (simulation_only = TRUE)
);

INSERT INTO research_campaign_scheduler(id, enabled, simulation_only)
VALUES (TRUE, FALSE, TRUE)
ON CONFLICT(id) DO NOTHING;

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
    CONSTRAINT research_campaign_worker_cycles_status_check CHECK (status IN ('running', 'completed', 'failed')),
    CONSTRAINT research_campaign_worker_cycles_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_campaign_worker_cycles_status_idx
    ON research_campaign_worker_cycles(status, started_at DESC);

ALTER TABLE elite_research_candidates
    ADD COLUMN IF NOT EXISTS forward_validation_state TEXT NOT NULL DEFAULT 'awaiting_paper_deployment',
    ADD COLUMN IF NOT EXISTS forward_validation_thresholds JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS forward_validation_updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS drift_status TEXT NOT NULL DEFAULT 'normal';

ALTER TABLE elite_research_candidates DROP CONSTRAINT IF EXISTS elite_research_candidates_forward_state_check;
ALTER TABLE elite_research_candidates ADD CONSTRAINT elite_research_candidates_forward_state_check
    CHECK (forward_validation_state IN (
        'awaiting_paper_deployment',
        'collecting_forward_evidence',
        'insufficient_forward_sample',
        'forward_validation_passed',
        'forward_validation_failed',
        'paused',
        'archived'
    ));

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
    CONSTRAINT elite_candidate_paper_rollups_state_check CHECK (forward_validation_state IN (
        'awaiting_paper_deployment',
        'collecting_forward_evidence',
        'insufficient_forward_sample',
        'forward_validation_passed',
        'forward_validation_failed',
        'paused',
        'archived'
    )),
    CONSTRAINT elite_candidate_paper_rollups_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS elite_candidate_paper_rollups_candidate_idx
    ON elite_candidate_paper_rollups(candidate_id, calculated_at DESC);

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
    CONSTRAINT elite_candidate_evidence_drift_classification_check CHECK (drift_classification IN ('normal', 'warning', 'severe')),
    CONSTRAINT elite_candidate_evidence_drift_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS elite_candidate_evidence_drift_candidate_idx
    ON elite_candidate_evidence_drift(candidate_id, detected_at DESC);

ALTER TABLE evidence_alerts DROP CONSTRAINT IF EXISTS evidence_alerts_type_check;
ALTER TABLE evidence_alerts ADD CONSTRAINT evidence_alerts_type_check CHECK (
    alert_type IN (
        'entry_setup_review',
        'exit_risk_review',
        'avoid_condition',
        'stale_data_warning',
        'scheduler_error',
        'duplicate_candle_skip',
        'evidence_drift_warning'
    )
);
