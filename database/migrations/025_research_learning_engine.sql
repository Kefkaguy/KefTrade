CREATE TABLE IF NOT EXISTS research_knowledge_versions (
    id BIGSERIAL PRIMARY KEY,
    knowledge_key TEXT NOT NULL,
    knowledge_type TEXT NOT NULL,
    campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
    source_ref TEXT NOT NULL,
    version INTEGER NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence_score NUMERIC NOT NULL DEFAULT 0,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_knowledge_versions_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_knowledge_versions_key_idx
    ON research_knowledge_versions(knowledge_key, version DESC);

CREATE TABLE IF NOT EXISTS research_failure_patterns (
    id BIGSERIAL PRIMARY KEY,
    pattern_key TEXT NOT NULL,
    campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
    pattern_type TEXT NOT NULL,
    description TEXT NOT NULL,
    frequency INTEGER NOT NULL DEFAULT 0,
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    supporting_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    recommendation TEXT,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_failure_patterns_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_failure_patterns_key_idx
    ON research_failure_patterns(pattern_key, created_at DESC);

CREATE TABLE IF NOT EXISTS research_success_patterns (
    id BIGSERIAL PRIMARY KEY,
    pattern_key TEXT NOT NULL,
    campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
    pattern_type TEXT NOT NULL,
    description TEXT NOT NULL,
    frequency INTEGER NOT NULL DEFAULT 0,
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    supporting_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    recommendation TEXT,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_success_patterns_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_success_patterns_key_idx
    ON research_success_patterns(pattern_key, created_at DESC);

CREATE TABLE IF NOT EXISTS research_recommendations (
    id BIGSERIAL PRIMARY KEY,
    recommendation_key TEXT NOT NULL,
    campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    finding TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'open',
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    expected_improvement TEXT NOT NULL,
    confidence_score NUMERIC NOT NULL DEFAULT 0,
    validation JSONB NOT NULL DEFAULT '{}'::jsonb,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_recommendations_status_check CHECK (status IN ('open', 'testing', 'validated', 'invalidated', 'archived')),
    CONSTRAINT research_recommendations_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_recommendations_status_idx
    ON research_recommendations(status, priority, created_at DESC);

CREATE TABLE IF NOT EXISTS research_confidence_history (
    id BIGSERIAL PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
    confidence_score NUMERIC NOT NULL,
    components JSONB NOT NULL DEFAULT '{}'::jsonb,
    explanation TEXT NOT NULL,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_confidence_history_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_confidence_history_candidate_idx
    ON research_confidence_history(candidate_id, created_at DESC);

CREATE TABLE IF NOT EXISTS research_evolution_history (
    id BIGSERIAL PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    parent_candidate_id TEXT,
    campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
    mutation JSONB NOT NULL DEFAULT '{}'::jsonb,
    reason TEXT NOT NULL,
    supporting_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    expected_improvement TEXT NOT NULL,
    confidence_score NUMERIC NOT NULL DEFAULT 0,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_evolution_history_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_evolution_history_parent_idx
    ON research_evolution_history(parent_candidate_id, created_at DESC);

CREATE TABLE IF NOT EXISTS research_timeline_events (
    id BIGSERIAL PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    event_timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_timeline_events_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_timeline_events_strategy_idx
    ON research_timeline_events(strategy_id, event_timestamp DESC);

CREATE TABLE IF NOT EXISTS research_campaign_plans (
    id BIGSERIAL PRIMARY KEY,
    plan_key TEXT NOT NULL,
    campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
    priorities JSONB NOT NULL DEFAULT '[]'::jsonb,
    exploration_targets JSONB NOT NULL DEFAULT '[]'::jsonb,
    confirmation_targets JSONB NOT NULL DEFAULT '[]'::jsonb,
    rationale JSONB NOT NULL DEFAULT '{}'::jsonb,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_campaign_plans_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_campaign_plans_key_idx
    ON research_campaign_plans(plan_key, created_at DESC);
