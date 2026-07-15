ALTER TABLE research_campaign_jobs
    ADD COLUMN IF NOT EXISTS recovery_classification TEXT,
    ADD COLUMN IF NOT EXISTS original_worker_id TEXT,
    ADD COLUMN IF NOT EXISTS original_lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS recovery_worker_id TEXT,
    ADD COLUMN IF NOT EXISTS recovered_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS execution_resumed BOOLEAN,
    ADD COLUMN IF NOT EXISTS failure_history JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE research_campaign_jobs DROP CONSTRAINT IF EXISTS research_campaign_jobs_recovery_classification_check;
ALTER TABLE research_campaign_jobs ADD CONSTRAINT research_campaign_jobs_recovery_classification_check
    CHECK (
        recovery_classification IS NULL OR recovery_classification IN (
            'recovered_stale_lease',
            'actual_worker_execution_timeout',
            'provider_timeout',
            'database_timeout',
            'permanent_job_failure'
        )
    );

CREATE INDEX IF NOT EXISTS research_campaign_jobs_recovery_idx
    ON research_campaign_jobs(recovery_classification, recovered_at DESC)
    WHERE recovery_classification IS NOT NULL;

ALTER TABLE strategy_deployments
    ADD COLUMN IF NOT EXISTS campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS candidate_id TEXT,
    ADD COLUMN IF NOT EXISTS strategy_id TEXT,
    ADD COLUMN IF NOT EXISTS forward_validation_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS evidence_version TEXT,
    ADD COLUMN IF NOT EXISTS lifecycle_state TEXT NOT NULL DEFAULT 'manual_simulation',
    ADD COLUMN IF NOT EXISTS deployment_origin TEXT NOT NULL DEFAULT 'manual_simulation';

ALTER TABLE paper_orders
    ADD COLUMN IF NOT EXISTS campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS candidate_id TEXT,
    ADD COLUMN IF NOT EXISTS strategy_id TEXT,
    ADD COLUMN IF NOT EXISTS strategy_version TEXT,
    ADD COLUMN IF NOT EXISTS decision_id TEXT,
    ADD COLUMN IF NOT EXISTS signal_timestamp TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS evidence_origin TEXT NOT NULL DEFAULT 'manual_simulation';

ALTER TABLE paper_fills
    ADD COLUMN IF NOT EXISTS campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS candidate_id TEXT,
    ADD COLUMN IF NOT EXISTS deployment_id BIGINT REFERENCES strategy_deployments(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS strategy_id TEXT,
    ADD COLUMN IF NOT EXISTS strategy_version TEXT,
    ADD COLUMN IF NOT EXISTS decision_id TEXT,
    ADD COLUMN IF NOT EXISTS signal_timestamp TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS evidence_origin TEXT NOT NULL DEFAULT 'manual_simulation';

CREATE TABLE IF NOT EXISTS paper_closed_trade_evidence (
    id BIGSERIAL PRIMARY KEY,
    evidence_key TEXT NOT NULL UNIQUE,
    classification TEXT NOT NULL,
    readiness_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    exclusion_reason TEXT,
    account_id BIGINT REFERENCES paper_accounts(id) ON DELETE SET NULL,
    campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
    candidate_id TEXT,
    deployment_id BIGINT REFERENCES strategy_deployments(id) ON DELETE SET NULL,
    strategy_id TEXT,
    strategy_version TEXT,
    symbol TEXT NOT NULL,
    timeframe TEXT,
    entry_order_id BIGINT REFERENCES paper_orders(id) ON DELETE SET NULL,
    exit_order_id BIGINT REFERENCES paper_orders(id) ON DELETE SET NULL,
    entry_fill_id BIGINT REFERENCES paper_fills(id) ON DELETE SET NULL,
    exit_fill_id BIGINT REFERENCES paper_fills(id) ON DELETE SET NULL,
    quantity NUMERIC NOT NULL,
    net_pnl NUMERIC NOT NULL,
    opened_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    calculated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT paper_closed_trade_evidence_simulation_only_check CHECK (simulation_only = TRUE),
    CONSTRAINT paper_closed_trade_evidence_classification_check CHECK (classification IN (
        'eligible_forward_evidence',
        'legacy_simulation',
        'manual_simulation',
        'unattributed_simulation',
        'test_activity',
        'invalid_evidence'
    ))
);

CREATE INDEX IF NOT EXISTS paper_closed_trade_evidence_candidate_idx
    ON paper_closed_trade_evidence(candidate_id, closed_at DESC)
    WHERE simulation_only = TRUE;

CREATE INDEX IF NOT EXISTS paper_closed_trade_evidence_classification_idx
    ON paper_closed_trade_evidence(classification, readiness_eligible)
    WHERE simulation_only = TRUE;
