-- Elite portfolio construction, immutable direction semantics, and resumable
-- internal activation. This migration is additive: it changes no historical
-- values and deliberately preserves every external broker long-only guard.

ALTER TABLE strategy_versions
    ADD COLUMN IF NOT EXISTS strategy_direction TEXT NOT NULL DEFAULT 'long';
ALTER TABLE strategy_versions DROP CONSTRAINT IF EXISTS strategy_versions_direction_check;
ALTER TABLE strategy_versions ADD CONSTRAINT strategy_versions_direction_check
    CHECK (strategy_direction IN ('long', 'short')) NOT VALID;
ALTER TABLE strategy_versions VALIDATE CONSTRAINT strategy_versions_direction_check;

ALTER TABLE elite_research_candidates
    ADD COLUMN IF NOT EXISTS strategy_direction TEXT NOT NULL DEFAULT 'long',
    ADD COLUMN IF NOT EXISTS execution_capability TEXT NOT NULL DEFAULT 'external_observe';
ALTER TABLE elite_research_candidates DROP CONSTRAINT IF EXISTS elite_research_candidates_direction_check;
ALTER TABLE elite_research_candidates ADD CONSTRAINT elite_research_candidates_direction_check
    CHECK (strategy_direction IN ('long', 'short')) NOT VALID;
ALTER TABLE elite_research_candidates VALIDATE CONSTRAINT elite_research_candidates_direction_check;
ALTER TABLE elite_research_candidates DROP CONSTRAINT IF EXISTS elite_research_candidates_capability_check;
ALTER TABLE elite_research_candidates ADD CONSTRAINT elite_research_candidates_capability_check
    CHECK (execution_capability IN ('internal_only', 'external_observe', 'paper_eligible')) NOT VALID;
ALTER TABLE elite_research_candidates VALIDATE CONSTRAINT elite_research_candidates_capability_check;

ALTER TABLE strategy_deployments
    ADD COLUMN IF NOT EXISTS strategy_direction TEXT NOT NULL DEFAULT 'long',
    ADD COLUMN IF NOT EXISTS execution_capability TEXT NOT NULL DEFAULT 'external_observe';
ALTER TABLE strategy_deployments DROP CONSTRAINT IF EXISTS strategy_deployments_direction_check;
ALTER TABLE strategy_deployments ADD CONSTRAINT strategy_deployments_direction_check
    CHECK (strategy_direction IN ('long', 'short')) NOT VALID;
ALTER TABLE strategy_deployments VALIDATE CONSTRAINT strategy_deployments_direction_check;
ALTER TABLE strategy_deployments DROP CONSTRAINT IF EXISTS strategy_deployments_capability_check;
ALTER TABLE strategy_deployments ADD CONSTRAINT strategy_deployments_capability_check
    CHECK (execution_capability IN ('internal_only', 'external_observe', 'paper_eligible')) NOT VALID;
ALTER TABLE strategy_deployments VALIDATE CONSTRAINT strategy_deployments_capability_check;

ALTER TABLE elite_shadow_replay_outcomes
    ADD COLUMN IF NOT EXISTS strategy_direction TEXT NOT NULL DEFAULT 'long';
ALTER TABLE elite_shadow_replay_outcomes DROP CONSTRAINT IF EXISTS elite_shadow_replay_outcomes_direction_check;
ALTER TABLE elite_shadow_replay_outcomes ADD CONSTRAINT elite_shadow_replay_outcomes_direction_check
    CHECK (strategy_direction IN ('long', 'short')) NOT VALID;
ALTER TABLE elite_shadow_replay_outcomes VALIDATE CONSTRAINT elite_shadow_replay_outcomes_direction_check;

-- Internal simulation positions may be signed. External orders retain positive
-- quantity constraints and proposed_broker_orders remains CHECK(side = 'buy').
ALTER TABLE paper_positions DROP CONSTRAINT IF EXISTS paper_positions_long_only_check;

CREATE TABLE IF NOT EXISTS elite_portfolio_runs (
    id BIGSERIAL PRIMARY KEY,
    run_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'draft',
    solver_version TEXT NOT NULL,
    objective TEXT NOT NULL,
    constraints JSONB NOT NULL,
    quality_thresholds JSONB NOT NULL,
    source_configuration JSONB NOT NULL DEFAULT '{}'::jsonb,
    candidate_order JSONB NOT NULL DEFAULT '[]'::jsonb,
    solver_iterations INTEGER NOT NULL DEFAULT 0,
    solver_operations JSONB NOT NULL DEFAULT '[]'::jsonb,
    termination_reason TEXT,
    statistics JSONB NOT NULL DEFAULT '{}'::jsonb,
    portfolio_analytics JSONB NOT NULL DEFAULT '{}'::jsonb,
    snapshot_hash TEXT,
    approved_snapshot_hash TEXT,
    approved_at TIMESTAMPTZ,
    activated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT elite_portfolio_runs_status_check CHECK (status IN (
        'draft','researching','optimizing','review_ready','infeasible','stale',
        'approved','activated_internal','failed','superseded','cancelled'
    )),
    CONSTRAINT elite_portfolio_runs_solver_iterations_check CHECK (solver_iterations >= 0),
    CONSTRAINT elite_portfolio_runs_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE TABLE IF NOT EXISTS elite_portfolio_snapshots (
    id BIGSERIAL PRIMARY KEY,
    portfolio_run_id BIGINT NOT NULL REFERENCES elite_portfolio_runs(id) ON DELETE RESTRICT,
    snapshot_hash TEXT NOT NULL UNIQUE,
    decision_inputs JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT elite_portfolio_snapshots_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE TABLE IF NOT EXISTS elite_portfolio_eligibility (
    id BIGSERIAL PRIMARY KEY,
    portfolio_run_id BIGINT NOT NULL REFERENCES elite_portfolio_runs(id) ON DELETE RESTRICT,
    elite_candidate_id BIGINT REFERENCES elite_research_candidates(id) ON DELETE RESTRICT,
    candidate_id TEXT NOT NULL,
    campaign_id BIGINT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    strategy_family TEXT NOT NULL,
    strategy_direction TEXT NOT NULL,
    execution_capability TEXT NOT NULL,
    eligible BOOLEAN NOT NULL,
    health_classification TEXT NOT NULL,
    checks JSONB NOT NULL,
    evidence JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(portfolio_run_id, candidate_id, symbol, timeframe),
    CONSTRAINT elite_portfolio_eligibility_direction_check CHECK (strategy_direction IN ('long','short')),
    CONSTRAINT elite_portfolio_eligibility_capability_check CHECK (execution_capability IN ('internal_only','external_observe','paper_eligible'))
);

CREATE TABLE IF NOT EXISTS elite_portfolio_correlations (
    id BIGSERIAL PRIMARY KEY,
    portfolio_run_id BIGINT NOT NULL REFERENCES elite_portfolio_runs(id) ON DELETE RESTRICT,
    left_candidate_key TEXT NOT NULL,
    right_candidate_key TEXT NOT NULL,
    correlation_type TEXT NOT NULL,
    coefficient DOUBLE PRECISION,
    observation_count INTEGER NOT NULL,
    confidence_classification TEXT NOT NULL,
    method TEXT NOT NULL,
    return_frequency TEXT NOT NULL,
    window_start TIMESTAMPTZ,
    window_end TIMESTAMPTZ,
    data_snapshot_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(portfolio_run_id, left_candidate_key, right_candidate_key, correlation_type),
    CONSTRAINT elite_portfolio_correlations_type_check CHECK (correlation_type IN ('strategy_return','signal_behavior')),
    CONSTRAINT elite_portfolio_correlations_observations_check CHECK (observation_count >= 0),
    CONSTRAINT elite_portfolio_correlations_confidence_check CHECK (confidence_classification IN ('insufficient','provisional','established'))
);

CREATE TABLE IF NOT EXISTS elite_portfolio_conflicts (
    id BIGSERIAL PRIMARY KEY,
    portfolio_run_id BIGINT NOT NULL REFERENCES elite_portfolio_runs(id) ON DELETE RESTRICT,
    left_candidate_key TEXT NOT NULL,
    right_candidate_key TEXT,
    conflict_type TEXT NOT NULL,
    hard_conflict BOOLEAN NOT NULL DEFAULT TRUE,
    reason TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS elite_portfolio_members (
    id BIGSERIAL PRIMARY KEY,
    portfolio_run_id BIGINT NOT NULL REFERENCES elite_portfolio_runs(id) ON DELETE RESTRICT,
    elite_candidate_id BIGINT REFERENCES elite_research_candidates(id) ON DELETE RESTRICT,
    campaign_id BIGINT,
    candidate_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    strategy_family TEXT NOT NULL,
    strategy_direction TEXT NOT NULL,
    execution_capability TEXT NOT NULL,
    rank INTEGER NOT NULL,
    activation_state TEXT NOT NULL DEFAULT 'selected',
    objective_score DOUBLE PRECISION NOT NULL DEFAULT 0,
    quality_score DOUBLE PRECISION NOT NULL DEFAULT 0,
    diversity_contribution DOUBLE PRECISION NOT NULL DEFAULT 0,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    selection_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    internal_deployment_id BIGINT REFERENCES strategy_deployments(id) ON DELETE RESTRICT,
    external_deployment_id BIGINT REFERENCES external_paper_deployments(id) ON DELETE RESTRICT,
    latest_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(portfolio_run_id, candidate_id, symbol, timeframe),
    CONSTRAINT elite_portfolio_members_direction_check CHECK (strategy_direction IN ('long','short')),
    CONSTRAINT elite_portfolio_members_capability_check CHECK (execution_capability IN ('internal_only','external_observe','paper_eligible')),
    CONSTRAINT elite_portfolio_members_rank_check CHECK (rank > 0),
    CONSTRAINT elite_portfolio_members_activation_check CHECK (activation_state IN (
        'selected','approved','internal_activation_pending','internal_active',
        'external_record_created','external_approval_required','blocked','failed'
    ))
);

CREATE TABLE IF NOT EXISTS elite_portfolio_activation_attempts (
    id BIGSERIAL PRIMARY KEY,
    portfolio_run_id BIGINT NOT NULL REFERENCES elite_portfolio_runs(id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'running',
    requested_snapshot_hash TEXT NOT NULL,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    error JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT elite_portfolio_activation_status_check CHECK (status IN ('running','partial','complete','failed'))
);

CREATE INDEX IF NOT EXISTS elite_portfolio_runs_status_created_idx
    ON elite_portfolio_runs(status, created_at DESC);
CREATE INDEX IF NOT EXISTS elite_portfolio_members_run_rank_idx
    ON elite_portfolio_members(portfolio_run_id, rank);
CREATE INDEX IF NOT EXISTS elite_portfolio_conflicts_run_type_idx
    ON elite_portfolio_conflicts(portfolio_run_id, conflict_type);
CREATE INDEX IF NOT EXISTS elite_portfolio_eligibility_run_eligible_idx
    ON elite_portfolio_eligibility(portfolio_run_id, eligible);

