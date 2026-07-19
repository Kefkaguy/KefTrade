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
);

CREATE INDEX IF NOT EXISTS research_command_center_snapshots_created_idx
    ON research_command_center_snapshots(created_at DESC);

CREATE TABLE IF NOT EXISTS research_candidate_objects (
    id BIGSERIAL PRIMARY KEY,
    candidate_scope_key TEXT NOT NULL UNIQUE,
    candidate_id TEXT NOT NULL,
    campaign_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    state TEXT NOT NULL,
    strategy_family TEXT NOT NULL,
    assets JSONB NOT NULL DEFAULT '[]'::jsonb,
    timeframes JSONB NOT NULL DEFAULT '[]'::jsonb,
    lineage JSONB NOT NULL DEFAULT '{}'::jsonb,
    generation_history JSONB NOT NULL DEFAULT '[]'::jsonb,
    validation_history JSONB NOT NULL DEFAULT '[]'::jsonb,
    repair_history JSONB NOT NULL DEFAULT '[]'::jsonb,
    promotion_history JSONB NOT NULL DEFAULT '[]'::jsonb,
    deployment_history JSONB NOT NULL DEFAULT '[]'::jsonb,
    forward_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    learning_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    calculation_version TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_candidate_objects_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_candidate_objects_candidate_idx
    ON research_candidate_objects(candidate_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS research_candidate_objects_state_idx
    ON research_candidate_objects(state, updated_at DESC);

CREATE TABLE IF NOT EXISTS research_global_learning_snapshots (
    id BIGSERIAL PRIMARY KEY,
    snapshot_key TEXT NOT NULL UNIQUE,
    evidence_window JSONB NOT NULL DEFAULT '{}'::jsonb,
    elite_explanations JSONB NOT NULL DEFAULT '[]'::jsonb,
    decision_intelligence JSONB NOT NULL DEFAULT '{}'::jsonb,
    forward_intelligence JSONB NOT NULL DEFAULT '{}'::jsonb,
    duplicate_intelligence JSONB NOT NULL DEFAULT '{}'::jsonb,
    candidate_object_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    campaign_guidance JSONB NOT NULL DEFAULT '{}'::jsonb,
    constraints JSONB NOT NULL DEFAULT '{}'::jsonb,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_global_learning_snapshots_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_global_learning_snapshots_created_idx
    ON research_global_learning_snapshots(created_at DESC);

CREATE TABLE IF NOT EXISTS candidate_missing_evidence_plans (
    id BIGSERIAL PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
    missing_evidence_reason TEXT NOT NULL,
    recommended_test TEXT NOT NULL,
    test_scope TEXT NOT NULL,
    falsification_condition TEXT NOT NULL,
    status TEXT NOT NULL,
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT candidate_missing_evidence_plans_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE UNIQUE INDEX IF NOT EXISTS candidate_missing_evidence_plan_unique
    ON candidate_missing_evidence_plans(candidate_id, COALESCE(campaign_id, 0), missing_evidence_reason);

