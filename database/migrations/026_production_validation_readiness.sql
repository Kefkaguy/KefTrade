CREATE TABLE IF NOT EXISTS production_validation_runs (
    id BIGSERIAL PRIMARY KEY,
    run_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'running',
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    universe_version TEXT NOT NULL,
    strategy_generation_version TEXT NOT NULL,
    validation_thresholds JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence_score_version TEXT NOT NULL,
    runtime_environment JSONB NOT NULL DEFAULT '{}'::jsonb,
    code_version TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    calculation_version TEXT NOT NULL,
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT production_validation_runs_status_check CHECK (status IN ('planned', 'running', 'completed', 'failed', 'canceled')),
    CONSTRAINT production_validation_runs_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS production_validation_runs_status_idx
    ON production_validation_runs(status, started_at DESC);

CREATE TABLE IF NOT EXISTS production_soak_snapshots (
    id BIGSERIAL PRIMARY KEY,
    validation_run_id BIGINT REFERENCES production_validation_runs(id) ON DELETE SET NULL,
    snapshot_key TEXT NOT NULL UNIQUE,
    window_hours INTEGER NOT NULL,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    health JSONB NOT NULL DEFAULT '{}'::jsonb,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT production_soak_snapshots_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE TABLE IF NOT EXISTS production_fault_injection_results (
    id BIGSERIAL PRIMARY KEY,
    validation_run_id BIGINT REFERENCES production_validation_runs(id) ON DELETE SET NULL,
    fault_key TEXT NOT NULL,
    fault_type TEXT NOT NULL,
    status TEXT NOT NULL,
    expected_recovery TEXT NOT NULL,
    observed_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    passed BOOLEAN NOT NULL DEFAULT FALSE,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT production_fault_injection_results_status_check CHECK (status IN ('planned', 'passed', 'failed', 'blocked')),
    CONSTRAINT production_fault_injection_results_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS production_fault_injection_results_type_idx
    ON production_fault_injection_results(fault_type, created_at DESC);

CREATE TABLE IF NOT EXISTS production_integrity_audit_results (
    id BIGSERIAL PRIMARY KEY,
    validation_run_id BIGINT REFERENCES production_validation_runs(id) ON DELETE SET NULL,
    audit_key TEXT NOT NULL UNIQUE,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    checks JSONB NOT NULL DEFAULT '[]'::jsonb,
    critical_failure_count INTEGER NOT NULL DEFAULT 0,
    warning_count INTEGER NOT NULL DEFAULT 0,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT production_integrity_audit_results_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE TABLE IF NOT EXISTS production_paper_reconciliation_results (
    id BIGSERIAL PRIMARY KEY,
    validation_run_id BIGINT REFERENCES production_validation_runs(id) ON DELETE SET NULL,
    reconciliation_key TEXT NOT NULL UNIQUE,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    mismatches JSONB NOT NULL DEFAULT '[]'::jsonb,
    mismatch_count INTEGER NOT NULL DEFAULT 0,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT production_paper_reconciliation_results_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE TABLE IF NOT EXISTS production_recommendation_outcomes (
    id BIGSERIAL PRIMARY KEY,
    recommendation_id BIGINT REFERENCES research_recommendations(id) ON DELETE SET NULL,
    outcome_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    supporting_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    follow_up_candidate_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    baseline_performance JSONB NOT NULL DEFAULT '{}'::jsonb,
    follow_up_performance JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence_change NUMERIC NOT NULL DEFAULT 0,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT production_recommendation_outcomes_status_check CHECK (status IN ('pending', 'supported', 'partially_supported', 'unsupported', 'inconclusive', 'invalidated')),
    CONSTRAINT production_recommendation_outcomes_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS production_recommendation_outcomes_status_idx
    ON production_recommendation_outcomes(status, created_at DESC);

CREATE TABLE IF NOT EXISTS production_learning_quality_snapshots (
    id BIGSERIAL PRIMARY KEY,
    validation_run_id BIGINT REFERENCES production_validation_runs(id) ON DELETE SET NULL,
    snapshot_key TEXT NOT NULL UNIQUE,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT production_learning_quality_snapshots_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE TABLE IF NOT EXISTS production_safety_audit_results (
    id BIGSERIAL PRIMARY KEY,
    validation_run_id BIGINT REFERENCES production_validation_runs(id) ON DELETE SET NULL,
    audit_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    checks JSONB NOT NULL DEFAULT '[]'::jsonb,
    blocking_failures JSONB NOT NULL DEFAULT '[]'::jsonb,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT production_safety_audit_results_status_check CHECK (status IN ('passed', 'warning', 'failed')),
    CONSTRAINT production_safety_audit_results_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE TABLE IF NOT EXISTS production_readiness_snapshots (
    id BIGSERIAL PRIMARY KEY,
    validation_run_id BIGINT REFERENCES production_validation_runs(id) ON DELETE SET NULL,
    readiness_key TEXT NOT NULL UNIQUE,
    readiness_state TEXT NOT NULL,
    readiness_score NUMERIC NOT NULL DEFAULT 0,
    category_scores JSONB NOT NULL DEFAULT '{}'::jsonb,
    gates JSONB NOT NULL DEFAULT '[]'::jsonb,
    blocking_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    calculation JSONB NOT NULL DEFAULT '{}'::jsonb,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT production_readiness_snapshots_state_check CHECK (readiness_state IN ('not_ready', 'conditionally_ready', 'ready_for_phase_10', 'blocked')),
    CONSTRAINT production_readiness_snapshots_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS production_readiness_snapshots_created_idx
    ON production_readiness_snapshots(created_at DESC);
