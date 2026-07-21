-- Phase 11: explainable decisions, bounded model risk, portfolio arbitration,
-- and auditable Alpaca Paper execution. This migration is append-only and
-- intentionally contains no destructive data statements.

ALTER TABLE external_paper_deployments
    DROP CONSTRAINT IF EXISTS external_execution_unreachable_check;

CREATE TABLE IF NOT EXISTS strategy_evaluations (
    id BIGSERIAL PRIMARY KEY,
    internal_deployment_id BIGINT NOT NULL REFERENCES strategy_deployments(id) ON DELETE RESTRICT,
    external_deployment_id BIGINT REFERENCES external_paper_deployments(id) ON DELETE RESTRICT,
    execution_epoch_id BIGINT REFERENCES external_execution_epochs(id) ON DELETE RESTRICT,
    trace_id UUID NOT NULL,
    decision_version TEXT NOT NULL,
    configuration_fingerprint TEXT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    completed_bar_timestamp TIMESTAMPTZ NOT NULL,
    signal_type TEXT NOT NULL,
    regime JSONB NOT NULL DEFAULT '{}'::jsonb,
    gates JSONB NOT NULL DEFAULT '[]'::jsonb,
    decision JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(internal_deployment_id, completed_bar_timestamp, decision_version),
    CONSTRAINT strategy_evaluations_signal_check CHECK (signal_type IN ('setup', 'watchlist', 'avoid', 'skipped'))
);

CREATE INDEX IF NOT EXISTS strategy_evaluations_deployment_bar_idx
    ON strategy_evaluations(internal_deployment_id, completed_bar_timestamp DESC);
CREATE INDEX IF NOT EXISTS strategy_evaluations_signal_idx
    ON strategy_evaluations(signal_type, created_at DESC);

CREATE TABLE IF NOT EXISTS model_risk_decisions (
    id BIGSERIAL PRIMARY KEY,
    strategy_evaluation_id BIGINT NOT NULL REFERENCES strategy_evaluations(id) ON DELETE RESTRICT,
    external_deployment_id BIGINT REFERENCES external_paper_deployments(id) ON DELETE RESTRICT,
    trace_id UUID NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    authority_level TEXT NOT NULL DEFAULT 'shadow',
    requested_action TEXT NOT NULL,
    requested_risk_pct NUMERIC NOT NULL DEFAULT 0,
    bounded_risk_pct NUMERIC NOT NULL DEFAULT 0,
    confidence NUMERIC NOT NULL DEFAULT 0,
    thesis TEXT NOT NULL,
    invalidation TEXT NOT NULL,
    holding_horizon TEXT,
    raw_response JSONB NOT NULL,
    safety_checks JSONB NOT NULL DEFAULT '[]'::jsonb,
    approved BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(strategy_evaluation_id, provider, model, prompt_version),
    CONSTRAINT model_risk_authority_check CHECK (authority_level IN ('observer', 'shadow', 'bounded_paper')),
    CONSTRAINT model_risk_action_check CHECK (requested_action IN ('enter', 'wait', 'reject')),
    CONSTRAINT model_risk_values_check CHECK (requested_risk_pct >= 0 AND bounded_risk_pct >= 0 AND bounded_risk_pct <= 0.01 AND confidence >= 0 AND confidence <= 1)
);

CREATE INDEX IF NOT EXISTS model_risk_decisions_created_idx
    ON model_risk_decisions(created_at DESC);

CREATE TABLE IF NOT EXISTS portfolio_risk_decisions (
    id BIGSERIAL PRIMARY KEY,
    external_deployment_id BIGINT NOT NULL REFERENCES external_paper_deployments(id) ON DELETE RESTRICT,
    strategy_evaluation_id BIGINT NOT NULL REFERENCES strategy_evaluations(id) ON DELETE RESTRICT,
    model_risk_decision_id BIGINT REFERENCES model_risk_decisions(id) ON DELETE RESTRICT,
    trace_id UUID NOT NULL,
    symbol TEXT NOT NULL,
    approved BOOLEAN NOT NULL,
    requested_risk_pct NUMERIC NOT NULL,
    allocated_risk_pct NUMERIC NOT NULL,
    portfolio_heat_pct NUMERIC NOT NULL,
    correlation_max NUMERIC,
    winner_external_deployment_id BIGINT REFERENCES external_paper_deployments(id) ON DELETE RESTRICT,
    checks JSONB NOT NULL DEFAULT '[]'::jsonb,
    decision JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(strategy_evaluation_id),
    CONSTRAINT portfolio_risk_values_check CHECK (requested_risk_pct >= 0 AND allocated_risk_pct >= 0 AND portfolio_heat_pct >= 0)
);

CREATE INDEX IF NOT EXISTS portfolio_risk_decisions_created_idx
    ON portfolio_risk_decisions(created_at DESC);

CREATE TABLE IF NOT EXISTS broker_execution_attempts (
    id BIGSERIAL PRIMARY KEY,
    external_deployment_id BIGINT NOT NULL REFERENCES external_paper_deployments(id) ON DELETE RESTRICT,
    execution_epoch_id BIGINT NOT NULL REFERENCES external_execution_epochs(id) ON DELETE RESTRICT,
    proposed_order_id BIGINT NOT NULL REFERENCES proposed_broker_orders(id) ON DELETE RESTRICT,
    portfolio_risk_decision_id BIGINT NOT NULL REFERENCES portfolio_risk_decisions(id) ON DELETE RESTRICT,
    model_risk_decision_id BIGINT REFERENCES model_risk_decisions(id) ON DELETE RESTRICT,
    trace_id UUID NOT NULL,
    client_order_id TEXT NOT NULL UNIQUE,
    broker_order_id TEXT,
    provider_request_id TEXT,
    status TEXT NOT NULL DEFAULT 'intent_recorded',
    request_payload JSONB NOT NULL,
    response_payload JSONB,
    error JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    submitted_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    CONSTRAINT broker_execution_attempt_status_check CHECK (status IN ('intent_recorded', 'submitted', 'accepted', 'rejected', 'ambiguous', 'failed', 'reconciled'))
);

CREATE INDEX IF NOT EXISTS broker_execution_attempts_deployment_created_idx
    ON broker_execution_attempts(external_deployment_id, created_at DESC);

CREATE INDEX IF NOT EXISTS strategy_deployments_recent_idx
    ON strategy_deployments(simulation_only, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS paper_orders_recent_idx
    ON paper_orders(simulation_only, submitted_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS paper_fills_recent_idx
    ON paper_fills(simulation_only, filled_at DESC, id DESC);

